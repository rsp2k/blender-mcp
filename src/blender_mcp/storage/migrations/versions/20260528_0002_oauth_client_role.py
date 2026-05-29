"""Add oauth_client_role table for persistent role attribution.

Mirror of the in-memory client_role._role_by_client_id dict so that
role attribution survives server restarts. DCR-capture middleware
writes here; startup hook rehydrates the cache.

Revision ID: 20260528_0002
Revises: 20260527_0001
Create Date: 2026-05-28
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260528_0002"
down_revision: Union[str, None] = "20260527_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "oauth_client_role",
        sa.Column("client_id", sa.String(128), primary_key=True),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("software_id", sa.String(128), nullable=True),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )


def downgrade() -> None:
    op.drop_table("oauth_client_role")
