"""Add chat_settings.advisor: the model an account's chat may escalate to.

NULL follows the server's CHAT_ANTHROPIC_ADVISOR; "off" turns it off.

Revision ID: 20260929_0010
Revises: 20260928_0009
Create Date: 2026-09-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260929_0010"
down_revision: str | None = "20260928_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("chat_settings", sa.Column("advisor", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("chat_settings", "advisor")
