"""Add ffb_tool_calls for fastmcp-feedback tool-call instrumentation.

Schema comes from fastmcp_feedback.instrumentation.build_metadata(prefix="ffb_")
(fastmcp-feedback 2026.9.27.1). The package never creates tables when given
our engine, so they're migrated here.

Autogenerate also proposed swapping access_token's unique index
(ux_access_token_token_hash, from 20260926_0004) for a unique constraint.
That's pre-existing model/migration drift with the same effect, unrelated to
this change, so it was removed from this revision.

Revision ID: 20260927_0007
Revises: 20260926_0006
Create Date: 2026-09-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260927_0007"
down_revision: str | None = "20260926_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")
_INDEXED = ("ok", "server_version", "session_id", "started_at", "tool", "user_sub")


def upgrade() -> None:
    op.create_table(
        "ffb_tool_calls",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_ms", sa.Float(), nullable=False),
        sa.Column("tool", sa.String(length=255), nullable=False),
        sa.Column("mode", sa.String(length=8), nullable=False),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("error_type", sa.String(length=255), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("session_id", sa.String(length=255), nullable=True),
        sa.Column("request_id", sa.String(length=255), nullable=True),
        sa.Column("client_id", sa.String(length=255), nullable=True),
        sa.Column("user_sub", sa.String(length=255), nullable=True),
        sa.Column("caller_kind", sa.String(length=64), nullable=True),
        sa.Column("identity", _JSON, nullable=True),
        sa.Column("server_version", sa.String(length=64), nullable=True),
        sa.Column("args_size", sa.Integer(), nullable=True),
        sa.Column("result_size", sa.Integer(), nullable=True),
        sa.Column("args", _JSON, nullable=True),
        sa.Column("result", _JSON, nullable=True),
        sa.Column("extra", _JSON, nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    for col in _INDEXED:
        op.create_index(f"ix_ffb_tool_calls_{col}", "ffb_tool_calls", [col], unique=False)


def downgrade() -> None:
    for col in _INDEXED:
        op.drop_index(f"ix_ffb_tool_calls_{col}", table_name="ffb_tool_calls")
    op.drop_table("ffb_tool_calls")
