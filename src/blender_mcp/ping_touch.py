"""Monkey-patch mcp.server.lowlevel.server._ping_handler to refresh bus.last_seen.

Why this is here, not in middleware: MCP-SDK PingRequests are handled
at the protocol layer BEFORE FastMCP middleware fires (verified
empirically 2026-05-30 by logging every middleware on_message hit and
observing that PingRequest never appeared). So a heartbeat ping reaches
the SDK's ``_ping_handler``, returns ``EmptyResult``, and never touches
our middleware. Any approach using FastMCP middleware to track
transport-level liveness is structurally blocked.

This module patches ``_ping_handler`` at the source. The SDK's
``Server.__init__`` populates ``self.request_handlers[PingRequest]``
by reading the module-level ``_ping_handler`` reference at construction
time. By replacing the module-level reference BEFORE any Server is
constructed, every Server picks up our wrapped version.

The wrapped handler reads the current session from the SDK's
``request_ctx`` ContextVar (which ``_handle_request`` sets just before
calling the handler), looks up the matching client via
``bus_manager.lookup_session``, touches its ``last_seen``, then
delegates to the original handler's behavior (return ``EmptyResult``).
On any error inside our hook the original ping flow proceeds unchanged
— we never want to break ping handling for a liveness side-effect.

Call ``install_ping_touch()`` once at startup before the FastMCP
server is built. Idempotent.
"""

from __future__ import annotations

from mcp.server.lowlevel import server as _mcp_lowlevel
from mcp.server.lowlevel.server import request_ctx
from mcp.types import PingRequest, ServerResult

_installed = False


def install_ping_touch() -> None:
    """Patch the SDK's _ping_handler. Safe to call multiple times."""
    global _installed
    if _installed:
        return

    original = _mcp_lowlevel._ping_handler

    async def _patched_ping_handler(request: PingRequest) -> ServerResult:
        # Touch bus state for the session this ping belongs to.
        # Imports inside to avoid pulling bus_manager into the SDK
        # module's namespace + to keep startup-time import cost down.
        try:
            from .message_bus import bus_manager

            ctx = request_ctx.get()
            session = ctx.session

            # Fast path: O(1) lookup in the manager's session index.
            indexed = bus_manager.lookup_session(session)
            if indexed is not None:
                bus_id, client_uuid = indexed
                bus = bus_manager.all_buses().get(bus_id)
                if bus is not None:
                    bus.touch(client_uuid)
            else:
                # Fallback iteration — handles the case where
                # register_client and this handler see different session
                # wrapper objects. Self-corrects by indexing.
                for bus in bus_manager.all_buses().values():
                    for client in bus.all_clients():
                        if client.session is session:
                            bus.touch(client.uuid)
                            bus_manager.index_session(
                                session, bus.bus_id, client.uuid,
                            )
                            break
                    else:
                        continue
                    break
        except Exception:
            # Never let a liveness side-effect break ping handling.
            pass

        # Original behavior: return EmptyResult. We call the original in
        # case some other layer also wrapped it before us.
        return await original(request)

    _mcp_lowlevel._ping_handler = _patched_ping_handler
    _installed = True
