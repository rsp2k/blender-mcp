"""Priority enum + dedicated message_bus logger.

The actual routing happens in message_bus.py via MCP log notifications.
This file kept slim: priority levels (MCP log levels 0..7) and the named
logger that bus traffic flows through.
"""

import logging
from enum import IntEnum

# Dedicated logger for bus traffic. Tools/components emit records here;
# a forwarding handler attached at server startup turns each record into
# an MCP notifications/message for subscribed clients.
_message_bus = logging.getLogger("_message_bus")
_message_bus.setLevel(logging.DEBUG)
_message_bus.propagate = False


class Priority(IntEnum):
    """MCP log levels repurposed as job priorities (0=highest)."""
    EMERGENCY = 0
    ALERT = 1
    CRITICAL = 2
    ERROR = 3
    WARNING = 4
    NOTICE = 5
    INFO = 6
    DEBUG = 7


# MCP wire-level log level strings (lowercase)
PRIORITY_TO_MCP_LEVEL: dict[Priority, str] = {
    Priority.EMERGENCY: "emergency",
    Priority.ALERT: "alert",
    Priority.CRITICAL: "critical",
    Priority.ERROR: "error",
    Priority.WARNING: "warning",
    Priority.NOTICE: "notice",
    Priority.INFO: "info",
    Priority.DEBUG: "debug",
}

MCP_LEVEL_TO_PRIORITY: dict[str, Priority] = {v: k for k, v in PRIORITY_TO_MCP_LEVEL.items()}


# Bridge MCP level strings to Python logging levels for the local logger.
PRIORITY_TO_PY_LEVEL: dict[Priority, int] = {
    Priority.EMERGENCY: logging.CRITICAL,
    Priority.ALERT: logging.CRITICAL,
    Priority.CRITICAL: logging.CRITICAL,
    Priority.ERROR: logging.ERROR,
    Priority.WARNING: logging.WARNING,
    Priority.NOTICE: logging.INFO,
    Priority.INFO: logging.INFO,
    Priority.DEBUG: logging.DEBUG,
}


def parse_priority(value: str | int | Priority) -> Priority:
    """Coerce strings ('info', 'error', ...) or ints into Priority."""
    if isinstance(value, Priority):
        return value
    if isinstance(value, int):
        return Priority(value)
    s = str(value).lower().strip()
    if s in MCP_LEVEL_TO_PRIORITY:
        return MCP_LEVEL_TO_PRIORITY[s]
    try:
        return Priority[s.upper()]
    except KeyError:
        raise ValueError(f"Unknown priority: {value!r}")
