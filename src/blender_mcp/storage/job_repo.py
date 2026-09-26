"""Async CRUD for bus_job (resumable dispatch jobs).

State transitions only move forward: queued -> running -> a terminal
state. A late or duplicate update for a job that's already terminal is a
no-op, so an addon re-sending from its outbox can't overwrite a result.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import JOB_TERMINAL, BusJob

JOB_TTL = timedelta(hours=24)
RESULT_CAP = 1_000_000          # characters kept of result / error text
PARAMS_CAP = 100_000            # serialized params larger than this are summarized
TRUNCATION_MARKER = "\n[... truncated by BlenderMCP: {n} more characters]"


def utcnow() -> datetime:
    return datetime.now(UTC)


def _aware(dt: datetime | None) -> datetime | None:
    # SQLite (tests) hands back naive datetimes; Postgres returns aware ones.
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def cap_text(text: str | None, cap: int = RESULT_CAP) -> str | None:
    if text is None or len(text) <= cap:
        return text
    return text[:cap] + TRUNCATION_MARKER.format(n=len(text) - cap)


def _cap_params(params: dict | None) -> dict | None:
    if params is None:
        return None
    try:
        size = len(json.dumps(params, default=str))
    except (TypeError, ValueError):
        return {"_unserializable": True}
    if size > PARAMS_CAP:
        return {"_truncated": True, "_size": size, "_keys": sorted(map(str, params))[:50]}
    return params


async def create_job(
    s: AsyncSession,
    job_id: str,
    bus_id: str,
    target_uuid: str,
    command: str,
    params: dict | None,
    caller_sub: str | None = None,
    caller_client: str | None = None,
) -> BusJob:
    now = utcnow()
    row = BusJob(
        job_id=job_id,
        bus_id=bus_id,
        target_uuid=target_uuid,
        caller_sub=caller_sub,
        caller_client=caller_client,
        command=command,
        params=_cap_params(params),
        status="queued",
        created_at=now,
        expires_at=now + JOB_TTL,
    )
    s.add(row)
    await s.commit()
    return row


async def get_job(s: AsyncSession, job_id: str) -> BusJob | None:
    return await s.get(BusJob, job_id)


async def mark_running(s: AsyncSession, job_id: str) -> BusJob | None:
    row = await s.get(BusJob, job_id)
    if row is None:
        return None
    if row.status == "queued":
        row.status = "running"
        row.started_at = utcnow()
        await s.commit()
    return row


async def finish_job(
    s: AsyncSession,
    job_id: str,
    status: str,
    result: str | None = None,
    error: str | None = None,
) -> tuple[BusJob | None, bool]:
    """Move a job to a terminal state. Returns (row, changed)."""
    row = await s.get(BusJob, job_id)
    if row is None:
        return None, False
    if row.status in JOB_TERMINAL:
        return row, False
    now = utcnow()
    row.status = status
    row.result = cap_text(result)
    row.error = cap_text(error)
    row.finished_at = now
    if row.started_at is None and status != "cancelled":
        row.started_at = now
    await s.commit()
    return row, True


async def list_jobs(
    s: AsyncSession,
    bus_ids: Sequence[str],
    status: str | None = None,
    limit: int = 20,
) -> list[BusJob]:
    if not bus_ids:
        return []
    q = select(BusJob).where(BusJob.bus_id.in_(list(bus_ids)))
    if status:
        q = q.where(BusJob.status == status)
    q = q.order_by(BusJob.created_at.desc()).limit(max(1, min(limit, 200)))
    return list((await s.execute(q)).scalars())


async def delete_expired(s: AsyncSession, now: datetime | None = None) -> int:
    now = now or utcnow()
    res = await s.execute(delete(BusJob).where(BusJob.expires_at < now))
    await s.commit()
    return res.rowcount or 0


async def open_jobs_older_than(s: AsyncSession, cutoff: datetime) -> list[BusJob]:
    q = select(BusJob).where(
        BusJob.status.in_(["queued", "running"]),
        BusJob.created_at < cutoff,
    )
    return list((await s.execute(q)).scalars())


def to_dict(row: BusJob, include_output: bool = True) -> dict:
    d = {
        "job_id": row.job_id,
        "bus_id": row.bus_id,
        "target_uuid": row.target_uuid,
        "command": row.command,
        "status": row.status,
        "created_at": _iso(row.created_at),
        "started_at": _iso(row.started_at),
        "finished_at": _iso(row.finished_at),
        "expires_at": _iso(row.expires_at),
    }
    if include_output:
        d["result"] = row.result
        d["error"] = row.error or ""
    return d


def _iso(dt: datetime | None) -> str | None:
    dt = _aware(dt)
    return dt.isoformat() if dt else None
