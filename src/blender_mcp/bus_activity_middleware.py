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

Lookup is **O(1)** via ``BusManager._session_index`` (a dict keyed by
``id(session)`` populated at register/unregister time). Per-message
overhead is one hash lookup and one float assignment regardless of how
many clients are registered — important at the planned thousands-of-
clients scale where the prior O(N) cross-bus scan would have added
noticeable per-message cost.
"""

from __future__ import annotations

import logging
from typing import Any

from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext

logger = logging.getLogger(__name__)


class BusActivityMiddleware(Middleware):
    """Touch bus.last_seen on every incoming message from a registered client."""

    async def on_message(self, context: MiddlewareContext, call_next: CallNext) -> Any:
        # TEMP DEBUG: log every middleware invocation with message type +
        # session id so we can see what's hitting this layer.
        msg = context.message if hasattr(context, "message") else None
        msg_type = type(msg).__name__ if msg is not None else "<no-message>"
        sess = getattr(context.fastmcp_context, "session", None) if context.fastmcp_context else None
        logger.info(
            "BusActivityMiddleware.on_message msg_type=%s session_id=%s ctx=%s",
            msg_type, id(sess) if sess is not None else "<none>",
            "yes" if context.fastmcp_context is not None else "no",
        )
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

        # Primary path: O(1) lookup in the manager's session index.
        # Returns (bus_id, client_uuid) populated at register/unregister
        # time. Works at thousands-of-clients scale.
        indexed = bus_manager.lookup_session(session)
        if indexed is not None:
            bus_id, client_uuid = indexed
            bus = bus_manager.all_buses().get(bus_id)
            if bus is not None:
                bus.touch(client_uuid)
            return

        # Fallback: O(N) iteration. Fires when register_client stored a
        # different session object than what the middleware sees here
        # (e.g. FastMCP wraps the session differently in tool-call
        # contexts vs middleware contexts). When this path lands, we
        # auto-correct by populating the index for next time.
        for bus in bus_manager.all_buses().values():
            for client in bus.all_clients():
                if client.session is session:
                    bus.touch(client.uuid)
                    bus_manager.index_session(session, bus.bus_id, client.uuid)
                    return
