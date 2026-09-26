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
    """Push a job onto the client's priority queue (thread-safe).

    A ``job_cancel`` message is acted on here instead of being queued, so it
    doesn't wait behind the very job it's trying to cancel.
    """
    payload = log_data.get("payload", log_data) if isinstance(log_data, dict) else {}
    if isinstance(payload, dict) and payload.get("message_type") == "job_cancel":
        target = log_data.get("target_uuid")
        if not target or target == getattr(client, "client_uuid", None):
            cancel_queued_job(client, payload.get("job_id"))
        return
    with client.queue_lock:
        heapq.heappush(client.job_queue, (priority, time.time(), log_data))


def cancel_queued_job(client: "BlenderMCPClient", job_id) -> bool:
    """Remove a not-yet-started job from the queue and report it cancelled.

    A job that's already running or finished is left alone: the drainer
    has reported "running" for it, which is how the server learns the
    cancel came too late.
    """
    if not job_id:
        return False
    with client.queue_lock:
        kept = [
            item for item in client.job_queue
            if (item[2].get("payload", item[2]) or {}).get("job_id") != job_id
        ]
        removed = len(kept) != len(client.job_queue)
        if removed:
            client.job_queue[:] = kept
            heapq.heapify(client.job_queue)
    if removed:
        from .job_reporter import submit_job_update
        submit_job_update(client, job_id, "cancelled", error="Cancelled before it started.")
        print(f"[BlenderMCP] Cancelled queued job {job_id}")
    return removed
