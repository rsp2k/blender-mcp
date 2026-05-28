"""Storage subsystem — Postgres-backed shared-bus state.

Exposes:

- :func:`get_engine` — process-wide async SQLAlchemy engine, lazily
  built from ``DATABASE_URL`` env. Singleton.
- :func:`get_session` — async context-manager yielding an
  ``AsyncSession`` bound to ``get_engine()``.
- :data:`Base` — DeclarativeBase for migrations to autogenerate from.

The runtime uses ``asyncpg`` (DATABASE_URL must use the
``postgresql+asyncpg://`` scheme). Alembic uses a sync driver
(``psycopg``); its ``env.py`` rewrites the URL to
``postgresql+psycopg://`` for migration operations only — the runtime
URL still uses asyncpg.
"""

from __future__ import annotations

import logging
import os
from typing import AsyncIterator, Optional

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .models import Base, Bus, BusInvitation, BusMembership, BusRole

logger = logging.getLogger(__name__)

_engine: Optional[AsyncEngine] = None
_sessionmaker: Optional[async_sessionmaker[AsyncSession]] = None


def get_database_url() -> str:
    """Resolve DATABASE_URL from env, with a localhost-dev fallback."""
    url = os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL not set. Phase I requires Postgres; "
            "see .env.example for the connection string format."
        )
    # Defensive: if the user pasted a vanilla ``postgresql://`` URL
    # (the Postgres-canonical form), rewrite to asyncpg so the runtime
    # engine actually works.
    if url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url[len("postgresql://"):]
    return url


def get_engine() -> AsyncEngine:
    """Process-wide engine, built lazily on first access."""
    global _engine, _sessionmaker
    if _engine is None:
        url = get_database_url()
        _engine = create_async_engine(
            url,
            # Echo only in dev (noisy in prod logs).
            echo=os.getenv("SQLALCHEMY_ECHO", "").lower() in ("1", "true", "yes"),
            # Modest pool — the bus is mostly cached in memory; DB hits
            # are for membership changes (rare) + startup loads.
            pool_size=5,
            max_overflow=10,
            pool_pre_ping=True,
        )
        _sessionmaker = async_sessionmaker(
            _engine, expire_on_commit=False, class_=AsyncSession
        )
        logger.info("DB engine created (url=%s)", _scrub(url))
    return _engine


def _scrub(url: str) -> str:
    """Strip password from DATABASE_URL for log output."""
    if "@" not in url or "://" not in url:
        return url
    scheme, rest = url.split("://", 1)
    if "@" in rest:
        creds, host = rest.split("@", 1)
        if ":" in creds:
            user = creds.split(":", 1)[0]
            return f"{scheme}://{user}:***@{host}"
    return url


def get_session() -> AsyncSession:
    """Return a new AsyncSession bound to the engine.

    Use as ``async with get_session() as session:`` so the session is
    properly closed + transaction released.
    """
    if _sessionmaker is None:
        get_engine()  # initializes _sessionmaker as a side effect
    assert _sessionmaker is not None
    return _sessionmaker()


__all__ = [
    "Base",
    "Bus",
    "BusInvitation",
    "BusMembership",
    "BusRole",
    "get_engine",
    "get_session",
    "get_database_url",
]
