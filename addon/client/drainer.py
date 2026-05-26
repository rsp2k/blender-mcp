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
    # `payload` holds the LLM-sent dict. Two message_types supported:
    #   "job_dispatch"     — {job_id, script} (pre-Phase-A, free-form exec)
    #   "command_dispatch" — {job_id, command, params} (Phase A, named handler)
    target = log_data.get("target_uuid")
    if target and target != client.client_uuid:
        return 0.0  # not for us; check again immediately

    payload = log_data.get("payload", log_data)
    msg_type = payload.get("message_type")
    job_id = payload.get("job_id")
    if not job_id:
        return 0.0

    if msg_type == "job_dispatch":
        script = payload.get("script", "")
        if not script:
            return 0.0
        execute_script(client, job_id, script)
    elif msg_type == "command_dispatch":
        command = payload.get("command", "")
        params = payload.get("params") or {}
        if not command:
            return 0.0
        execute_command(client, job_id, command, params)
    # Unknown message_type silently dropped (forward-compatible: future
    # message types in the wire won't make older addons crash).

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


def execute_command(
    client: "BlenderMCPClient",
    job_id: str,
    command: str,
    params: dict,
) -> None:
    """Dispatch a named command via the executor's registry, report result.

    The server-side dispatch_component sends this shape instead of free-form
    scripts so MCP tools can present a typed surface (e.g. blender_get_scene_info)
    without script-injection hazards. The executor's @command-registered
    method runs on Blender's main thread (we're called from drain_queue
    which IS the main thread, via bpy.app.timers), captures any stdout,
    and the result/error round-trip back to the server via submit_job_update
    exactly like execute_script does.

    Unknown commands or commands gated off by AddonPreferences come back
    as status="failed" with the addon-side error message intact — server
    tier-3 wrappers can recognize "gated_off" hints and surface them.
    """
    client.active_jobs[job_id] = time.time()
    output = io.StringIO()

    try:
        with redirect_stdout(output):
            result = client.executor.execute_command(
                {"type": command, "params": params}
            )
        # execute_command already wraps its return in {"status", "result"}
        # or {"status", "message"}; collapse that into our wire shape.
        if isinstance(result, dict) and result.get("status") == "error":
            submit_job_update(
                client, job_id, "failed",
                result=output.getvalue(),
                error=str(result.get("message", "")),
            )
        else:
            submit_job_update(
                client, job_id, "completed",
                # Serialize the inner result; the dispatch handler returns
                # arbitrary JSON-able shapes.
                result=_safe_json(result.get("result") if isinstance(result, dict) else result),
                error="",
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


def _safe_json(value) -> str:
    """Best-effort JSON encoding for handler return values.

    Handlers can return dicts, lists, primitives — but also bpy objects
    that aren't JSON-serializable. Fall back to repr() for those rather
    than failing the whole job.
    """
    import json
    try:
        return json.dumps(value)
    except (TypeError, ValueError):
        return repr(value)
