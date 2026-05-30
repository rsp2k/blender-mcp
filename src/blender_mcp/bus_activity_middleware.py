"""FastMCP middleware that refreshes bus.last_seen on incoming traffic.

Without this, the bus's ``ClientInfo.last_seen`` only updates on the few
explicit code paths that call ``bus.touch`` or that re-register the
client (``register_client``, ``route`` for dispatch replies). The
addon's transport-level heartbeat (30s ``ping`` per 1.5.10) keeps the
HTTP/SSE connection alive but doesn't trigger any of those code paths,
so a healthy quiet addon's ``last_seen`` slowly aged out — the bus
looked like the client had disconnected when it hadn't.

This middleware fires on every incoming MCP message (ping, tool call,
notification, anything) and touches the corresponding bus client's
``last_seen``. Now the heartbeat actually refreshes liveness.

Lookup is by session identity: each ``ClientInfo`` stores its FastMCP
session reference at register-time, so we iterate registered clients
and match by ``is``-comparison. O(N) where N is total clients across
all buses. With single-digit clients per user, that's nothing.
"""

from __future__ import annotations

from typing import Any

from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext


class BusActivityMiddleware(Middleware):
    """Touch bus.last_seen on every incoming message from a registered client."""

    async def on_message(self, context: MiddlewareContext, call_next: CallNext) -> Any:
        self._touch_for_session(context)
        return await call_next(context)

    @staticmethod
    def _touch_for_session(context: MiddlewareContext) -> None:
        # Defer the import so this module is cheap at app-build time and
        # doesn't drag bus state into FastMCP middleware registration.
        from .message_bus import bus_manager

        # context.fastmcp_context may be None during framework-level events
        # (e.g. server shutdown notifications). No session → nothing to touch.
        if context.fastmcp_context is None:
            return
        session = getattr(context.fastmcp_context, "session", None)
        if session is None:
            return

        for bus in bus_manager.all_buses().values():
            for client in bus.all_clients():
                if client.session is session:
                    bus.touch(client.uuid)
                    return  # one client per session — done
