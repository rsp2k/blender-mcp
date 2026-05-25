"""Typed payload dataclasses for bus messages.

These are optional. Tools accept plain dicts; these dataclasses give a
typed shape to callers that want one. No transport adapters here — the
transport is MCP logging notifications, handled in message_bus.py.
"""

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal, Optional

from .message_router import Priority


class MessageCategory(Enum):
    SYSTEM = "system"
    JOB = "job"
    CLIENT = "client"
    DATA = "data"
    EVENT = "event"
    CONTROL = "control"


@dataclass
class ProtocolHeader:
    """Common envelope fields."""
    message_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: float = field(default_factory=time.time)
    correlation_id: Optional[str] = None
    source_id: str = ""
    destination_id: Optional[str] = None
    priority: Priority = Priority.INFO
    category: MessageCategory = MessageCategory.DATA
    ttl: Optional[float] = None


@dataclass
class SystemMessage:
    """ping/pong/status/shutdown/restart/config_update."""
    action: Literal["ping", "pong", "shutdown", "restart", "status", "config_update"]
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class JobMessage:
    """submit/status/cancel/result/error/progress."""
    action: Literal["submit", "status", "cancel", "result", "error", "progress"]
    job_id: str
    job_type: Optional[str] = None
    parameters: dict[str, Any] = field(default_factory=dict)
    priority: Priority = Priority.INFO
    progress: Optional[float] = None
    result: Any = None
    error_message: Optional[str] = None


@dataclass
class ClientMessage:
    """register/unregister/heartbeat/capability_update/status."""
    action: Literal["register", "unregister", "heartbeat", "capability_update", "status"]
    client_uuid: str
    client_type: str = "ephemeral"
    capabilities: list[str] = field(default_factory=list)
    group_id: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class EventMessage:
    """Event notification."""
    event_type: str
    source: str
    data: dict[str, Any] = field(default_factory=dict)
    severity: Priority = Priority.INFO
    occurred_at: float = field(default_factory=time.time)


@dataclass
class ControlMessage:
    """pause/resume/throttle/backpressure/flow_control."""
    action: Literal["pause", "resume", "throttle", "backpressure", "flow_control"]
    rate_limit: Optional[float] = None
    queue_size: Optional[int] = None
