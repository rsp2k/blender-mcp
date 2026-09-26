"""Personal access tokens: static bearer auth for CI and scripted MCP clients.

A token looks like ``bmcp_<43 urlsafe chars>``. Only its sha256 is stored
(table ``access_token``). The auth provider checks the ``bmcp_`` prefix
before handing anything to OIDCProxy, so OAuth-issued tokens never touch
this module. A verified token becomes an AccessToken whose claims carry
the owner's ``sub`` and whose client_id is ``pat:<token id>``; that id is
never in the role registry, so the caller resolves to ``llm-client``.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select, update

from .storage.models import PersonalAccessToken

logger = logging.getLogger(__name__)

PAT_PREFIX = "bmcp_"
PAT_CLIENT_ID_PREFIX = "pat:"
DEFAULT_EXPIRES_DAYS = 90
MAX_EXPIRES_DAYS = 366
# Verified tokens are cached this long; a revoke takes effect within it.
CACHE_TTL_S = 30.0
LAST_USED_WRITE_INTERVAL_S = 60.0

# token_hash -> (cached_at, AccessToken or None for a negative result)
_cache: dict[str, tuple[float, object]] = {}
_last_used_written: dict[uuid.UUID, float] = {}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def is_pat(token: Optional[str]) -> bool:
    return bool(token) and token.startswith(PAT_PREFIX)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def generate_token() -> str:
    return PAT_PREFIX + secrets.token_urlsafe(32)


def display_prefix(token: str) -> str:
    return token[: len(PAT_PREFIX) + 8]


def clear_cache() -> None:
    _cache.clear()


def caller_is_pat() -> bool:
    """True when the current request authenticated with a personal access token."""
    try:
        from fastmcp.server.dependencies import get_access_token

        access = get_access_token()
    except Exception:
        return False
    if access is None:
        return False
    return is_pat(getattr(access, "token", None)) or str(
        getattr(access, "client_id", "") or ""
    ).startswith(PAT_CLIENT_ID_PREFIX)


# ---- repository -----------------------------------------------------------


async def create_token(
    session, user_sub: str, name: str, expires_days: Optional[int]
) -> tuple[str, PersonalAccessToken]:
    """Create and persist a token. Returns (plaintext, row). expires_days=None never expires."""
    token = generate_token()
    row = PersonalAccessToken(
        id=uuid.uuid4(),
        user_sub=user_sub,
        name=name.strip()[:100] or "token",
        token_prefix=display_prefix(token),
        token_hash=hash_token(token),
        created_at=_utcnow(),
        expires_at=None if expires_days is None else _utcnow() + timedelta(days=expires_days),
    )
    session.add(row)
    await session.commit()
    return token, row


async def list_tokens(session, user_sub: Optional[str] = None) -> list[PersonalAccessToken]:
    stmt = select(PersonalAccessToken).order_by(PersonalAccessToken.created_at.desc())
    if user_sub is not None:
        stmt = stmt.where(PersonalAccessToken.user_sub == user_sub)
    return list((await session.execute(stmt)).scalars())


async def revoke_token(session, id_or_prefix: str, user_sub: Optional[str] = None) -> Optional[PersonalAccessToken]:
    """Revoke by id or display prefix. user_sub limits it to that owner's tokens."""
    rows = await list_tokens(session, user_sub)
    key = id_or_prefix.strip()
    matches = [r for r in rows if str(r.id) == key or r.token_prefix == key]
    if len(matches) != 1:
        return None
    row = matches[0]
    if row.revoked_at is None:
        row.revoked_at = _utcnow()
        await session.commit()
    clear_cache()
    return row


def to_dict(row: PersonalAccessToken) -> dict:
    def iso(dt):
        return dt.isoformat() if dt else None

    now = _utcnow()
    if row.revoked_at:
        status = "revoked"
    elif row.expires_at and row.expires_at <= now:
        status = "expired"
    else:
        status = "active"
    return {
        "id": str(row.id),
        "name": row.name,
        "prefix": row.token_prefix,
        "status": status,
        "created_at": iso(row.created_at),
        "expires_at": iso(row.expires_at),
        "last_used_at": iso(row.last_used_at),
        "revoked_at": iso(row.revoked_at),
    }


# ---- verification ---------------------------------------------------------


async def lookup_active(session, token: str) -> Optional[PersonalAccessToken]:
    row = (
        await session.execute(
            select(PersonalAccessToken).where(PersonalAccessToken.token_hash == hash_token(token))
        )
    ).scalar_one_or_none()
    if row is None or row.revoked_at is not None:
        return None
    expires = row.expires_at
    if expires is not None:
        if expires.tzinfo is None:  # SQLite in tests drops tzinfo
            expires = expires.replace(tzinfo=timezone.utc)
        if expires <= _utcnow():
            return None
    return row


async def _touch_last_used(session, row: PersonalAccessToken) -> None:
    now = time.monotonic()
    if now - _last_used_written.get(row.id, 0.0) < LAST_USED_WRITE_INTERVAL_S:
        return
    _last_used_written[row.id] = now
    await session.execute(
        update(PersonalAccessToken)
        .where(PersonalAccessToken.id == row.id)
        .values(last_used_at=_utcnow())
    )
    await session.commit()


async def verify_with_pat_first(token: str, oauth_verify):
    """Route a bearer: personal access tokens here, everything else to
    ``oauth_verify`` (OIDCProxy.verify_token) with the token unchanged."""
    if is_pat(token):
        return await verify_pat(token)
    return await oauth_verify(token)


async def verify_pat(token: str, session_factory=None):
    """Return an AccessToken for a valid PAT, else None. Never raises."""
    from fastmcp.server.auth import AccessToken

    if not is_pat(token):
        return None
    key = hash_token(token)
    cached = _cache.get(key)
    if cached and time.monotonic() - cached[0] < CACHE_TTL_S:
        return cached[1]

    if session_factory is None:
        from .storage import get_session as session_factory
    try:
        async with session_factory() as s:
            row = await lookup_active(s, token)
            result = None
            if row is not None:
                expires_at = None
                if row.expires_at is not None:
                    exp = row.expires_at
                    if exp.tzinfo is None:
                        exp = exp.replace(tzinfo=timezone.utc)
                    expires_at = int(exp.timestamp())
                result = AccessToken(
                    token=token,
                    client_id=f"{PAT_CLIENT_ID_PREFIX}{row.id}",
                    scopes=[],
                    expires_at=expires_at,
                    claims={"sub": row.user_sub, "pat_id": str(row.id), "pat_name": row.name},
                )
                try:
                    await _touch_last_used(s, row)
                except Exception as e:
                    logger.warning("PAT last_used update failed: %s", e)
    except Exception as e:
        logger.warning("PAT verification failed: %s", e)
        return None

    _cache[key] = (time.monotonic(), result)
    return result
