"""MCP tools exposing the message bus.

User identity flows in via the `current_user_id` ContextVar set by the
JWT middleware in oauth_server.py. Each tool also stamps the caller's
MCP session into the ClientInfo so the forwarding handler can address it.
"""

import json
from contextvars import ContextVar
from typing import Any, Optional

from fastmcp import Context
from fastmcp.contrib.mcp_mixin import MCPMixin, mcp_tool

from .message_bus import bus_manager, ClientInfo
from .message_router import Priority, parse_priority


# Populated by the JWT middleware in oauth_server.py before tool dispatch.
current_user_id: ContextVar[Optional[str]] = ContextVar("current_user_id", default=None)
# Tracks job_id -> originating client uuid so job_update can route replies back.
_pending_jobs: dict[str, tuple[str, str]] = {}  # job_id -> (user_id, originator_uuid)


def _resolve_user_id(ctx: Optional[Context]) -> Optional[str]:
    """Prefer ContextVar (set by middleware); fall back to request state."""
    uid = current_user_id.get()
    if uid:
        return uid
    if ctx is None:
        return None
    try:
        req = ctx.get_http_request()
        return getattr(req.state, "user_id", None) if req else None
    except Exception:
        return None


def _session_from_ctx(ctx: Optional[Context]) -> Any:
    """Pull the MCP session handle so the forwarding handler can deliver to it."""
    if ctx is None:
        return None
    try:
        return ctx.session
    except Exception:
        return None


class BlenderBusComponent(MCPMixin):
    """Five-tool message-bus surface."""

    @mcp_tool()
    async def register_client(
        self,
        client_uuid: str,
        client_type: str,
        is_persistent: bool = False,
        capabilities: Optional[list[str]] = None,
        group_id: Optional[str] = None,
        ctx: Context = None,
    ) -> str:
        """Join the caller's user bus. Returns JSON {status, client}."""
        user_id = _resolve_user_id(ctx)
        if not user_id:
            return json.dumps({"status": "error", "error": "unauthenticated"})

        info = ClientInfo(
            uuid=client_uuid,
            client_type=client_type,
            is_persistent=bool(is_persistent),
            capabilities=list(capabilities or []),
            group_id=group_id,
            session=_session_from_ctx(ctx),
        )
        bus = bus_manager.get_bus(user_id)
        registered = bus.register(info)
        return json.dumps({"status": "ok", "client": registered.to_dict()})

    @mcp_tool()
    async def unregister_client(self, client_uuid: str, ctx: Context = None) -> str:
        """Leave the user bus."""
        user_id = _resolve_user_id(ctx)
        if not user_id:
            return json.dumps({"status": "error", "error": "unauthenticated"})
        bus = bus_manager.get_bus(user_id)
        ok = bus.unregister(client_uuid)
        return json.dumps({"status": "ok" if ok else "not_found", "client_uuid": client_uuid})

    @mcp_tool()
    async def send_message(
        self,
        payload: dict,
        target_uuid: Optional[str] = None,
        group_id: Optional[str] = None,
        client_type: Optional[str] = None,
        priority: str = "info",
        from_uuid: Optional[str] = None,
        ctx: Context = None,
    ) -> str:
        """Route a message. Mode picked by which targeting arg is set.
        Precedence: target_uuid > group_id > client_type > broadcast."""
        user_id = _resolve_user_id(ctx)
        if not user_id:
            return json.dumps({"status": "error", "error": "unauthenticated"})

        if target_uuid:
            routing = {"type": "direct", "target_uuid": target_uuid}
        elif group_id:
            routing = {"type": "group", "group_id": group_id}
        elif client_type:
            routing = {"type": "type_filter", "client_type": client_type}
        else:
            routing = {"type": "broadcast"}

        try:
            prio = parse_priority(priority)
        except ValueError as e:
            return json.dumps({"status": "error", "error": str(e)})

        bus = bus_manager.get_bus(user_id)
        # Use caller's from_uuid if given; otherwise tag as 'server:<user_id>'.
        origin = from_uuid or f"server:{user_id}"

        # Honor client-provided job_id as the canonical correlation key so that
        # the addon's job_update reply (which echoes payload.job_id) finds the
        # right _pending_jobs entry. Falls back to server-generated message_id.
        client_job_id = payload.get("job_id")
        result = bus.route(payload, from_uuid=origin, routing=routing,
                           priority=prio, job_id=client_job_id)
        tracking_id = client_job_id or result.message_id

        if origin and not origin.startswith("server:"):
            _pending_jobs[tracking_id] = (user_id, origin)

        return json.dumps({
            "status": "ok",
            "message_id": result.message_id,
            "job_id": tracking_id,
            "targets": result.targets,
            "routing": result.routing,
        })

    @mcp_tool()
    async def job_update(
        self,
        job_id: str,
        status: str,
        result: str = "",
        error: str = "",
        ctx: Context = None,
    ) -> str:
        """Client -> server reply. Routed back to the originator via the bus."""
        user_id = _resolve_user_id(ctx)
        if not user_id:
            return json.dumps({"status": "error", "error": "unauthenticated"})

        bus = bus_manager.get_bus(user_id)
        update_payload = {
            "kind": "job_update",
            "job_id": job_id,
            "status": status,
            "result": result,
            "error": error,
        }

        # Look up where to send it.
        entry = _pending_jobs.get(job_id)
        if entry is None:
            # No originator known — broadcast on the user's bus.
            r = bus.route(update_payload, from_uuid=f"server:{user_id}",
                          routing={"type": "broadcast"}, priority=Priority.NOTICE)
            return json.dumps({"status": "ok", "delivered": "broadcast", "targets": r.targets})

        owner_user, originator_uuid = entry
        if owner_user != user_id:
            return json.dumps({"status": "error", "error": "cross_user_job_update"})

        r = bus.route(
            update_payload,
            from_uuid=f"server:{user_id}",
            routing={"type": "direct", "target_uuid": originator_uuid},
            priority=Priority.NOTICE,
            job_id=job_id,
        )
        # Terminal states clean up the tracking entry.
        if status in {"completed", "failed", "cancelled"}:
            _pending_jobs.pop(job_id, None)
        return json.dumps({"status": "ok", "delivered": "direct", "targets": r.targets})

    @mcp_tool()
    async def list_available_clients(self, ctx: Context = None) -> str:
        """List persistent + ephemeral clients on the caller's user bus."""
        user_id = _resolve_user_id(ctx)
        if not user_id:
            return json.dumps({"status": "error", "error": "unauthenticated"})
        bus = bus_manager.get_bus(user_id)
        return json.dumps({
            "status": "ok",
            "user_id": user_id,
            "persistent": [c.to_dict() for c in bus.persistent_clients.values()],
            "ephemeral": [c.to_dict() for c in bus.ephemeral_clients.values()],
        })
