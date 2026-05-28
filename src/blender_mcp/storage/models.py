"""SQLAlchemy 2.x async models for the Phase I shared-bus schema.

Three tables — Bus, BusMembership, BusInvitation — capture the
multi-membership data model. All other state (live ClientInfo, dispatched
jobs, message routing) stays in memory; the DB is only for the things
that need to survive a restart (who owns / belongs to / can join which
bus).

User identity uses Authentik's ``hashed_user_id`` (the JWT ``sub`` claim
when ``sub_mode: hashed_user_id`` is set on the OAuth provider), stored
as a plain string column. No separate User table — Authentik IS the
identity store, we just reference its opaque user IDs by value.
"""

from __future__ import annotations

import enum
import secrets
import uuid as _uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


# All TZ-aware. Storing naive UTC would invite the usual "is this UTC?"
# bug class when other services consume the DB later.
def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    """Single declarative base for all Phase I tables."""


class BusRole(str, enum.Enum):
    """Membership role within a single bus.

    ``owner``  — created the bus; can invite, revoke any member, never
                 demoted automatically. There's always exactly one owner
                 per bus (the user who created it). Personal buses have
                 their owner as the sole member.
    ``member`` — full dispatch + read access on the bus. Can invite
                 new members (and revoke their own invitations).
    ``guest``  — read-only: can list clients + read resources, but cannot
                 dispatch tools or send messages. Useful for "observer"
                 collaborators.
    """

    owner = "owner"
    member = "member"
    guest = "guest"


class Bus(Base):
    """A bus has an owner + a set of memberships + a name.

    Every authenticated user has exactly one ``is_personal=True`` bus
    auto-created on first bus-relevant request (see bus_repo.ensure_personal_bus).
    Additional shared buses are created explicitly via ``bus_create_bus``.
    """

    __tablename__ = "bus"

    bus_id: Mapped[_uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=_uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(String(512))
    owner_user_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    # Personal buses are bootstrapped automatically; users can't leave or
    # delete them, and there's at most one per user (enforced by the
    # partial-unique-index below).
    is_personal: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    # Soft-delete: revoked buses stay in DB for audit; query layer
    # filters out by default. Personal buses can't be revoked.
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    memberships: Mapped[list[BusMembership]] = relationship(
        back_populates="bus", cascade="all, delete-orphan"
    )
    invitations: Mapped[list[BusInvitation]] = relationship(
        back_populates="bus", cascade="all, delete-orphan"
    )

    __table_args__ = (
        # At most one personal bus per user (partial index — applies only
        # to rows where is_personal=True; shared buses have no such limit).
        Index(
            "ix_bus_one_personal_per_user",
            "owner_user_id",
            unique=True,
            postgresql_where="is_personal = TRUE AND revoked_at IS NULL",
        ),
    )


class BusMembership(Base):
    """Who is a member of which bus, and in what role.

    Composite PK on (bus_id, user_id) — a user can only be a member of
    a bus once (cannot have two memberships with different roles).
    """

    __tablename__ = "bus_membership"

    bus_id: Mapped[_uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("bus.bus_id", ondelete="CASCADE"),
        primary_key=True,
    )
    user_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    role: Mapped[BusRole] = mapped_column(
        Enum(BusRole, name="bus_role"), nullable=False, default=BusRole.member
    )
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    # Soft delete: revoked memberships stay for audit. Active query in
    # bus_repo filters ``revoked_at IS NULL``.
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    bus: Mapped[Bus] = relationship(back_populates="memberships")

    __table_args__ = (
        Index("ix_bus_membership_user", "user_id"),
    )


def _new_invitation_code() -> str:
    """``BMI-XXXXXXXXXX`` — base32 + uppercase, 10 chars after prefix.

    ~50 bits of entropy. Single-use + 24h expiry handle the rest of the
    threat model; this just needs to be hard to guess + easy to type.
    """
    # secrets.token_hex(5) → 10 hex chars (~40 bits) — enough for our
    # threat model since codes are single-use + expire fast.
    return f"BMI-{secrets.token_hex(5).upper()}"


def _default_expiry() -> datetime:
    return _utcnow() + timedelta(hours=24)


class BusInvitation(Base):
    """Single-use invitation code for joining a bus.

    Owner-or-member calls ``bus_invite_user`` → row written here →
    code returned to caller. Recipient calls ``bus_join(code)`` → row's
    ``consumed_at`` set in same transaction as the new ``BusMembership``
    insert (atomic — prevents double-claim races).
    """

    __tablename__ = "bus_invitation"

    invitation_id: Mapped[_uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=_uuid.uuid4
    )
    bus_id: Mapped[_uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("bus.bus_id", ondelete="CASCADE"),
        nullable=False,
    )
    invited_by: Mapped[str] = mapped_column(String(128), nullable=False)
    code: Mapped[str] = mapped_column(
        String(20), unique=True, nullable=False, default=_new_invitation_code
    )
    # Optional — if set, only this specific user can consume the
    # invitation. None = anyone with the code (the default code-share flow).
    invitee_user_id: Mapped[str | None] = mapped_column(String(128))
    role: Mapped[BusRole] = mapped_column(
        Enum(BusRole, name="bus_role"), nullable=False, default=BusRole.member
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_default_expiry
    )
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consumed_by_user_id: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    bus: Mapped[Bus] = relationship(back_populates="invitations")

    __table_args__ = (
        Index("ix_bus_invitation_code", "code"),
    )
