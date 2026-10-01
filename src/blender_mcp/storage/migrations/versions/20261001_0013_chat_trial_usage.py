"""Add chat_trial_usage: free chat turns spent on the server's default backend.

One row per account that has used the trial (CHAT_TRIAL_TURNS); the turn
counter is incremented in place with a guarded UPDATE.

Revision ID: 20261001_0013
Revises: 20260930_0012
Create Date: 2026-10-01
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261001_0013"
down_revision: str | None = "20260930_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "chat_trial_usage",
        sa.Column("user_sub", sa.String(length=128), primary_key=True),
        sa.Column("turns_used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("chat_trial_usage")
