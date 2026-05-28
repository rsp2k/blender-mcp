"""Per-user in-memory client registry + dispatcher.

Dispatch happens by writing structured records to the `_message_bus` logger.
A forwarding handler in oauth_server.py turns each record into an MCP
log notification routed to subscribed clients on the right user bus.
"""

import logging
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

from .message_router import (
    _message_bus,
    Priority,
    PRIORITY_TO_PY_LEVEL,
)

logger = logging.getLogger(__name__)


@dataclass
class ClientInfo:
    """One client on a user's bus."""
    uuid: str
    client_type: str  # "blender" | "llm" | other
    # Human-readable identity for multi-instance disambiguation.
    # The addon defaults this to e.g. "Blender 5.1 on rpm-bullet"; an
    # LLM session can register a label like "Claude Code · Ryan's
    # terminal". Falls back to the uuid in displays when None.
    label: Optional[str] = None
    is_persistent: bool = False
    capabilities: list[str] = field(default_factory=list)
    group_id: Optional[str] = None
    connected_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    # Session handle so the forwarding handler can address THIS client.
    # Set by bus_tools when a tool call comes in from the client.
    session: Any = None

    def to_dict(self) -> dict[str, Any]:
        # Build manually — asdict() deep-copies all fields including `session`,
        # which holds an MCP ServerSession with asyncio.Future objects that
        # cannot be pickled/deepcopied.
        return {
            "uuid": self.uuid,
            "client_type": self.client_type,
            "label": self.label,
            "is_persistent": self.is_persistent,
            "capabilities": list(self.capabilities),
            "group_id": self.group_id,
            "connected_at": self.connected_at,
            "last_seen": self.last_seen,
        }


@dataclass
class RouteResult:
    """Result of a route() call."""
    message_id: str
    targets: list[str]
    routing: dict[str, Any]


class MessageBus:
    """Per-bus client registry. Routing emits log records, not direct sends.

    Phase I rename + re-key: was ``UserMessageBus`` keyed by ``user_id``.
    Now keyed by ``bus_id`` (UUID from the DB) so the same bus can have
    multiple users as members. The user identity for any given
    *registration* still lives in ``ClientInfo`` and on the bearer
    token; the bus itself is just a routing namespace.
    """

    def __init__(self, bus_id, name: str = ""):
        # bus_id is a uuid.UUID; stored verbatim so log lines + record_extra
        # carry the UUID type (callers can str() at the edge).
        self.bus_id = bus_id
        self.name = name
        self.persistent_clients: dict[str, ClientInfo] = {}
        self.ephemeral_clients: dict[str, ClientInfo] = {}
        self.created_at = time.time()
        self.last_activity = time.time()

    # ---- registry ----

    def register(self, client_info: ClientInfo) -> ClientInfo:
        bucket = self.persistent_clients if client_info.is_persistent else self.ephemeral_clients
        # Re-registration updates in place.
        if client_info.uuid in bucket:
            existing = bucket[client_info.uuid]
            existing.client_type = client_info.client_type
            existing.capabilities = client_info.capabilities
            existing.group_id = client_info.group_id
            existing.last_seen = time.time()
            # Only overwrite label if a new one is supplied — None means
            # "keep what's there." That way the addon can re-register on
            # reconnect without having to recompute its label every time.
            if client_info.label is not None:
                existing.label = client_info.label
            if client_info.session is not None:
                existing.session = client_info.session
            self.last_activity = time.time()
            return existing
        bucket[client_info.uuid] = client_info
        self.last_activity = time.time()
        logger.info(
            "Registered client %s (%s) on bus %s",
            client_info.uuid, client_info.client_type, self.bus_id,
        )
        return client_info

    def unregister(self, client_uuid: str) -> bool:
        removed = self.persistent_clients.pop(client_uuid, None) or self.ephemeral_clients.pop(client_uuid, None)
        if removed:
            self.last_activity = time.time()
            logger.info("Unregistered client %s on bus %s", client_uuid, self.bus_id)
        return removed is not None

    def get(self, client_uuid: str) -> Optional[ClientInfo]:
        return self.persistent_clients.get(client_uuid) or self.ephemeral_clients.get(client_uuid)

    def all_clients(self) -> list[ClientInfo]:
        return list(self.persistent_clients.values()) + list(self.ephemeral_clients.values())

    def touch(self, client_uuid: str) -> None:
        c = self.get(client_uuid)
        if c:
            c.last_seen = time.time()

    # ---- routing ----

    def _resolve_targets(self, routing: dict[str, Any], from_uuid: str) -> list[ClientInfo]:
        mode = routing.get("type", "broadcast")
        clients = self.all_clients()

        if mode == "direct":
            target = routing.get("target_uuid")
            c = self.get(target) if target else None
            return [c] if c else []

        if mode == "group":
            gid = routing.get("group_id")
            return [c for c in clients if c.group_id == gid]

        if mode == "type_filter":
            ct = routing.get("client_type")
            return [c for c in clients if c.client_type == ct]

        # broadcast — exclude sender
        return [c for c in clients if c.uuid != from_uuid]

    def route(
        self,
        payload: dict[str, Any],
        from_uuid: str,
        routing: dict[str, Any],
        priority: Priority = Priority.INFO,
        job_id: Optional[str] = None,
        message_id: Optional[str] = None,
    ) -> RouteResult:
        """Emit one log record per target. Forwarding handler delivers via MCP."""
        self.last_activity = time.time()
        message_id = message_id or str(uuid.uuid4())
        job_id = job_id or message_id

        targets = self._resolve_targets(routing, from_uuid)
        target_uuids = [c.uuid for c in targets]

        py_level = PRIORITY_TO_PY_LEVEL.get(priority, logging.INFO)

        for client in targets:
            record_extra = {
                # Phase I: was "user_id"; now bus_id (str-coerced for the
                # log subscriber which expects JSON-serializable values).
                "bus_id": str(self.bus_id),
                "from_uuid": from_uuid,
                "target_uuid": client.uuid,
                "target_session": client.session,
                "routing": routing,
                "payload": payload,
                "job_id": job_id,
                "message_id": message_id,
                "priority": int(priority),
                "timestamp": time.time(),
            }
            _message_bus.log(py_level, "bus dispatch", extra={"bus": record_extra})

        return RouteResult(message_id=message_id, targets=target_uuids, routing=routing)


class BusManager:
    """Process-wide in-memory cache of MessageBus instances, keyed by bus_id.

    The DB layer (storage/bus_repo.py) is the source of truth for
    membership + invitations. This cache holds the live client
    registrations and the per-bus message-routing state — none of
    which survive a restart anyway, since sessions die with the
    process. The cache populates lazily on first reference to a
    given bus_id.
    """

    def __init__(self):
        # uuid.UUID → MessageBus. Stored UUID type (not str) so callers
        # get a single source-of-truth identifier.
        self._buses: dict[Any, MessageBus] = {}

    def get_or_create(self, bus_id, name: str = "") -> MessageBus:
        """Return the MessageBus for ``bus_id``, creating in-memory state
        on first reference.

        Caller MUST have already verified that ``bus_id`` is a real DB
        row + that the user is a member (see ``resolve_bus`` in
        bus_tools). This is a pure in-memory accessor — it does no DB
        work.
        """
        bus = self._buses.get(bus_id)
        if bus is None:
            bus = MessageBus(bus_id, name=name)
            self._buses[bus_id] = bus
            logger.info("In-memory bus state created for %s (%r)", bus_id, name)
        return bus

    def remove(self, bus_id) -> None:
        self._buses.pop(bus_id, None)

    def all_buses(self) -> dict[Any, MessageBus]:
        return dict(self._buses)


# Module-level singleton.
bus_manager = BusManager()
