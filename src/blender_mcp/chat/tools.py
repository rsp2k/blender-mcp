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
from collections.abc import Callable
from typing import Any

from fastmcp import Context
from fastmcp.contrib.mcp_mixin import MCPMixin, mcp_tool

from ..bus_tools import _resolve_user_id, _session_from_ctx
from ..client_role import require_role
from . import settings as chat_settings
from . import user_tools, vision
from .catalog import build_catalog
from .config import ChatConfig, load_config
from .executor import ChatExecutor
from .providers import ProviderError
from .routing import RoutingSamplingHandler, current_backend
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
        self._busy: set[str] = set()

    @mcp_tool()
    @require_role("addon")
    async def chat(
        self,
        message: str,
        history: list[dict[str, Any]] | None = None,
        ctx: Context = None,
    ) -> str:
        """Run one chat turn in the Blender that sent it (the add-on's Chat tab).

        ``history`` is the recent conversation, text only, at most 20
        ``{role, content}`` entries. Progress arrives as progress
        notifications whose message is a JSON event; approvals for risky
        tools arrive as elicitation requests starting "BlenderMCP approval:".
        Returns JSON with ``status`` ok / disabled / no_backend / busy /
        backend_error / timeout.
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

        if user_sub in self._busy:
            return _dump({"status": "busy"})
        self._busy.add(user_sub)
        t0 = time.monotonic()
        try:
            try:
                backend = await self.handler.resolve(user_sub, cfg)
            except ProviderError as e:
                return _dump({"status": "backend_error", "detail": str(e)})
            if backend is None:
                return _dump({"status": "no_backend", "hint": NO_BACKEND_HINT})
            return await self._run(ctx, cfg, backend, user_sub, bus_id, blender_uuid,
                                   message, history, t0)
        finally:
            self._busy.discard(user_sub)

    async def _run(self, ctx, cfg, backend, user_sub, bus_id, blender_uuid,
                   message, history, t0) -> str:
        catalog = await build_catalog(
            ctx.fastmcp, cfg.tools or None,
            vision=vision.available(cfg, user_sub, backend),
        )
        catalog = user_tools.merge(
            catalog, user_tools.registry.get(bus_id, blender_uuid, _session_from_ctx(ctx)),
            cfg.max_tools,
        )
        token = current_backend.set(backend)
        turn = None
        base: dict[str, Any] = {}
        try:
            async with ChatExecutor(ctx.fastmcp, user_sub, blender_uuid, bus_id) as executor:
                turn = Turn(ctx, cfg, self.handler, backend, executor, catalog, user_sub)
                try:
                    reply = await asyncio.wait_for(turn.run(message, history),
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
            current_backend.reset(token)
        base.update({
            "steps": turn.steps if turn else [],
            "elapsed_s": round(time.monotonic() - t0, 1),
            "backend": backend.public(),
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

        ``provider``: ``gateway`` (the shared GPU gateway, the default),
        ``anthropic`` (your API key; ``model`` defaults to claude-sonnet-5) or
        ``openai`` (any OpenAI-compatible ``base_url`` + ``model``, key
        optional). The key is stored encrypted and never returned; omit it to
        keep the saved one when only the model changes. ``clear`` removes the
        setting (back to the gateway). Returns ``{status, backend: {provider,
        model, base_url, has_key}}``.
        """
        cfg = self.config_loader()
        user_sub = _resolve_user_id(ctx)
        if not user_sub:
            return _dump({"status": "error", "error": "unauthenticated"})
        try:
            row = await chat_settings.save(
                self.handler.session_factory, cfg, user_sub, provider,
                model=model, base_url=base_url, api_key=api_key, clear=clear,
            )
        except chat_settings.SettingsError as e:
            return _dump({"status": "error", "error": e.code, "detail": e.detail})
        return _dump({"status": "ok", "backend": chat_settings.public_view(row, cfg)})

    @mcp_tool()
    @require_role("addon")
    async def get_chat_backend(self, ctx: Context = None) -> str:
        """This account's chat backend: ``{status, backend: {provider, model,
        base_url, has_key}}``. The key itself is never returned."""
        cfg = self.config_loader()
        user_sub = _resolve_user_id(ctx)
        if not user_sub:
            return _dump({"status": "error", "error": "unauthenticated"})
        row = await chat_settings.load(self.handler.session_factory, user_sub)
        return _dump({"status": "ok", "backend": chat_settings.public_view(row, cfg)})
