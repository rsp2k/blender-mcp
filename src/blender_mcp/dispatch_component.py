"""Flat MCP tools that round-trip through the bus to a Blender addon.

The 24 addon-side commands (registered in ``addon/executor/registry.py``)
are reachable from MCP clients via ``send_message`` + listen-for-reply
notifications, but that requires the caller to do their own job_id
correlation. This component exposes each command as a *flat* MCP tool
that handles the round-trip internally:

    blender_get_scene_info()          -> dict snapshot
    blender_execute_code(code="...")  -> stdout capture
    blender_get_viewport_screenshot() -> {filepath, width, height}
    ...

The dispatch tools share a common ``_dispatch`` helper that:

1. Picks a target blender client (explicit ``target_uuid`` or
   auto-pick if exactly one ``client_type=="blender"`` is registered).
2. Generates a fresh ``job_id`` and registers an ``asyncio.Future``
   via :class:`JobWaiter`.
3. Routes a ``command_dispatch`` payload through the user's bus.
4. Awaits the Future with the per-tool timeout.
5. Returns the result as a JSON string (matching the
   ``bus_tools.py`` / ``diagnostics_component.py`` convention).

Timeouts, no-client, ambiguous-target, and addon-side failures all
surface as structured JSON; nothing raises. MCP clients (Claude
Desktop, etc.) render JSON way better than tool exceptions.

Phase A ships Tier-1 (7 core commands). Phase B adds Tier-2 status
checks + Tier-3 integration-gated commands.
"""

from __future__ import annotations

import asyncio
import json
import uuid as _uuid_mod
from typing import Optional

from fastmcp import Context
from fastmcp.contrib.mcp_mixin import MCPMixin, mcp_tool

from .bus_tools import _pending_jobs, _resolve_user_id
from .job_waiter import job_waiter
from .message_bus import bus_manager
from .message_router import Priority


# Per-tool default timeout (seconds) — kept short enough that a stuck
# Blender doesn't hang the MCP client forever, long enough that real
# work fits. Override per call via the underscore-prefixed ``_timeout``
# kwarg so the name doesn't collide with handler params.
DEFAULT_TIMEOUT_S = 30.0


def _new_job_id() -> str:
    """Short readable job_id; not a UUID4 because we want compact logs."""
    return f"j-{_uuid_mod.uuid4().hex[:12]}"


def _pick_blender_target(user_id: str, target_uuid: Optional[str]) -> dict:
    """Resolve a target Blender client or return a structured error.

    Returns ``{"ok": True, "uuid": ...}`` on success, or
    ``{"ok": False, "status": "...", ...}`` for the various failure modes
    so the caller can convert to JSON wire output without branching on
    multiple exception types.
    """
    bus = bus_manager.get_bus(user_id)
    blender_clients = [
        c for c in bus.all_clients() if c.client_type == "blender"
    ]

    if target_uuid:
        for c in blender_clients:
            if c.uuid == target_uuid:
                return {"ok": True, "uuid": target_uuid}
        return {
            "ok": False,
            "status": "unknown_target",
            "target_uuid": target_uuid,
            "hint": "No registered Blender client has that UUID.",
        }

    if not blender_clients:
        return {
            "ok": False,
            "status": "no_client",
            "hint": (
                "No Blender client connected to your bus. Open Blender, "
                "enable the BlenderMCP addon, click Login then Connect."
            ),
        }

    if len(blender_clients) > 1:
        return {
            "ok": False,
            "status": "ambiguous_target",
            "candidates": [c.uuid for c in blender_clients],
            "hint": (
                "Multiple Blender clients connected; pass target_uuid="
                "<one of candidates> to disambiguate."
            ),
        }

    return {"ok": True, "uuid": blender_clients[0].uuid}


async def _dispatch(
    user_id: str,
    command: str,
    params: dict,
    target_uuid: Optional[str],
    timeout: float,
) -> str:
    """Common send-then-await for every dispatch tool. Returns JSON string."""
    pick = _pick_blender_target(user_id, target_uuid)
    if not pick["ok"]:
        return json.dumps(pick | {"ok": False, "command": command})

    chosen_uuid = pick["uuid"]
    job_id = _new_job_id()

    # Register the Future BEFORE sending so we can't race the reply.
    future = job_waiter.register(user_id, job_id)

    # Manually populate _pending_jobs so job_update's lookup finds us
    # without having to come back through send_message's tracking (the
    # server doesn't have a from_uuid here — we're the dispatcher itself).
    _pending_jobs[job_id] = (user_id, f"server-dispatch:{user_id}")

    bus = bus_manager.get_bus(user_id)
    bus.route(
        payload={
            "message_type": "command_dispatch",
            "job_id": job_id,
            "command": command,
            "params": params,
        },
        from_uuid=f"server-dispatch:{user_id}",
        routing={"type": "direct", "target_uuid": chosen_uuid},
        priority=Priority.INFO,
        job_id=job_id,
    )

    try:
        result = await asyncio.wait_for(future, timeout=timeout)
    except asyncio.TimeoutError:
        job_waiter.cancel(user_id, job_id)
        _pending_jobs.pop(job_id, None)
        return json.dumps({
            "status": "timeout",
            "command": command,
            "target_uuid": chosen_uuid,
            "waited_seconds": timeout,
            "hint": (
                "The Blender client didn't reply within the timeout. The "
                "addon may be stuck, or the bus delivery dropped. Check the "
                "Blender system console for tracebacks."
            ),
        })

    return json.dumps({
        "status": result.get("status", "unknown"),
        "command": command,
        "target_uuid": chosen_uuid,
        "job_id": job_id,
        "result": result.get("result"),
        "error": result.get("error", ""),
    })


class BlenderDispatchComponent(MCPMixin):
    """Tier-1 dispatch tools — 7 always-on core commands.

    All tools share the same signature shape:

        - <handler-specific kwargs>
        - target_uuid: Optional[str]  — explicit override
        - _timeout: float              — override default
        - ctx: Context = None          — MCP plumbing

    Each tool body is a one-liner that delegates to :func:`_dispatch`.
    Phase B adds the Tier-2 + Tier-3 surfaces; this file stays small
    until then.
    """

    # ---- Tier 1: always-on core (7 commands) -----------------------

    @mcp_tool()
    async def get_scene_info(
        self,
        target_uuid: Optional[str] = None,
        _timeout: float = DEFAULT_TIMEOUT_S,
        ctx: Context = None,
    ) -> str:
        """Snapshot of the active Blender scene: name, object count, first N objects."""
        user_id = _resolve_user_id(ctx)
        if not user_id:
            return json.dumps({"status": "error", "error": "unauthenticated"})
        return await _dispatch(user_id, "get_scene_info", {}, target_uuid, _timeout)

    @mcp_tool()
    async def get_object_info(
        self,
        name: str,
        target_uuid: Optional[str] = None,
        _timeout: float = DEFAULT_TIMEOUT_S,
        ctx: Context = None,
    ) -> str:
        """Detailed info for a specific Blender object by name."""
        user_id = _resolve_user_id(ctx)
        if not user_id:
            return json.dumps({"status": "error", "error": "unauthenticated"})
        return await _dispatch(
            user_id, "get_object_info", {"name": name}, target_uuid, _timeout
        )

    @mcp_tool()
    async def browse_data(
        self,
        collection: Optional[str] = None,
        item_name: Optional[str] = None,
        page: int = 1,
        page_size: int = 50,
        detail_level: str = "summary",
        target_uuid: Optional[str] = None,
        _timeout: float = DEFAULT_TIMEOUT_S,
        ctx: Context = None,
    ) -> str:
        """Browse ``bpy.data.*`` collections with pagination + detail levels."""
        user_id = _resolve_user_id(ctx)
        if not user_id:
            return json.dumps({"status": "error", "error": "unauthenticated"})
        return await _dispatch(
            user_id,
            "browse_data",
            {
                "collection": collection,
                "item_name": item_name,
                "page": page,
                "page_size": page_size,
                "detail_level": detail_level,
            },
            target_uuid,
            _timeout,
        )

    @mcp_tool()
    async def execute_code(
        self,
        code: str,
        target_uuid: Optional[str] = None,
        _timeout: float = 60.0,  # code can take longer than default
        ctx: Context = None,
    ) -> str:
        """Execute arbitrary Python in the Blender main thread. Returns stdout."""
        user_id = _resolve_user_id(ctx)
        if not user_id:
            return json.dumps({"status": "error", "error": "unauthenticated"})
        return await _dispatch(
            user_id, "execute_code", {"code": code}, target_uuid, _timeout
        )

    @mcp_tool()
    async def get_viewport_screenshot(
        self,
        filepath: str,
        max_size: int = 800,
        format: str = "png",
        target_uuid: Optional[str] = None,
        _timeout: float = DEFAULT_TIMEOUT_S,
        ctx: Context = None,
    ) -> str:
        """Save a 3D viewport screenshot to ``filepath`` (resized to ``max_size``)."""
        user_id = _resolve_user_id(ctx)
        if not user_id:
            return json.dumps({"status": "error", "error": "unauthenticated"})
        return await _dispatch(
            user_id,
            "get_viewport_screenshot",
            {"filepath": filepath, "max_size": max_size, "format": format},
            target_uuid,
            _timeout,
        )

    @mcp_tool()
    async def get_console_output(
        self,
        level: str = "all",
        page: int = 1,
        page_size: int = 50,
        target_uuid: Optional[str] = None,
        _timeout: float = DEFAULT_TIMEOUT_S,
        ctx: Context = None,
    ) -> str:
        """Paginated Blender console scrape; ``level`` in {all, info, warning, error, output}."""
        user_id = _resolve_user_id(ctx)
        if not user_id:
            return json.dumps({"status": "error", "error": "unauthenticated"})
        return await _dispatch(
            user_id,
            "get_console_output",
            {"level": level, "page": page, "page_size": page_size},
            target_uuid,
            _timeout,
        )

    @mcp_tool()
    async def console_operations(
        self,
        operation: str = "get_info",
        params: Optional[dict] = None,
        target_uuid: Optional[str] = None,
        _timeout: float = DEFAULT_TIMEOUT_S,
        ctx: Context = None,
    ) -> str:
        """Invoke a ``bpy.ops.console.*`` operator. See addon's console_operations handler."""
        user_id = _resolve_user_id(ctx)
        if not user_id:
            return json.dumps({"status": "error", "error": "unauthenticated"})
        return await _dispatch(
            user_id,
            "console_operations",
            {"operation": operation, "params": params or {}},
            target_uuid,
            _timeout,
        )
