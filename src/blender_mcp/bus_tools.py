"""MCP tools exposing the message bus.

User identity flows in via the `current_user_id` ContextVar set by the
JWT middleware in oauth_server.py. Each tool also stamps the caller's
MCP session into the ClientInfo so the forwarding handler can address it.
"""

import json
from contextvars import ContextVar
from typing import Any, Optional

from fastmcp import Context
from fastmcp.contrib.mcp_mixin import MCPMixin, mcp_tool, mcp_resource, mcp_prompt
from fastmcp.prompts.base import Message

from .client_role import require_role
from .job_waiter import job_waiter
from .message_bus import bus_manager, ClientInfo
from .message_router import Priority, parse_priority


# Populated by the JWT middleware in oauth_server.py before tool dispatch.
current_user_id: ContextVar[Optional[str]] = ContextVar("current_user_id", default=None)
# Tracks job_id -> originating client uuid so job_update can route replies back.
_pending_jobs: dict[str, tuple[str, str]] = {}  # job_id -> (user_id, originator_uuid)


def _resolve_user_id(ctx: Optional[Context]) -> Optional[str]:
    """Resolve the authenticated user_id from whichever auth path is active.

    Phase G priority order:
      1. ContextVar (set by legacy jwt_middleware — still works for any
         pre-G3 paths that haven't migrated yet)
      2. Legacy request.state.user_id (same)
      3. FastMCP's get_access_token() — set by the new auth pipeline.
         Resolves the bearer to a user_id via the provider:
         - BlenderMCPOAuthProvider: look up _token_to_user
         - OIDCProxy: decode the JWT and read 'sub' claim
    """
    uid = current_user_id.get()
    if uid:
        return uid

    if ctx is not None:
        try:
            req = ctx.get_http_request()
            if req and getattr(req.state, "user_id", None):
                return req.state.user_id
        except Exception:
            pass

    # FastMCP auth pipeline path
    try:
        from fastmcp.server.dependencies import get_access_token

        from .mcp_oauth_provider import BlenderMCPOAuthProvider
        from .server_proper import mcp

        access = get_access_token()
        if access is None:
            return None

        provider = mcp.auth
        # In-memory provider path: token→user mapping
        if isinstance(provider, BlenderMCPOAuthProvider):
            return provider.get_user_for_token(access.token)

        # OIDCProxy path: decode the JWT, read 'sub' (Authentik's hashed_user_id)
        try:
            import jwt as _jwt
            claims = _jwt.decode(
                access.token,
                options={"verify_signature": False, "verify_aud": False},
            )
            return claims.get("sub") or claims.get("preferred_username")
        except Exception:
            return None
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
    @require_role("addon")
    async def register_client(
        self,
        client_uuid: str,
        client_type: str,
        is_persistent: bool = False,
        capabilities: Optional[list[str]] = None,
        group_id: Optional[str] = None,
        ctx: Context = None,
    ) -> str:
        """Join the caller's user bus. Returns JSON {status, client}.

        Gated to ``addon`` role (phase H): only the BlenderMCP addon should
        register as a bus participant. LLM clients drive the addon via
        dispatch tools instead of registering themselves.
        """
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
    @require_role("addon")
    async def unregister_client(self, client_uuid: str, ctx: Context = None) -> str:
        """Leave the user bus. Gated to ``addon`` role (phase H)."""
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
    @require_role("addon")
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
        # Wake any awaiter registered through the dispatch_component layer.
        # No-op if the job came from old-style send_message + listen-pattern
        # callers (no Future was ever registered for it).
        job_waiter.deliver(user_id, job_id, status, result, error)
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

    @mcp_resource(uri="blender://bus/clients")
    async def clients_resource(self, ctx: Context = None) -> str:
        """JSON snapshot of the caller's bus: persistent + ephemeral clients."""
        user_id = _resolve_user_id(ctx)
        if not user_id:
            return json.dumps({"status": "error", "error": "unauthenticated"})
        bus = bus_manager.get_bus(user_id)
        return json.dumps({
            "user_id": user_id,
            "persistent": [c.to_dict() for c in bus.persistent_clients.values()],
            "ephemeral": [c.to_dict() for c in bus.ephemeral_clients.values()],
        })

    @mcp_resource(uri="blender://bus/stats")
    async def stats_resource(self, ctx: Context = None) -> str:
        """JSON counts for the caller's bus."""
        user_id = _resolve_user_id(ctx)
        if not user_id:
            return json.dumps({"status": "error", "error": "unauthenticated"})
        bus = bus_manager.get_bus(user_id)
        pending = sum(1 for owner, _ in _pending_jobs.values() if owner == user_id)
        return json.dumps({
            "user_id": user_id,
            "persistent_count": len(bus.persistent_clients),
            "ephemeral_count": len(bus.ephemeral_clients),
            "pending_jobs_for_user": pending,
        })

    @mcp_prompt()
    def dispatch_script(
        self,
        target: str,
        script: str,
        priority: str = "info",
        description: Optional[str] = None,
    ) -> list[Message]:
        """Render a blender_send_message call template for a Blender job_dispatch."""
        target_uuid_line = "<omit>"
        group_id_line = "<omit>"
        client_type_line = "<omit>"

        if target.startswith("uuid:"):
            target_uuid_line = target[len("uuid:"):]
        elif target.startswith("group:"):
            group_id_line = target[len("group:"):]
        elif target.startswith("type:"):
            client_type_line = target[len("type:"):]
        elif target == "broadcast":
            pass
        else:
            # Unknown prefix — surface it as a hint to the LLM.
            target_uuid_line = f"<invalid target spec: {target!r}; use uuid:/group:/type:/broadcast>"

        desc_line = description if description else "<optional human description>"

        payload_block = (
            "{\n"
            '  "message_type": "job_dispatch",\n'
            '  "job_id": "<generate a uuid>",\n'
            f'  "script": {json.dumps(script)},\n'
            f'  "description": {json.dumps(desc_line)}\n'
            "}"
        )

        text = (
            "To execute this script in Blender, call blender_send_message with:\n\n"
            f"target_uuid:  {target_uuid_line}\n"
            f"group_id:     {group_id_line}\n"
            f"client_type:  {client_type_line}\n"
            f"priority:     {priority}\n"
            f"payload:      {payload_block}\n\n"
            "Routing precedence is target_uuid > group_id > client_type > broadcast.\n"
            "Set exactly one of target_uuid / group_id / client_type (or none for broadcast).\n"
            "The job_id inside payload MUST be a fresh UUID; the addon echoes it on completion."
        )

        return [Message(text, role="user")]
