"""Incoming notification filter.

FastMCP delivers every notification through the client's `message_handler`.
This module distills the subset we care about — log records on the
`_message_bus` logger — and hands the decoded payload to the client's
priority queue.

Kept as free functions so the logic can be unit-tested with a stub client
object (no real FastMCP/asyncio needed).
"""

from __future__ import annotations

import heapq
import json
import time
from typing import TYPE_CHECKING, Any

from . import LOG_LEVEL_TO_PRIORITY, MESSAGE_BUS_LOGGER

if TYPE_CHECKING:
    from .bus_client import BlenderMCPClient


def _extract(params: Any, key: str) -> Any:
    """Read a field from a pydantic model OR a dict, transparently."""
    val = getattr(params, key, None)
    if val is None and isinstance(params, dict):
        val = params.get(key)
    return val


async def handle_message(client: "BlenderMCPClient", message: Any) -> None:
    """Filter an MCP notification; enqueue if it's a bus log message."""
    try:
        # Notifications arrive as mcp.types union wrappers; unwrap via `.root`.
        inner = getattr(message, "root", message)
        if getattr(inner, "method", None) != "notifications/message":
            return

        params = getattr(inner, "params", None)
        if params is None:
            return

        if _extract(params, "logger") != MESSAGE_BUS_LOGGER:
            return

        data = _extract(params, "data")
        if data is None:
            return

        # `data` may arrive as JSON-encoded string when the transport
        # round-trips through MCP's string-typed log fields.
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except json.JSONDecodeError:
                return

        level = str(_extract(params, "level") or "info").lower()
        priority = LOG_LEVEL_TO_PRIORITY.get(level, 6)
        enqueue_job(client, priority, data)
    except Exception as e:
        print(f"[BlenderMCP] handle_message error: {e}")


def enqueue_job(client: "BlenderMCPClient", priority: int, log_data: dict) -> None:
    """Push a job onto the client's priority queue (thread-safe)."""
    with client.queue_lock:
        heapq.heappush(client.job_queue, (priority, time.time(), log_data))
