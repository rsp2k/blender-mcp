"""Chat pieces on their own: text-call recovery, provider conversions,
settings encryption and routing, config, registration flag, QA identity."""

import json
import uuid
from types import SimpleNamespace

import httpx
import pytest
from cryptography.fernet import Fernet
from mcp.types import (
    ImageContent,
    SamplingMessage,
    TextContent,
    Tool,
    ToolResultContent,
    ToolUseContent,
)
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from blender_mcp.chat import settings as chat_settings
from blender_mcp.chat.config import ChatConfig, load_config
from blender_mcp.chat.executor import ChatExecutor, result_ok
from blender_mcp.chat.providers import Backend, ProviderError
from blender_mcp.chat.providers import anthropic as an
from blender_mcp.chat.providers import openai_compat as oa
from blender_mcp.chat.providers.textcalls import recover
from blender_mcp.chat.routing import RoutingSamplingHandler, to_result
from blender_mcp.chat.vision import find_object_key
from blender_mcp.storage.models import ChatSettings


def _no_advisor(view: dict) -> dict:
    """The view without the escalation keys (tested on their own)."""
    if "backend" in view:
        return {**view, "backend": _no_advisor(view["backend"])}
    return {k: v for k, v in view.items() if not k.startswith("advisor")}

ALLOWED = {"create_mesh", "get_scene_info"}
TOOLS = [Tool(name="create_mesh", description="make a mesh",
              inputSchema={"type": "object", "properties": {"name": {"type": "string"}}})]


# ---- text-form tool calls ------------------------------------------------------

@pytest.mark.parametrize("text", [
    '<tool_call>{"name": "create_mesh", "arguments": {"name": "A"}}</tool_call>',
    '```json\n{"name": "blender_create_mesh", "arguments": {"name": "A"}}\n```',
    '{"name": "create_mesh", "parameters": {"name": "A"}}',
    '[{"name": "create_mesh", "arguments": "{\\"name\\": \\"A\\"}"}]',
    '{"type": "function", "function": {"name": "create_mesh", "arguments": {"name": "A"}}}',
])
def test_recover_accepts_common_shapes(text):
    calls, rest = recover(text, ALLOWED)
    assert [(c.name, c.arguments) for c in calls] == [("create_mesh", {"name": "A"})]
    assert rest == ""


def test_recover_keeps_prose_and_ignores_unknown_names():
    calls, rest = recover('Sure. {"name": "rm_rf", "arguments": {}} ok', ALLOWED)
    assert calls == [] and rest.startswith("Sure.")
    calls, rest = recover('Doing it now. {"name": "get_scene_info", "arguments": {}}', ALLOWED)
    assert [c.name for c in calls] == ["get_scene_info"] and rest == "Doing it now."
    assert recover("no json here", ALLOWED) == ([], "no json here")


# ---- OpenAI-compatible ---------------------------------------------------------

def _history():
    return [
        SamplingMessage(role="user", content=TextContent(type="text", text="hi")),
        SamplingMessage(role="assistant", content=[
            TextContent(type="text", text="calling"),
            ToolUseContent(type="tool_use", id="c1", name="create_mesh", input={"name": "A"})]),
        SamplingMessage(role="user", content=[
            ToolResultContent(type="tool_result", toolUseId="c1",
                              content=[TextContent(type="text", text="made A")])]),
        SamplingMessage(role="user", content=[
            TextContent(type="text", text="look"),
            ImageContent(type="image", data="QUJD", mimeType="image/png")]),
    ]


def test_openai_message_conversion():
    msgs = oa.to_openai_messages("sys", _history())
    assert msgs[0] == {"role": "system", "content": "sys"}
    assert msgs[1] == {"role": "user", "content": "hi"}
    assert msgs[2]["tool_calls"][0]["function"] == {"name": "create_mesh", "arguments": '{"name": "A"}'}
    assert msgs[3] == {"role": "tool", "tool_call_id": "c1", "content": "made A"}
    assert msgs[4]["content"][1]["image_url"]["url"] == "data:image/png;base64,QUJD"


def test_openai_parse_strips_think_and_bad_arguments():
    data = {"choices": [{"message": {"content": "<think>hmm</think>ok", "tool_calls": [
        {"id": "x", "function": {"name": "blender_create_mesh", "arguments": "{not json"}}]}}]}
    c = oa.parse_response(data, ALLOWED)
    assert c.text == "ok"
    assert c.tool_calls[0].name == "create_mesh"
    assert oa.INVALID_ARGS in c.tool_calls[0].arguments


def _backend(provider="gateway", key="k-secret"):
    base = "https://api.anthropic.com" if provider == "anthropic" else "https://gw.example/v1"
    return Backend(provider, "m", base, key)


async def test_openai_retries_5xx_then_succeeds():
    seen = []

    def handler(request):
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(502, text="bad gateway")
        return httpx.Response(200, json={"choices": [{"message": {"content": "fine"}}]})

    c = await oa.complete(_backend(), None, _history()[:1], TOOLS,
                          transport=httpx.MockTransport(handler))
    assert c.text == "fine" and len(seen) == 2
    assert json.loads(seen[0].content)["tools"][0]["function"]["name"] == "create_mesh"


async def test_openai_4xx_raises_without_leaking_the_key():
    def handler(request):
        return httpx.Response(401, json={"error": {"message": "bad key"}})

    with pytest.raises(ProviderError) as e:
        await oa.complete(_backend(), None, _history()[:1], None, transport=httpx.MockTransport(handler))
    assert "401" in str(e.value) and "k-secret" not in str(e.value)


# ---- Anthropic -----------------------------------------------------------------

def test_anthropic_message_conversion_merges_and_orders():
    msgs = an.to_anthropic_messages(
        [SamplingMessage(role="assistant", content=TextContent(type="text", text="stray"))] + _history())
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    last = msgs[-1]["content"]
    assert last[0]["type"] == "tool_result" and last[0]["tool_use_id"] == "c1"
    assert last[-1] == {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                    "data": "QUJD"}}
    assert msgs[1]["content"][1] == {"type": "tool_use", "id": "c1", "name": "create_mesh",
                                     "input": {"name": "A"}}


# Request shape, errors, refusals and thinking replay: tests/test_chat_anthropic.py.


def test_to_result_shape():
    from blender_mcp.chat.providers import Completion, ToolCall

    r = to_result(Completion("hi", [ToolCall("i", "create_mesh", {})]), "qwen3")
    assert r.stopReason == "toolUse" and r.model == "qwen3"
    assert [b.type for b in r.content] == ["text", "tool_use"]
    assert to_result(Completion("done"), "m").stopReason == "endTurn"


# ---- settings + routing ----------------------------------------------------------

@pytest.fixture
async def sessions(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 's.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(ChatSettings.__table__.create)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _cfg(**kw):
    return ChatConfig(enabled=True, gpu_api_key="gpu", secret_key=Fernet.generate_key().decode(), **kw)


async def test_routing_prefers_user_settings(sessions):
    cfg = _cfg()
    handler = RoutingSamplingHandler(session_factory=sessions, config_loader=lambda: cfg)
    assert (await handler.resolve("alice")).provider == "gateway"
    await chat_settings.save(sessions, cfg, "alice", "anthropic", api_key="ak", model="claude-opus-5-5")
    b = await handler.resolve("alice")
    assert (b.provider, b.model, b.api_key) == ("anthropic", "claude-opus-5-5", "ak")
    assert (await handler.resolve("bob")).provider == "gateway"
    # Changing only the model keeps the saved key.
    await chat_settings.save(sessions, cfg, "alice", "anthropic", model="claude-sonnet-5")
    assert (await handler.resolve("alice")).api_key == "ak"
    # Switching provider drops it.
    await chat_settings.save(sessions, cfg, "alice", "openai", base_url="http://x/v1", model="m")
    assert (await handler.resolve("alice")).api_key == ""


async def test_gateway_user_list_and_missing_key(sessions):
    handler = RoutingSamplingHandler(session_factory=sessions,
                                     config_loader=lambda: _cfg(gateway_users=frozenset({"alice"})))
    assert (await handler.resolve("alice")).provider == "gateway"
    assert await handler.resolve("bob") is None
    no_key = RoutingSamplingHandler(session_factory=sessions,
                                    config_loader=lambda: ChatConfig(enabled=True))
    assert await no_key.resolve("alice") is None


async def test_rotated_secret_is_a_clear_error(sessions):
    cfg = _cfg()
    await chat_settings.save(sessions, cfg, "alice", "anthropic", api_key="ak")
    rotated = RoutingSamplingHandler(session_factory=sessions, config_loader=lambda: _cfg())
    with pytest.raises(ProviderError, match="save it again"):
        await rotated.resolve("alice")


def test_non_fernet_secret_still_works():
    cfg = ChatConfig(secret_key="a0b1c2" * 10)
    token = chat_settings.encrypt(cfg, "plain")
    assert token != "plain" and chat_settings.decrypt(cfg, token) == "plain"


def test_public_view_never_has_the_key():
    row = ChatSettings(user_sub="u", provider="openai", model="m", base_url="http://x", api_key_enc="enc")
    view = chat_settings.public_view(row, ChatConfig())
    assert _no_advisor(view) == {"provider": "openai", "model": "m", "base_url": "http://x", "has_key": True,
                    "source": "user"}
    assert chat_settings.public_view(None, ChatConfig())["provider"] == "gateway"
    assert chat_settings.public_view(None, ChatConfig())["source"] == "server"


# ---- the server-wide default backend -------------------------------------------------

SERVER_KEY = "sk-ant-server-" + "s" * 24


def _server_anthropic(**kw):
    return _cfg(default_provider="anthropic", anthropic_api_key=SERVER_KEY, **kw)


async def test_precedence_user_then_server_default_then_nothing(sessions):
    cfg = _server_anthropic()
    handler = RoutingSamplingHandler(session_factory=sessions, config_loader=lambda: cfg)
    # No saved backend: the server default, with the server's key and Opus 5.
    b = await handler.resolve("bob")
    assert (b.provider, b.model, b.api_key) == ("anthropic", "claude-opus-5", SERVER_KEY)
    # The user's own backend wins.
    await chat_settings.save(sessions, cfg, "alice", "anthropic", api_key="alice-key",
                             model="claude-sonnet-5")
    b = await handler.resolve("alice")
    assert (b.model, b.api_key) == ("claude-sonnet-5", "alice-key")
    await chat_settings.save(sessions, cfg, "carol", "openai", base_url="http://x/v1", model="m")
    assert (await handler.resolve("carol")).provider == "openai"
    # A saved "gateway" row means "the server's backend": it follows the default.
    await chat_settings.save(sessions, cfg, "dave", "gateway", model="gemma4")
    assert (await handler.resolve("dave")).provider == "anthropic"
    # Clearing goes back to the default.
    await chat_settings.save(sessions, cfg, "alice", "gateway", clear=True)
    assert (await handler.resolve("alice")).api_key == SERVER_KEY


async def test_server_default_gateway_is_unchanged_behaviour(sessions):
    cfg = _cfg()  # CHAT_DEFAULT_PROVIDER unset
    handler = RoutingSamplingHandler(session_factory=sessions, config_loader=lambda: cfg)
    b = await handler.resolve("bob")
    assert (b.provider, b.model) == ("gateway", "qwen3")
    await chat_settings.save(sessions, cfg, "dave", "gateway", model="gemma4")
    assert (await handler.resolve("dave")).model == "gemma4"
    cfg2 = _cfg(default_model="gemma4")
    h2 = RoutingSamplingHandler(session_factory=sessions, config_loader=lambda: cfg2)
    assert (await h2.resolve("bob")).model == "gemma4"


async def test_server_default_needs_its_key_and_honours_the_user_list(sessions):
    no_key = _cfg(default_provider="anthropic")
    h = RoutingSamplingHandler(session_factory=sessions, config_loader=lambda: no_key)
    assert await h.resolve("bob") is None
    listed = _server_anthropic(gateway_users=frozenset({"alice"}))
    h = RoutingSamplingHandler(session_factory=sessions, config_loader=lambda: listed)
    assert (await h.resolve("alice")).provider == "anthropic"
    assert await h.resolve("bob") is None
    openai_default = _cfg(default_provider="openai", default_base_url="http://llm/v1",
                          default_model="llama3", default_api_key="oa-key")
    h = RoutingSamplingHandler(session_factory=sessions, config_loader=lambda: openai_default)
    b = await h.resolve("bob")
    assert (b.provider, b.base_url, b.model, b.api_key) == ("openai", "http://llm/v1", "llama3", "oa-key")


def test_public_view_of_server_default_hides_the_server_key():
    cfg = _server_anthropic()
    view = chat_settings.public_view(None, cfg)
    assert _no_advisor(view) == {"provider": "anthropic", "model": "claude-opus-5", "base_url": None,
                    "has_key": True, "source": "server"}
    assert SERVER_KEY not in json.dumps(view) and SERVER_KEY not in repr(cfg)
    gw_row = ChatSettings(user_sub="u", provider="gateway", model="gemma4")
    assert chat_settings.public_view(gw_row, cfg)["provider"] == "anthropic"
    assert chat_settings.public_view(None, _cfg(default_provider="anthropic"))["has_key"] is False


# ---- config ------------------------------------------------------------------------

def test_config_defaults_and_parsing():
    c = load_config({})
    assert not c.enabled and c.model == "qwen3" and c.vision_model == "qwen2.5vl"
    assert (c.max_steps, c.llm_timeout_s, c.turn_timeout_s) == (10, 120, 300)
    assert c.gpu_base_url == "https://blender-chat.gpu.supported.systems/v1"
    c = load_config({"CHAT_ENABLED": "true", "CHAT_MAX_STEPS": "", "CHAT_TOOLS": "a, b",
                     "CHAT_GATEWAY_USERS": "", "GPU_API_KEY": "zz-secret-zz", "CHAT_TURN_TIMEOUT_S": "junk"})
    assert c.enabled and c.max_steps == 10 and c.tools == {"a", "b"} and c.turn_timeout_s == 300
    assert c.gateway_allowed("anyone") and not c.gateway_allowed(None)
    assert "zz-secret" not in repr(c)  # key fields are repr=False
    assert (c.default_provider, c.anthropic_fallbacks, c.anthropic_effort) == ("gateway", True, "")
    c = load_config({"CHAT_DEFAULT_PROVIDER": "Anthropic", "ANTHROPIC_API_KEY": "zz-ant-zz",
                     "CHAT_ANTHROPIC_FALLBACKS": "off", "CHAT_ANTHROPIC_EFFORT": "medium"})
    assert (c.default_provider, c.anthropic_fallbacks, c.anthropic_effort) == ("anthropic", False, "medium")
    assert "zz-ant" not in repr(c)
    c = load_config({"CHAT_DEFAULT_PROVIDER": "bogus", "CHAT_ANTHROPIC_EFFORT": "turbo"})
    assert (c.default_provider, c.anthropic_effort) == ("gateway", "")


# ---- executor helpers, vision parsing ------------------------------------------------

def test_executor_forces_target_and_bus():
    ex = ChatExecutor(server=None, user_sub="u", blender_uuid="me", bus_id="bus")
    sent = ex.aimed({"name": "A", "target_uuid": "other", "bus_id": "x", "_timeout": 1, "store": True})
    assert sent == {"name": "A", "target_uuid": "me", "bus_id": "bus"}
    assert ex.aimed({}, extra={"store": True})["store"] is True


def test_result_ok():
    assert result_ok('{"status": "completed"}')
    assert result_ok("plain text")
    assert not result_ok('{"status": "failed", "error": "x"}')
    assert not result_ok('{"error": "boom"}')
    assert not result_ok('{"status": "wrong_role"}')


def test_vision_uses_claude_when_the_turn_is_on_claude():
    from blender_mcp.chat.vision import vision_backend

    claude = Backend("anthropic", "claude-opus-5", "https://api.anthropic.com", "k")
    cfg = _cfg()
    assert vision_backend(cfg, "alice", claude) is claude
    gw = vision_backend(cfg, "alice", Backend("gateway", "qwen3", "https://gw", "gpu"))
    assert (gw.provider, gw.model) == ("gateway", "qwen2.5vl")
    assert vision_backend(ChatConfig(), "alice", None) is None


def test_find_object_key_in_nested_reply():
    key = f"b/{uuid.uuid4().hex}/viewport.png"
    reply = json.dumps({"status": "completed", "result": json.dumps({"stored": {"object_key": key}})})
    assert find_object_key(reply) == key
    assert find_object_key('{"status": "completed", "result": "{}"}') is None


# ---- registration flag, QA identity ----------------------------------------------------

async def test_register_client_advertises_chat_only_when_enabled(monkeypatch):
    from blender_mcp import bus_tools, client_role
    from blender_mcp.message_bus import MessageBus

    bus_uuid = uuid.uuid4()

    async def fake_resolve(user_id, bus_id=None):
        return {"ok": True, "bus": MessageBus(bus_uuid, name="t"), "bus_id": bus_uuid, "name": "t"}

    monkeypatch.setattr(bus_tools, "resolve_bus", fake_resolve)
    monkeypatch.setattr(bus_tools, "_resolve_user_id", lambda ctx: "u")
    monkeypatch.setattr(client_role, "get_caller_role", lambda ctx=None: "addon")
    comp = bus_tools.BlenderBusComponent()

    monkeypatch.setenv("CHAT_ENABLED", "1")
    out = json.loads(await comp.register_client(client_uuid="c", client_type="blender"))
    assert out["features"] == ["chat"]
    monkeypatch.setenv("CHAT_ENABLED", "")
    out = json.loads(await comp.register_client(client_uuid="c", client_type="blender"))
    assert "features" not in out


def test_identify_reports_chat_caller(monkeypatch):
    from blender_mcp import bus_tools, instrumentation
    from blender_mcp.client_role import current_downstream_client_id

    monkeypatch.setattr(bus_tools, "_resolve_user_id", lambda ctx: "u")
    token = current_downstream_client_id.set("chat:blender-1")
    try:
        out = instrumentation.identify(SimpleNamespace(fastmcp_context=None))
    finally:
        current_downstream_client_id.reset(token)
    assert out["caller_kind"] == "chat" and out["client_id"] == "chat:blender-1"
    assert {"blender_set_chat_backend", "blender_chat"} <= instrumentation.META_ONLY_TOOLS
