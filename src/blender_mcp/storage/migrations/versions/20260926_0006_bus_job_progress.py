"""Add progress columns to bus_job.

Job code can call report_progress(fraction, message); the latest report
is kept on the job row and returned by blender_job_status.

Revision ID: 20260926_0006
Revises: 20260926_0005
Create Date: 2026-09-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260926_0006"
down_revision: str | None = "20260926_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("bus_job", sa.Column("progress", sa.Float, nullable=True))
    op.add_column("bus_job", sa.Column("progress_message", sa.String(500), nullable=True))
    op.add_column("bus_job", sa.Column("progress_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("bus_job", "progress_at")
    op.drop_column("bus_job", "progress_message")
    op.drop_column("bus_job", "progress")
