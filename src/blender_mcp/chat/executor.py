"""Run the model's tool calls through the server's own MCP tools.

An in-process ``fastmcp.Client`` of the same server, opened with the calling
user's identity in ``bus_tools.current_user_id`` and a ``chat:<uuid>``
downstream client id (role ``llm-client``, which the dispatch tools require),
so every tool's validation, role gate and QA instrumentation applies as for
any other client. ContextVars set before the client connects carry into the
in-process server's tasks.

``target_uuid`` and ``bus_id`` are always the calling Blender's: whatever the
model sends for them, or for ``_timeout``/``store``, is dropped first.
"""

from __future__ import annotations

import json
from typing import Any, Self

from fastmcp import Client
from mcp.types import ImageContent, TextContent

from ..bus_tools import current_user_id
from ..client_role import current_downstream_client_id
from .catalog import STRIPPED_ARGS

CHAT_CLIENT_PREFIX = "chat:"
_FAIL_STATUS = frozenset({
    "error", "failed", "timeout", "cancelled", "lost", "wrong_role", "unknown_target",
    "no_client", "no_live_client", "ambiguous_target", "not_a_member",
})


def result_ok(text: str) -> bool:
    """False when a tool's JSON reply reports a failure."""
    try:
        d = json.loads(text)
    except (ValueError, TypeError):
        return True
    if not isinstance(d, dict):
        return True
    status = d.get("status")
    if status in _FAIL_STATUS:
        return False
    return not (status is None and d.get("error"))


class ChatExecutor:
    def __init__(self, server, user_sub: str, blender_uuid: str, bus_id: str | None = None):
        self.server = server
        self.user_sub = user_sub
        self.blender_uuid = blender_uuid
        self.bus_id = bus_id
        self._client: Client | None = None
        self._tokens: tuple[Any, Any] | None = None

    async def __aenter__(self) -> Self:
        self._tokens = (
            current_user_id.set(self.user_sub),
            current_downstream_client_id.set(CHAT_CLIENT_PREFIX + self.blender_uuid),
        )
        try:
            self._client = Client(self.server)
            await self._client.__aenter__()
        except BaseException:
            self._reset()
            raise
        return self

    async def __aexit__(self, *exc) -> None:
        try:
            if self._client is not None:
                await self._client.__aexit__(*exc)
        finally:
            self._client = None
            self._reset()

    def _reset(self) -> None:
        if self._tokens is not None:
            current_downstream_client_id.reset(self._tokens[1])
            current_user_id.reset(self._tokens[0])
            self._tokens = None

    def aimed(self, args: dict | None, extra: dict | None = None) -> dict:
        """The arguments actually sent: model args minus aiming ones, plus ours."""
        clean = {k: v for k, v in (args or {}).items() if k not in STRIPPED_ARGS}
        clean.update(extra or {})
        clean["target_uuid"] = self.blender_uuid
        if self.bus_id:
            clean["bus_id"] = self.bus_id
        return clean

    async def call(self, server_name: str, args: dict | None,
                   extra: dict | None = None) -> tuple[bool, str]:
        """(ok, text) for one tool call. Never raises for a tool-side failure."""
        if self._client is None:
            raise RuntimeError("ChatExecutor used outside its context")
        res = await self._client.call_tool_mcp(server_name, self.aimed(args, extra))
        parts = []
        for c in res.content or []:
            if isinstance(c, TextContent):
                parts.append(c.text)
            elif isinstance(c, ImageContent):
                parts.append("[image returned; use look_at_viewport to have it described]")
        text = "\n".join(parts)
        return (not res.isError) and result_ok(text), text

    async def call_user_tool(self, server: str, tool: str, args: dict | None,
                             timeout_s: int | None = None) -> tuple[bool, str]:
        """(ok, text) for a tool on the calling Blender's own tool server.

        Not an MCP tool: dispatched as ``user_tool_call`` straight to this
        Blender (chat/user_tools.py). Arguments go through untouched, since
        they only reach the user's tool server, never a Blender tool.
        """
        from . import user_tools

        return await user_tools.call(self.user_sub, self.bus_id, self.blender_uuid,
                                     server, tool, dict(args or {}),
                                     timeout_s or user_tools.CALL_TIMEOUT_S)
