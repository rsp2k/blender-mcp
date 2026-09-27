"""Add ffb_feedback_call_links for fastmcp-feedback correlation.

Schema from fastmcp_feedback.instrumentation.build_metadata(prefix="ffb_")
(fastmcp-feedback 2026.9.27.3). Links a feedback shortcode (bug-XXXX etc.)
to the tool calls that preceded it. No foreign key to ffb_tool_calls on
purpose: a link can be written before its call record leaves the queue.

Revision ID: 20260927_0008
Revises: 20260927_0007
Create Date: 2026-09-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260927_0008"
down_revision: str | None = "20260927_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ffb_feedback_call_links",
        sa.Column("feedback_ref", sa.String(length=255), nullable=False),
        sa.Column("call_id", sa.String(length=36), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("rule", sa.String(length=32), nullable=False),
        sa.Column("linked_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("feedback_ref", "call_id"),
    )
    op.create_index(
        "ix_ffb_feedback_call_links_call_id", "ffb_feedback_call_links", ["call_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_ffb_feedback_call_links_call_id", table_name="ffb_feedback_call_links")
    op.drop_table("ffb_feedback_call_links")
