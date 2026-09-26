"""Per-user in-memory client registry + dispatcher.

Dispatch happens by writing structured records to the `_message_bus` logger.
A forwarding handler in oauth_server.py turns each record into an MCP
log notification routed to subscribed clients on the right user bus.
"""

import logging
import os
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


def _env_seconds(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


# A client whose last_seen is older than this is "stale": hidden from
# list_available_clients by default and skipped by implicit target
# selection, but still routable by explicit uuid. The addon heartbeats
# every 30s, so 180s is six missed beats. Soft rather than hard because
# a Blender running a long GIL-holding operator (exact booleans, big
# bakes) can starve its heartbeat thread for minutes while still alive.
CLIENT_STALE_SECONDS = _env_seconds("BLENDER_MCP_CLIENT_STALE_SECONDS", 180.0)

# Blender clients silent for this long are unregistered outright, which
# is what finally clears dead relaunches out of memory. Only applied to
# client_type == "blender": LLM clients don't heartbeat, and evicting an
# idle LLM would break reply routing to it.
CLIENT_EVICT_SECONDS = _env_seconds("BLENDER_MCP_CLIENT_EVICT_SECONDS", 3600.0)

TARGET_LATEST = "latest"
TARGET_PID_PREFIX = "pid:"


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

    # Control-lock state (per-Blender-instance). Advisory only in v1:
    # dispatch tools don't refuse when a lock is held, but well-behaved
    # LLMs call blender_request_control first + inspect this field via
    # blender_get_control_state. Only meaningful when client_type=="blender";
    # left blank on LLM/ephemeral clients.
    control_holder_uuid: Optional[str] = None
    control_holder_label: Optional[str] = None
    control_expires_at: Optional[float] = None  # unix epoch, None = no lock
    control_reason: Optional[str] = None

    # Per-process disambiguation metadata (Blender clients only). Old
    # addons omit these and the server leaves them None; new addons
    # send them at register_client time AND re-send on any bpy load_post
    # so blend_file stays live. Exposed on list_available_clients so
    # LLMs facing multiple concurrent Blenders can pick unambiguously
    # (e.g., filter by blend_file endswith "terrahawk.blend").
    pid: Optional[int] = None
    hostname: Optional[str] = None
    blend_file: Optional[str] = None

    def lock_is_active(self, now: Optional[float] = None) -> bool:
        """True iff a non-expired lock is held. Lazy-expiry: callers that
        see False after this returned True should treat the lock as gone."""
        if self.control_holder_uuid is None or self.control_expires_at is None:
            return False
        return (now if now is not None else time.time()) < self.control_expires_at

    def seen_ago(self, now: Optional[float] = None) -> float:
        return (now if now is not None else time.time()) - self.last_seen

    def is_stale(self, now: Optional[float] = None) -> bool:
        return self.seen_ago(now) > CLIENT_STALE_SECONDS

    def clear_lock(self) -> None:
        self.control_holder_uuid = None
        self.control_holder_label = None
        self.control_expires_at = None
        self.control_reason = None

    def to_dict(self) -> dict[str, Any]:
        # Build manually — asdict() deep-copies all fields including `session`,
        # which holds an MCP ServerSession with asyncio.Future objects that
        # cannot be pickled/deepcopied.
        d: dict[str, Any] = {
            "uuid": self.uuid,
            "client_type": self.client_type,
            "label": self.label,
            "is_persistent": self.is_persistent,
            "capabilities": list(self.capabilities),
            "group_id": self.group_id,
            "connected_at": self.connected_at,
            "last_seen": self.last_seen,
            "last_seen_seconds_ago": round(self.seen_ago(), 1),
            "stale": self.is_stale(),
        }
        # Advertise the lock only when active so list_available_clients
        # payloads stay small for the common no-lock case.
        if self.lock_is_active():
            d["control_lock"] = {
                "holder_uuid": self.control_holder_uuid,
                "holder_label": self.control_holder_label,
                "expires_at": self.control_expires_at,
                "reason": self.control_reason,
            }
        # Per-process metadata — only included when the client actually
        # sent them (old addons don't). Keeps the payload shape identical
        # for pre-metadata clients.
        if self.pid is not None:
            d["pid"] = self.pid
        if self.hostname is not None:
            d["hostname"] = self.hostname
        if self.blend_file is not None:
            d["blend_file"] = self.blend_file
        return d


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

    def __init__(self, bus_id, name: str = "", manager: Optional["BusManager"] = None):
        # bus_id is a uuid.UUID; stored verbatim so log lines + record_extra
        # carry the UUID type (callers can str() at the edge).
        self.bus_id = bus_id
        self.name = name
        # Back-reference to the global BusManager so register/unregister
        # can maintain BusManager._session_index — used by the activity
        # middleware for O(1) session → client_uuid lookup at thousands-
        # of-clients scale. Optional so MessageBus stays usable in tests
        # / standalone construction without a manager.
        self._manager = manager
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
            # Same "None = keep" semantics for per-process metadata, so
            # a bare re-register (from load_post etc.) doesn't wipe the
            # fields when the addon didn't recompute them.
            if client_info.pid is not None:
                existing.pid = client_info.pid
            if client_info.hostname is not None:
                existing.hostname = client_info.hostname
            if client_info.blend_file is not None:
                existing.blend_file = client_info.blend_file
            if client_info.session is not None:
                # Session changed → re-index. Drop the old session's entry
                # (whatever it was pointing to is stale) and add the new
                # one. Idempotent if the session is the same object.
                if self._manager is not None and existing.session is not None and existing.session is not client_info.session:
                    self._manager.forget_session(existing.session)
                existing.session = client_info.session
                if self._manager is not None:
                    self._manager.index_session(client_info.session, self.bus_id, client_info.uuid)
            self.last_activity = time.time()
            return existing
        bucket[client_info.uuid] = client_info
        # First-time registration: index the session.
        if self._manager is not None:
            self._manager.index_session(client_info.session, self.bus_id, client_info.uuid)
        self.last_activity = time.time()
        logger.info(
            "Registered client %s (%s) on bus %s",
            client_info.uuid, client_info.client_type, self.bus_id,
        )
        return client_info

    def unregister(self, client_uuid: str) -> bool:
        removed = self.persistent_clients.pop(client_uuid, None) or self.ephemeral_clients.pop(client_uuid, None)
        if removed:
            # Drop the session→uuid index entry too.
            if self._manager is not None and removed.session is not None:
                self._manager.forget_session(removed.session)
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

    def evict_dead(self, now: Optional[float] = None) -> list[str]:
        """Unregister Blender clients silent for CLIENT_EVICT_SECONDS.

        Lazy: called from the list/target-selection paths rather than a
        background sweep, so an idle bus costs nothing.
        """
        now = now if now is not None else time.time()
        dead = [
            c.uuid for c in self.all_clients()
            if c.client_type == "blender" and c.seen_ago(now) > CLIENT_EVICT_SECONDS
        ]
        for client_uuid in dead:
            self.unregister(client_uuid)
        return dead

    def blender_clients(self, include_stale: bool = False) -> list[ClientInfo]:
        now = time.time()
        return [
            c for c in self.all_clients()
            if c.client_type == "blender" and (include_stale or not c.is_stale(now))
        ]

    def resolve_target(self, spec: str) -> Optional[ClientInfo]:
        """Resolve a target spec to a client.

        Accepts a client uuid, ``"latest"`` (the newest-registered live
        Blender client, falling back to the newest stale one so a Blender
        busy in a long operator is still reachable), or ``"pid:<n>"``
        (newest Blender client reporting that pid). uuids change on every
        Blender relaunch; pid is what callers tend to know.
        """
        if spec == TARGET_LATEST:
            candidates = self.blender_clients() or self.blender_clients(include_stale=True)
            return max(candidates, key=lambda c: c.connected_at, default=None)
        if spec.startswith(TARGET_PID_PREFIX):
            try:
                pid = int(spec[len(TARGET_PID_PREFIX):])
            except ValueError:
                return None
            matches = [c for c in self.blender_clients(include_stale=True) if c.pid == pid]
            return max(matches, key=lambda c: c.connected_at, default=None)
        return self.get(spec)

    # ---- routing ----

    def _resolve_targets(self, routing: dict[str, Any], from_uuid: str) -> list[ClientInfo]:
        mode = routing.get("type", "broadcast")
        clients = self.all_clients()

        if mode == "direct":
            target = routing.get("target_uuid")
            c = self.resolve_target(target) if target else None
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
        # session-identity → (bus_id, client_uuid). Maintained by
        # MessageBus.register/unregister via the back-reference set in
        # get_or_create. Lets BusActivityMiddleware do O(1) lookup
        # instead of iterating all clients across all buses on every
        # incoming message — critical at thousands-of-clients scale.
        # Key is id(session) since FastMCP sessions don't expose a stable
        # hashable id of their own and Python's object id is unique for
        # the object's lifetime.
        self._session_index: dict[int, tuple[Any, str]] = {}

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
            bus = MessageBus(bus_id, name=name, manager=self)
            self._buses[bus_id] = bus
            logger.info("In-memory bus state created for %s (%r)", bus_id, name)
        return bus

    def remove(self, bus_id) -> None:
        self._buses.pop(bus_id, None)

    def all_buses(self) -> dict[Any, MessageBus]:
        return dict(self._buses)

    # ---- session index (O(1) lookup for the middleware) ----

    def index_session(self, session: Any, bus_id: Any, client_uuid: str) -> None:
        """Record session → (bus_id, client_uuid). Idempotent."""
        if session is None:
            return
        self._session_index[id(session)] = (bus_id, client_uuid)

    def forget_session(self, session: Any) -> None:
        """Remove a session's index entry. Idempotent."""
        if session is None:
            return
        self._session_index.pop(id(session), None)

    def lookup_session(self, session: Any) -> Optional[tuple[Any, str]]:
        """Return (bus_id, client_uuid) for the given session, or None."""
        if session is None:
            return None
        return self._session_index.get(id(session))


# Module-level singleton.
bus_manager = BusManager()
