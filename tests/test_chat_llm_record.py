"""Every model call the chat makes is recorded as an llm.call event (QA_LOG on),
keyed by the chat turn, with token counts and never the backend's key."""

import httpx
import httpx2
import pytest
from fastmcp import Client, FastMCP
from mcp.types import SamplingMessage, TextContent
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from blender_mcp import instrumentation
from blender_mcp.chat import routing
from blender_mcp.chat.config import ChatConfig
from blender_mcp.chat.providers import Backend, ProviderError
from blender_mcp.chat.routing import RoutingSamplingHandler, current_turn

KEY = "sk-ant-user-" + "k" * 24


class FakeMiddleware:
    def __init__(self, fail=False):
        self.calls: list[dict] = []
        self.fail = fail

    def record_llm_call(self, **kw):
        if self.fail:
            raise RuntimeError("sink exploded")
        self.calls.append(kw)
        return True


@pytest.fixture
def fake_mw(monkeypatch):
    mw = FakeMiddleware()
    monkeypatch.setattr(instrumentation, "middleware", lambda ctx=None: mw)
    return mw


def _cfg():
    return ChatConfig(enabled=True, max_tokens=2048)


def _user():
    return [SamplingMessage(role="user", content=TextContent(type="text", text="make a box"))]


def _anthropic_api(usage):
    def handler(request):
        return httpx2.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-sonnet-5",
            "content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn",
            "stop_sequence": None, "usage": usage})
    return httpx2.MockTransport(handler)


async def test_complete_records_the_turn_and_its_tokens(fake_mw):
    usage = {"input_tokens": 120, "output_tokens": 40, "cache_read_input_tokens": 3000,
             "cache_creation_input_tokens": 500,
             "iterations": [{"type": "advisor_message", "input_tokens": 3400, "output_tokens": 90}]}
    handler = RoutingSamplingHandler(config_loader=_cfg, anthropic_transport=_anthropic_api(usage))
    backend = Backend("anthropic", "claude-sonnet-5", "https://api.anthropic.com", KEY,
                      advisor="claude-opus-5")
    token = current_turn.set("alice:turn-7")
    try:
        c = await handler.complete(backend, "sys", _user(), [])
    finally:
        current_turn.reset(token)
    assert c.text == "ok"
    [rec] = fake_mw.calls
    assert rec["key"] == "alice:turn-7"
    assert rec["model"] == "claude-sonnet-5" and rec["provider"] == "anthropic"
    assert (rec["input_tokens"], rec["output_tokens"]) == (120, 40)
    assert rec["error"] is None and rec["duration_ms"] >= 0
    assert rec["attrs"] == {"advisor": "claude-opus-5", "cache_read_input_tokens": 3000,
                            "cache_creation_input_tokens": 500, "advisor_calls": 1,
                            "advisor_input_tokens": 3400, "advisor_output_tokens": 90}
    assert KEY not in repr(rec) and "api.anthropic.com" not in repr(rec)


async def test_a_failed_call_is_recorded_and_still_raised(fake_mw):
    def refuse(request):
        return httpx.Response(401, json={"error": {"message": "bad key"}})

    handler = RoutingSamplingHandler(config_loader=_cfg, transport=httpx.MockTransport(refuse))
    backend = Backend("gateway", "qwen3", "https://gw.example/v1", "gw-secret")
    token = current_turn.set("bob:turn-1")
    try:
        with pytest.raises(ProviderError):
            await handler.complete(backend, "sys", _user(), [])
    finally:
        current_turn.reset(token)
    [rec] = fake_mw.calls
    assert rec["key"] == "bob:turn-1" and rec["provider"] == "gateway"
    assert isinstance(rec["error"], ProviderError)
    assert rec["input_tokens"] is None and rec["attrs"] == {}
    assert "gw-secret" not in repr(rec)


async def test_outside_a_turn_the_key_is_none(fake_mw):
    def ok(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": "fine"}}],
                                         "usage": {"prompt_tokens": 9, "completion_tokens": 2}})

    handler = RoutingSamplingHandler(config_loader=_cfg, transport=httpx.MockTransport(ok))
    await handler.complete(Backend("gateway", "qwen3", "https://gw.example/v1"), None, _user(), [])
    [rec] = fake_mw.calls
    assert rec["key"] is None and (rec["input_tokens"], rec["output_tokens"]) == (9, 2)


async def test_a_broken_recorder_never_breaks_the_chat(monkeypatch):
    monkeypatch.setattr(instrumentation, "middleware", lambda ctx=None: FakeMiddleware(fail=True))
    routing.record_llm_call(Backend("gateway", "m", "https://gw.example/v1"), 0.0)


def test_middleware_is_none_outside_a_request_and_read_from_the_server():
    assert instrumentation.middleware() is None
    app = FastMCP("t")
    app.qa_middleware = sentinel = object()

    class Ctx:
        fastmcp = app

    assert instrumentation.middleware(Ctx()) is sentinel
    assert instrumentation.middleware(object()) is None


async def test_llm_call_lands_linked_to_the_tool_call(tmp_path):
    """End to end: the real middleware, found through the request context from
    inside a tool, writes an llm.call whose call_id is the tool call's id."""
    from fastmcp_feedback.instrumentation import (
        DatabaseSink,
        build_metadata,
        instrument,
    )

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'qa.db'}")
    metadata = build_metadata(prefix=instrumentation.FFB_TABLE_PREFIX)
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    app = FastMCP("t")
    app.qa_middleware = mw = instrument(
        app, [DatabaseSink(engine, prefix=instrumentation.FFB_TABLE_PREFIX)], mode="meta")

    def gateway(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": "a box"}}],
                                         "usage": {"prompt_tokens": 50, "completion_tokens": 4,
                                                   "prompt_tokens_details": {"cached_tokens": 32}}})

    handler = RoutingSamplingHandler(config_loader=_cfg, transport=httpx.MockTransport(gateway))

    @app.tool
    async def blender_chat(message: str) -> str:
        token = current_turn.set("carol:turn-3")
        try:
            c = await handler.complete(Backend("gateway", "qwen3", "https://gw.example/v1", "gw-secret"),
                                       None, _user(), [])
        finally:
            current_turn.reset(token)
        return c.text

    async with Client(app) as client:
        r = await client.call_tool("blender_chat", {"message": "hi"})
    assert r.content[0].text == "a box"

    await mw.flush()
    calls = metadata.tables[f"{instrumentation.FFB_TABLE_PREFIX}tool_calls"]
    events = metadata.tables[f"{instrumentation.FFB_TABLE_PREFIX}events"]
    async with engine.connect() as conn:
        call = (await conn.execute(select(calls))).mappings().one()
        event = (await conn.execute(select(events))).mappings().one()
    assert event["kind"] == "llm.call" and event["key"] == "carol:turn-3"
    assert event["call_id"] == call["id"]
    assert event["attrs"]["model"] == "qwen3" and event["attrs"]["provider"] == "gateway"
    assert event["attrs"]["input_tokens"] == 50 and event["attrs"]["output_tokens"] == 4
    assert event["attrs"]["cache_read_input_tokens"] == 32
    assert "gw-secret" not in repr(dict(event))
    await mw.aclose()
    await engine.dispose()
