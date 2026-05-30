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

Surface tiers:

- **Tier 1** (7) — always-on core: scene/object inspection, code exec,
  viewport screenshot, console scrape.
- **Tier 2** (3) — always-on integration status probes.
- **Tier 3** (14) — gated by addon prefs (polyhaven / hyper3d /
  sketchfab / msgbus). When the gate is off, the addon returns an
  "Unknown command type" error which the dispatcher faithfully relays.
"""

from __future__ import annotations

import asyncio
import json
import uuid as _uuid_mod
from typing import Any, Optional

from fastmcp import Context
from fastmcp.contrib.mcp_mixin import MCPMixin, mcp_resource, mcp_tool

from .bus_tools import _pending_jobs, _resolve_user_id, resolve_bus
from .client_role import check_role_or_reject
from .job_waiter import job_waiter
from .message_router import Priority


# Per-tool timeouts (seconds). Kept short enough that a stuck Blender
# doesn't hang the MCP client forever, long enough that real work fits.
# Override per call via the underscore-prefixed ``_timeout`` kwarg so
# the name doesn't collide with any handler params.
TIMEOUT_FAST = 15.0        # status checks, msgbus reads
DEFAULT_TIMEOUT_S = 30.0   # most read-only commands
TIMEOUT_MEDIUM = 60.0      # execute_code, rodin job creation
TIMEOUT_LONG = 180.0       # polyhaven/sketchfab downloads, asset imports


def _new_job_id() -> str:
    """Short readable job_id; not a UUID4 because we want compact logs."""
    return f"j-{_uuid_mod.uuid4().hex[:12]}"


def _pick_blender_target(bus, target_uuid: Optional[str]) -> dict:
    """Resolve a target Blender client on ``bus`` or return a structured error.

    Returns ``{"ok": True, "uuid": ...}`` on success, or
    ``{"ok": False, "status": "...", ...}`` for the various failure modes
    so the caller can convert to JSON wire output without branching on
    multiple exception types.
    """
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
            "hint": "No registered Blender client has that UUID on this bus.",
        }

    if not blender_clients:
        return {
            "ok": False,
            "status": "no_client",
            "hint": (
                "No Blender client connected to this bus. Open Blender, "
                "enable the BlenderMCP addon, click Login then Connect "
                "(addon prefs let you pick which bus to register on)."
            ),
        }

    if len(blender_clients) > 1:
        return {
            "ok": False,
            "status": "ambiguous_target",
            "candidates": [
                {"uuid": c.uuid, "label": c.label} for c in blender_clients
            ],
            "hint": (
                "Multiple Blender clients connected; pass target_uuid="
                "<one of candidates> to disambiguate."
            ),
        }

    return {"ok": True, "uuid": blender_clients[0].uuid}


async def _dispatch(
    bus,
    bus_id_str: str,
    command: str,
    params: dict,
    target_uuid: Optional[str],
    timeout: float,
) -> str:
    """Common send-then-await for every dispatch tool. Returns JSON string."""
    pick = _pick_blender_target(bus, target_uuid)
    if not pick["ok"]:
        return json.dumps(pick | {"ok": False, "command": command})

    chosen_uuid = pick["uuid"]
    job_id = _new_job_id()

    # Register the Future BEFORE sending so we can't race the reply.
    # Phase I5: job_waiter is keyed by (bus_id, job_id).
    future = job_waiter.register(bus_id_str, job_id)

    # Manually populate _pending_jobs so job_update's lookup finds us
    # without having to come back through send_message's tracking (the
    # server doesn't have a from_uuid here — we're the dispatcher itself).
    # Phase I5: entry is keyed (bus_id_str, originator_uuid).
    _pending_jobs[job_id] = (bus_id_str, f"server-dispatch:{bus_id_str}")

    bus.route(
        payload={
            "message_type": "command_dispatch",
            "job_id": job_id,
            "command": command,
            "params": params,
        },
        from_uuid=f"server-dispatch:{bus_id_str}",
        routing={"type": "direct", "target_uuid": chosen_uuid},
        priority=Priority.INFO,
        job_id=job_id,
    )

    try:
        result = await asyncio.wait_for(future, timeout=timeout)
    except asyncio.TimeoutError:
        job_waiter.cancel(bus_id_str, job_id)
        _pending_jobs.pop(job_id, None)
        # Enrich the timeout response with the target's liveness info so
        # the calling LLM can self-diagnose. Without this, every dispatch
        # failure looks the same — "Blender client didn't reply" — even
        # when the actual cause varies wildly:
        #   - Addon long-disconnected (last_seen ages ago)
        #   - Addon alive (heartbeat fresh) but main thread busy on a
        #     long render — most common when running heavy bpy ops
        #   - Addon alive but drainer stuck (rare; needs Disconnect/Connect)
        # The recency of last_seen disambiguates: heartbeat pings update
        # it every 30s, so a value >60s ago means the addon's bus_client
        # is gone, not just slow.
        import time as _time
        client_info = bus.get(chosen_uuid)
        now = _time.time()
        # IMPORTANT: ``last_seen`` reflects "last bus activity from this
        # client" (tool call, dispatch reply, register), NOT transport-
        # level heartbeat. MCP-SDK pings don't fire FastMCP middleware,
        # so a stale value here does NOT mean the addon is disconnected
        # — it just means no bus traffic in a while. Worth reporting
        # the number for caller context, but the hint should not draw
        # transport-liveness conclusions from it.
        # (See bus_activity_middleware docstring for the model gap.)
        if client_info is not None:
            seen_ago = now - client_info.last_seen
            hint = (
                f"Dispatch sent but no reply within {timeout:.0f}s "
                f"(last bus activity from this client: {seen_ago:.0f}s "
                f"ago — note: this counter doesn't track transport "
                f"heartbeat, only bus tool calls and dispatch replies). "
                f"Most common cause: Blender's main thread is busy with "
                f"a long bpy operation (heavy render, scene evaluation "
                f"on a complex graph, modal popup). Either wait and "
                f"retry, raise _timeout, or simplify the dispatched "
                f"code. Use Disconnect→Connect in the addon sidebar to "
                f"reset if the addon Status shows disconnected."
            )
        else:
            seen_ago = None
            hint = (
                "Target client is no longer registered on the bus. The "
                "addon may have unregistered between dispatch send and "
                "timeout. Verify with blender_list_available_clients."
            )
        return json.dumps({
            "status": "timeout",
            "command": command,
            "target_uuid": chosen_uuid,
            "waited_seconds": timeout,
            # Reports "last bus activity" not "last heartbeat" — see hint.
            # Caller should not interpret a stale value as "disconnected."
            "target_last_bus_activity_seconds_ago": (
                round(seen_ago, 1) if seen_ago is not None else None
            ),
            "target_is_registered": client_info is not None,
            "hint": hint,
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
    """24 flat dispatch tools, one per ``@command`` in the addon registry.

    Each tool body is a one-liner that delegates to :meth:`_call`, which
    in turn does the auth check + structured-error return + ``_dispatch``
    round-trip. Keeps the file under 500 lines despite covering 24 tools.

    Common signature shape:

        - <handler-specific kwargs>
        - target_uuid: Optional[str]  — explicit override
        - _timeout: float             — override per-tool default
        - ctx: Context = None         — MCP plumbing
    """

    async def _call(
        self,
        ctx: Optional[Context],
        command: str,
        params: dict,
        target_uuid: Optional[str],
        timeout: float = DEFAULT_TIMEOUT_S,
        bus_id: Optional[str] = None,
    ) -> str:
        """Auth-check + role-gate + bus-resolve + dispatch. Returns JSON.

        Role gating happens here (phase H) because every dispatch tool +
        every dispatch-backed resource funnels through this method. The
        ``blender_<command>`` name (vs. the funnel's "_call") appears in
        rejection logs so audit trails identify the actual offending tool.

        Bus resolution (phase I5): ``bus_id`` is optional; None defaults
        to the caller's personal bus. ``resolve_bus`` also enforces
        membership (returns ``not_a_member`` if the user isn't a member
        of the explicitly requested bus).
        """
        rejection = check_role_or_reject(f"blender_{command}", ctx, "llm-client")
        if rejection:
            return rejection
        user_id = _resolve_user_id(ctx)
        if not user_id:
            return json.dumps({"status": "error", "error": "unauthenticated"})
        resolved = await resolve_bus(user_id, bus_id)
        if not resolved["ok"]:
            return json.dumps(resolved)
        return await _dispatch(
            resolved["bus"],
            str(resolved["bus_id"]),
            command,
            params,
            target_uuid,
            timeout,
        )

    # ---- Tier 1: always-on core (7 commands) -----------------------

    @mcp_tool()
    async def get_scene_info(
        self,
        target_uuid: Optional[str] = None,
        _timeout: float = DEFAULT_TIMEOUT_S,
        bus_id: Optional[str] = None,
        ctx: Context = None,
    ) -> str:
        """Snapshot of the active Blender scene: name, object count, first N objects."""
        return await self._call(ctx, "get_scene_info", {}, target_uuid, _timeout, bus_id=bus_id)

    @mcp_tool()
    async def get_object_info(
        self,
        name: str,
        target_uuid: Optional[str] = None,
        _timeout: float = DEFAULT_TIMEOUT_S,
        bus_id: Optional[str] = None,
        ctx: Context = None,
    ) -> str:
        """Detailed info for a specific Blender object by name."""
        return await self._call(
            ctx, "get_object_info", {"name": name}, target_uuid, _timeout, bus_id=bus_id
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
        bus_id: Optional[str] = None,
        ctx: Context = None,
    ) -> str:
        """Browse ``bpy.data.*`` collections with pagination + detail levels."""
        return await self._call(
            ctx,
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
            bus_id=bus_id,
        )

    @mcp_tool()
    async def execute_code(
        self,
        code: str,
        target_uuid: Optional[str] = None,
        _timeout: float = TIMEOUT_MEDIUM,
        bus_id: Optional[str] = None,
        ctx: Context = None,
    ) -> str:
        """Execute arbitrary Python in the Blender main thread. Returns stdout."""
        return await self._call(
            ctx, "execute_code", {"code": code}, target_uuid, _timeout, bus_id=bus_id
        )

    @mcp_tool()
    async def get_viewport_screenshot(
        self,
        filepath: str,
        max_size: int = 800,
        format: str = "png",
        target_uuid: Optional[str] = None,
        _timeout: float = DEFAULT_TIMEOUT_S,
        bus_id: Optional[str] = None,
        ctx: Context = None,
    ) -> str:
        """Save a 3D viewport screenshot to ``filepath`` (resized to ``max_size``)."""
        return await self._call(
            ctx,
            "get_viewport_screenshot",
            {"filepath": filepath, "max_size": max_size, "format": format},
            target_uuid,
            _timeout,
            bus_id=bus_id,
        )

    @mcp_tool()
    async def get_console_output(
        self,
        level: str = "all",
        page: int = 1,
        page_size: int = 50,
        target_uuid: Optional[str] = None,
        _timeout: float = DEFAULT_TIMEOUT_S,
        bus_id: Optional[str] = None,
        ctx: Context = None,
    ) -> str:
        """Paginated Blender console scrape; ``level`` in {all, info, warning, error, output}."""
        return await self._call(
            ctx,
            "get_console_output",
            {"level": level, "page": page, "page_size": page_size},
            target_uuid,
            _timeout,
            bus_id=bus_id,
        )

    @mcp_tool()
    async def console_operations(
        self,
        operation: str = "get_info",
        params: Optional[dict] = None,
        target_uuid: Optional[str] = None,
        _timeout: float = DEFAULT_TIMEOUT_S,
        bus_id: Optional[str] = None,
        ctx: Context = None,
    ) -> str:
        """Invoke a ``bpy.ops.console.*`` operator. See addon's console_operations handler."""
        return await self._call(
            ctx,
            "console_operations",
            {"operation": operation, "params": params or {}},
            target_uuid,
            _timeout,
            bus_id=bus_id,
        )

    # ---- Tier 2: always-on integration status (3 commands) ---------

    @mcp_tool()
    async def get_polyhaven_status(
        self,
        target_uuid: Optional[str] = None,
        _timeout: float = TIMEOUT_FAST,
        bus_id: Optional[str] = None,
        ctx: Context = None,
    ) -> str:
        """Report whether PolyHaven integration is enabled in the addon prefs."""
        return await self._call(
            ctx, "get_polyhaven_status", {}, target_uuid, _timeout, bus_id=bus_id
        )

    @mcp_tool()
    async def get_hyper3d_status(
        self,
        target_uuid: Optional[str] = None,
        _timeout: float = TIMEOUT_FAST,
        bus_id: Optional[str] = None,
        ctx: Context = None,
    ) -> str:
        """Report Hyper3D Rodin integration status (enabled flag, mode, key type)."""
        return await self._call(
            ctx, "get_hyper3d_status", {}, target_uuid, _timeout, bus_id=bus_id
        )

    @mcp_tool()
    async def get_sketchfab_status(
        self,
        target_uuid: Optional[str] = None,
        _timeout: float = DEFAULT_TIMEOUT_S,  # talks to api.sketchfab.com
        bus_id: Optional[str] = None,
        ctx: Context = None,
    ) -> str:
        """Report Sketchfab integration status; also verifies the API key against /v3/me."""
        return await self._call(
            ctx, "get_sketchfab_status", {}, target_uuid, _timeout, bus_id=bus_id
        )

    # ---- Tier 3: gated by addon prefs ------------------------------
    # When the matching ``use_*`` pref is off the addon returns an
    # "Unknown command type" error rather than running the handler.

    # ---- Tier 3a: msgbus (5 commands, no addon-side gate) ----------

    @mcp_tool()
    async def msgbus_clear_by_owner(
        self,
        owner_id: str = "default",
        target_uuid: Optional[str] = None,
        _timeout: float = TIMEOUT_FAST,
        bus_id: Optional[str] = None,
        ctx: Context = None,
    ) -> str:
        """Drop all ``bpy.msgbus`` subscriptions owned by ``owner_id``."""
        return await self._call(
            ctx,
            "msgbus_clear_by_owner",
            {"owner_id": owner_id},
            target_uuid,
            _timeout,
            bus_id=bus_id,
        )

    @mcp_tool()
    async def msgbus_publish_rna(
        self,
        data_path: Optional[str] = None,
        target_uuid: Optional[str] = None,
        _timeout: float = TIMEOUT_FAST,
        bus_id: Optional[str] = None,
        ctx: Context = None,
    ) -> str:
        """Publish an RNA-property change on the bus.

        ``data_path`` is one of: ``frame_current``, ``active_object``,
        ``selected_objects``. Pass nothing to flush all pending messages.
        Tuple-form ``key`` argument from the addon handler is intentionally
        not exposed (tuples aren't JSON-friendly).
        """
        return await self._call(
            ctx,
            "msgbus_publish_rna",
            {"data_path": data_path},
            target_uuid,
            _timeout,
            bus_id=bus_id,
        )

    @mcp_tool()
    async def msgbus_subscribe_rna(
        self,
        data_path: str,
        owner_id: str = "default",
        notify_type: str = "UPDATE",
        persistent: bool = True,
        target_uuid: Optional[str] = None,
        _timeout: float = TIMEOUT_FAST,
        bus_id: Optional[str] = None,
        ctx: Context = None,
    ) -> str:
        """Subscribe to RNA-property changes; notifications queue in the addon."""
        return await self._call(
            ctx,
            "msgbus_subscribe_rna",
            {
                "owner_id": owner_id,
                "data_path": data_path,
                "notify_type": notify_type,
                "persistent": persistent,
            },
            target_uuid,
            _timeout,
            bus_id=bus_id,
        )

    @mcp_tool()
    async def msgbus_get_notifications(
        self,
        owner_id: Optional[str] = None,
        clear: bool = False,
        target_uuid: Optional[str] = None,
        _timeout: float = TIMEOUT_FAST,
        bus_id: Optional[str] = None,
        ctx: Context = None,
    ) -> str:
        """Drain the addon's RNA-change notification queue (optionally clearing)."""
        return await self._call(
            ctx,
            "msgbus_get_notifications",
            {"owner_id": owner_id, "clear": clear},
            target_uuid,
            _timeout,
            bus_id=bus_id,
        )

    @mcp_tool()
    async def msgbus_list_subscriptions(
        self,
        owner_id: Optional[str] = None,
        target_uuid: Optional[str] = None,
        _timeout: float = TIMEOUT_FAST,
        bus_id: Optional[str] = None,
        ctx: Context = None,
    ) -> str:
        """List active RNA subscriptions (filtered by owner if given)."""
        return await self._call(
            ctx,
            "msgbus_list_subscriptions",
            {"owner_id": owner_id},
            target_uuid,
            _timeout,
            bus_id=bus_id,
        )

    # ---- Tier 3b: PolyHaven (4 commands, gate: use_polyhaven) ------

    @mcp_tool()
    async def get_polyhaven_categories(
        self,
        asset_type: str,
        target_uuid: Optional[str] = None,
        _timeout: float = DEFAULT_TIMEOUT_S,
        bus_id: Optional[str] = None,
        ctx: Context = None,
    ) -> str:
        """List PolyHaven categories for ``asset_type`` in {hdris, textures, models, all}."""
        return await self._call(
            ctx,
            "get_polyhaven_categories",
            {"asset_type": asset_type},
            target_uuid,
            _timeout,
            bus_id=bus_id,
        )

    @mcp_tool()
    async def search_polyhaven_assets(
        self,
        asset_type: Optional[str] = None,
        categories: Optional[str] = None,
        target_uuid: Optional[str] = None,
        _timeout: float = DEFAULT_TIMEOUT_S,
        bus_id: Optional[str] = None,
        ctx: Context = None,
    ) -> str:
        """Search PolyHaven for assets (response capped at 20 entries by the addon)."""
        return await self._call(
            ctx,
            "search_polyhaven_assets",
            {"asset_type": asset_type, "categories": categories},
            target_uuid,
            _timeout,
            bus_id=bus_id,
        )

    @mcp_tool()
    async def download_polyhaven_asset(
        self,
        asset_id: str,
        asset_type: str,
        resolution: str = "1k",
        file_format: Optional[str] = None,
        target_uuid: Optional[str] = None,
        _timeout: float = TIMEOUT_LONG,
        bus_id: Optional[str] = None,
        ctx: Context = None,
    ) -> str:
        """Download + import a PolyHaven asset.

        ``asset_type`` in {hdris, textures, models}. Models prefer glTF;
        HDRIs default to .hdr; textures default to .jpg. HDRIs replace
        the active world; textures create a new material; models are
        appended to the active scene.
        """
        return await self._call(
            ctx,
            "download_polyhaven_asset",
            {
                "asset_id": asset_id,
                "asset_type": asset_type,
                "resolution": resolution,
                "file_format": file_format,
            },
            target_uuid,
            _timeout,
            bus_id=bus_id,
        )

    @mcp_tool()
    async def set_texture(
        self,
        object_name: str,
        texture_id: str,
        target_uuid: Optional[str] = None,
        _timeout: float = DEFAULT_TIMEOUT_S,
        bus_id: Optional[str] = None,
        ctx: Context = None,
    ) -> str:
        """Apply a previously downloaded PolyHaven texture to ``object_name``.

        Builds a fresh Principled-BSDF material; handles ARM packing and
        AO multiplication into base color.
        """
        return await self._call(
            ctx,
            "set_texture",
            {"object_name": object_name, "texture_id": texture_id},
            target_uuid,
            _timeout,
            bus_id=bus_id,
        )

    # ---- Tier 3c: Hyper3D Rodin (3 commands, gate: use_hyper3d) ----

    @mcp_tool()
    async def create_rodin_job(
        self,
        text_prompt: Optional[str] = None,
        images: Optional[list] = None,
        bbox_condition: Optional[Any] = None,
        target_uuid: Optional[str] = None,
        _timeout: float = TIMEOUT_MEDIUM,
        bus_id: Optional[str] = None,
        ctx: Context = None,
    ) -> str:
        """Start a Rodin 3D-from-text/image job. Backend is mode-dependent (MAIN_SITE / FAL_AI).

        ``images`` shape varies by backend: MAIN_SITE wants
        ``[[suffix, base64_data], ...]``; FAL_AI wants ``[url, ...]``.
        Returns the provider's raw response (subscription_key / request_id).
        """
        return await self._call(
            ctx,
            "create_rodin_job",
            {
                "text_prompt": text_prompt,
                "images": images,
                "bbox_condition": bbox_condition,
            },
            target_uuid,
            _timeout,
            bus_id=bus_id,
        )

    @mcp_tool()
    async def poll_rodin_job_status(
        self,
        subscription_key: Optional[str] = None,
        request_id: Optional[str] = None,
        target_uuid: Optional[str] = None,
        _timeout: float = TIMEOUT_FAST,
        bus_id: Optional[str] = None,
        ctx: Context = None,
    ) -> str:
        """Poll a Rodin job. Pass ``subscription_key`` for MAIN_SITE, ``request_id`` for FAL_AI."""
        return await self._call(
            ctx,
            "poll_rodin_job_status",
            {"subscription_key": subscription_key, "request_id": request_id},
            target_uuid,
            _timeout,
            bus_id=bus_id,
        )

    @mcp_tool()
    async def import_generated_asset(
        self,
        name: str,
        task_uuid: Optional[str] = None,
        request_id: Optional[str] = None,
        target_uuid: Optional[str] = None,
        _timeout: float = TIMEOUT_LONG,
        bus_id: Optional[str] = None,
        ctx: Context = None,
    ) -> str:
        """Import a completed Rodin GLB into Blender as ``name``.

        Pass ``task_uuid`` for MAIN_SITE, ``request_id`` for FAL_AI.
        Returns the imported object's name + transform + bounding box.
        """
        return await self._call(
            ctx,
            "import_generated_asset",
            {"task_uuid": task_uuid, "request_id": request_id, "name": name},
            target_uuid,
            _timeout,
            bus_id=bus_id,
        )

    # ---- Tier 3d: Sketchfab (2 commands, gate: use_sketchfab) ------

    @mcp_tool()
    async def search_sketchfab_models(
        self,
        query: str,
        categories: Optional[str] = None,
        count: int = 20,
        downloadable: bool = True,
        target_uuid: Optional[str] = None,
        _timeout: float = DEFAULT_TIMEOUT_S,
        bus_id: Optional[str] = None,
        ctx: Context = None,
    ) -> str:
        """Search Sketchfab models. Returns the raw Sketchfab v3 search response."""
        return await self._call(
            ctx,
            "search_sketchfab_models",
            {
                "query": query,
                "categories": categories,
                "count": count,
                "downloadable": downloadable,
            },
            target_uuid,
            _timeout,
            bus_id=bus_id,
        )

    @mcp_tool()
    async def download_sketchfab_model(
        self,
        uid: str,
        target_uuid: Optional[str] = None,
        _timeout: float = TIMEOUT_LONG,
        bus_id: Optional[str] = None,
        ctx: Context = None,
    ) -> str:
        """Download + import a Sketchfab model by its UID (zip-slip protected)."""
        return await self._call(
            ctx,
            "download_sketchfab_model",
            {"uid": uid},
            target_uuid,
            _timeout,
            bus_id=bus_id,
        )

    # ---- Resources (live, dispatch-backed) -------------------------
    # MCP clients render these as attachable, re-fetchable state snapshots
    # rather than tool invocations. Each one shares the dispatch round-trip
    # with the analogous tool above — same JSON shape, same auth/no_client/
    # ambiguous_target semantics.

    @mcp_resource(uri="blender://scene/info")
    async def scene_info_resource(self, ctx: Context = None) -> str:
        """JSON snapshot of the active scene (round-trip ``get_scene_info``)."""
        return await self._call(ctx, "get_scene_info", {}, None, DEFAULT_TIMEOUT_S)

    @mcp_resource(uri="blender://scene/objects")
    async def scene_objects_resource(self, ctx: Context = None) -> str:
        """First page of objects in ``bpy.data.objects`` (round-trip ``browse_data``)."""
        return await self._call(
            ctx,
            "browse_data",
            {
                "collection": "objects",
                "page": 1,
                "page_size": 50,
                "detail_level": "summary",
            },
            None,
            DEFAULT_TIMEOUT_S,
        )

    # Templated resources are NOT decorated with @mcp_resource — that path
    # (fastmcp.contrib.mcp_mixin.register_resources -> Resource.from_function)
    # skips the "{" in uri template-detection check that the standalone
    # @mcp.resource(...) decorator does, so braces get URL-encoded by pydantic
    # AnyUrl validation and the URI ends up registered as a static URI literal
    # named ``blender://console/%7Blevel%7D``. Instead we expose them as plain
    # methods and wire them via :meth:`register_templated_resources` at
    # server-build time, which uses ``mcp.resource(uri)(method)`` — that path
    # runs the template detection correctly.

    async def console_resource(self, level: str, ctx: Context = None) -> str:
        """Page 1 of Blender console (``level`` in {all, info, warning, error, output})."""
        return await self._call(
            ctx,
            "get_console_output",
            {"level": level, "page": 1, "page_size": 50},
            None,
            DEFAULT_TIMEOUT_S,
        )

    async def console_paged_resource(
        self, level: str, page: int, ctx: Context = None
    ) -> str:
        """Paginated Blender console scrape (``level`` in {all, info, warning, error, output})."""
        return await self._call(
            ctx,
            "get_console_output",
            {"level": level, "page": page, "page_size": 50},
            None,
            DEFAULT_TIMEOUT_S,
        )

    def register_templated_resources(self, mcp_server) -> None:
        """Register URI-template resources via ``mcp.resource(uri)(method)``.

        Workaround for the ``mcp_resource`` decorator's latent bug — see the
        block comment above the methods for details.
        """
        mcp_server.resource(uri="blender://console/{level}")(self.console_resource)
        mcp_server.resource(uri="blender://console/{level}/{page}")(
            self.console_paged_resource
        )
