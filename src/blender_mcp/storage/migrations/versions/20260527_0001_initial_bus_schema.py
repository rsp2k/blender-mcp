"""Initial Phase I shared-bus schema: bus, bus_membership, bus_invitation.

Revision ID: 20260527_0001
Revises:
Create Date: 2026-05-27
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "20260527_0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ---- ENUM type (shared across membership.role + invitation.role) ----
    bus_role = postgresql.ENUM("owner", "member", "guest", name="bus_role")
    bus_role.create(op.get_bind(), checkfirst=True)

    # ---- bus ----
    op.create_table(
        "bus",
        sa.Column("bus_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("description", sa.String(512), nullable=True),
        sa.Column("owner_user_id", sa.String(128), nullable=False),
        sa.Column(
            "is_personal",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("FALSE"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_bus_owner_user_id", "bus", ["owner_user_id"])
    # Partial unique index — at most one ACTIVE personal bus per user.
    op.create_index(
        "ix_bus_one_personal_per_user",
        "bus",
        ["owner_user_id"],
        unique=True,
        postgresql_where=sa.text("is_personal = TRUE AND revoked_at IS NULL"),
    )

    # ---- bus_membership ----
    op.create_table(
        "bus_membership",
        sa.Column(
            "bus_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("bus.bus_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("user_id", sa.String(128), primary_key=True),
        sa.Column(
            "role",
            postgresql.ENUM(
                "owner", "member", "guest", name="bus_role", create_type=False
            ),
            nullable=False,
            server_default="member",
        ),
        sa.Column(
            "joined_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_bus_membership_user", "bus_membership", ["user_id"])

    # ---- bus_invitation ----
    op.create_table(
        "bus_invitation",
        sa.Column("invitation_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "bus_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("bus.bus_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("invited_by", sa.String(128), nullable=False),
        sa.Column("code", sa.String(20), nullable=False, unique=True),
        sa.Column("invitee_user_id", sa.String(128), nullable=True),
        sa.Column(
            "role",
            postgresql.ENUM(
                "owner", "member", "guest", name="bus_role", create_type=False
            ),
            nullable=False,
            server_default="member",
        ),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consumed_by_user_id", sa.String(128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_bus_invitation_code", "bus_invitation", ["code"])


def downgrade() -> None:
    op.drop_table("bus_invitation")
    op.drop_table("bus_membership")
    op.drop_table("bus")
    # Drop enum AFTER all tables that reference it are gone.
    postgresql.ENUM(name="bus_role").drop(op.get_bind(), checkfirst=True)
