"""MCP tools for the add-on's Chat tab: blender_chat and the backend settings.

All three are add-on only. ``blender_chat`` runs one turn against the Blender
whose add-on called it; the conversation lives in the add-on, which sends a
bounded history with each message. Wire contract:
docs-site/src/content/docs/how-to/chat-in-blender.mdx.
"""

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Callable
from typing import Any

from fastmcp import Context
from fastmcp.contrib.mcp_mixin import MCPMixin, mcp_tool

from ..bus_tools import _resolve_user_id, _session_from_ctx
from ..client_role import require_role
from . import settings as chat_settings
from . import trial, user_tools, vision
from .catalog import build_catalog
from .config import ChatConfig, load_config
from .executor import ChatExecutor
from .providers import ProviderError
from .routing import RoutingSamplingHandler, current_backend, current_turn
from .turn import Turn

logger = logging.getLogger(__name__)

MAX_MESSAGE_CHARS = 8000
NO_BACKEND_HINT = (
    "The shared GPU gateway isn't available to this account. Choose a backend in "
    "Preferences > Add-ons > Blender MCP > Chat backend (your Claude API key, or an "
    "OpenAI-compatible endpoint)."
)


def _dump(d: dict) -> str:
    return json.dumps(d)


class BlenderChatComponent(MCPMixin):
    """blender_chat, blender_set_chat_backend, blender_get_chat_backend."""

    def __init__(self, handler: RoutingSamplingHandler | None = None,
                 config_loader: Callable[[], ChatConfig] = load_config):
        super().__init__()
        self.handler = handler or RoutingSamplingHandler(config_loader=config_loader)
        self.config_loader = config_loader
        # One turn in flight per user; the add-on is the only caller.
        # (user, Blender) -> (MCP session, task) of the turn in flight. One
        # turn per Blender; the same Blender on a new session (it reconnected,
        # so nobody waits on the old turn) replaces it.
        self._busy: dict[tuple[str, str], tuple[Any, asyncio.Task | None]] = {}

    @mcp_tool()
    @require_role("addon")
    async def chat(
        self,
        message: str,
        history: list[dict[str, Any]] | None = None,
        attachments: dict[str, Any] | None = None,
        ctx: Context = None,
    ) -> str:
        """Run one chat turn in the Blender that sent it (the add-on's Chat tab).

        ``history`` is the recent conversation, text only, at most 20
        ``{role, content}`` entries. ``attachments`` is what the user clipped
        to this message: ``selection`` (objects), ``text`` ({name, body}) and
        ``viewport`` (true to include what they see). Progress arrives as progress
        notifications whose message is a JSON event; approvals for risky
        tools arrive as elicitation requests starting "BlenderMCP approval:".
        Returns JSON with ``status`` ok / disabled / no_backend / busy /
        backend_error / timeout / trial_ended. From the backend lookup on,
        results carry ``trial``: ``{limit, used, remaining}`` for an account
        on the free trial (CHAT_TRIAL_TURNS), null otherwise.
        """
        cfg = self.config_loader()
        if not cfg.enabled:
            return _dump({"status": "disabled"})
        user_sub = _resolve_user_id(ctx)
        if not user_sub:
            return _dump({"status": "error", "error": "unauthenticated"})
        message = (message or "").strip()
        if not message:
            return _dump({"status": "error", "error": "empty_message"})
        message = message[:MAX_MESSAGE_CHARS]

        from ..message_bus import bus_manager

        where = bus_manager.lookup_session(_session_from_ctx(ctx))
        if not where:
            return _dump({"status": "error", "error": "not_registered",
                          "hint": "The add-on must be connected to the bus to chat."})
        bus_id, blender_uuid = str(where[0]), where[1]

        key = (user_sub, blender_uuid)
        session = _session_from_ctx(ctx)
        held = self._busy.get(key)
        if held is not None and held[1] is not None and not held[1].done():
            if held[0] is session:
                return _dump({"status": "busy"})
            logger.info("chat: %s reconnected mid-turn; replacing the orphaned turn", blender_uuid)
            held[1].cancel()
        me = asyncio.current_task()
        self._busy[key] = (session, me)
        t0 = time.monotonic()
        try:
            try:
                row = await self.handler.load_settings(user_sub)
            except ProviderError as e:
                return _dump({"status": "backend_error", "detail": str(e)})
            try:
                backend = chat_settings.backend_for(row, cfg, user_sub)
            except ProviderError as e:
                return _dump({"status": "backend_error", "detail": str(e),
                              "trial": await self._trial(cfg, row, user_sub)})
            if backend is None:
                return _dump({"status": "no_backend", "hint": NO_BACKEND_HINT,
                              "trial": await self._trial(cfg, row, user_sub)})
            trial_view = None
            if chat_settings.on_trial(row, cfg, user_sub):
                # Spent here, before the turn's first model call: a turn that
                # is cancelled or fails later still counts.
                try:
                    granted, trial_view = await trial.consume(self.handler.session_factory,
                                                              cfg, user_sub)
                except Exception as e:  # noqa: BLE001 - reported, never a crash
                    logger.warning("chat trial count failed: %s", type(e).__name__)
                    return _dump({"status": "backend_error", "trial": None,
                                  "detail": "could not check this account's free messages"})
                if not granted:
                    return _dump({"status": "trial_ended", "trial": trial_view,
                                  "hint": trial.ENDED_HINT})
            return await self._run(ctx, cfg, backend, user_sub, bus_id, blender_uuid,
                                   message, history, t0, attachments, trial_view)
        finally:
            if self._busy.get(key, (None, None))[1] is me:
                self._busy.pop(key, None)

    async def _trial(self, cfg: ChatConfig, row, user_sub: str) -> dict | None:
        """The account's ``trial`` view; None when it isn't on the trial."""
        if not chat_settings.on_trial(row, cfg, user_sub):
            return None
        return await trial.status(self.handler.session_factory, cfg, user_sub)

    async def _run(self, ctx, cfg, backend, user_sub, bus_id, blender_uuid,
                   message, history, t0, attachments=None, trial_view=None) -> str:
        catalog = await build_catalog(
            ctx.fastmcp, cfg.tools or None,
            vision=vision.available(cfg, user_sub, backend),
        )
        catalog = user_tools.merge(
            catalog, user_tools.registry.get(bus_id, blender_uuid, _session_from_ctx(ctx)),
            cfg.max_tools,
        )
        token = current_backend.set(backend)
        turn_id = f"{user_sub}:{uuid.uuid4().hex}"
        turn_token = current_turn.set(turn_id)
        turn = None
        base: dict[str, Any] = {}
        try:
            async with ChatExecutor(ctx.fastmcp, user_sub, blender_uuid, bus_id) as executor:
                turn = Turn(ctx, cfg, self.handler, backend, executor, catalog, user_sub)
                try:
                    reply = await asyncio.wait_for(turn.run(message, history, attachments),
                                                   timeout=cfg.turn_timeout_s)
                    base = {"status": "ok", "reply": reply}
                except TimeoutError:
                    base = {"status": "timeout", "reply": turn.reply,
                            "detail": f"the turn took longer than {cfg.turn_timeout_s}s"}
                except ProviderError as e:
                    base = {"status": "backend_error", "detail": str(e)}
                except ValueError as e:  # sampling not routable (no handler installed)
                    base = {"status": "backend_error", "detail": str(e)}
        finally:
            current_turn.reset(turn_token)
            current_backend.reset(token)
            self.handler.end_turn(turn_id)
        base.update({
            "steps": turn.steps if turn else [],
            "elapsed_s": round(time.monotonic() - t0, 1),
            "backend": backend.public(),
            "trial": trial_view,
        })
        return _dump(base)

    @mcp_tool()
    @require_role("addon")
    async def set_chat_backend(
        self,
        provider: str,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        clear: bool = False,
        ctx: Context = None,
    ) -> str:
        """Choose the model backend for this account's chat turns.

        ``provider``: ``gateway`` (the server's own backend, the default),
        ``anthropic`` (your API key; ``model`` defaults to the server's Claude
        model, else claude-opus-5) or ``openai`` (any OpenAI-compatible
        ``base_url`` + ``model``, key optional). A new Claude key is checked
        with Anthropic first and refused (``invalid_api_key`` /
        ``key_check_failed``) unless it works. The key is stored encrypted
        and never returned; omit it to keep the saved one when only the model
        changes. ``clear`` removes the setting (back to the server's
        default). Returns ``{status, backend: {provider, model, base_url,
        has_key, source}, trial}``.
        """
        cfg = self.config_loader()
        user_sub = _resolve_user_id(ctx)
        if not user_sub:
            return _dump({"status": "error", "error": "unauthenticated"})
        try:
            if provider == "anthropic" and not clear and (api_key or "").strip():
                chat_settings.check_key_storable(cfg, api_key, model)
                await chat_settings.verify_anthropic_key(api_key.strip(),
                                                         self.handler.anthropic_transport)
            row = await chat_settings.save(
                self.handler.session_factory, cfg, user_sub, provider,
                model=model, base_url=base_url, api_key=api_key, clear=clear,
            )
        except chat_settings.SettingsError as e:
            row = await chat_settings.load(self.handler.session_factory, user_sub)
            return _dump({"status": "error", "error": e.code, "detail": e.detail,
                          "trial": await self._trial(cfg, row, user_sub)})
        return _dump({"status": "ok", "backend": chat_settings.public_view(row, cfg),
                      "trial": await self._trial(cfg, row, user_sub)})

    @mcp_tool()
    @require_role("addon")
    async def set_chat_advisor(self, advisor: str, ctx: Context = None) -> str:
        """Choose the model this account's Claude chat may escalate to.

        ``advisor``: a model id from ``backend.advisors`` (see
        blender_get_chat_backend), ``off``, or empty to follow the server's
        default. Returns ``{status, backend, trial}`` like blender_get_chat_backend.
        """
        cfg = self.config_loader()
        user_sub = _resolve_user_id(ctx)
        if not user_sub:
            return _dump({"status": "error", "error": "unauthenticated"})
        try:
            row = await chat_settings.save_advisor(self.handler.session_factory, cfg,
                                                   user_sub, advisor or "")
        except chat_settings.SettingsError as e:
            return _dump({"status": "error", "error": e.code, "detail": e.detail})
        return _dump({"status": "ok", "backend": chat_settings.public_view(row, cfg),
                      "trial": await self._trial(cfg, row, user_sub)})

    @mcp_tool()
    @require_role("addon")
    async def get_chat_backend(self, ctx: Context = None) -> str:
        """This account's chat backend: ``{status, backend: {provider, model,
        base_url, has_key, source}, trial}``. ``source`` is ``user`` for a
        backend the account saved, ``server`` for the server's default.
        ``trial`` is ``{limit, used, remaining}`` for an account on the free
        trial, null otherwise. No key, the account's or the server's, is ever
        returned."""
        cfg = self.config_loader()
        user_sub = _resolve_user_id(ctx)
        if not user_sub:
            return _dump({"status": "error", "error": "unauthenticated"})
        row = await chat_settings.load(self.handler.session_factory, user_sub)
        return _dump({"status": "ok", "backend": chat_settings.public_view(row, cfg),
                      "trial": await self._trial(cfg, row, user_sub)})
