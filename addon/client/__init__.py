"""Bus client subpackage.

The client connects to the OAuth bus server over StreamableHttpTransport,
registers a persistent client on the per-user bus, and translates MCP
log-channel notifications into Blender main-thread job execution.

Public API:

    from addon.client import BlenderMCPClient

Internal layout:

    bus_client.py    — the BlenderMCPClient class (lifecycle + asyncio loop)
    message_pump.py  — incoming `notifications/message` filter + enqueue
    drainer.py       — Blender main-thread timer; pops jobs, runs scripts
    job_reporter.py  — sends `blender_job_update` back to the bus

Protocol constants live here so any submodule can import them without
the heavier bus_client transport machinery.
"""

from __future__ import annotations

# MCP log levels (RFC 5424) repurposed as job priorities; lower = more urgent.
LOG_LEVEL_TO_PRIORITY: dict[str, int] = {
    "emergency": 0, "alert": 1, "critical": 2, "error": 3,
    "warning": 4, "notice": 5, "info": 6, "debug": 7,
}

# Logger name the server uses for bus-forwarded log messages.
# Matches BusForwardingHandler in src/blender_mcp/oauth_server.py.
MESSAGE_BUS_LOGGER = "_message_bus"

from .bus_client import BlenderMCPClient  # noqa: E402  (constants must precede)

__all__ = ["BlenderMCPClient", "LOG_LEVEL_TO_PRIORITY", "MESSAGE_BUS_LOGGER"]
