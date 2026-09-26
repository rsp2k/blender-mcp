"""Reply path — sends `blender_job_update` back to the bus.

Called from Blender's main thread (in `drainer.execute_script`); marshals
the FastMCP `call_tool` coroutine onto the asyncio loop that lives on the
worker thread. Non-blocking: errors are logged when the future resolves.

Server derives the caller identity from the JWT in the bearer header on
the worker's persistent MCP session — the addon only sends four fields.

Results are kept in an in-memory outbox when the connection is down (a
server deploy, a network blip) and re-sent after the next registration,
so a job that finishes while disconnected still reports. The server
ignores repeats for jobs it has already recorded as finished.
"""

from __future__ import annotations

import asyncio
from collections import deque
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .bus_client import BlenderMCPClient

RESULT_CAP = 1_000_000
OUTBOX_MAX = 200
_MARKER = "\n[... truncated by BlenderMCP: {n} more characters]"


def cap_text(text: str, cap: int = RESULT_CAP) -> str:
    if not text or len(text) <= cap:
        return text
    return text[:cap] + _MARKER.format(n=len(text) - cap)


def _outbox(client: "BlenderMCPClient") -> deque:
    box = getattr(client, "job_outbox", None)
    if box is None:
        box = deque()
        client.job_outbox = box
    return box


def _queue(client: "BlenderMCPClient", update: dict) -> None:
    box = _outbox(client)
    if len(box) >= OUTBOX_MAX:
        dropped = box.popleft()
        print(f"[BlenderMCP] Job outbox full; dropped update for {dropped['job_id']}")
    box.append(update)


def _can_send(client: "BlenderMCPClient") -> bool:
    return bool(client.loop and client.client and client.loop.is_running()
                and getattr(client, "connected", True))


def _send(client: "BlenderMCPClient", update: dict) -> None:
    coro = client.client.call_tool("blender_job_update", update)
    future = asyncio.run_coroutine_threadsafe(coro, client.loop)

    def _done(fut):
        try:
            fut.result(timeout=0)
        except Exception as e:
            print(f"[BlenderMCP] job_update for {update['job_id']} failed: {e}")
            if update["status"] != "running":
                _queue(client, update)

    future.add_done_callback(_done)


def submit_job_update(
    client: "BlenderMCPClient",
    job_id: str,
    status: str,
    result: str = "",
    error: str = "",
    progress: float | None = None,
    progress_message: str | None = None,
) -> None:
    """Report a job's status; queue it for later if not connected.

    "running" updates are only useful live, so they're dropped rather
    than queued when the connection is down.
    """
    update = {
        "job_id": job_id,
        "status": status,
        "result": cap_text(result),
        "error": cap_text(error),
    }
    if progress is not None:
        update["progress"] = float(progress)
    if progress_message is not None:
        update["progress_message"] = str(progress_message)[:500]
    if not _can_send(client):
        if status == "running":
            return
        _queue(client, update)
        print(f"[BlenderMCP] Not connected; job {job_id} result queued ({status})")
        return
    _send(client, update)


PROGRESS_MIN_INTERVAL_S = 0.5


def make_progress_reporter(client: "BlenderMCPClient", job_id: str, clock=None):
    """Build the report_progress(fraction, message="") callable for one job.

    Rate-limited to one update per PROGRESS_MIN_INTERVAL_S; a report of
    1.0 always goes out. Never raises into the caller's code.
    """
    import time as _time
    clock = clock or _time.monotonic
    last = [float("-inf")]

    def report_progress(fraction: float, message: str = "") -> bool:
        """Report how far this job has got (0..1) with an optional note.
        Returns True if the update was sent, False if rate-limited."""
        try:
            fraction = max(0.0, min(1.0, float(fraction)))
        except (TypeError, ValueError):
            return False
        now = clock()
        if fraction < 1.0 and now - last[0] < PROGRESS_MIN_INTERVAL_S:
            return False
        last[0] = now
        try:
            submit_job_update(client, job_id, "running",
                              progress=fraction, progress_message=message or "")
        except Exception as e:  # noqa: BLE001 - progress must never break the job
            print(f"[BlenderMCP] report_progress failed: {e}")
            return False
        return True

    return report_progress


def flush_outbox(client: "BlenderMCPClient") -> int:
    """Re-send queued job updates. Call after a successful registration."""
    box = _outbox(client)
    if not box or not _can_send(client):
        return 0
    pending = list(box)
    box.clear()
    for update in pending:
        _send(client, update)
    print(f"[BlenderMCP] Re-sent {len(pending)} queued job update(s)")
    return len(pending)


def submit_force_release_control(client: "BlenderMCPClient", target_uuid: str) -> None:
    """Fire-and-forget ``blender_force_release_control`` call from the addon.

    Used by the "Take back" button so LLMs polling get_control_state
    see the release immediately instead of waiting for the natural
    expiry. Same asyncio-marshal pattern as ``submit_job_update``.
    """
    if not (client.loop and client.client and client.loop.is_running()):
        print("[BlenderMCP] Cannot force-release: client not connected")
        return

    coro = client.client.call_tool("blender_force_release_control", {
        "target_uuid": target_uuid,
    })
    future = asyncio.run_coroutine_threadsafe(coro, client.loop)

    def _log_err(fut):
        try:
            fut.result(timeout=0)
        except Exception as e:
            print(f"[BlenderMCP] force_release_control failed: {e}")

    future.add_done_callback(_log_err)
