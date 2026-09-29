"""The server's sampling handler: routes ``ctx.sample_step`` to the user's backend.

Installed with ``sampling_handler_behavior="fallback"``: a client that
advertises sampling with tools answers with its own model, and everything
else (the add-on) lands here. ``blender_chat`` resolves the backend once per
turn and puts it in ``current_backend``; a call without one resolves it from
the caller's identity.
"""

from __future__ import annotations

import hashlib
import logging
from collections import OrderedDict
from collections.abc import Callable
from contextvars import ContextVar
from typing import Any

import httpx
import httpx2
from mcp.types import CreateMessageResultWithTools, TextContent, ToolUseContent

from . import settings as chat_settings
from .config import ChatConfig, load_config
from .providers import Backend, Completion, ProviderError, complete

logger = logging.getLogger(__name__)

current_backend: ContextVar[Backend | None] = ContextVar("chat_current_backend", default=None)
# One chat turn's identity ("<user>:<uuid>"), set by blender_chat next to
# current_backend. Scopes the thinking cache so turns and users never share it.
current_turn: ContextVar[str | None] = ContextVar("chat_current_turn", default=None)


class ThinkingCache:
    """Raw Anthropic assistant content per tool round, for replay.

    Keyed by (scope, the round's tool_use ids). The scope is the turn when one
    is set, otherwise a hash of the backend's key, so one account's blocks can
    never be replayed into another's request. Bounded LRU; a finished turn's
    entries are dropped with ``forget``.
    """

    def __init__(self, max_entries: int = 256):
        self.max_entries = max_entries
        self._data: OrderedDict[tuple[str, frozenset[str]], list[dict]] = OrderedDict()

    @staticmethod
    def scope(backend: Backend) -> str:
        turn = current_turn.get()
        if turn:
            return "turn:" + turn
        digest = hashlib.sha256(f"{backend.base_url}\0{backend.api_key}".encode()).hexdigest()
        return "key:" + digest[:32]

    def put(self, scope: str, ids: frozenset[str], raw: list[dict]) -> None:
        if not ids or not raw:
            return
        key = (scope, ids)
        self._data[key] = raw
        self._data.move_to_end(key)
        while len(self._data) > self.max_entries:
            self._data.popitem(last=False)

    def get(self, scope: str, ids: frozenset[str]) -> list[dict] | None:
        key = (scope, ids)
        raw = self._data.get(key)
        if raw is not None:
            self._data.move_to_end(key)
        return raw

    def forget(self, scope: str) -> None:
        for key in [k for k in self._data if k[0] == scope]:
            del self._data[key]

    def __len__(self) -> int:
        return len(self._data)


def to_result(c: Completion, model: str) -> CreateMessageResultWithTools:
    content: list[Any] = []
    if c.text:
        content.append(TextContent(type="text", text=c.text))
    for call in c.tool_calls:
        content.append(ToolUseContent(type="tool_use", id=call.id, name=call.name,
                                      input=call.arguments))
    if not content:
        content.append(TextContent(type="text", text=""))
    return CreateMessageResultWithTools(
        role="assistant", content=content, model=model,
        stopReason="toolUse" if c.tool_calls else "endTurn",
    )


class RoutingSamplingHandler:
    def __init__(
        self,
        transport: httpx.AsyncBaseTransport | None = None,
        session_factory: Callable[[], Any] | None = None,
        config_loader: Callable[[], ChatConfig] = load_config,
        anthropic_transport: httpx2.AsyncBaseTransport | None = None,
    ):
        # Transports exist for tests; None = network. ``transport`` is an httpx
        # one for the OpenAI-compatible backends, ``anthropic_transport`` an
        # httpx2 one for the Anthropic SDK (which is built on httpx2).
        self.transport = transport
        self.anthropic_transport = anthropic_transport
        self.thinking = ThinkingCache()
        self._session_factory = session_factory
        self.config_loader = config_loader

    @property
    def session_factory(self) -> Callable[[], Any]:
        if self._session_factory is None:
            from ..storage import get_session

            return get_session
        return self._session_factory

    async def resolve(self, user_sub: str | None, cfg: ChatConfig | None = None) -> Backend | None:
        """The user's backend, None when they have none. ProviderError on bad state."""
        cfg = cfg or self.config_loader()
        row = None
        if user_sub:
            try:
                row = await chat_settings.load(self.session_factory, user_sub)
            except Exception as e:
                logger.warning("chat settings lookup failed: %s", type(e).__name__)
                raise ProviderError("could not load this account's chat settings") from e
        return chat_settings.backend_for(row, cfg, user_sub)

    async def complete(self, backend: Backend, system, messages, tools, *,
                       tool_choice: str | None = None, max_tokens: int | None = None,
                       temperature: float | None = None) -> Completion:
        cfg = self.config_loader()
        extra: dict[str, Any] = {}
        scope = None
        if backend.provider == "anthropic":
            scope = self.thinking.scope(backend)
            extra = {
                "anthropic_transport": self.anthropic_transport,
                "replay": lambda ids: self.thinking.get(scope, ids),
                "fallbacks": cfg.anthropic_fallbacks,
                "effort": cfg.anthropic_effort,
                "advisor": cfg.anthropic_advisor,
                "advisor_max_tokens": cfg.anthropic_advisor_max_tokens,
            }
        c = await complete(
            backend, system, messages, tools,
            max_tokens=max_tokens or cfg.max_tokens,
            temperature=temperature,
            tool_choice=tool_choice,
            timeout_s=cfg.llm_timeout_s,
            transport=self.transport,
            **extra,
        )
        if scope is not None and c.raw and c.tool_calls:
            self.thinking.put(scope, frozenset(t.id for t in c.tool_calls), c.raw)
        return c

    def end_turn(self, turn: str) -> None:
        self.thinking.forget("turn:" + turn)

    async def __call__(self, messages, params, request_context) -> CreateMessageResultWithTools:
        backend = current_backend.get()
        if backend is None:
            from ..bus_tools import _resolve_user_id

            backend = await self.resolve(_resolve_user_id(None))
            if backend is None:
                raise ProviderError("no chat backend is available for this account")
        c = await self.complete(
            backend, params.systemPrompt, messages, params.tools,
            tool_choice=params.toolChoice.mode if params.toolChoice else None,
            max_tokens=params.maxTokens,
            temperature=params.temperature,
        )
        return to_result(c, backend.model)
