"""Blender main-thread queue drainer.

`bpy.app.timers.register` calls into `drain_queue` ~100ms (configurable),
which pops the highest-priority queued job and executes it on Blender's
main thread. Returning a float reschedules; returning None unregisters
the timer.

Script execution runs synchronously here. The result/error is reported
back to the bus via `job_reporter.submit_job_update`, which marshals the
reply onto the asyncio loop on the worker thread.
"""

from __future__ import annotations

import heapq
import io
import time
import traceback
from contextlib import redirect_stdout
from typing import TYPE_CHECKING, Optional

import bmesh
import bpy
import mathutils

from .job_reporter import submit_job_update

if TYPE_CHECKING:
    from .bus_client import BlenderMCPClient


DRAIN_INTERVAL_S = 0.1


def drain_queue(client: "BlenderMCPClient") -> Optional[float]:
    """Pop the next job (if any) and dispatch it. Reschedules the timer.

    Returns:
        Next interval (seconds) to wait, or None to unregister.
    """
    if not client.running:
        client._timer_registered = False
        return None  # unregister timer

    with client.queue_lock:
        if not client.job_queue:
            return DRAIN_INTERVAL_S
        _, _, log_data = heapq.heappop(client.job_queue)

    # Server wire shape:
    #   {user_id, from_uuid, target_uuid, routing, payload,
    #    job_id, message_id, priority, timestamp}
    # `payload` holds the LLM-sent dict, expected to contain
    # {message_type: "job_dispatch", job_id, script, ...}.
    target = log_data.get("target_uuid")
    if target and target != client.client_uuid:
        return 0.0  # not for us; check again immediately

    payload = log_data.get("payload", log_data)
    if payload.get("message_type") != "job_dispatch":
        return 0.0

    job_id = payload.get("job_id")
    script = payload.get("script", "")
    if not job_id or not script:
        return 0.0

    execute_script(client, job_id, script)
    return 0.0  # check for next immediately


def execute_script(client: "BlenderMCPClient", job_id: str, script: str) -> None:
    """Execute a dispatched script in Blender's main thread, report result."""
    client.active_jobs[job_id] = time.time()
    output = io.StringIO()

    exec_globals = {
        "bpy": bpy,
        "bmesh": bmesh,
        "mathutils": mathutils,
        # Handler helpers (PolyHaven, Hyper3D, etc.) live on the executor.
        # The client stores a reference at construction time.
        "executor": client.executor,
        "__name__": "__blender_mcp_job__",
    }

    try:
        with redirect_stdout(output):
            exec(compile(script, f"<job_{job_id}>", "exec"), exec_globals)
        submit_job_update(
            client, job_id, "completed",
            result=output.getvalue(), error="",
        )
    except Exception as e:
        tb = traceback.format_exc()
        submit_job_update(
            client, job_id, "failed",
            result=output.getvalue(),
            error=f"{e}\n{tb}",
        )
    finally:
        client.active_jobs.pop(job_id, None)
