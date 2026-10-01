"""Add chat_trial_usage: what each account's free chat trial has spent.

One row per account that has used the trial (CHAT_TRIAL_USD): USD at list
prices on the server's default backend, plus running token totals. Each
model call adds its cost in place with ``used_usd = used_usd + :cost``.

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
        sa.Column("used_usd", sa.Numeric(12, 6), nullable=False, server_default="0"),
        sa.Column("tokens_in", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("tokens_out", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("chat_trial_usage")
