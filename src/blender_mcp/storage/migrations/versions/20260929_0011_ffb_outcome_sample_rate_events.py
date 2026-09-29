"""Catch the ffb_ tables up to fastmcp-feedback 2026.9.29.

Three releases since our 2026.9.27.3 pin touch the schema:
2026.9.27.4 adds ffb_tool_calls.outcome (indexed), 2026.9.28 adds
ffb_tool_calls.sample_rate, and 2026.9.29 adds ffb_events for records that
are not tool calls (llm.call from the chat backends).

Must run before the 2026.9.29 package deploys: DatabaseSink writes outcome
and sample_rate on every call, so without them every call write fails.

Revision ID: 20260929_0011
Revises: 20260929_0010
Create Date: 2026-09-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260929_0011"
down_revision: str | None = "20260929_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")
_EVENTS_INDEXED = ("call_id", "key", "kind", "occurred_at", "user_sub")


def upgrade() -> None:
    op.add_column("ffb_tool_calls", sa.Column("outcome", sa.String(length=16), nullable=True))
    op.create_index("ix_ffb_tool_calls_outcome", "ffb_tool_calls", ["outcome"], unique=False)
    op.add_column("ffb_tool_calls", sa.Column("sample_rate", sa.Float(), nullable=True))
    op.create_table(
        "ffb_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("kind", sa.String(length=128), nullable=False),
        sa.Column("key", sa.String(length=255), nullable=True),
        sa.Column("call_id", sa.String(length=36), nullable=True),
        sa.Column("session_id", sa.String(length=255), nullable=True),
        sa.Column("user_sub", sa.String(length=255), nullable=True),
        sa.Column("caller_kind", sa.String(length=64), nullable=True),
        sa.Column("client_id", sa.String(length=255), nullable=True),
        sa.Column("server_version", sa.String(length=64), nullable=True),
        sa.Column("attrs", _JSON, nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    for col in _EVENTS_INDEXED:
        op.create_index(f"ix_ffb_events_{col}", "ffb_events", [col], unique=False)


def downgrade() -> None:
    for col in _EVENTS_INDEXED:
        op.drop_index(f"ix_ffb_events_{col}", table_name="ffb_events")
    op.drop_table("ffb_events")
    op.drop_column("ffb_tool_calls", "sample_rate")
    op.drop_index("ix_ffb_tool_calls_outcome", table_name="ffb_tool_calls")
    op.drop_column("ffb_tool_calls", "outcome")
