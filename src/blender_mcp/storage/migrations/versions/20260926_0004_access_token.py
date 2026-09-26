"""Add access_token table for personal access tokens (CI / scripted clients).

Tokens are "bmcp_" + urlsafe random; only the sha256 is stored. Verified
ahead of the OIDCProxy path in server_proper, resolved to the owner's
user sub with role llm-client.

Revision ID: 20260926_0004
Revises: 20260916_0003
Create Date: 2026-09-26
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "20260926_0004"
down_revision: Union[str, None] = "20260916_0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "access_token",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("user_sub", sa.String(128), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("token_prefix", sa.String(16), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_access_token_user_sub", "access_token", ["user_sub"])
    op.create_index("ux_access_token_token_hash", "access_token", ["token_hash"], unique=True)


def downgrade() -> None:
    op.drop_index("ux_access_token_token_hash", table_name="access_token")
    op.drop_index("ix_access_token_user_sub", table_name="access_token")
    op.drop_table("access_token")
