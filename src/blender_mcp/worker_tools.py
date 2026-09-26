"""MCP tools for headless background workers.

A GUI Blender can spawn a headless ``blender -b`` on a saved copy of its
scene. The worker registers on the bus as its own client (``role:
worker``, ``parent_uuid``: the spawner), so heavy work runs there while
the user's viewport stays responsive. The caller orchestrates:

    blender_spawn_worker()                       -> worker_uuid
    blender_submit(..., target_uuid=worker_uuid) -> job_id
    blender_job_status(job_id, wait_seconds=50)  (report_progress shows up here)
    blender_offer_reload(path=...)               -> banner in the GUI Blender
    blender_stop_worker(worker_uuid)

Workers are never picked implicitly; target them by uuid. A worker exits
on its own when its parent Blender goes away, when its time runs out
(two hours at most, and never past the parent's access token), or when
stopped.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastmcp import Context
from fastmcp.contrib.mcp_mixin import MCPMixin, mcp_tool

from .bus_tools import _resolve_user_id, resolve_bus
from .client_role import require_role

SPAWN_ACK_TIMEOUT_S = 60.0
POLL_S = 0.5
GRACEFUL_EXIT_WAIT_S = 8.0


def _err(error: str, **extra) -> str:
    return json.dumps({"status": "error", "error": error} | extra)


def _parse_result(dispatch_json: str) -> tuple[bool, Any, dict]:
    """Unpack a _dispatch reply: (ok, inner result, whole reply)."""
    reply = json.loads(dispatch_json)
    if reply.get("status") != "completed":
        return False, None, reply
    inner = reply.get("result")
    if isinstance(inner, str):
        try:
            inner = json.loads(inner)
        except ValueError:
            pass
    return True, inner, reply


async def _wait_until(predicate, timeout: float) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(POLL_S)
    return predicate()


async def stop_worker_process(bus, bus_id_str: str, worker, caller_sub: str | None,
                              graceful: bool = True, timeout: float = 30.0) -> dict:
    """Stop a worker and unregister it. Shared by stop_worker and job_cancel.

    Graceful first (the worker finishes its reply, unregisters and exits),
    unless it's busy or ``graceful`` is False; then the parent terminates
    the process and the server drops the registration itself, since a
    killed worker can't unregister.
    """
    from .dispatch_component import _dispatch

    worker_uuid = worker.uuid
    if graceful:
        await _dispatch(bus, bus_id_str, "worker_exit", {}, worker_uuid,
                        GRACEFUL_EXIT_WAIT_S, caller_sub=caller_sub)
        if await _wait_until(lambda: bus.get(worker_uuid) is None, GRACEFUL_EXIT_WAIT_S):
            return {"worker_uuid": worker_uuid, "stopped": True, "how": "graceful"}

    parent = bus.get(worker.parent_uuid) if worker.parent_uuid else None
    if parent is None:
        # Parent is gone; the worker notices within ~10 s and exits by itself.
        bus.unregister(worker_uuid)
        return {"worker_uuid": worker_uuid, "stopped": True, "how": "parent_gone",
                "note": "Parent Blender is gone; the worker exits on its own shortly."}

    ok, inner, reply = _parse_result(await _dispatch(
        bus, bus_id_str, "stop_worker", {"worker_uuid": worker_uuid},
        parent.uuid, timeout, caller_sub=caller_sub,
    ))
    if not ok:
        return {"worker_uuid": worker_uuid, "stopped": False, "how": "kill",
                "error": reply.get("error") or reply.get("status"), "reply": reply}
    exited = bool(inner.get("exited")) if isinstance(inner, dict) else False
    if exited:
        bus.unregister(worker_uuid)
    return {"worker_uuid": worker_uuid, "stopped": exited, "how": "kill",
            "returncode": inner.get("returncode") if isinstance(inner, dict) else None,
            "worker_dir": inner.get("worker_dir") if isinstance(inner, dict) else None}


class BlenderWorkerComponent(MCPMixin):
    """spawn_worker / stop_worker / offer_reload."""

    @mcp_tool()
    @require_role("llm-client")
    async def spawn_worker(
        self,
        target_uuid: str | None = None,
        bus_id: str | None = None,
        timeout_s: float = 90.0,
        ctx: Context = None,
    ) -> str:
        """Start a headless background Blender on a copy of a GUI Blender's scene.

        The GUI Blender (``target_uuid``, or the only live one) saves a copy
        of its current scene, unsaved changes included, and starts
        ``blender -b`` on it. Returns once the worker has joined the bus:
        ``{worker_uuid, pid, snapshot_path, worker_dir, expires_at}``.

        Send heavy work to it with blender_submit(target_uuid=worker_uuid)
        so the user's viewport stays responsive; job code can call
        report_progress(fraction, message). Save results inside
        ``worker_dir`` and offer them with blender_offer_reload. Stop it
        with blender_stop_worker when done. A worker loads a full copy of
        the scene, so memory use roughly doubles while it runs.
        """
        from .dispatch_component import _dispatch, _pick_blender_target

        user_id = _resolve_user_id(ctx)
        if not user_id:
            return _err("unauthenticated")
        resolved = await resolve_bus(user_id, bus_id)
        if not resolved["ok"]:
            return json.dumps(resolved)
        bus, bus_id_str = resolved["bus"], str(resolved["bus_id"])
        pick = _pick_blender_target(bus, target_uuid)
        if not pick["ok"]:
            return json.dumps(pick | {"ok": False, "command": "spawn_worker"})
        parent = bus.get(pick["uuid"])
        if parent is not None and parent.is_worker:
            return _err("target_is_worker", target_uuid=parent.uuid,
                        hint="Spawn from the user's Blender, not from a worker.")

        ok, inner, reply = _parse_result(await _dispatch(
            bus, bus_id_str, "spawn_worker", {}, pick["uuid"],
            SPAWN_ACK_TIMEOUT_S, caller_sub=user_id,
        ))
        if not ok or not isinstance(inner, dict) or not inner.get("worker_uuid"):
            return _err("spawn_failed", detail=reply.get("error") or reply.get("status"),
                        reply=reply)
        worker_uuid = inner["worker_uuid"]
        registered = await _wait_until(lambda: bus.get(worker_uuid) is not None,
                                       max(5.0, min(float(timeout_s), 600.0)))
        out = {"status": "ok" if registered else "worker_not_registered",
               "parent_uuid": pick["uuid"]} | inner
        if not registered:
            out["hint"] = (f"The worker process started but hasn't joined the bus yet. "
                           f"Check its log at {inner.get('log_path')} on the Blender "
                           f"machine, or call blender_list_available_clients shortly.")
        return json.dumps(out)

    @mcp_tool()
    @require_role("llm-client")
    async def stop_worker(
        self,
        worker_uuid: str,
        bus_id: str | None = None,
        ctx: Context = None,
    ) -> str:
        """Stop a background worker and remove it from the bus.

        Asks the worker to exit cleanly; if it's busy with a job, its parent
        Blender terminates the process instead. Files in the worker's
        directory (snapshot, results, log) are left on disk.
        """
        user_id = _resolve_user_id(ctx)
        if not user_id:
            return _err("unauthenticated")
        resolved = await resolve_bus(user_id, bus_id)
        if not resolved["ok"]:
            return json.dumps(resolved)
        bus, bus_id_str = resolved["bus"], str(resolved["bus_id"])
        worker = bus.get(worker_uuid)
        if worker is None:
            return json.dumps({"status": "ok", "worker_uuid": worker_uuid, "stopped": True,
                               "note": "No such worker on this bus (already gone?)."})
        if not worker.is_worker:
            return _err("not_a_worker", worker_uuid=worker_uuid)
        busy = await _worker_busy(bus_id_str, worker_uuid)
        result = await stop_worker_process(bus, bus_id_str, worker, user_id,
                                           graceful=not busy)
        return json.dumps({"status": "ok" if result.get("stopped") else "error"} | result)

    @mcp_tool()
    @require_role("llm-client")
    async def offer_reload(
        self,
        path: str,
        message: str | None = None,
        target_uuid: str | None = None,
        bus_id: str | None = None,
        ctx: Context = None,
    ) -> str:
        """Offer the user a finished background result to open.

        Shows a "Background result ready" banner in the GUI Blender's
        sidebar with Reload and Dismiss. Nothing is loaded until the user
        clicks Reload, and the banner warns when reloading would discard
        unsaved changes. ``path`` is a .blend file on the Blender machine,
        typically saved by a worker inside its worker_dir.
        """
        from .dispatch_component import TIMEOUT_FAST, _dispatch, _pick_blender_target

        user_id = _resolve_user_id(ctx)
        if not user_id:
            return _err("unauthenticated")
        resolved = await resolve_bus(user_id, bus_id)
        if not resolved["ok"]:
            return json.dumps(resolved)
        bus, bus_id_str = resolved["bus"], str(resolved["bus_id"])
        pick = _pick_blender_target(bus, target_uuid)
        if not pick["ok"]:
            return json.dumps(pick | {"ok": False, "command": "offer_reload"})
        target = bus.get(pick["uuid"])
        if target is not None and target.is_worker:
            return _err("target_is_worker", target_uuid=target.uuid,
                        hint="Offer the result to the user's Blender (the worker's parent_uuid).")
        return await _dispatch(bus, bus_id_str, "offer_reload",
                               {"path": path, "message": message or ""},
                               pick["uuid"], TIMEOUT_FAST, caller_sub=user_id)


async def _worker_busy(bus_id_str: str, worker_uuid: str) -> bool:
    """True if the worker has a running job (a graceful exit would wait on it)."""
    from . import jobs
    from .storage import job_repo
    try:
        async with jobs._sessions() as s:
            rows = await job_repo.list_jobs(s, [bus_id_str], status="running", limit=50)
    except Exception:  # noqa: BLE001 - unknown means try graceful first
        return False
    return any(r.target_uuid == worker_uuid for r in rows)
