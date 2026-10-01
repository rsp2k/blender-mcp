# ruff: noqa: F811  (cfg/addon_identity are pytest fixtures imported from test_chat_flow)
"""Free trial on the server's default backend (a USD budget, CHAT_TRIAL_USD),
then the account's own Claude key, verified with Anthropic before it is stored.

Contract: docs-site/src/content/docs/reference/chat-protocol.mdx. The chat
harness and fake model endpoints come from test_chat_flow.
"""

import asyncio
import importlib.util
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

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
    FakeStore,
    Harness,
    addon_identity,
    an_text,
    cfg,
    oa_call,
    sessions,
)

from blender_mcp.chat import pricing, trial, vision
from blender_mcp.chat import settings as chat_settings
from blender_mcp.chat.config import DEFAULT_ANTHROPIC_MODEL, ChatConfig, load_config
from blender_mcp.chat.providers import Usage
from blender_mcp.chat.providers import anthropic as an
from blender_mcp.chat.routing import RoutingSamplingHandler
from blender_mcp.storage.models import ChatSettings, ChatTrialUsage

ROOT = Path(__file__).resolve().parents[1]
SERVER_KEY = "sk-ant-server-" + "s" * 24
USER_KEY = "sk-ant-user-" + "u" * 24
ENDED_HINT = ("Your free trial is used up. Paste your Claude API key in the "
              "Chat panel to keep going.")
INVALID_DETAIL = ("Anthropic didn't accept that key. Check it at "
                  "console.anthropic.com/settings/keys.")
BUDGET = 0.05
FRESH = {"unit": "usd", "limit": BUDGET, "used": 0.0, "remaining": BUDGET, "percent_left": 100}


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


def an_reply(text, **usage):
    """A Claude reply with the given usage."""
    return {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-haiku-4-5",
            "content": [{"type": "text", "text": text}] if text else [],
            "stop_reason": "end_turn", "stop_sequence": None, "usage": usage}


# 10,000 input + 2,000 output tokens on Haiku 4.5: 10,000 x $1 + 2,000 x $5 per
# million = $0.02.
TWO_CENTS = {"input_tokens": 10_000, "output_tokens": 2_000}


def oa_reply(text="", calls=None, prompt=1000, completion=100):
    msg = {"role": "assistant", "content": text}
    if calls:
        msg["tool_calls"] = calls["choices"][0]["message"]["tool_calls"]
    return {"choices": [{"message": msg, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": prompt, "completion_tokens": completion}}


async def _call(client, tool, args=None) -> dict:
    res = await client.call_tool(tool, args or {})
    return json.loads(res.content[0].text)


async def _row(sessions_, user=USER) -> ChatTrialUsage | None:
    async with sessions_() as s:
        return await s.get(ChatTrialUsage, user)


# ---- pricing -------------------------------------------------------------------------

PRICE_TABLE = {  # USD per million: input, cache write 5m, cache write 1h, cache read, output
    "claude-fable-5-1": (10, 12.50, 20, 0.25, 50), "claude-mythos-5-1": (10, 12.50, 20, 0.25, 50),
    "claude-fable-5": (10, 12.50, 20, 1, 50), "claude-mythos-5": (10, 12.50, 20, 1, 50),
    "claude-opus-5-5": (4, 5, 8, 0.20, 20),
    "claude-opus-5": (5, 6.25, 10, 0.50, 25), "claude-opus-4-8": (5, 6.25, 10, 0.50, 25),
    "claude-opus-4-7": (5, 6.25, 10, 0.50, 25), "claude-opus-4-6": (5, 6.25, 10, 0.50, 25),
    "claude-opus-4-5": (5, 6.25, 10, 0.50, 25),
    "claude-sonnet-5-5": (2, 2.50, 4, 0.20, 10), "claude-sonnet-5": (2, 2.50, 4, 0.20, 10),
    "claude-sonnet-4-6": (3, 3.75, 6, 0.30, 15), "claude-sonnet-4-5": (3, 3.75, 6, 0.30, 15),
    "claude-haiku-4-5": (1, 1.25, 2, 0.10, 5),
}


@pytest.mark.parametrize("model", sorted(PRICE_TABLE))
def test_price_table(model):
    r = pricing.rates_for(model)
    assert (r.input, r.cache_write_5m, r.cache_write_1h, r.cache_read, r.output) == PRICE_TABLE[model]


def test_model_ids_resolve_to_the_right_row():
    assert pricing.rates_for("claude-haiku-4-5-20251001") is pricing.rates_for("claude-haiku-4-5")
    assert pricing.rates_for("claude-haiku-4-5@20251001") is pricing.rates_for("claude-haiku-4-5")
    # Opus 5.5 is not Opus 5, and Fable 5.1 is not Fable 5.
    assert pricing.rates_for("claude-opus-5-5").input == 4
    assert pricing.rates_for("claude-fable-5-1").cache_read == 0.25
    for unknown in ("qwen3", "llama3", "claude-next-9", "", None):
        assert pricing.rates_for(unknown) is pricing.HAIKU_4_5


def test_cost_of_a_plain_claude_call():
    u = Usage(input_tokens=1000, output_tokens=500, cache_read_input_tokens=2000,
              cache_creation_input_tokens=400)
    # Haiku: 1000 x 1 + 2000 x 0.10 + 400 x 1.25 (5 minute writes) + 500 x 5 = 4200 micro-USD.
    assert pricing.cost_usd(u, "claude-haiku-4-5") == pytest.approx(0.0042)


def test_cache_writes_use_the_ttl_breakdown_when_present():
    u = Usage(cache_creation_input_tokens=300, cache_creation_5m_input_tokens=100,
              cache_creation_1h_input_tokens=200)
    # Opus 5.5: 100 x 5 + 200 x 8 = 2100 micro-USD.
    assert pricing.cost_usd(u, "claude-opus-5-5") == pytest.approx(0.0021)


def test_advisor_tokens_are_priced_at_the_advisors_rates():
    u = Usage(input_tokens=100, output_tokens=10, advisor_input_tokens=1000,
              advisor_output_tokens=100, advisor_calls=1)
    # Sonnet 5: 100 x 2 + 10 x 10 = 300; Fable 5.1: 1000 x 10 + 100 x 50 = 15000.
    assert pricing.cost_usd(u, "claude-sonnet-5", "claude-fable-5-1") == pytest.approx(0.0153)
    # No advisor model given: the executor's rates.
    assert pricing.cost_usd(u, "claude-sonnet-5") == pytest.approx((300 + 2000 + 1000) / 1e6)


def test_non_claude_calls_cost_haiku_rates_with_cached_inside_input():
    # OpenAI-style usage counts the 400 cached tokens inside the 1000 prompt tokens.
    u = Usage(input_tokens=1000, output_tokens=100, cache_read_input_tokens=400)
    # 600 x 1 + 400 x 0.10 + 100 x 5 = 1140 micro-USD.
    assert pricing.cost_usd(u, "qwen3", provider="gateway") == pytest.approx(0.00114)
    assert pricing.tokens(u, provider="gateway") == (1000, 100)
    assert pricing.cost_usd(None, "claude-opus-5") == 0.0


def test_token_totals_count_every_billed_input_token():
    u = Usage(input_tokens=10, output_tokens=5, cache_read_input_tokens=100,
              cache_creation_input_tokens=20, advisor_input_tokens=300, advisor_output_tokens=7)
    assert pricing.tokens(u) == (430, 12)


def test_anthropic_usage_reads_the_cache_write_breakdown():
    msg = SimpleNamespace(usage={"input_tokens": 1, "output_tokens": 2,
                                 "cache_creation_input_tokens": 30,
                                 "cache_creation": {"ephemeral_5m_input_tokens": 10,
                                                    "ephemeral_1h_input_tokens": 20}})
    u = an.usage_of(msg)
    assert (u.cache_creation_5m_input_tokens, u.cache_creation_1h_input_tokens) == (10, 20)
    plain = an.usage_of(SimpleNamespace(usage={"input_tokens": 1, "output_tokens": 2}))
    assert plain.cache_creation_5m_input_tokens is None


# ---- the trial view and config ----------------------------------------------------------

def test_trial_view_shape_and_rounding():
    c = ChatConfig(trial_usd=0.5)
    assert trial.view(c, 0.1234) == {"unit": "usd", "limit": 0.5, "used": 0.1234,
                                     "remaining": 0.3766, "percent_left": 75}
    assert trial.view(c, 0.123456)["used"] == 0.1235
    assert trial.view(c, 0) == {"unit": "usd", "limit": 0.5, "used": 0.0, "remaining": 0.5,
                                "percent_left": 100}
    # A turn may overshoot: used shows the real spend, remaining stops at 0.
    assert trial.view(c, 0.6) == {"unit": "usd", "limit": 0.5, "used": 0.6, "remaining": 0.0,
                                  "percent_left": 0}
    # Floor, without float noise: 0.29 left of 1.0 is 29, not 28.
    assert trial.view(ChatConfig(trial_usd=1.0), 0.71)["percent_left"] == 29
    assert trial.view(c, 0.0026)["percent_left"] == 99


def test_config_reads_trial_usd():
    assert load_config({}).trial_usd == 0
    assert load_config({"CHAT_TRIAL_USD": ""}).trial_usd == 0
    assert load_config({"CHAT_TRIAL_USD": "0.5"}).trial_usd == 0.5
    for junk in ("-1", "lots", "inf", "nan"):
        assert load_config({"CHAT_TRIAL_USD": junk}).trial_usd == 0
    assert not hasattr(load_config({}), "trial_turns")


def test_who_is_on_the_trial():
    c = ChatConfig(trial_usd=0.5, gateway_users=frozenset({"vip"}))
    gw = ChatSettings(user_sub="u", provider="gateway", advisor="off")
    own = ChatSettings(user_sub="u", provider="anthropic", api_key_enc="enc")
    oa = ChatSettings(user_sub="u", provider="openai", base_url="http://x/v1", model="m")
    assert chat_settings.on_trial(None, c, "u")
    assert chat_settings.on_trial(gw, c, "u")  # an advisor-only row still uses the server's key
    assert not chat_settings.on_trial(own, c, "u")
    assert not chat_settings.on_trial(oa, c, "u")
    assert not chat_settings.on_trial(None, c, "vip")
    assert not chat_settings.on_trial(None, c, None)
    assert not chat_settings.on_trial(None, replace(c, trial_usd=0), "u")


# ---- trial off: today's behaviour --------------------------------------------------

async def test_no_trial_configured_means_null_and_no_trial_table(cfg, sessions):
    # ``sessions`` has no chat_trial_usage table: with CHAT_TRIAL_USD=0 the
    # server must never touch it.
    models = FakeModels(an_reply("one", **TWO_CENTS), an_reply("two", **TWO_CENTS))
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


# ---- the trial budget ----------------------------------------------------------------

async def test_budget_drains_by_cost_then_ends_without_a_model_call(cfg, trial_sessions):
    models = FakeModels(*[an_reply(f"r{i}", **TWO_CENTS) for i in range(4)])
    h = Harness(_prod(cfg, trial_usd=BUDGET), models, trial_sessions)
    async with h.client() as client:
        before = await _call(client, "blender_get_chat_backend")
        outs = [await h.chat(client) for _ in range(3)]
        ended = await h.chat(client)
        after = await _call(client, "blender_get_chat_backend")

    assert before["trial"] == FRESH
    assert [o["status"] for o in outs] == ["ok", "ok", "ok"]
    assert outs[0]["trial"] == {"unit": "usd", "limit": BUDGET, "used": 0.02,
                                "remaining": 0.03, "percent_left": 60}
    assert outs[1]["trial"] == {"unit": "usd", "limit": BUDGET, "used": 0.04,
                                "remaining": 0.01, "percent_left": 20}
    # The third turn started with budget left and overshot by its own cost.
    assert outs[2]["trial"] == {"unit": "usd", "limit": BUDGET, "used": 0.06,
                                "remaining": 0.0, "percent_left": 0}
    assert ended == {"status": "trial_ended", "trial": outs[2]["trial"], "hint": ENDED_HINT}
    assert len(models.requests) == 3  # the fourth turn never reached Claude
    assert all(r["headers"]["x-api-key"] == SERVER_KEY for r in models.requests)
    assert after["trial"] == outs[2]["trial"]
    row = await _row(trial_sessions)
    assert (row.tokens_in, row.tokens_out) == (30_000, 6_000)


async def test_exactly_at_the_budget_is_ended(cfg, trial_sessions):
    await trial.spend(trial_sessions, USER, BUDGET)
    h = Harness(_prod(cfg, trial_usd=BUDGET), FakeModels(), trial_sessions)
    async with h.client() as client:
        out = await h.chat(client)
    assert out["status"] == "trial_ended" and out["trial"]["percent_left"] == 0
    assert h.models.requests == []


async def test_retries_and_failed_calls_are_charged(cfg, trial_sessions):
    # Two empty replies: the call fails, but both requests were billed.
    empty = an_reply("", **TWO_CENTS)
    h = Harness(_prod(cfg, trial_usd=BUDGET), FakeModels(empty, empty), trial_sessions)
    async with h.client() as client:
        out = await h.chat(client)
    assert out["status"] == "backend_error"
    assert out["trial"]["used"] == 0.04


async def test_vision_calls_in_a_trial_turn_are_charged(cfg, trial_sessions, monkeypatch):
    monkeypatch.setattr(vision, "get_store", lambda: FakeStore())
    models = FakeModels(oa_reply(calls=oa_call("look_at_viewport", {"question": "red?"})),
                        oa_reply("A red cube."), oa_reply("Yes."))
    # The GPU gateway as the server default: priced at Haiku rates.
    h = Harness(replace(cfg, trial_usd=BUDGET), models, trial_sessions)
    async with h.client() as client:
        out = await h.chat(client)
    assert out["status"] == "ok" and out["reply"] == "Yes."
    assert models.requests[1]["body"]["model"] == cfg.vision_model
    # Three calls of 1000 prompt + 100 completion tokens: 3 x (1000 x 1 + 100 x 5) = 4500 micro-USD.
    assert out["trial"]["used"] == 0.0045
    row = await _row(trial_sessions)
    assert (row.tokens_in, row.tokens_out) == (3000, 300)


async def test_trial_replaces_no_backend_for_unlisted_accounts(cfg, trial_sessions):
    models = FakeModels(an_reply("hello", **TWO_CENTS))
    h = Harness(_prod(cfg, trial_usd=BUDGET, gateway_users=frozenset({"someone-else"})),
                models, trial_sessions)
    async with h.client() as client:
        out = await h.chat(client)
    assert out["status"] == "ok" and out["trial"]["used"] == 0.02


async def test_listed_accounts_are_unlimited(cfg, trial_sessions):
    models = FakeModels(*[an_reply(c, **TWO_CENTS) for c in "abc"])
    h = Harness(_prod(cfg, trial_usd=0.01, gateway_users=frozenset({USER})), models, trial_sessions)
    async with h.client() as client:
        got = await _call(client, "blender_get_chat_backend")
        outs = [await h.chat(client) for _ in range(3)]
    assert got["trial"] is None
    assert [(o["status"], o["trial"]) for o in outs] == [("ok", None)] * 3
    assert await _row(trial_sessions) is None


async def test_own_key_lifts_the_limit_and_is_not_charged(cfg, trial_sessions):
    models = FakeModels(an_reply("free one", input_tokens=60_000, output_tokens=0),
                        an_reply("on my key", **TWO_CENTS), an_reply("and again", **TWO_CENTS))
    h = Harness(_prod(cfg, trial_usd=BUDGET), models, trial_sessions)
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
    assert (await _row(trial_sessions)).used_usd == pytest.approx(0.06)


async def test_own_openai_endpoint_is_unlimited(cfg, trial_sessions):
    h = Harness(_prod(cfg, trial_usd=BUDGET), FakeModels(), trial_sessions)
    async with h.client() as client:
        saved = await _call(client, "blender_set_chat_backend", {
            "provider": "openai", "base_url": "http://ollama.internal:11434/v1", "model": "llama3"})
        outs = [await h.chat(client), await h.chat(client)]
    assert saved["trial"] is None
    assert [(o["status"], o["trial"]) for o in outs] == [("ok", None), ("ok", None)]
    assert h.models.key_checks == []  # only Claude keys are checked


async def test_concurrent_spends_sum_exactly(trial_sessions):
    await asyncio.gather(*[trial.spend(trial_sessions, "racer", 0.125, 1000, 10)
                           for _ in range(8)])
    row = await _row(trial_sessions, "racer")
    assert row.used_usd == 1.0
    assert (row.tokens_in, row.tokens_out) == (8000, 80)


async def test_only_blender_chat_may_spend_the_trial(cfg, trial_sessions):
    c = _prod(cfg, trial_usd=BUDGET)
    handler = RoutingSamplingHandler(session_factory=trial_sessions, config_loader=lambda: c)
    # The sampling fallback (no turn in progress) resolves without trial_ok.
    assert await handler.resolve("trial-user") is None
    b = await handler.resolve("trial-user", trial_ok=True)
    assert (b.provider, b.model, b.api_key) == ("anthropic", "claude-haiku-4-5", SERVER_KEY)


# ---- key verification --------------------------------------------------------------

async def test_good_key_is_checked_then_stored(cfg, trial_sessions):
    h = Harness(_prod(cfg, trial_usd=BUDGET), FakeModels(), trial_sessions)
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
    h = Harness(_prod(cfg, trial_usd=BUDGET), FakeModels(), trial_sessions)
    h.models.key_status = status
    async with h.client() as client:
        out = await _call(client, "blender_set_chat_backend",
                          {"provider": "anthropic", "api_key": USER_KEY})
        got = await _call(client, "blender_get_chat_backend")
    assert out == {"status": "error", "error": "invalid_api_key", "detail": INVALID_DETAIL,
                   "trial": FRESH}
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
    h = Harness(_prod(cfg, trial_usd=BUDGET), FakeModels(), trial_sessions)

    def fail(request):
        raise exc("no answer", request=request)

    h.handler.anthropic_transport = httpx2.MockTransport(fail)
    async with h.client() as client:
        out = await _call(client, "blender_set_chat_backend",
                          {"provider": "anthropic", "api_key": USER_KEY})
    assert out["status"] == "error" and out["error"] == "key_check_failed"
    assert out["detail"] and USER_KEY not in out["detail"]
    assert out["trial"] == FRESH
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

def _own_row(model=None, advisor=None):
    return ChatSettings(user_sub="u", provider="anthropic", model=model, advisor=advisor,
                        api_key_enc=None)


def test_own_key_defaults_to_the_servers_claude_model():
    c = _prod(ChatConfig(secret_key=Fernet.generate_key().decode()))
    row = _own_row()
    assert chat_settings.backend_for(row, c, "u").model == "claude-haiku-4-5"
    view = chat_settings.public_view(row, c)
    assert view["model"] == "claude-haiku-4-5" and view["source"] == "user"
    # A model the account chose wins.
    assert chat_settings.backend_for(_own_row("claude-sonnet-5"), c, "u").model == "claude-sonnet-5"


def test_own_key_default_model_without_a_claude_server_default():
    gateway = ChatConfig(default_model="gemma4")
    assert chat_settings.backend_for(_own_row(), gateway, "u").model == DEFAULT_ANTHROPIC_MODEL
    claude_no_model = ChatConfig(default_provider="anthropic", anthropic_api_key="k")
    assert chat_settings.backend_for(_own_row(), claude_no_model, "u").model == DEFAULT_ANTHROPIC_MODEL


def test_own_key_gets_the_same_advisor_default():
    c = _prod(ChatConfig())
    b = chat_settings.backend_for(_own_row(), c, "u")
    assert (b.model, b.advisor) == ("claude-haiku-4-5", "claude-fable-5-1")
    view = chat_settings.public_view(_own_row(), c)
    assert view["advisor"] == view["advisor_default"] == "claude-fable-5-1"
    assert chat_settings.backend_for(_own_row(advisor="off"), c, "u").advisor == ""
    assert chat_settings.backend_for(_own_row(advisor="claude-opus-5"), c, "u").advisor == "claude-opus-5"


async def test_saved_key_turn_uses_the_default_model_and_advisor(cfg, trial_sessions):
    models = FakeModels(an_text("on my key"))
    h = Harness(_prod(cfg, trial_usd=BUDGET), models, trial_sessions)
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
        assert set(cols) == {"user_sub", "used_usd", "tokens_in", "tokens_out", "updated_at"}
        assert inspect(conn).get_pk_constraint("chat_trial_usage")["constrained_columns"] == ["user_sub"]
        conn.execute(text("INSERT INTO chat_trial_usage (user_sub, updated_at) "
                          "VALUES ('u', CURRENT_TIMESTAMP)"))
        assert conn.execute(text("SELECT used_usd + tokens_in + tokens_out FROM chat_trial_usage")).scalar() == 0
        with Operations.context(MigrationContext.configure(conn)):
            mig.downgrade()
        assert "chat_trial_usage" not in inspect(conn).get_table_names()
    engine.dispose()
