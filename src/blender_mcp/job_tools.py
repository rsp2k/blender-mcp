"""MCP tools for long-running Blender jobs.

Any dispatch tool that outlives its wait now returns a job_id instead of a
bare timeout; these tools follow it up. ``blender_submit`` starts a job
without waiting at all, for work known to be long (heavy booleans,
renders, big imports).

Jobs are visible to every member of the bus they ran on and are kept for
24 hours. A queued job can be cancelled; a running one can't, because
Blender runs jobs on its main thread and Python can't safely interrupt a
bpy operation mid-way.
"""

from __future__ import annotations

import json

from fastmcp import Context
from fastmcp.contrib.mcp_mixin import MCPMixin, mcp_tool

from . import jobs
from .bus_tools import _resolve_user_id, resolve_bus
from .client_role import require_role
from .message_router import Priority
from .storage import job_repo

CANCEL_ACK_WAIT_S = 5.0


def _err(error: str, **extra) -> str:
    return json.dumps({"status": "error", "error": error} | extra)


async def _authorized_job(ctx, job_id: str):
    """Return (user_id, row, None) or (None, None, error_json)."""
    user_id = _resolve_user_id(ctx)
    if not user_id:
        return None, None, _err("unauthenticated")
    row = await jobs.get(job_id)
    if row is None or not await jobs.is_member(user_id, row.bus_id):
        # Same answer for "missing" and "not yours", so job ids can't be probed.
        return None, None, _err("job_not_found", job_id=job_id)
    return user_id, row, None


class BlenderJobComponent(MCPMixin):
    """submit / job_status / job_result / list_jobs / job_cancel."""

    @mcp_tool()
    @require_role("llm-client")
    async def submit(
        self,
        command: str,
        params: dict | None = None,
        target_uuid: str | None = None,
        bus_id: str | None = None,
        ctx: Context = None,
    ) -> str:
        """Start a Blender command as a background job and return immediately.

        Use this for work that takes longer than a tool call should wait
        (heavy booleans, renders, large imports). ``command`` is the
        dispatch command name without the ``blender_`` prefix, e.g.
        ``execute_code`` with ``params={"code": "..."}``. Returns
        ``{"status": "queued", "job_id": ...}``; follow up with
        blender_job_status(job_id, wait_seconds=50) and blender_job_result.
        """
        from .dispatch_component import _new_job_id, _pick_blender_target, _route_job

        user_id = _resolve_user_id(ctx)
        if not user_id:
            return _err("unauthenticated")
        resolved = await resolve_bus(user_id, bus_id)
        if not resolved["ok"]:
            return json.dumps(resolved)
        bus, bus_id_str = resolved["bus"], str(resolved["bus_id"])
        pick = _pick_blender_target(bus, target_uuid)
        if not pick["ok"]:
            return json.dumps(pick | {"ok": False, "command": command})
        job_id = _new_job_id()
        params = params or {}
        if not await jobs.record(job_id, bus_id_str, pick["uuid"], command, params,
                                 caller_sub=user_id, caller_client=jobs.caller_client_id()):
            return _err("job_store_unavailable",
                        hint="Background jobs need the job store; call the dispatch tool directly.")
        _route_job(bus, bus_id_str, pick["uuid"], job_id, command, params)
        return json.dumps({"status": "queued", "job_id": job_id, "command": command,
                           "target_uuid": pick["uuid"], "bus_id": bus_id_str})

    @mcp_tool()
    @require_role("llm-client")
    async def job_status(self, job_id: str, wait_seconds: float = 0, ctx: Context = None) -> str:
        """Report a job's status; optionally wait for it to change.

        ``wait_seconds`` (0-50) long-polls: the call returns as soon as the
        job reports progress or finishes, or when the wait runs out. Loop
        on it until status is completed, failed, cancelled or lost, then
        read the output with blender_job_result.
        """
        _user, row, error = await _authorized_job(ctx, job_id)
        if error:
            return error
        if not jobs.is_terminal(row.status) and wait_seconds > 0:
            before = row.status
            if await jobs.wait_for_change(job_id, wait_seconds):
                row = await jobs.get(job_id) or row
            if row.status == before and not jobs.is_terminal(row.status):
                # A change may have landed just before we started waiting.
                row = await jobs.get(job_id) or row
        out = job_repo.to_dict(row, include_output=False)
        out["done"] = jobs.is_terminal(row.status)
        return json.dumps(out)

    @mcp_tool()
    @require_role("llm-client")
    async def job_result(self, job_id: str, ctx: Context = None) -> str:
        """Return a job's status together with its full result and error text.

        Output is capped at about 1 MB; renders and exported files come back
        as paths on the Blender machine, like blender_get_viewport_screenshot.
        """
        _user, row, error = await _authorized_job(ctx, job_id)
        if error:
            return error
        out = job_repo.to_dict(row, include_output=True)
        out["done"] = jobs.is_terminal(row.status)
        return json.dumps(out)

    @mcp_tool()
    @require_role("llm-client")
    async def list_jobs(
        self,
        bus_id: str | None = None,
        status: str | None = None,
        limit: int = 20,
        ctx: Context = None,
    ) -> str:
        """List recent jobs (newest first) on your buses, or on one bus.

        ``status`` filters to queued, running, completed, failed, cancelled
        or lost. Jobs are kept for 24 hours.
        """
        user_id = _resolve_user_id(ctx)
        if not user_id:
            return _err("unauthenticated")
        if bus_id:
            if not await jobs.is_member(user_id, bus_id):
                return _err("not_a_member", bus_id=bus_id)
            bus_ids = [bus_id]
        else:
            bus_ids = await jobs.member_bus_ids(user_id)
        try:
            async with jobs._sessions() as s:
                rows = await job_repo.list_jobs(s, bus_ids, status=status, limit=limit)
        except Exception as e:  # noqa: BLE001 - surface any store failure as a tool result
            return _err("job_store_unavailable", detail=str(e)[:200])
        return json.dumps({"status": "ok",
                           "jobs": [job_repo.to_dict(r, include_output=False) for r in rows]})

    @mcp_tool()
    @require_role("llm-client")
    async def job_cancel(self, job_id: str, ctx: Context = None) -> str:
        """Cancel a job that hasn't started yet.

        Only queued jobs can be cancelled. A running job can't be
        interrupted: Blender runs jobs on its main thread and Python can't
        safely stop a bpy operation part-way, so it will finish on its own.
        """
        user_id, row, error = await _authorized_job(ctx, job_id)
        if error:
            return error
        if jobs.is_terminal(row.status):
            return json.dumps({"status": "error", "error": "already_finished",
                               "job_id": job_id, "job_status": row.status})
        resolved = await resolve_bus(user_id, row.bus_id)
        bus = resolved["bus"] if resolved.get("ok") else None

        if row.status == "running":
            target = bus.get(row.target_uuid) if bus is not None else None
            if target is not None and target.is_worker:
                # A worker is its own process, so a running job there can be
                # stopped: the parent kills the worker.
                from .worker_tools import stop_worker_process
                stopped = await stop_worker_process(bus, row.bus_id, target, user_id,
                                                    graceful=False)
                if stopped.get("stopped"):
                    await jobs.finish(job_id, "cancelled",
                                      error="Cancelled by stopping its background worker.")
                    return json.dumps({"status": "ok", "job_id": job_id,
                                       "job_status": "cancelled", "worker_stopped": stopped})
                return json.dumps({"status": "error", "error": "worker_stop_failed",
                                   "job_id": job_id, "detail": stopped})
            return json.dumps({
                "status": "error", "error": "job_running", "job_id": job_id,
                "message": "The job is already running in Blender and can't be "
                           "interrupted; it will finish on its own. Jobs sent to a "
                           "background worker (blender_spawn_worker) can be cancelled.",
            })
        if bus is None or bus.get(row.target_uuid) is None:
            await jobs.finish(job_id, "cancelled",
                              error="Cancelled while the target Blender was disconnected.")
            return json.dumps({"status": "ok", "job_id": job_id, "job_status": "cancelled",
                               "note": "The target Blender is not connected, so the job was "
                                       "marked cancelled without its acknowledgement."})

        bus.route(
            payload={"message_type": "job_cancel", "job_id": job_id},
            from_uuid=f"server-dispatch:{row.bus_id}",
            routing={"type": "direct", "target_uuid": row.target_uuid},
            priority=Priority.NOTICE,
        )
        await jobs.wait_for_change(job_id, CANCEL_ACK_WAIT_S)
        row = await jobs.get(job_id) or row
        if row.status == "cancelled":
            return json.dumps({"status": "ok", "job_id": job_id, "job_status": "cancelled"})
        if row.status == "running":
            return json.dumps({"status": "error", "error": "job_running", "job_id": job_id,
                               "message": "The job started before the cancel arrived and "
                                          "can't be interrupted."})
        if jobs.is_terminal(row.status):
            return json.dumps({"status": "error", "error": "already_finished",
                               "job_id": job_id, "job_status": row.status})
        # No acknowledgement: the cancel notification may have been lost the
        # same way the dispatch can be. Mark it cancelled here so the addon's
        # pull fallback never delivers it, and so the addon drops it from its
        # queue at its next check-in (pending_dispatches reports it).
        await jobs.finish(job_id, "cancelled",
                          error="Cancelled; Blender hadn't acknowledged, so it drops "
                                "the job at its next check-in.")
        return json.dumps({"status": "ok", "job_id": job_id, "job_status": "cancelled",
                           "note": "Blender didn't acknowledge within "
                                   f"{CANCEL_ACK_WAIT_S:g}s; the job is marked cancelled and "
                                   "will be dropped at its next check-in."})

    @mcp_tool()
    @require_role("addon")
    async def pending_dispatches(
        self,
        client_uuid: str,
        held_job_ids: list[str] | None = None,
        ctx: Context = None,
    ) -> str:
        """For the Blender addon: dispatches queued for this client that it
        hasn't picked up, and which of its held jobs were cancelled.

        A fallback for dispatch notifications lost on the event stream; the
        addon polls it and dedupes by job_id. Only the calling session's own
        registered client can be queried.
        """
        from .bus_tools import _session_from_ctx

        out = await jobs.pending_dispatches(_session_from_ctx(ctx), client_uuid, held_job_ids)
        return json.dumps(out)
