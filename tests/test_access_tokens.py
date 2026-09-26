"""Personal access tokens: create, verify, expire, revoke, mint refusal, OAuth routing."""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from blender_mcp import access_tokens as pat
from blender_mcp.storage.models import PersonalAccessToken


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def db():
    async def setup():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(PersonalAccessToken.__table__.create)
        return async_sessionmaker(engine, expire_on_commit=False)

    pat.clear_cache()
    pat._last_used_written.clear()
    return run(setup())


def test_create_then_verify(db):
    async def go():
        async with db() as s:
            token, row = await pat.create_token(s, "sub-alice", "ci", 90)
        assert token.startswith("bmcp_") and len(token) > 40
        assert row.token_hash != token and row.token_prefix == token[:13]
        access = await pat.verify_pat(token, session_factory=db)
        assert access.claims["sub"] == "sub-alice"
        assert access.client_id == f"pat:{row.id}"
        assert access.expires_at is not None
    run(go())


def test_unknown_and_non_pat_tokens_rejected(db):
    async def go():
        assert await pat.verify_pat("bmcp_" + "x" * 43, session_factory=db) is None
        assert await pat.verify_pat("eyJhbGciOi.a.b", session_factory=db) is None
    run(go())


def test_expired_token_rejected(db):
    async def go():
        async with db() as s:
            token, row = await pat.create_token(s, "sub-alice", "ci", 1)
            row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
            await s.commit()
        assert await pat.verify_pat(token, session_factory=db) is None
    run(go())


def test_revoke_takes_effect_and_is_owner_scoped(db):
    async def go():
        async with db() as s:
            token, row = await pat.create_token(s, "sub-alice", "ci", 90)
        assert await pat.verify_pat(token, session_factory=db) is not None
        async with db() as s:
            assert await pat.revoke_token(s, row.token_prefix, user_sub="sub-mallory") is None
        async with db() as s:
            assert await pat.revoke_token(s, row.token_prefix, user_sub="sub-alice") is not None
        assert await pat.verify_pat(token, session_factory=db) is None
    run(go())


def test_verification_is_cached(db):
    calls = {"n": 0}

    def counting_factory():
        calls["n"] += 1
        return db()

    async def go():
        async with db() as s:
            token, _ = await pat.create_token(s, "sub-alice", "ci", 90)
        for _ in range(5):
            assert await pat.verify_pat(token, session_factory=counting_factory) is not None
        assert calls["n"] == 1
    run(go())


def test_oauth_tokens_route_to_oauth_verifier_unchanged(db):
    seen = []

    async def oauth_verify(token):
        seen.append(token)
        return "oauth-result"

    async def go():
        jwt_like = "eyJhbGciOiJSUzI1NiJ9.payload.sig"
        assert await pat.verify_with_pat_first(jwt_like, oauth_verify) == "oauth-result"
        assert seen == [jwt_like]
        # A PAT never reaches the OAuth verifier.
        await pat.verify_with_pat_first("bmcp_" + "y" * 43, oauth_verify)
        assert seen == [jwt_like]
    run(go())


def test_pat_caller_resolves_to_llm_client_and_owner(monkeypatch):
    from fastmcp.server.auth import AccessToken

    from blender_mcp import bus_tools, client_role

    access = AccessToken(token="bmcp_" + "z" * 43, client_id="pat:123", scopes=[], claims={"sub": "sub-alice"})
    monkeypatch.setattr("fastmcp.server.dependencies.get_access_token", lambda: access)
    monkeypatch.setattr(client_role, "get_access_token", lambda: access)
    assert client_role.get_caller_role() == "llm-client"
    assert bus_tools._resolve_user_id(None) == "sub-alice"
    assert pat.caller_is_pat() is True


def test_minting_refused_for_pat_caller(monkeypatch):
    from blender_mcp.access_token_tools import BlenderAccessTokenComponent

    monkeypatch.setattr(pat, "caller_is_pat", lambda: True)
    comp = BlenderAccessTokenComponent()
    for coro in (
        comp.create_access_token(name="x"),
        comp.list_access_tokens(),
        comp.revoke_access_token("bmcp_abc"),
    ):
        assert json.loads(run(coro))["error"] == "oauth_required"
