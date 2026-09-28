"""The server's sampling handler: routes ``ctx.sample_step`` to the user's backend.

Installed with ``sampling_handler_behavior="fallback"``: a client that
advertises sampling with tools answers with its own model, and everything
else (the add-on) lands here. ``blender_chat`` resolves the backend once per
turn and puts it in ``current_backend``; a call without one resolves it from
the caller's identity.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextvars import ContextVar
from typing import Any

import httpx
from mcp.types import CreateMessageResultWithTools, TextContent, ToolUseContent

from . import settings as chat_settings
from .config import ChatConfig, load_config
from .providers import Backend, Completion, ProviderError, complete

logger = logging.getLogger(__name__)

current_backend: ContextVar[Backend | None] = ContextVar("chat_current_backend", default=None)


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
    ):
        # ``transport`` exists for tests (httpx.MockTransport); None = network.
        self.transport = transport
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
        return await complete(
            backend, system, messages, tools,
            max_tokens=max_tokens or cfg.max_tokens,
            temperature=temperature,
            tool_choice=tool_choice,
            timeout_s=cfg.llm_timeout_s,
            transport=self.transport,
        )

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
