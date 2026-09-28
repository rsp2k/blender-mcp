"""The Claude provider on the anthropic SDK: request shape, tool_choice and
sampling rules, server-side fallbacks, refusals, errors, and thinking blocks
carried across tool rounds. No network: the SDK runs on an httpx2 MockTransport."""

import json

import httpx2
import pytest
from mcp.types import SamplingMessage, TextContent, Tool, ToolResultContent, ToolUseContent

from blender_mcp.chat.config import ChatConfig
from blender_mcp.chat.providers import Backend, ProviderError
from blender_mcp.chat.providers import anthropic as an
from blender_mcp.chat.routing import RoutingSamplingHandler, current_turn

KEY = "sk-ant-user-" + "k" * 24
TOOLS = [Tool(name="create_mesh", description="make a mesh",
              inputSchema={"type": "object", "properties": {"name": {"type": "string"}}})]
THINKING = {"type": "thinking", "thinking": "", "signature": "sig-abc123"}


def message(content, stop_reason="end_turn", model="claude-opus-5", **extra):
    return {"id": "msg_1", "type": "message", "role": "assistant", "model": model,
            "content": content, "stop_reason": stop_reason, "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 5}, **extra}


class FakeAPI:
    """Scripted Messages API: each item is a (status, json) pair or a json body."""

    def __init__(self, *script):
        self.script = list(script)
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        item = self.script.pop(0) if self.script else message([{"type": "text", "text": "done"}])
        status, body = item if isinstance(item, tuple) else (200, item)
        return httpx2.Response(status, json=body)

    def body(self, i=0) -> dict:
        return json.loads(self.requests[i].content)

    @property
    def transport(self):
        return httpx2.MockTransport(self)


def backend(model="claude-opus-5", key=KEY):
    return Backend("anthropic", model, "https://api.anthropic.com", key)


def user(text="make a box"):
    return [SamplingMessage(role="user", content=TextContent(type="text", text=text))]


async def run(api, model="claude-opus-5", messages=None, tools=TOOLS, **kw):
    return await an.complete(backend(model), "sys", messages or user(), tools,
                             transport=api.transport, **kw)


# ---- request shape -----------------------------------------------------------

async def test_request_shape_defaults_for_opus_5():
    api = FakeAPI(message([{"type": "text", "text": "ok"},
                           {"type": "tool_use", "id": "t1", "name": "create_mesh",
                            "input": {"name": "B"}}], stop_reason="tool_use"))
    c = await run(api, max_tokens=2048, temperature=0.3, tool_choice="auto")
    req = api.requests[0]
    body = api.body()
    assert req.url.path == "/v1/messages"
    assert req.headers["x-api-key"] == KEY
    assert req.headers["anthropic-version"] == "2023-06-01"
    assert "server-side-fallback-2026-07-01" in req.headers["anthropic-beta"]
    assert body["model"] == "claude-opus-5"
    assert body["max_tokens"] >= 16000  # CHAT_MAX_TOKENS is for the gateway only
    assert "temperature" not in body and "top_p" not in body and "top_k" not in body
    assert "thinking" not in body  # Opus 5 runs adaptive thinking by default
    assert body["fallbacks"] == "default"
    assert body["system"] == "sys"
    assert body["tool_choice"] == {"type": "auto"}
    assert body["tools"][0] == {"name": "create_mesh", "description": "make a mesh",
                                "input_schema": TOOLS[0].inputSchema}
    assert "output_config" not in body
    assert c.text == "ok" and c.tool_calls[0].arguments == {"name": "B"}
    assert c.raw[1]["id"] == "t1"


async def test_fallbacks_off_and_effort_on():
    api = FakeAPI(message([{"type": "text", "text": "ok"}]))
    await run(api, fallbacks=False, effort="medium")
    assert "anthropic-beta" not in api.requests[0].headers
    body = api.body()
    assert "fallbacks" not in body and body["output_config"] == {"effort": "medium"}


async def test_fallbacks_only_for_models_that_take_the_default_form():
    api = FakeAPI(message([{"type": "text", "text": "a"}]), message([{"type": "text", "text": "b"}]),
                  message([{"type": "text", "text": "c"}]))
    await run(api, model="claude-sonnet-5")
    await run(api, model="claude-opus-5-5")
    await run(api, model="claude-fable-5-1")
    assert "fallbacks" not in api.body(0) and "fallbacks" not in api.body(1)
    assert api.body(2)["fallbacks"] == "default"


@pytest.mark.parametrize("model,expected", [
    ("claude-opus-5", {"type": "any"}),
    ("claude-sonnet-5", {"type": "any"}),
    ("claude-fable-5-1", {"type": "auto"}),
    ("claude-opus-5-5", {"type": "auto"}),
    ("claude-mythos-5-1", {"type": "auto"}),
])
async def test_required_tool_choice_only_where_forced_use_is_allowed(model, expected):
    api = FakeAPI(message([{"type": "text", "text": "ok"}]))
    await run(api, model=model, tool_choice="required")
    assert api.body()["tool_choice"] == expected


async def test_none_tool_choice_and_no_tools():
    api = FakeAPI(message([{"type": "text", "text": "x"}]), message([{"type": "text", "text": "y"}]))
    await run(api, tool_choice="none")
    await run(api, tools=None, tool_choice="none")
    assert api.body(0)["tool_choice"] == {"type": "none"}
    assert "tools" not in api.body(1) and "tool_choice" not in api.body(1)


def test_sampling_parameters_only_for_models_that_take_them():
    for model in ("claude-opus-5", "claude-sonnet-5", "claude-opus-4-8", "claude-opus-4-7",
                  "claude-fable-5-1", "claude-opus-5-5", "claude-opus-6"):
        assert not an.allows_sampling(model), model
    for model in ("claude-sonnet-4-6", "claude-haiku-4-5", "claude-opus-4-6"):
        assert an.allows_sampling(model), model
    req = an.build_request(backend("claude-haiku-4-5"), None, [], None, max_tokens=10,
                           temperature=0.2, tool_choice=None, fallbacks=True, effort="")
    assert req["extra_body"] == {"temperature": 0.2} and "fallbacks" not in req


# ---- stop reasons ------------------------------------------------------------

async def test_refusal_is_a_readable_provider_error():
    api = FakeAPI(message([], stop_reason="refusal",
                          stop_details={"type": "refusal", "category": "cyber",
                                        "explanation": "declined by policy"}))
    with pytest.raises(ProviderError) as e:
        await run(api)
    assert "declined" in str(e.value) and "cyber" in str(e.value)


async def test_refusal_without_details_still_reads_well():
    api = FakeAPI(message([{"type": "text", "text": "partial"}], stop_reason="refusal"))
    with pytest.raises(ProviderError, match="declined this request"):
        await run(api)


async def test_fallback_marker_is_not_replayed():
    api = FakeAPI(message([
        {"type": "fallback", "from": {"model": "claude-opus-5"}, "to": {"model": "claude-opus-4-8"}},
        {"type": "tool_use", "id": "t9", "name": "create_mesh", "input": {}},
    ], stop_reason="tool_use", model="claude-opus-4-8"))
    c = await run(api)
    assert [b["type"] for b in c.raw] == ["tool_use"]


async def test_truncated_tool_call_is_not_run():
    api = FakeAPI(message([{"type": "tool_use", "id": "t1", "name": "create_mesh", "input": {}}],
                          stop_reason="max_tokens"))
    with pytest.raises(ProviderError, match="ran out of output tokens"):
        await run(api)


async def test_empty_answer_is_retried_then_reported():
    api = FakeAPI(message([]), message([]))
    with pytest.raises(ProviderError, match="empty response"):
        await run(api)
    assert len(api.requests) == 2


# ---- errors ------------------------------------------------------------------

@pytest.mark.parametrize("status", [401, 403])
async def test_auth_errors_name_the_saved_key(status):
    api = FakeAPI((status, {"type": "error", "error": {"type": "authentication_error",
                                                       "message": "invalid x-api-key"}}))
    with pytest.raises(ProviderError) as e:
        await run(api)
    assert "rejected the saved key" in str(e.value) and KEY not in str(e.value)


async def test_bad_request_carries_the_api_text():
    api = FakeAPI((400, {"type": "error", "error": {"type": "invalid_request_error",
                                                    "message": "messages.0: bad block"}}))
    with pytest.raises(ProviderError, match="messages.0: bad block"):
        await run(api)


async def test_no_key_is_refused_before_any_request():
    with pytest.raises(ProviderError, match="no Anthropic API key"):
        await an.complete(backend(key=""), None, user(), None)


async def test_server_errors_are_retried_by_the_sdk(monkeypatch):
    monkeypatch.setattr(an, "MAX_RETRIES", 1)
    api = FakeAPI((529, {"type": "error", "error": {"type": "overloaded_error", "message": "busy"}}),
                  message([{"type": "text", "text": "fine"}]))
    c = await run(api)
    assert c.text == "fine" and len(api.requests) == 2


# ---- thinking across tool rounds ----------------------------------------------

def _cfg():
    return ChatConfig(enabled=True, max_tokens=2048)


def _round_two(history, call_id):
    return [*history,
            SamplingMessage(role="assistant", content=[
                TextContent(type="text", text="Making it."),
                ToolUseContent(type="tool_use", id=call_id, name="create_mesh", input={"name": "Box"})]),
            SamplingMessage(role="user", content=[
                ToolResultContent(type="tool_result", toolUseId=call_id,
                                  content=[TextContent(type="text", text="made Box")])])]


async def test_thinking_blocks_are_replayed_verbatim_in_the_next_round():
    first = [THINKING,
             {"type": "text", "text": "Making it."},
             {"type": "tool_use", "id": "toolu_A", "name": "create_mesh", "input": {"name": "Box"}}]
    api = FakeAPI(message(first, stop_reason="tool_use"), message([{"type": "text", "text": "Done."}]))
    handler = RoutingSamplingHandler(config_loader=_cfg, anthropic_transport=api.transport)
    b = backend()
    token = current_turn.set("alice:turn1")
    try:
        c1 = await handler.complete(b, "sys", user(), TOOLS)
        assert [t.id for t in c1.tool_calls] == ["toolu_A"]
        c2 = await handler.complete(b, "sys", _round_two(user(), "toolu_A"), TOOLS)
    finally:
        current_turn.reset(token)
    assert c2.text == "Done."
    sent = api.body(1)["messages"]
    assert [m["role"] for m in sent] == ["user", "assistant", "user"]
    assert sent[1]["content"] == first  # thinking + text + tool_use, unchanged
    assert sent[2]["content"][0]["type"] == "tool_result"
    handler.end_turn("alice:turn1")
    assert len(handler.thinking) == 0


async def test_replay_never_crosses_turns_or_accounts():
    first = [THINKING, {"type": "tool_use", "id": "toolu_B", "name": "create_mesh", "input": {"name": "Box"}}]
    api = FakeAPI(message(first, stop_reason="tool_use"), message([{"type": "text", "text": "x"}]),
                  message([{"type": "text", "text": "y"}]))
    handler = RoutingSamplingHandler(config_loader=_cfg, anthropic_transport=api.transport)
    token = current_turn.set("alice:t1")
    try:
        await handler.complete(backend(), "sys", user(), TOOLS)
    finally:
        current_turn.reset(token)
    # Another turn with the same tool_use id gets the rebuilt message, no thinking.
    token = current_turn.set("mallory:t2")
    try:
        await handler.complete(backend(key="sk-ant-other"), "sys", _round_two(user(), "toolu_B"), TOOLS)
    finally:
        current_turn.reset(token)
    # Outside any turn the scope is the key: a different key sees nothing either.
    await handler.complete(backend(key="sk-ant-third"), "sys", _round_two(user(), "toolu_B"), TOOLS)
    for i in (1, 2):
        blocks = api.body(i)["messages"][1]["content"]
        assert all(b["type"] != "thinking" for b in blocks)


async def test_thinking_cache_is_bounded():
    from blender_mcp.chat.routing import ThinkingCache

    cache = ThinkingCache(max_entries=3)
    for i in range(5):
        cache.put("s", frozenset({f"t{i}"}), [{"type": "tool_use", "id": f"t{i}"}])
    assert len(cache) == 3 and cache.get("s", frozenset({"t0"})) is None
    assert cache.get("s", frozenset({"t4"})) is not None


async def test_rejected_replayed_thinking_retries_without_it():
    first = [THINKING, {"type": "tool_use", "id": "toolu_C", "name": "create_mesh", "input": {"name": "Box"}}]
    bound_error = (400, {"type": "error", "error": {
        "type": "invalid_request_error",
        "message": "messages.1.content.0: Invalid `signature` in `thinking` block. "
                   "The block is bound to a different conversation."}})
    api = FakeAPI(message(first, stop_reason="tool_use"), bound_error,
                  message([{"type": "text", "text": "Recovered."}]))
    handler = RoutingSamplingHandler(config_loader=_cfg, anthropic_transport=api.transport)
    token = current_turn.set("alice:t3")
    try:
        await handler.complete(backend(), "sys", user(), TOOLS)
        c = await handler.complete(backend(), "sys", _round_two(user(), "toolu_C"), TOOLS)
    finally:
        current_turn.reset(token)
    assert c.text == "Recovered."
    assert api.body(1)["messages"][1]["content"][0]["type"] == "thinking"
    assert all(b["type"] != "thinking" for b in api.body(2)["messages"][1]["content"])
