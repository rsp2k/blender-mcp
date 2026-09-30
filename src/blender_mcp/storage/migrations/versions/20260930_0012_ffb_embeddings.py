"""ffb_embeddings for fastmcp-feedback 2026.9.30 similarity search.

EmbeddingSink stores one vector per feedback report, failed tool call,
event and (with capture_llm_text) chat LLM call, keyed by
(source_type, source_id, model). 1024 dims matches mxbai-embed-large on
the gpu gateway; a different model needs a new column size.

Needs the pgvector extension, which postgres:16-alpine does not ship: the
database must run the Alpine+pgvector image before this upgrade. The
downgrade drops the table but leaves the extension, since other objects
may come to depend on it.

Postgres only. On any other dialect this revision does nothing.

Revision ID: 20260930_0012
Revises: 20260929_0011
Create Date: 2026-09-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import VECTOR

revision: str = "20260930_0012"
down_revision: str | None = "20260929_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EMBEDDING_DIM = 1024
_INDEXED = ("created_at", "source_id", "source_type")


def _is_postgres() -> bool:
    # get_context() works offline (--sql) too, where there is no bind.
    return op.get_context().dialect.name == "postgresql"


def upgrade() -> None:
    if not _is_postgres():
        return
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "ffb_embeddings",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("source_type", sa.String(length=16), nullable=False),
        sa.Column("source_id", sa.String(length=255), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("dim", sa.Integer(), nullable=False),
        sa.Column("text_hash", sa.String(length=64), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("embedding", VECTOR(EMBEDDING_DIM), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_type", "source_id", "model", name="ffb_embeddings_source_uq"),
    )
    op.create_index(
        "ffb_embeddings_hnsw",
        "ffb_embeddings",
        ["embedding"],
        unique=False,
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
    for col in _INDEXED:
        op.create_index(f"ix_ffb_embeddings_{col}", "ffb_embeddings", [col], unique=False)


def downgrade() -> None:
    if not _is_postgres():
        return
    op.drop_table("ffb_embeddings")
