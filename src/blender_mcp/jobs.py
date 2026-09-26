"""Resumable dispatch jobs: persistence, late delivery and long-polling.

Every dispatch writes a bus_job row before routing. The addon's
``blender_job_update`` replies are persisted here whether or not anyone is
still waiting, so a job that outlives its dispatch timeout, or one that
finishes while the server restarts, still has a retrievable result.

Only the job's target client may report on it: an update is accepted when
the calling MCP session is the one registered as ``target_uuid`` on the
job's bus (``bus_manager.lookup_session``), so one client can't overwrite
another's results.

All DB work is best-effort. If Postgres is unavailable, dispatch keeps its
old behavior (wait, or time out) instead of failing short calls.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid as _uuid
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

from .job_waiter import job_waiter
from .message_bus import bus_manager
from .storage import job_repo
from .storage.models import JOB_TERMINAL

logger = logging.getLogger(__name__)

MAX_WAIT_S = 50.0
PRUNE_EVERY_S = 60.0
LOST_AFTER = timedelta(hours=1)
REPORTABLE = frozenset({"completed", "failed", "cancelled"})

# Seams for tests: a session factory and a membership predicate.
_session_factory: Callable[[], Any] | None = None
_membership: Callable[[str, str], Awaitable[bool]] | None = None
_member_buses: Callable[[str], Awaitable[list[str]]] | None = None

_waiters: dict[str, set[asyncio.Event]] = {}
_last_prune = 0.0


def _sessions():
    if _session_factory is not None:
        return _session_factory()
    from .storage import get_session
    return get_session()


async def is_member(user_id: str, bus_id: str) -> bool:
    if _membership is not None:
        return await _membership(user_id, bus_id)
    from .storage import bus_repo
    try:
        bus_uuid = _uuid.UUID(bus_id)
    except ValueError:
        return False
    async with _sessions() as s:
        return await bus_repo.is_member(s, bus_uuid, user_id)


async def member_bus_ids(user_id: str) -> list[str]:
    if _member_buses is not None:
        return await _member_buses(user_id)
    from .storage import bus_repo
    async with _sessions() as s:
        rows = await bus_repo.list_buses_for_user(s, user_id)
    return [str(bus.bus_id) for bus, _role in rows]


def caller_client_id() -> str | None:
    try:
        from fastmcp.server.dependencies import get_access_token
        access = get_access_token()
        return getattr(access, "client_id", None) if access else None
    except Exception:  # noqa: BLE001 - job store is best-effort; dispatch must not fail on it
        return None


# ---- long-poll signalling ------------------------------------------------

def _signal(job_id: str) -> None:
    for ev in _waiters.get(job_id, ()):
        ev.set()


async def wait_for_change(job_id: str, timeout: float) -> bool:
    """Wait until an update for ``job_id`` arrives or ``timeout`` passes."""
    timeout = max(0.0, min(float(timeout), MAX_WAIT_S))
    if timeout == 0:
        return False
    ev = asyncio.Event()
    _waiters.setdefault(job_id, set()).add(ev)
    try:
        await asyncio.wait_for(ev.wait(), timeout)
        return True
    except TimeoutError:
        return False
    finally:
        bucket = _waiters.get(job_id)
        if bucket is not None:
            bucket.discard(ev)
            if not bucket:
                _waiters.pop(job_id, None)


# ---- persistence ---------------------------------------------------------

async def record(
    job_id: str,
    bus_id: str,
    target_uuid: str,
    command: str,
    params: dict | None,
    caller_sub: str | None,
    caller_client: str | None = None,
) -> bool:
    try:
        async with _sessions() as s:
            await job_repo.create_job(
                s, job_id, bus_id, target_uuid, command, params,
                caller_sub=caller_sub, caller_client=caller_client,
            )
    except Exception as e:  # noqa: BLE001 - job store is best-effort; dispatch must not fail on it
        logger.warning("Job %s not persisted (dispatch continues): %s", job_id, e)
        return False
    await maybe_prune()
    return True


async def get(job_id: str):
    try:
        async with _sessions() as s:
            return await job_repo.get_job(s, job_id)
    except Exception as e:  # noqa: BLE001 - job store is best-effort; dispatch must not fail on it
        logger.warning("Job %s lookup failed: %s", job_id, e)
        return None


async def finish(job_id: str, status: str, result: str = "", error: str = ""):
    async with _sessions() as s:
        row, changed = await job_repo.finish_job(s, job_id, status, result, error)
    if changed:
        _signal(job_id)
    return row, changed


# ---- addon replies -------------------------------------------------------

async def handle_update(
    job_id: str,
    status: str,
    result: str,
    error: str,
    session: Any,
) -> dict | None:
    """Persist an addon's job_update. Returns None when the job isn't a
    tracked dispatch job (caller falls back to the legacy routing path)."""
    try:
        async with _sessions() as s:
            row = await job_repo.get_job(s, job_id)
    except Exception as e:  # noqa: BLE001 - job store is best-effort; dispatch must not fail on it
        logger.warning("job_update %s: DB unavailable, legacy path: %s", job_id, e)
        return None
    if row is None:
        return None

    where = bus_manager.lookup_session(session)
    if where is None or str(where[0]) != row.bus_id or where[1] != row.target_uuid:
        logger.warning(
            "Rejected job_update for %s: sender %s is not the job's target %s",
            job_id, where, row.target_uuid,
        )
        return {"status": "error", "error": "not_job_target", "job_id": job_id}

    if status == "running":
        async with _sessions() as s:
            await job_repo.mark_running(s, job_id)
        _signal(job_id)
        return {"status": "ok", "job_id": job_id, "recorded": "running"}

    if status in REPORTABLE:
        async with _sessions() as s:
            _row, changed = await job_repo.finish_job(s, job_id, status, result, error)
        job_waiter.deliver(row.bus_id, job_id, status, result, error)
        _signal(job_id)
        return {"status": "ok", "job_id": job_id, "recorded": status, "duplicate": not changed}

    return {"status": "ok", "job_id": job_id, "ignored_status": status}


# ---- retention -----------------------------------------------------------

async def maybe_prune(force: bool = False) -> None:
    """Delete expired rows and mark long-orphaned jobs lost, at most once a minute."""
    global _last_prune
    now = time.monotonic()
    if not force and now - _last_prune < PRUNE_EVERY_S:
        return
    _last_prune = now
    try:
        async with _sessions() as s:
            await job_repo.delete_expired(s)
            stale = await job_repo.open_jobs_older_than(s, job_repo.utcnow() - LOST_AFTER)
        buses = bus_manager.all_buses()
        for row in stale:
            try:
                bus = buses.get(_uuid.UUID(row.bus_id))
            except ValueError:
                bus = None
            if bus is not None and bus.get(row.target_uuid) is not None:
                continue
            await finish(row.job_id, "lost",
                         error="The target Blender has been gone for over an hour.")
    except Exception as e:  # noqa: BLE001 - job store is best-effort; dispatch must not fail on it
        logger.warning("Job pruning failed: %s", e)


def is_terminal(status: str) -> bool:
    return status in JOB_TERMINAL
