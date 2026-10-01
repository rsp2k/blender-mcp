# ruff: noqa: F811  (cfg/addon_identity are pytest fixtures imported from test_chat_flow)
"""Free trial on the server's default backend (CHAT_TRIAL_TURNS), then the
account's own Claude key, verified with Anthropic before it is stored.

Contract: docs-site/src/content/docs/reference/chat-protocol.mdx. The chat
harness and fake model endpoints come from test_chat_flow.
"""

import asyncio
import importlib.util
import json
from dataclasses import replace
from pathlib import Path

import httpx2
import pytest
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_chat_flow import (  # noqa: F401
    USER,
    FakeModels,
    Harness,
    addon_identity,
    an_text,
    cfg,
    sessions,
)

from blender_mcp.chat import settings as chat_settings
from blender_mcp.chat import trial
from blender_mcp.chat.config import DEFAULT_ANTHROPIC_MODEL, ChatConfig, load_config
from blender_mcp.chat.routing import RoutingSamplingHandler
from blender_mcp.storage.models import ChatSettings, ChatTrialUsage

ROOT = Path(__file__).resolve().parents[1]
SERVER_KEY = "sk-ant-server-" + "s" * 24
USER_KEY = "sk-ant-user-" + "u" * 24
ENDED_HINT = ("Your free messages are used up. Paste your Claude API key in the "
              "Chat panel to keep going.")
INVALID_DETAIL = ("Anthropic didn't accept that key. Check it at "
                  "console.anthropic.com/settings/keys.")


@pytest.fixture
async def trial_sessions(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'trial.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(ChatSettings.__table__.create)
        await conn.run_sync(ChatTrialUsage.__table__.create)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _prod(cfg: ChatConfig, **kw) -> ChatConfig:
    """Production's shape: Claude Haiku on the server's key, Fable advisor."""
    return replace(cfg, default_provider="anthropic", default_model="claude-haiku-4-5",
                   anthropic_api_key=SERVER_KEY, anthropic_advisor="claude-fable-5-1", **kw)


async def _call(client, tool, args=None) -> dict:
    res = await client.call_tool(tool, args or {})
    return json.loads(res.content[0].text)


# ---- trial off: today's behaviour --------------------------------------------------

async def test_no_trial_configured_means_null_and_no_trial_table(cfg, sessions):
    # ``sessions`` has no chat_trial_usage table: with CHAT_TRIAL_TURNS=0 the
    # server must never touch it.
    models = FakeModels(an_text("one"), an_text("two"))
    h = Harness(_prod(cfg), models, sessions)
    async with h.client() as client:
        got = await _call(client, "blender_get_chat_backend")
        outs = [await h.chat(client), await h.chat(client)]
    assert got["trial"] is None and got["backend"]["source"] == "server"
    assert [o["status"] for o in outs] == ["ok", "ok"]
    assert all(o["trial"] is None for o in outs)


async def test_no_trial_keeps_the_gateway_user_list_strict(cfg, sessions):
    h = Harness(_prod(cfg, gateway_users=frozenset({"someone-else"})), FakeModels(), sessions)
    async with h.client() as client:
        out = await h.chat(client)
    assert out["status"] == "no_backend" and out["trial"] is None
    assert h.models.requests == []


def test_config_reads_trial_turns():
    assert load_config({}).trial_turns == 0
    assert load_config({"CHAT_TRIAL_TURNS": ""}).trial_turns == 0
    assert load_config({"CHAT_TRIAL_TURNS": "20"}).trial_turns == 20
    assert load_config({"CHAT_TRIAL_TURNS": "-3"}).trial_turns == 0
    assert load_config({"CHAT_TRIAL_TURNS": "lots"}).trial_turns == 0


# ---- the trial ---------------------------------------------------------------------

async def test_trial_counts_down_then_ends_without_a_model_call(cfg, trial_sessions):
    models = FakeModels(an_text("one"), an_text("two"), an_text("never sent"))
    h = Harness(_prod(cfg, trial_turns=2), models, trial_sessions)
    async with h.client() as client:
        before = await _call(client, "blender_get_chat_backend")
        first = await h.chat(client)
        second = await h.chat(client)
        third = await h.chat(client)
        after = await _call(client, "blender_get_chat_backend")

    assert before["trial"] == {"limit": 2, "used": 0, "remaining": 2}
    assert first["status"] == "ok" and first["reply"] == "one"
    assert first["trial"] == {"limit": 2, "used": 1, "remaining": 1}
    assert second["trial"] == {"limit": 2, "used": 2, "remaining": 0}
    assert third == {"status": "trial_ended", "trial": {"limit": 2, "used": 2, "remaining": 0},
                     "hint": ENDED_HINT}
    assert len(models.requests) == 2  # the third turn never reached Claude
    assert all(r["headers"]["x-api-key"] == SERVER_KEY for r in models.requests)
    assert after["trial"] == {"limit": 2, "used": 2, "remaining": 0}


async def test_a_turn_that_fails_after_starting_still_counts(cfg, trial_sessions):
    refused = httpx2.Response(400, json={"type": "error", "error": {
        "type": "invalid_request_error", "message": "nope"}})
    h = Harness(_prod(cfg, trial_turns=3), FakeModels(refused), trial_sessions)
    async with h.client() as client:
        out = await h.chat(client)
    assert out["status"] == "backend_error"
    assert out["trial"] == {"limit": 3, "used": 1, "remaining": 2}


async def test_trial_replaces_no_backend_for_unlisted_accounts(cfg, trial_sessions):
    models = FakeModels(an_text("hello"))
    h = Harness(_prod(cfg, trial_turns=1, gateway_users=frozenset({"someone-else"})),
                models, trial_sessions)
    async with h.client() as client:
        out = await h.chat(client)
        again = await h.chat(client)
    assert out["status"] == "ok" and out["trial"] == {"limit": 1, "used": 1, "remaining": 0}
    assert again["status"] == "trial_ended"


async def test_listed_accounts_are_unlimited(cfg, trial_sessions):
    models = FakeModels(an_text("a"), an_text("b"), an_text("c"))
    h = Harness(_prod(cfg, trial_turns=1, gateway_users=frozenset({USER})), models, trial_sessions)
    async with h.client() as client:
        got = await _call(client, "blender_get_chat_backend")
        outs = [await h.chat(client) for _ in range(3)]
    assert got["trial"] is None
    assert [(o["status"], o["trial"]) for o in outs] == [("ok", None)] * 3
    async with trial_sessions() as s:
        assert await s.get(ChatTrialUsage, USER) is None


async def test_own_key_lifts_the_limit(cfg, trial_sessions):
    models = FakeModels(an_text("free one"), an_text("on my key"), an_text("and again"))
    h = Harness(_prod(cfg, trial_turns=1), models, trial_sessions)
    async with h.client() as client:
        assert (await h.chat(client))["status"] == "ok"
        assert (await h.chat(client))["status"] == "trial_ended"
        saved = await _call(client, "blender_set_chat_backend",
                            {"provider": "anthropic", "api_key": USER_KEY})
        outs = [await h.chat(client), await h.chat(client)]

    assert saved["status"] == "ok" and saved["trial"] is None
    assert saved["backend"]["source"] == "user"
    assert [(o["status"], o["trial"]) for o in outs] == [("ok", None), ("ok", None)]
    assert [r["headers"]["x-api-key"] for r in models.requests] == [SERVER_KEY, USER_KEY, USER_KEY]


async def test_own_openai_endpoint_is_unlimited(cfg, trial_sessions):
    h = Harness(_prod(cfg, trial_turns=1), FakeModels(), trial_sessions)
    async with h.client() as client:
        saved = await _call(client, "blender_set_chat_backend", {
            "provider": "openai", "base_url": "http://ollama.internal:11434/v1", "model": "llama3"})
        outs = [await h.chat(client), await h.chat(client)]
    assert saved["trial"] is None
    assert [(o["status"], o["trial"]) for o in outs] == [("ok", None), ("ok", None)]
    assert h.models.key_checks == []  # only Claude keys are checked


async def test_trial_turns_are_spent_atomically(cfg, trial_sessions):
    c = _prod(cfg, trial_turns=3)
    results = await asyncio.gather(*[trial.consume(trial_sessions, c, "racer") for _ in range(8)])
    assert sum(granted for granted, _ in results) == 3
    assert await trial.used(trial_sessions, "racer") == 3
    assert (await trial.consume(trial_sessions, c, "racer"))[0] is False


async def test_lowered_limit_never_shows_more_used_than_allowed(cfg, trial_sessions):
    async with trial_sessions() as s:
        s.add(ChatTrialUsage(user_sub="old", turns_used=9))
        await s.commit()
    c = _prod(cfg, trial_turns=5)
    assert await trial.status(trial_sessions, c, "old") == {"limit": 5, "used": 5, "remaining": 0}
    assert (await trial.consume(trial_sessions, c, "old"))[0] is False


async def test_only_blender_chat_may_spend_trial_turns(cfg, trial_sessions):
    c = _prod(cfg, trial_turns=2)
    handler = RoutingSamplingHandler(session_factory=trial_sessions, config_loader=lambda: c)
    # The sampling fallback (no turn in progress) resolves without trial_ok.
    assert await handler.resolve("trial-user") is None
    b = await handler.resolve("trial-user", trial_ok=True)
    assert (b.provider, b.model, b.api_key) == ("anthropic", "claude-haiku-4-5", SERVER_KEY)


def test_who_is_on_the_trial():
    c = ChatConfig(trial_turns=5, gateway_users=frozenset({"vip"}))
    gw = ChatSettings(user_sub="u", provider="gateway", advisor="off")
    own = ChatSettings(user_sub="u", provider="anthropic", api_key_enc="enc")
    oa = ChatSettings(user_sub="u", provider="openai", base_url="http://x/v1", model="m")
    assert chat_settings.on_trial(None, c, "u")
    assert chat_settings.on_trial(gw, c, "u")  # an advisor-only row still uses the server's key
    assert not chat_settings.on_trial(own, c, "u")
    assert not chat_settings.on_trial(oa, c, "u")
    assert not chat_settings.on_trial(None, c, "vip")
    assert not chat_settings.on_trial(None, c, None)
    assert not chat_settings.on_trial(None, replace(c, trial_turns=0), "u")


# ---- key verification --------------------------------------------------------------

async def test_good_key_is_checked_then_stored(cfg, trial_sessions):
    h = Harness(_prod(cfg, trial_turns=2), FakeModels(), trial_sessions)
    async with h.client() as client:
        saved = await _call(client, "blender_set_chat_backend",
                            {"provider": "anthropic", "api_key": f"  {USER_KEY}  "})
        # Changing only the model keeps the key and doesn't check it again.
        moved = await _call(client, "blender_set_chat_backend",
                            {"provider": "anthropic", "model": "claude-sonnet-5"})
    assert saved["status"] == "ok" and saved["backend"]["has_key"] is True
    assert moved["status"] == "ok" and moved["backend"]["model"] == "claude-sonnet-5"
    assert len(h.models.key_checks) == 1
    check = h.models.key_checks[0]
    assert check["url"] == "https://api.anthropic.com/v1/models"
    assert check["headers"]["x-api-key"] == USER_KEY
    assert check["headers"]["anthropic-version"] == "2023-06-01"
    assert h.models.requests == []  # no model call, no tokens spent
    async with trial_sessions() as s:
        row = await s.get(ChatSettings, USER)
    assert chat_settings.decrypt(h.cfg, row.api_key_enc) == USER_KEY


@pytest.mark.parametrize("status", [401, 403])
async def test_rejected_key_is_not_stored(cfg, trial_sessions, status):
    h = Harness(_prod(cfg, trial_turns=2), FakeModels(), trial_sessions)
    h.models.key_status = status
    async with h.client() as client:
        out = await _call(client, "blender_set_chat_backend",
                          {"provider": "anthropic", "api_key": USER_KEY})
        got = await _call(client, "blender_get_chat_backend")
    assert out == {"status": "error", "error": "invalid_api_key", "detail": INVALID_DETAIL,
                   "trial": {"limit": 2, "used": 0, "remaining": 2}}
    assert got["backend"]["source"] == "server"
    async with trial_sessions() as s:
        assert await s.get(ChatSettings, USER) is None


async def test_rejected_key_keeps_the_previous_one(cfg, sessions):
    h = Harness(cfg, FakeModels(), sessions)
    async with h.client() as client:
        await _call(client, "blender_set_chat_backend", {"provider": "anthropic", "api_key": USER_KEY})
        h.models.key_status = 401
        out = await _call(client, "blender_set_chat_backend",
                          {"provider": "anthropic", "api_key": "sk-ant-typo"})
    assert out["error"] == "invalid_api_key" and out["trial"] is None
    async with sessions() as s:
        row = await s.get(ChatSettings, USER)
    assert chat_settings.decrypt(cfg, row.api_key_enc) == USER_KEY


@pytest.mark.parametrize("exc", [httpx2.ReadTimeout, httpx2.ConnectError])
async def test_unreachable_anthropic_stores_nothing(cfg, trial_sessions, exc):
    h = Harness(_prod(cfg, trial_turns=2), FakeModels(), trial_sessions)

    def fail(request):
        raise exc("no answer", request=request)

    h.handler.anthropic_transport = httpx2.MockTransport(fail)
    async with h.client() as client:
        out = await _call(client, "blender_set_chat_backend",
                          {"provider": "anthropic", "api_key": USER_KEY})
    assert out["status"] == "error" and out["error"] == "key_check_failed"
    assert out["detail"] and USER_KEY not in out["detail"]
    assert out["trial"] == {"limit": 2, "used": 0, "remaining": 2}
    async with trial_sessions() as s:
        assert await s.get(ChatSettings, USER) is None


async def test_unexpected_answer_is_a_failed_check(cfg, sessions):
    h = Harness(cfg, FakeModels(), sessions)
    h.models.key_status = 529
    async with h.client() as client:
        out = await _call(client, "blender_set_chat_backend",
                          {"provider": "anthropic", "api_key": USER_KEY})
    assert out["error"] == "key_check_failed" and "529" in out["detail"]


async def test_local_refusals_come_before_the_key_check(cfg, sessions):
    h = Harness(replace(cfg, secret_key=""), FakeModels(), sessions)
    async with h.client() as client:
        out = await _call(client, "blender_set_chat_backend",
                          {"provider": "anthropic", "api_key": USER_KEY})
    assert out["error"] == "secret_key_not_configured"
    assert h.models.key_checks == []


# ---- default model for an account's own Claude key --------------------------------

def _row(model=None, advisor=None):
    return ChatSettings(user_sub="u", provider="anthropic", model=model, advisor=advisor,
                        api_key_enc=None)


def test_own_key_defaults_to_the_servers_claude_model():
    c = _prod(ChatConfig(secret_key=Fernet.generate_key().decode()))
    row = _row()
    assert chat_settings.backend_for(row, c, "u").model == "claude-haiku-4-5"
    view = chat_settings.public_view(row, c)
    assert view["model"] == "claude-haiku-4-5" and view["source"] == "user"
    # A model the account chose wins.
    assert chat_settings.backend_for(_row("claude-sonnet-5"), c, "u").model == "claude-sonnet-5"


def test_own_key_default_model_without_a_claude_server_default():
    gateway = ChatConfig(default_model="gemma4")
    assert chat_settings.backend_for(_row(), gateway, "u").model == DEFAULT_ANTHROPIC_MODEL
    claude_no_model = ChatConfig(default_provider="anthropic", anthropic_api_key="k")
    assert chat_settings.backend_for(_row(), claude_no_model, "u").model == DEFAULT_ANTHROPIC_MODEL


def test_own_key_gets_the_same_advisor_default():
    c = _prod(ChatConfig())
    b = chat_settings.backend_for(_row(), c, "u")
    assert (b.model, b.advisor) == ("claude-haiku-4-5", "claude-fable-5-1")
    view = chat_settings.public_view(_row(), c)
    assert view["advisor"] == view["advisor_default"] == "claude-fable-5-1"
    assert chat_settings.backend_for(_row(advisor="off"), c, "u").advisor == ""
    assert chat_settings.backend_for(_row(advisor="claude-opus-5"), c, "u").advisor == "claude-opus-5"


async def test_saved_key_turn_uses_the_default_model_and_advisor(cfg, trial_sessions):
    models = FakeModels(an_text("on my key"))
    h = Harness(_prod(cfg, trial_turns=1), models, trial_sessions)
    async with h.client() as client:
        saved = await _call(client, "blender_set_chat_backend",
                            {"provider": "anthropic", "api_key": USER_KEY})
        out = await h.chat(client)
    assert saved["backend"]["model"] == "claude-haiku-4-5"
    assert saved["backend"]["advisor"] == "claude-fable-5-1"
    assert out["backend"] == {"provider": "anthropic", "model": "claude-haiku-4-5"}
    body = models.requests[0]["body"]
    assert body["model"] == "claude-haiku-4-5"
    advisor_tools = [t for t in body.get("tools", []) if t.get("type") == "advisor_20260301"]
    assert advisor_tools and advisor_tools[0]["model"] == "claude-fable-5-1"


# ---- migration -----------------------------------------------------------------------

def test_migration_0013_is_the_single_head_after_0012():
    config = Config()
    config.set_main_option("script_location", str(ROOT / "src/blender_mcp/storage/migrations"))
    script = ScriptDirectory.from_config(config)
    assert script.get_heads() == ["20261001_0013"]
    assert script.get_revision("20261001_0013").down_revision == "20260930_0012"


def test_migration_0013_upgrades_and_downgrades(tmp_path):
    path = ROOT / "src/blender_mcp/storage/migrations/versions/20261001_0013_chat_trial_usage.py"
    spec = importlib.util.spec_from_file_location("mig_0013", path)
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)
    engine = create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            mig.upgrade()
        cols = {c["name"]: c for c in inspect(conn).get_columns("chat_trial_usage")}
        assert set(cols) == {"user_sub", "turns_used", "updated_at"}
        assert inspect(conn).get_pk_constraint("chat_trial_usage")["constrained_columns"] == ["user_sub"]
        conn.execute(text("INSERT INTO chat_trial_usage (user_sub, updated_at) "
                          "VALUES ('u', CURRENT_TIMESTAMP)"))
        assert conn.execute(text("SELECT turns_used FROM chat_trial_usage")).scalar() == 0
        with Operations.context(MigrationContext.configure(conn)):
            mig.downgrade()
        assert "chat_trial_usage" not in inspect(conn).get_table_names()
    engine.dispose()
