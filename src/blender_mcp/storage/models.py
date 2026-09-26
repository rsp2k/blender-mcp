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
    Float,
    JSON,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
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


class FeedbackCategory(str, enum.Enum):
    """Four categories, mapped to short prefixes in the public shortcode.

    Prefixes are stable — they're baked into shortcodes that get pasted
    into chat and referenced later. Renaming a category later means
    supporting the old prefix as an alias forever.
    """

    feature_request = "feature-request"  # prefix "fr"
    bug = "bug"                           # prefix "bug"
    friction = "friction"                 # prefix "fx"
    other = "other"                       # prefix "ot"


class FeedbackStatus(str, enum.Enum):
    open = "open"
    in_progress = "in-progress"
    resolved = "resolved"
    duplicate = "duplicate"
    wontfix = "wontfix"


_CATEGORY_PREFIX = {
    FeedbackCategory.feature_request: "fr",
    FeedbackCategory.bug: "bug",
    FeedbackCategory.friction: "fx",
    FeedbackCategory.other: "ot",
}


def new_feedback_id(category: FeedbackCategory) -> str:
    """``{prefix}-{11-char-token}`` — e.g. ``fr-Kf3nQp7Xy_A``.

    ``secrets.token_urlsafe(8)`` yields 11 chars from ``[A-Za-z0-9_-]``.
    64^11 ≈ 7e19 keyspace per prefix; collision-free at any realistic
    scale, and the prefix is a human-readable "what kind of thing is
    this" hint when the id shows up in URLs or messages.
    """
    return f"{_CATEGORY_PREFIX[category]}-{secrets.token_urlsafe(8)}"


class Feedback(Base):
    """LLM- or human-submitted feedback / feature request / bug report.

    Every authenticated user can list + read every row (transparent
    model per v1). Only the submitter's user_id is written to
    ``submitter_user_id``; replies are appended to ``replies`` as
    ``{"author", "text", "created_at"}`` dicts by the reply CLI, not
    via any MCP tool in v1 (admin-side workflow, not LLM-side).

    ``id`` is the public shortcode, primary key. See ``new_feedback_id``.
    """

    __tablename__ = "feedback"

    id: Mapped[str] = mapped_column(String(20), primary_key=True)
    submitter_user_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    # Optional — LLM sessions can register a client UUID via
    # bus_register_session but many don't. When present, useful for
    # correlating multiple submissions from the same tool/agent.
    submitter_client_uuid: Mapped[str | None] = mapped_column(String(128))
    submitter_client_label: Mapped[str | None] = mapped_column(String(255))

    # values_callable is mandatory here — without it, SQLAlchemy sends
    # the enum's .name (Python attribute — ``feature_request`` with an
    # underscore) instead of its .value (``feature-request`` with a
    # hyphen). The Postgres enum type was created from .value strings,
    # so the mismatch triggers InvalidTextRepresentationError only for
    # entries where name != value (feature_request; the others slip
    # through by coincidence). Verified in prod by bug-pYH00BElpAs.
    category: Mapped[FeedbackCategory] = mapped_column(
        Enum(
            FeedbackCategory,
            name="feedback_category",
            values_callable=lambda cls: [e.value for e in cls],
        ),
        nullable=False,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    # Free-form JSON — the tool signature suggests fields like
    # {attempted_tool, error, addon_version, blender_version} but doesn't
    # enforce a schema. Callers write whatever they think is useful.
    context: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    # Same values_callable fix as `category` above — FeedbackStatus.in_progress
    # would blow up identically the first time anyone tried to set it
    # from Python (``.name == "in_progress"`` vs ``.value == "in-progress"``).
    status: Mapped[FeedbackStatus] = mapped_column(
        Enum(
            FeedbackStatus,
            name="feedback_status",
            values_callable=lambda cls: [e.value for e in cls],
        ),
        nullable=False,
        default=FeedbackStatus.open,
        index=True,
    )
    # Replies: list of {"author": str, "text": str, "created_at": iso8601-str}.
    # Appended by the reply CLI. Never mutated (append-only history).
    replies: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    # Resolution note: filled by the admin when marking resolved/wontfix
    # etc. Optional — a status change without a note is allowed.
    resolution: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


class OAuthClientRole(Base):
    """Persisted ``client_id → role`` map for role attribution.

    Mirror of the in-memory ``client_role._role_by_client_id`` dict. The
    DCR-capture middleware writes here at /register time; on server startup
    a rehydration hook does ``SELECT * FROM oauth_client_role`` and primes
    the in-memory cache.

    Why both DB + cache: ``get_caller_role`` is called from sync code paths
    inside @require_role and check_role_or_reject. Making it async would
    cascade through every gated tool. Keeping the in-memory dict for the
    fast path + rehydrating from DB on startup gives us restart-survives
    without changing the call-site API.

    No FK to anything — client_id is owned by FastMCP's OAuthProxy state
    (which lives in the auto-created oauth_kv_store table, ALSO not in
    Alembic's metadata). If a DCR record is deleted upstream the role row
    becomes orphaned but harmless; cleanup is a future cron concern.
    """

    __tablename__ = "oauth_client_role"

    client_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    # Captured from the DCR request for audit / debugging. Same value
    # used to derive the role; stored separately so we can re-derive roles
    # later if the software_id→role map changes.
    software_id: Mapped[str | None] = mapped_column(String(128))
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class PersonalAccessToken(Base):
    """Long-lived bearer token for CI and scripted MCP clients.

    Lets a caller skip the OAuth browser flow by sending
    ``Authorization: Bearer bmcp_...``. Only the sha256 of the token is
    stored; the plaintext is shown once at creation. A token acts as its
    owner's LLM client (role ``llm-client``) and can't mint more tokens.
    """

    __tablename__ = "access_token"

    id: Mapped[_uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=_uuid.uuid4
    )
    # Same identity the bus uses: the OIDC ``sub`` (Authentik hashed user id).
    user_sub: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    # Non-secret display handle, e.g. "bmcp_Ab3xYz9Q", for list/revoke.
    token_prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    # NULL = never expires (admin script only).
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# bus_job.status values. Plain strings rather than a DB enum, so adding a
# state later is a code change, not a migration.
JOB_STATUSES = ("queued", "running", "completed", "failed", "cancelled", "lost")
JOB_TERMINAL = frozenset({"completed", "failed", "cancelled", "lost"})


class BusJob(Base):
    """A dispatched command and its outcome, kept after the caller stops waiting.

    Dispatch tools wait a bounded time for the addon's reply; when that
    runs out the job keeps going in Blender, and the reply lands here so
    the caller can poll for it (blender_job_status / blender_job_result).
    Rows live 24 h (expires_at) and are pruned lazily.
    """

    __tablename__ = "bus_job"

    job_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    bus_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    target_uuid: Mapped[str] = mapped_column(String(128), nullable=False)
    caller_sub: Mapped[str | None] = mapped_column(String(128))
    # DCR client_id or "pat:<token id>" of the caller, when known.
    caller_client: Mapped[str | None] = mapped_column(String(128))
    command: Mapped[str] = mapped_column(String(100), nullable=False)
    params: Mapped[dict | None] = mapped_column(JSONB().with_variant(JSON(), "sqlite"))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    result: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Latest report_progress() from the job's code: 0..1 plus a short note.
    progress: Mapped[float | None] = mapped_column(Float)
    progress_message: Mapped[str | None] = mapped_column(String(500))
    progress_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_bus_job_status_created", "status", "created_at"),
        Index("ix_bus_job_expires_at", "expires_at"),
    )
