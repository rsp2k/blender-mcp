"""Alembic environment for Phase I shared-bus schema.

Synchronous mode (Alembic's default) — at migration time we don't need
async, and using the sync ``psycopg`` driver here keeps the migration
runner simple. The runtime engine in ``storage/__init__.py`` still uses
``asyncpg`` for async ops.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from blender_mcp.storage import Base  # populated import of all models

# Alembic Config object — reads .ini if present, otherwise we set fields
# programmatically below.
config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Inject DATABASE_URL → sqlalchemy.url so the migration runner uses the
# same connection string the runtime does. Convert the asyncpg scheme
# (used at runtime) to psycopg (sync, what Alembic wants).
db_url = os.getenv("DATABASE_URL", "")
if db_url:
    if db_url.startswith("postgresql+asyncpg://"):
        db_url = "postgresql+psycopg://" + db_url[len("postgresql+asyncpg://"):]
    elif db_url.startswith("postgresql://"):
        db_url = "postgresql+psycopg://" + db_url[len("postgresql://"):]
    config.set_main_option("sqlalchemy.url", db_url)

target_metadata = Base.metadata

# Tables owned by external libraries that live in the same DB but aren't
# part of our SQLAlchemy models. Autogenerate would otherwise try to drop
# them on every revision (FastMCP's PostgreSQLStore auto-creates these).
_EXTERNALLY_MANAGED_TABLES = {
    "oauth_kv_store",  # FastMCP OIDCProxy persistence (JTI, refresh, DCR, ...)
}


def _include_object(obj, name, type_, reflected, compare_to):
    """Exclude externally-managed tables from autogenerate."""
    if type_ == "table" and name in _EXTERNALLY_MANAGED_TABLES:
        return False
    return True


def run_migrations_offline() -> None:
    """Render SQL without an actual DB connection (for review/audit)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=_include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Apply migrations against the live DB."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=_include_object,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
