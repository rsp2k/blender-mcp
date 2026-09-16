"""Async CRUD over the ``feedback`` table.

Thin layer matching bus_repo.py's shape. Higher-level code (the MCP
tools in ``feedback_tools.py``, the reply CLI in
``scripts/reply_to_feedback.py``) calls these helpers rather than
touching SQLAlchemy directly.

Transparency: v1 model is "all bus users see all rows." No per-user
filtering on read paths. If we ever need per-user gating, do it here
rather than in the tools so the CLI path stays consistent.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, Sequence

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Feedback, FeedbackCategory, FeedbackStatus, new_feedback_id


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def create_feedback(
    session: AsyncSession,
    *,
    submitter_user_id: str,
    submitter_client_uuid: Optional[str],
    submitter_client_label: Optional[str],
    category: FeedbackCategory,
    title: str,
    body: str,
    context: Optional[dict] = None,
) -> Feedback:
    """Insert a new feedback row. Returns the persisted ORM object.

    ``id`` is generated in Python so we can return it in the same tool
    call that created it. Collision retry isn't wired up — the 64^11
    keyspace per prefix makes it astronomically unlikely — but the
    insert would raise IntegrityError if it ever happened, which the
    caller can surface as a "please retry" hint.
    """
    row = Feedback(
        id=new_feedback_id(category),
        submitter_user_id=submitter_user_id,
        submitter_client_uuid=submitter_client_uuid,
        submitter_client_label=submitter_client_label,
        category=category,
        title=title,
        body=body,
        context=context or {},
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def get_feedback(session: AsyncSession, feedback_id: str) -> Optional[Feedback]:
    stmt = select(Feedback).where(Feedback.id == feedback_id)
    return (await session.execute(stmt)).scalar_one_or_none()


async def list_feedback(
    session: AsyncSession,
    *,
    submitter_user_id: Optional[str] = None,
    category: Optional[FeedbackCategory] = None,
    status: Optional[FeedbackStatus] = None,
    limit: int = 50,
) -> Sequence[Feedback]:
    """Newest-first listing. All three filters are optional; pass none to
    list every row on the server (transparent v1 model). ``limit`` caps
    the return so a growing table doesn't dump megabytes into an MCP
    tool response — pagination is a follow-up if it becomes relevant."""
    stmt = select(Feedback).order_by(desc(Feedback.created_at))
    if submitter_user_id is not None:
        stmt = stmt.where(Feedback.submitter_user_id == submitter_user_id)
    if category is not None:
        stmt = stmt.where(Feedback.category == category)
    if status is not None:
        stmt = stmt.where(Feedback.status == status)
    stmt = stmt.limit(limit)
    return (await session.execute(stmt)).scalars().all()


async def append_reply(
    session: AsyncSession,
    feedback_id: str,
    *,
    author: str,
    text: str,
) -> Optional[Feedback]:
    """Append a reply to the JSONB ``replies`` array. Returns the updated
    row or None if the feedback doesn't exist.

    JSONB list append via read-modify-write is fine at our expected
    write rate (admin-driven, low volume). If replies ever get
    contended we'd move to a separate rows table with an ordering
    column, but that's premature for v1.
    """
    row = await get_feedback(session, feedback_id)
    if row is None:
        return None
    reply = {
        "author": author,
        "text": text,
        "created_at": _utcnow().isoformat(),
    }
    # SQLAlchemy doesn't track mutations into JSONB by default; assign a
    # new list so the ORM sees the change and issues an UPDATE.
    row.replies = [*row.replies, reply]
    row.updated_at = _utcnow()
    await session.commit()
    await session.refresh(row)
    return row


async def update_status(
    session: AsyncSession,
    feedback_id: str,
    *,
    status: FeedbackStatus,
    resolution: Optional[str] = None,
) -> Optional[Feedback]:
    row = await get_feedback(session, feedback_id)
    if row is None:
        return None
    row.status = status
    if resolution is not None:
        row.resolution = resolution
    row.updated_at = _utcnow()
    await session.commit()
    await session.refresh(row)
    return row


def to_dict(row: Feedback) -> dict:
    """Wire shape for MCP tool responses."""
    return {
        "id": row.id,
        "submitter_user_id": row.submitter_user_id,
        "submitter_client_uuid": row.submitter_client_uuid,
        "submitter_client_label": row.submitter_client_label,
        "category": row.category.value,
        "title": row.title,
        "body": row.body,
        "context": row.context,
        "status": row.status.value,
        "replies": row.replies,
        "resolution": row.resolution,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }
