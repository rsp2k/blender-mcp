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


async def resolve_bus(
    user_id: str,
    bus_id_str: Optional[str] = None,
) -> dict:
    """Resolve a (user, bus_id_or_None) request into a live MessageBus.

    Three-step gate used by every tool that needs to operate on a bus:

    1. If ``bus_id_str`` is None → ensure the user's personal bus
       exists in DB → return its in-memory MessageBus.
    2. If ``bus_id_str`` is set → parse to UUID → verify the bus
       exists and the user is an active member → return its in-memory
       MessageBus.
    3. On any failure return a dict the tool can ``json.dumps`` straight
       to the wire: ``{"status": "error", "error": <kind>, ...}``.

    Success shape: ``{"ok": True, "bus": MessageBus, "bus_id": UUID,
    "name": str}``. Failure shape: ``{"ok": False, "status": "error",
    "error": <kind>}``.
    """
    import uuid as _u

    from .message_bus import bus_manager
    from .storage import bus_repo, get_session

    if bus_id_str is None:
        async with get_session() as s:
            bus_row = await bus_repo.ensure_personal_bus(s, user_id)
    else:
        try:
            bus_uuid = _u.UUID(bus_id_str)
        except (ValueError, AttributeError):
            return {"ok": False, "status": "error", "error": "invalid_bus_id"}
        async with get_session() as s:
            bus_row = await bus_repo.get_bus(s, bus_uuid)
            if bus_row is None:
                return {"ok": False, "status": "error", "error": "bus_not_found"}
            if not await bus_repo.is_member(s, bus_uuid, user_id):
                return {"ok": False, "status": "error", "error": "not_a_member"}

    mb = bus_manager.get_or_create(bus_row.bus_id, name=bus_row.name)
    return {"ok": True, "bus": mb, "bus_id": bus_row.bus_id, "name": bus_row.name}


class BlenderBusComponent(MCPMixin):
    """Five-tool message-bus surface."""

    @mcp_tool()
    @require_role("addon")
    async def register_client(
        self,
        client_uuid: str,
        client_type: str,
        label: Optional[str] = None,
        bus_id: Optional[str] = None,
        is_persistent: bool = False,
        capabilities: Optional[list[str]] = None,
        group_id: Optional[str] = None,
        ctx: Context = None,
    ) -> str:
        """Join the caller's user bus. Returns JSON {status, client}.

        Args:
            client_uuid: Stable UUID for THIS client instance (the addon
                generates a sticky UUID per Blender install).
            client_type: ``"blender"`` for the addon; future expansion
                ``"llm"`` for LLM sessions that opt in to be visible.
            label: Human-readable identity for multi-instance
                disambiguation (e.g. ``"Blender 5.1 on rpm-bullet"``).
                Optional — if omitted on re-registration, the prior
                label is preserved; if omitted on first registration,
                the client appears with just its uuid.
            is_persistent, capabilities, group_id: as before.

        Gated to ``addon`` role (phase H): only the BlenderMCP addon should
        register as a bus participant. LLM clients drive the addon via
        dispatch tools instead of registering themselves.
        """
        user_id = _resolve_user_id(ctx)
        if not user_id:
            return json.dumps({"status": "error", "error": "unauthenticated"})

        resolved = await resolve_bus(user_id, bus_id)
        if not resolved["ok"]:
            return json.dumps(resolved)

        info = ClientInfo(
            uuid=client_uuid,
            client_type=client_type,
            label=label,
            is_persistent=bool(is_persistent),
            capabilities=list(capabilities or []),
            group_id=group_id,
            session=_session_from_ctx(ctx),
        )
        registered = resolved["bus"].register(info)
        return json.dumps({
            "status": "ok",
            "bus_id": str(resolved["bus_id"]),
            "client": registered.to_dict(),
        })

    @mcp_tool()
    @require_role("addon")
    async def unregister_client(
        self,
        client_uuid: str,
        bus_id: Optional[str] = None,
        ctx: Context = None,
    ) -> str:
        """Leave the bus. Gated to ``addon`` role (phase H)."""
        user_id = _resolve_user_id(ctx)
        if not user_id:
            return json.dumps({"status": "error", "error": "unauthenticated"})
        resolved = await resolve_bus(user_id, bus_id)
        if not resolved["ok"]:
            return json.dumps(resolved)
        ok = resolved["bus"].unregister(client_uuid)
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
        bus_id: Optional[str] = None,
        ctx: Context = None,
    ) -> str:
        """Route a message on a bus. Mode picked by which targeting arg is set.
        Precedence: target_uuid > group_id > client_type > broadcast.

        ``bus_id`` defaults to the caller's personal bus.
        """
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

        resolved = await resolve_bus(user_id, bus_id)
        if not resolved["ok"]:
            return json.dumps(resolved)
        bus = resolved["bus"]
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
            # Track (bus_id, originator_uuid) per job so job_update can route
            # the reply back to the right bus + sender across shared buses.
            _pending_jobs[tracking_id] = (str(resolved["bus_id"]), origin)

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
        """Client -> server reply. Routed back to the originator via the bus.

        Bus is inferred from ``_pending_jobs[job_id]`` (which holds the
        bus_id the original send_message used). If no entry exists, falls
        back to broadcasting on the addon's PERSONAL bus — preserves
        legacy "no-context" behavior for any pre-Phase-I clients.
        """
        user_id = _resolve_user_id(ctx)
        if not user_id:
            return json.dumps({"status": "error", "error": "unauthenticated"})

        update_payload = {
            "kind": "job_update",
            "job_id": job_id,
            "status": status,
            "result": result,
            "error": error,
        }

        entry = _pending_jobs.get(job_id)
        if entry is None:
            # No originator known — broadcast on the addon's personal bus.
            resolved = await resolve_bus(user_id, None)
            if not resolved["ok"]:
                return json.dumps(resolved)
            bus = resolved["bus"]
            r = bus.route(update_payload, from_uuid=f"server:{user_id}",
                          routing={"type": "broadcast"}, priority=Priority.NOTICE)
            return json.dumps({"status": "ok", "delivered": "broadcast", "targets": r.targets})

        bus_id_str, originator_uuid = entry
        # The dispatching bus must be one the responding addon is a member
        # of — otherwise this is a cross-bus job_update attempt.
        resolved = await resolve_bus(user_id, bus_id_str)
        if not resolved["ok"]:
            return json.dumps({"status": "error", "error": "cross_bus_job_update"})
        bus = resolved["bus"]

        r = bus.route(
            update_payload,
            from_uuid=f"server:{user_id}",
            routing={"type": "direct", "target_uuid": originator_uuid},
            priority=Priority.NOTICE,
            job_id=job_id,
        )
        # Wake any awaiter registered through the dispatch_component layer.
        # job_waiter keys by (bus_id_str, job_id) — see I5 dispatch refactor.
        job_waiter.deliver(bus_id_str, job_id, status, result, error)
        # Terminal states clean up the tracking entry.
        if status in {"completed", "failed", "cancelled"}:
            _pending_jobs.pop(job_id, None)
        return json.dumps({"status": "ok", "delivered": "direct", "targets": r.targets})

    @mcp_tool()
    async def list_available_clients(
        self,
        bus_id: Optional[str] = None,
        ctx: Context = None,
    ) -> str:
        """List persistent + ephemeral clients on the given bus.

        ``bus_id`` defaults to the caller's personal bus. Returns
        ``not_a_member`` if the caller isn't a member of the bus.
        """
        user_id = _resolve_user_id(ctx)
        if not user_id:
            return json.dumps({"status": "error", "error": "unauthenticated"})
        resolved = await resolve_bus(user_id, bus_id)
        if not resolved["ok"]:
            return json.dumps(resolved)
        bus = resolved["bus"]
        return json.dumps({
            "status": "ok",
            "user_id": user_id,
            "persistent": [c.to_dict() for c in bus.persistent_clients.values()],
            "ephemeral": [c.to_dict() for c in bus.ephemeral_clients.values()],
        })

    @mcp_resource(uri="blender://bus/clients")
    async def clients_resource(self, ctx: Context = None) -> str:
        """JSON snapshot of the caller's PERSONAL bus.

        MCP resources can't take dynamic args, so this resource always
        targets the personal bus. For shared-bus listings use the
        ``bus_list_available_clients(bus_id=...)`` tool.
        """
        user_id = _resolve_user_id(ctx)
        if not user_id:
            return json.dumps({"status": "error", "error": "unauthenticated"})
        resolved = await resolve_bus(user_id, None)
        if not resolved["ok"]:
            return json.dumps(resolved)
        bus = resolved["bus"]
        return json.dumps({
            "bus_id": str(resolved["bus_id"]),
            "user_id": user_id,
            "persistent": [c.to_dict() for c in bus.persistent_clients.values()],
            "ephemeral": [c.to_dict() for c in bus.ephemeral_clients.values()],
        })

    @mcp_resource(uri="blender://bus/stats")
    async def stats_resource(self, ctx: Context = None) -> str:
        """JSON counts for the caller's personal bus."""
        user_id = _resolve_user_id(ctx)
        if not user_id:
            return json.dumps({"status": "error", "error": "unauthenticated"})
        resolved = await resolve_bus(user_id, None)
        if not resolved["ok"]:
            return json.dumps(resolved)
        bus = resolved["bus"]
        bid_str = str(resolved["bus_id"])
        pending = sum(1 for bid, _ in _pending_jobs.values() if bid == bid_str)
        return json.dumps({
            "bus_id": bid_str,
            "user_id": user_id,
            "persistent_count": len(bus.persistent_clients),
            "ephemeral_count": len(bus.ephemeral_clients),
            "pending_jobs_for_bus": pending,
        })

    # ---- Phase I3: bus membership management ------------------------------

    @mcp_tool()
    async def list_buses(self, ctx: Context = None) -> str:
        """List every bus the caller is a member of.

        Returns ``{"status": "ok", "buses": [{bus_id, name, role,
        is_personal, owner_user_id, created_at}, ...]}``. Personal bus
        auto-provisions on first call so every authenticated user sees
        at least one entry.

        Note: ``role`` is the CALLER'S role on each bus (owner /
        member / guest). The bus itself has a fixed ``owner_user_id``
        but each member sees their own role.
        """
        user_id = _resolve_user_id(ctx)
        if not user_id:
            return json.dumps({"status": "error", "error": "unauthenticated"})

        from .storage import bus_repo, get_session
        async with get_session() as s:
            # Auto-provision personal bus on first contact.
            await bus_repo.ensure_personal_bus(s, user_id)
        async with get_session() as s:
            rows = await bus_repo.list_buses_for_user(s, user_id)

        return json.dumps({
            "status": "ok",
            "user_id": user_id,
            "buses": [
                {
                    "bus_id": str(bus.bus_id),
                    "name": bus.name,
                    "description": bus.description,
                    "role": role.value,
                    "is_personal": bus.is_personal,
                    "owner_user_id": bus.owner_user_id,
                    "is_owned_by_me": bus.owner_user_id == user_id,
                    "created_at": bus.created_at.isoformat(),
                }
                for bus, role in rows
            ],
        })

    @mcp_tool()
    async def create_bus(
        self,
        name: str,
        description: str = "",
        ctx: Context = None,
    ) -> str:
        """Create a new shared bus owned by the caller. Returns the new bus_id.

        ``name`` is required (max 128 chars). The caller becomes the
        sole initial member with role ``owner``. To add others, call
        ``bus_invite_user(bus_id=...)`` and share the returned code
        out-of-band.

        For the user's private workspace, no need to create anything —
        a personal bus auto-provisions on first ``list_buses`` call.
        """
        user_id = _resolve_user_id(ctx)
        if not user_id:
            return json.dumps({"status": "error", "error": "unauthenticated"})
        name = (name or "").strip()
        if not name:
            return json.dumps({"status": "error", "error": "name_required"})
        if len(name) > 128:
            return json.dumps({"status": "error", "error": "name_too_long", "max": 128})

        from .storage import bus_repo, get_session
        async with get_session() as s:
            bus = await bus_repo.create_shared_bus(
                s, owner_user_id=user_id, name=name, description=description
            )
        return json.dumps({
            "status": "ok",
            "bus_id": str(bus.bus_id),
            "name": bus.name,
            "description": bus.description,
        })

    # ---- Phase I4: invitations + membership ops ---------------------------

    @mcp_tool()
    async def invite_user(
        self,
        bus_id: str,
        role: str = "member",
        ctx: Context = None,
    ) -> str:
        """Issue a single-use invitation code for ``bus_id``.

        Caller must be a member (any role) of the bus. Returns the code
        as ``{"status": "ok", "code": "BMI-XXXXXXXXXX", "expires_at":
        "...", "role": "member"}``. Share the code out-of-band; recipient
        calls ``bus_join(code)``.

        Personal buses cannot be invited to — they're permanent
        single-member buses by definition. Returns ``cannot_invite_to_personal``
        if you try.

        ``role`` is the role the joiner will get; valid: ``member``
        (default) or ``guest`` (read-only). Cannot invite as ``owner``
        — bus owners are immutable.
        """
        import uuid as _u
        from .storage import BusRole, bus_repo, get_session

        user_id = _resolve_user_id(ctx)
        if not user_id:
            return json.dumps({"status": "error", "error": "unauthenticated"})
        try:
            bus_uuid = _u.UUID(bus_id)
        except ValueError:
            return json.dumps({"status": "error", "error": "invalid_bus_id"})
        if role not in ("member", "guest"):
            return json.dumps({"status": "error", "error": "invalid_role",
                               "allowed": ["member", "guest"]})

        async with get_session() as s:
            bus = await bus_repo.get_bus(s, bus_uuid)
            if bus is None:
                return json.dumps({"status": "error", "error": "bus_not_found"})
            if bus.is_personal:
                return json.dumps({"status": "error", "error": "cannot_invite_to_personal"})
            membership = await bus_repo.get_membership(s, bus_uuid, user_id)
            if membership is None:
                return json.dumps({"status": "error", "error": "not_a_member"})
            inv = await bus_repo.create_invitation(
                s, bus_id=bus_uuid, invited_by=user_id, role=BusRole(role),
            )

        return json.dumps({
            "status": "ok",
            "code": inv.code,
            "bus_id": str(inv.bus_id),
            "role": inv.role.value,
            "expires_at": inv.expires_at.isoformat(),
        })

    @mcp_tool()
    async def join_bus(self, code: str, ctx: Context = None) -> str:
        """Accept an invitation code. Returns the joined bus_id + name.

        On success the caller becomes a member of the bus with the
        role the invitation specified. The code is single-use — second
        claim returns ``already_consumed``.

        Failure statuses: ``not_found``, ``expired``, ``already_consumed``,
        ``wrong_invitee`` (if the invitation was targeted at a specific
        user_id and you're not them), ``already_member`` (you were
        already a member; the invitation still gets consumed to prevent
        replay).
        """
        from .storage import bus_repo, get_session

        user_id = _resolve_user_id(ctx)
        if not user_id:
            return json.dumps({"status": "error", "error": "unauthenticated"})
        code = (code or "").strip().upper()
        if not code:
            return json.dumps({"status": "error", "error": "code_required"})

        async with get_session() as s:
            bus, status = await bus_repo.consume_invitation(
                s, code=code, joining_user_id=user_id,
            )

        if bus is None:
            return json.dumps({"status": status})
        return json.dumps({
            "status": status,
            "bus_id": str(bus.bus_id),
            "name": bus.name,
            "description": bus.description,
        })

    @mcp_tool()
    async def leave_bus(self, bus_id: str, ctx: Context = None) -> str:
        """Leave a bus you're a member of.

        Personal buses cannot be left (returns ``cannot_leave_personal``).
        Bus owners cannot leave their own bus — they'd be orphaning it
        (returns ``cannot_leave_as_owner``); to retire a bus, use
        ``bus_revoke_bus`` (not yet implemented — future phase).
        """
        import uuid as _u
        from .storage import bus_repo, get_session

        user_id = _resolve_user_id(ctx)
        if not user_id:
            return json.dumps({"status": "error", "error": "unauthenticated"})
        try:
            bus_uuid = _u.UUID(bus_id)
        except ValueError:
            return json.dumps({"status": "error", "error": "invalid_bus_id"})

        async with get_session() as s:
            bus = await bus_repo.get_bus(s, bus_uuid)
            if bus is None:
                return json.dumps({"status": "error", "error": "bus_not_found"})
            if bus.is_personal:
                return json.dumps({"status": "error", "error": "cannot_leave_personal"})
            if bus.owner_user_id == user_id:
                return json.dumps({"status": "error", "error": "cannot_leave_as_owner"})
            ok = await bus_repo.revoke_member(s, bus_id=bus_uuid, user_id=user_id)
        if not ok:
            return json.dumps({"status": "error", "error": "not_a_member"})
        return json.dumps({"status": "ok", "bus_id": str(bus_uuid)})

    @mcp_tool()
    async def revoke_member(
        self,
        bus_id: str,
        target_user_id: str,
        ctx: Context = None,
    ) -> str:
        """Kick a member off a bus (owner-only).

        Owners can revoke any non-owner member. Owners cannot revoke
        themselves (returns ``cannot_revoke_owner``). Use to clean up
        memberships from shared invitation codes that landed with
        unintended recipients.
        """
        import uuid as _u
        from .storage import bus_repo, get_session

        user_id = _resolve_user_id(ctx)
        if not user_id:
            return json.dumps({"status": "error", "error": "unauthenticated"})
        try:
            bus_uuid = _u.UUID(bus_id)
        except ValueError:
            return json.dumps({"status": "error", "error": "invalid_bus_id"})
        if target_user_id == user_id:
            return json.dumps({"status": "error", "error": "cannot_revoke_owner"})

        async with get_session() as s:
            bus = await bus_repo.get_bus(s, bus_uuid)
            if bus is None:
                return json.dumps({"status": "error", "error": "bus_not_found"})
            if bus.owner_user_id != user_id:
                return json.dumps({"status": "error", "error": "not_owner"})
            ok = await bus_repo.revoke_member(s, bus_id=bus_uuid, user_id=target_user_id)
        if not ok:
            return json.dumps({"status": "error", "error": "not_a_member"})
        return json.dumps({"status": "ok"})

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
