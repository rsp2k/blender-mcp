"""Addon job-pump health over the bus (feedback bug-iDJHVyy4e2Q).

A stalled job pump used to look exactly like a busy main thread from the
bus: the client stayed "connected", the heartbeat kept updating, and
every call timed out. The addon now attaches its queue depth, the job it
is running (and for how long) and its drain-timer state to the
pending_dispatches poll it makes every ~10 s from its network thread,
so a report still arrives while Blender's main thread is busy. That's
what lets a timeout say which of the two it is. A stalled pump can be
restarted with blender_reset_pump: the instruction rides back on the
poll reply and is applied on the network thread, which works even when
no dispatched command can run.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from fastmcp import Context
from fastmcp.contrib.mcp_mixin import MCPMixin, mcp_tool

from .bus_tools import _resolve_user_id, resolve_bus
from .client_role import require_role

# The addon polls every ~10 s; allow a few misses before calling a report stale.
HEALTH_FRESH_S = 35.0
# A queue with work but no drain for this long, while nothing runs, is a stall.
STALL_DRAIN_S = 5.0
RESET_WAIT_S = 15.0

_INT_KEYS = ("queue_depth",)
_FLOAT_KEYS = ("last_drain_seconds_ago",)
_BOOL_KEYS = ("drain_timer_registered", "main_thread_busy")


def sanitize(report: Any) -> dict | None:
    """Keep only known, well-typed fields from an addon health report."""
    if not isinstance(report, dict):
        return None
    out: dict[str, Any] = {}
    for k in _INT_KEYS:
        if isinstance(report.get(k), (int, float)):
            out[k] = int(report[k])
    for k in _FLOAT_KEYS:
        if isinstance(report.get(k), (int, float)):
            out[k] = round(float(report[k]), 1)
    for k in _BOOL_KEYS:
        if isinstance(report.get(k), bool):
            out[k] = report[k]
    active = []
    for job in report.get("active_jobs") or []:
        if isinstance(job, dict) and isinstance(job.get("job_id"), str):
            running = job.get("running_seconds")
            active.append({
                "job_id": job["job_id"][:64],
                "running_seconds": round(float(running), 1) if isinstance(running, (int, float)) else None,
            })
    out["active_jobs"] = active[:8]
    return out


def record(client_info, report: Any, now: float | None = None) -> None:
    """Store a sanitized report on the client."""
    clean = sanitize(report)
    if clean is None:
        return
    client_info.health = clean
    client_info.health_at = now if now is not None else time.time()


def take_reset(client_info) -> bool:
    """Consume a pending reset request (True once)."""
    if client_info.reset_pump_requested:
        client_info.reset_pump_requested = False
        return True
    return False


def describe(client_info, now: float | None = None) -> tuple[dict | None, str | None]:
    """(summary, hint) from the client's latest health report, or (None, None)
    when it has never reported. The summary ages running times to ``now``."""
    if client_info is None or client_info.health is None or client_info.health_at is None:
        return None, None
    now = now if now is not None else time.time()
    age = now - client_info.health_at
    h = client_info.health
    active = [
        {"job_id": j["job_id"],
         "running_seconds": (round(j["running_seconds"] + age, 1)
                             if j.get("running_seconds") is not None else None)}
        for j in h.get("active_jobs", [])
    ]
    queued = h.get("queue_depth", 0)
    summary = {
        "queue_depth": queued,
        "active_jobs": active,
        "drain_timer_registered": h.get("drain_timer_registered"),
        "last_drain_seconds_ago": h.get("last_drain_seconds_ago"),
        "reported_seconds_ago": round(age, 1),
    }
    if age > HEALTH_FRESH_S:
        return summary, (
            f"The addon hasn't reported its queue in {age:.0f}s (it reports about every 10s "
            "from its network thread, even while Blender is busy), so its connection is "
            "probably down, or the addon predates health reports."
        )
    if active:
        job = active[0]
        running = f" for {job['running_seconds']:.0f}s" if job["running_seconds"] is not None else ""
        return summary, (
            f"Blender is busy running job {job['job_id']}{running}"
            f"{f', with {queued} more queued' if queued else ''}. The job pump is working; "
            "wait for it or follow the job with blender_job_status."
        )
    drain_age = h.get("last_drain_seconds_ago")
    stalled = queued > 0 and (
        h.get("drain_timer_registered") is False
        or (drain_age is not None and drain_age > STALL_DRAIN_S)
    )
    if stalled:
        return summary, (
            f"The addon's job pump has stalled: {queued} queued, nothing running, "
            f"last drain {drain_age if drain_age is not None else '?'}s ago. "
            "Call blender_reset_pump to restart it over the bus."
        )
    if queued == 0:
        return summary, (
            "Blender reports an empty queue and nothing running, so this dispatch hasn't "
            "reached it yet (a dropped event-stream message). The addon's pull fallback "
            "picks it up within about 10s."
        )
    return summary, f"Blender has {queued} queued and is draining them."


class BlenderHealthComponent(MCPMixin):
    """reset_pump."""

    @mcp_tool()
    @require_role("llm-client")
    async def reset_pump(
        self,
        target_uuid: str | None = None,
        bus_id: str | None = None,
        ctx: Context = None,
    ) -> str:
        """Restart a Blender addon's job pump over the bus.

        For the "connected, jobs queued, nothing running" stall that used to
        need someone to click Disconnect/Connect in the sidebar. The request
        is delivered with the addon's next queue check-in (~10 s) and handled
        on its network thread, so it works when no command can run. It can't
        help when Blender's main thread itself is busy with a long job; the
        health in the reply tells the two apart.
        """
        from .dispatch_component import _pick_blender_target

        user_id = _resolve_user_id(ctx)
        if not user_id:
            return json.dumps({"status": "error", "error": "unauthenticated"})
        resolved = await resolve_bus(user_id, bus_id)
        if not resolved["ok"]:
            return json.dumps(resolved)
        bus = resolved["bus"]
        pick = _pick_blender_target(bus, target_uuid)
        if not pick["ok"]:
            return json.dumps(pick)
        client = bus.get(pick["uuid"])
        requested_at = time.time()
        client.reset_pump_requested = True

        loop = asyncio.get_running_loop()
        deadline = loop.time() + RESET_WAIT_S
        while loop.time() < deadline:
            if not client.reset_pump_requested and (client.health_at or 0) >= requested_at:
                break
            await asyncio.sleep(0.25)
        applied = not client.reset_pump_requested
        summary, hint = describe(client)
        return json.dumps({
            "status": "ok",
            "target_uuid": client.uuid,
            "reset_delivered": applied,
            "health": summary,
            "hint": hint if applied else (
                "The addon hasn't checked in within "
                f"{RESET_WAIT_S:.0f}s; the reset is still pending and applies at its next "
                "check-in. Addons that predate health reports don't support it."
            ),
        })
