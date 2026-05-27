"""Async CRUD over the Phase I bus schema.

Thin layer over SQLAlchemy 2.x — every public function takes (or opens)
an AsyncSession and returns plain dicts or ORM objects. Higher-level
code (``bus_manager``, the new ``bus_*`` tools) calls these helpers
rather than touching SQLAlchemy directly.

Conventions:

- Soft delete: ``Bus.revoked_at`` and ``BusMembership.revoked_at``. All
  read paths filter ``revoked_at IS NULL`` unless ``include_revoked=True``.
- Personal buses are auto-provisioned by :func:`ensure_personal_bus`,
  called from ``bus_manager.get_bus()`` on first hit for a user.
- Invitations are claimed atomically: in one transaction we check
  validity, write the ``BusMembership`` row, and stamp
  ``consumed_at`` on the invitation. Prevents double-claim races.
"""

from __future__ import annotations

import logging
import uuid as _uuid
from datetime import datetime, timezone
from typing import Optional, Sequence

from sqlalchemy import and_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Bus, BusInvitation, BusMembership, BusRole

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---- Bus CRUD ----------------------------------------------------------


async def ensure_personal_bus(session: AsyncSession, user_id: str) -> Bus:
    """Return the user's personal bus, creating it if it doesn't exist.

    Idempotent + race-safe: the partial unique index
    ``ix_bus_one_personal_per_user`` enforces at-most-one-active-personal-bus
    per user, so the INSERT-then-rollback-on-conflict path is correct
    even under concurrent first-request.
    """
    stmt = select(Bus).where(
        Bus.owner_user_id == user_id,
        Bus.is_personal.is_(True),
        Bus.revoked_at.is_(None),
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is not None:
        return row

    # Pre-generate the UUID in Python so both INSERTs (bus + owner
    # membership) can reference it. SQLAlchemy's column ``default=uuid4``
    # only fires at flush, which is too late — we'd queue a membership
    # row with bus_id=None and the FK insert would 23502 (not-null).
    bus_id = _uuid.uuid4()
    bus = Bus(
        bus_id=bus_id,
        name="Personal",
        description="Your private bus — only you can dispatch here.",
        owner_user_id=user_id,
        is_personal=True,
    )
    session.add(bus)
    # Also seed the owner membership so list_buses_for_user returns it.
    session.add(BusMembership(bus_id=bus_id, user_id=user_id, role=BusRole.owner))
    try:
        await session.commit()
    except IntegrityError:
        # Lost the race — another concurrent request created the personal
        # bus first. Roll back + read it out.
        await session.rollback()
        row = (await session.execute(stmt)).scalar_one_or_none()
        assert row is not None, "personal-bus race fallback: still nothing in DB"
        return row
    await session.refresh(bus)
    logger.info("Created personal bus %s for user %s", bus.bus_id, user_id)
    return bus


async def create_shared_bus(
    session: AsyncSession, *, owner_user_id: str, name: str, description: str = ""
) -> Bus:
    """Create a new shared bus owned by ``owner_user_id``."""
    bus_id = _uuid.uuid4()
    bus = Bus(
        bus_id=bus_id,
        name=name,
        description=description or None,
        owner_user_id=owner_user_id,
        is_personal=False,
    )
    session.add(bus)
    session.add(BusMembership(bus_id=bus_id, user_id=owner_user_id, role=BusRole.owner))
    await session.commit()
    await session.refresh(bus)
    logger.info("Created shared bus %s (%s) for user %s", bus.bus_id, name, owner_user_id)
    return bus


async def get_bus(session: AsyncSession, bus_id: _uuid.UUID) -> Optional[Bus]:
    """Fetch a single bus by id (active only)."""
    stmt = select(Bus).where(Bus.bus_id == bus_id, Bus.revoked_at.is_(None))
    return (await session.execute(stmt)).scalar_one_or_none()


async def list_buses_for_user(
    session: AsyncSession, user_id: str
) -> Sequence[tuple[Bus, BusRole]]:
    """Return (bus, my_role) for every bus the user is an active member of."""
    stmt = (
        select(Bus, BusMembership.role)
        .join(BusMembership, BusMembership.bus_id == Bus.bus_id)
        .where(
            BusMembership.user_id == user_id,
            BusMembership.revoked_at.is_(None),
            Bus.revoked_at.is_(None),
        )
        .order_by(Bus.is_personal.desc(), Bus.created_at.asc())
    )
    return list((await session.execute(stmt)).all())


# ---- Membership --------------------------------------------------------


async def get_membership(
    session: AsyncSession, bus_id: _uuid.UUID, user_id: str
) -> Optional[BusMembership]:
    """Return the active membership row for (bus_id, user_id) or None."""
    stmt = select(BusMembership).where(
        BusMembership.bus_id == bus_id,
        BusMembership.user_id == user_id,
        BusMembership.revoked_at.is_(None),
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def is_member(session: AsyncSession, bus_id: _uuid.UUID, user_id: str) -> bool:
    """Convenience predicate — used by dispatch tools to gate by membership."""
    return (await get_membership(session, bus_id, user_id)) is not None


async def revoke_member(
    session: AsyncSession, *, bus_id: _uuid.UUID, user_id: str
) -> bool:
    """Soft-delete a membership. Returns True if a row was actually affected."""
    stmt = (
        update(BusMembership)
        .where(
            BusMembership.bus_id == bus_id,
            BusMembership.user_id == user_id,
            BusMembership.revoked_at.is_(None),
        )
        .values(revoked_at=_utcnow())
    )
    result = await session.execute(stmt)
    await session.commit()
    return result.rowcount > 0


# ---- Invitations -------------------------------------------------------


async def create_invitation(
    session: AsyncSession,
    *,
    bus_id: _uuid.UUID,
    invited_by: str,
    role: BusRole = BusRole.member,
    invitee_user_id: Optional[str] = None,
) -> BusInvitation:
    """Issue a single-use invitation. Caller MUST be a member of the bus."""
    inv = BusInvitation(
        bus_id=bus_id,
        invited_by=invited_by,
        role=role,
        invitee_user_id=invitee_user_id,
    )
    session.add(inv)
    await session.commit()
    await session.refresh(inv)
    return inv


async def consume_invitation(
    session: AsyncSession, *, code: str, joining_user_id: str
) -> tuple[Optional[Bus], str]:
    """Atomically claim an invitation and write the BusMembership.

    Returns ``(bus, "joined")`` on success, ``(None, reason)`` on
    failure. Reasons: ``not_found``, ``expired``, ``already_consumed``,
    ``wrong_invitee``, ``already_member``.

    The whole flow runs in one transaction so two simultaneous claims
    on the same code can't both succeed.
    """
    stmt = select(BusInvitation).where(BusInvitation.code == code).with_for_update()
    inv = (await session.execute(stmt)).scalar_one_or_none()

    if inv is None:
        return None, "not_found"
    if inv.consumed_at is not None:
        return None, "already_consumed"
    if inv.expires_at < _utcnow():
        return None, "expired"
    if inv.invitee_user_id and inv.invitee_user_id != joining_user_id:
        return None, "wrong_invitee"

    # If the user is already a member, surface it but don't fail — they
    # might re-use a code accidentally. Consume the invitation anyway
    # so it can't be replayed.
    existing = await get_membership(session, inv.bus_id, joining_user_id)
    inv.consumed_at = _utcnow()
    inv.consumed_by_user_id = joining_user_id
    if existing is None:
        session.add(
            BusMembership(bus_id=inv.bus_id, user_id=joining_user_id, role=inv.role)
        )
    await session.commit()
    bus = await get_bus(session, inv.bus_id)
    return bus, ("already_member" if existing else "joined")
