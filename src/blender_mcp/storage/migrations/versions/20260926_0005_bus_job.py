"""Add bus_job table for resumable dispatch jobs.

A dispatch that outlives its wait becomes a background job: the reply is
persisted here when it arrives (including after a server restart) and the
caller polls blender_job_status / blender_job_result. Rows expire after
24 h.

Revision ID: 20260926_0005
Revises: 20260926_0004
Create Date: 2026-09-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "20260926_0005"
down_revision: str | None = "20260926_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "bus_job",
        sa.Column("job_id", sa.String(32), primary_key=True),
        sa.Column("bus_id", sa.String(64), nullable=False),
        sa.Column("target_uuid", sa.String(128), nullable=False),
        sa.Column("caller_sub", sa.String(128), nullable=True),
        sa.Column("caller_client", sa.String(128), nullable=True),
        sa.Column("command", sa.String(100), nullable=False),
        sa.Column("params", JSONB, nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("result", sa.Text, nullable=True),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_bus_job_bus_id", "bus_job", ["bus_id"])
    op.create_index("ix_bus_job_status_created", "bus_job", ["status", "created_at"])
    op.create_index("ix_bus_job_expires_at", "bus_job", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_bus_job_expires_at", table_name="bus_job")
    op.drop_index("ix_bus_job_status_created", table_name="bus_job")
    op.drop_index("ix_bus_job_bus_id", table_name="bus_job")
    op.drop_table("bus_job")
