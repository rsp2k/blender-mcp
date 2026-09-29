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
    assert body["system"] == [{"type": "text", "text": "sys", "cache_control": {"type": "ephemeral"}}]
    assert body["cache_control"] == {"type": "ephemeral"}  # automatic caching for the tail
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


def test_scene_snapshot_goes_after_the_cache_breakpoint():
    from blender_mcp.chat.providers import SCENE_MARKER
    from blender_mcp.chat.providers.anthropic import system_blocks

    blocks = system_blocks(f"RULES{SCENE_MARKER}{{\"objects\": 3}}")
    assert blocks[0] == {"type": "text", "text": "RULES", "cache_control": {"type": "ephemeral"}}
    assert "cache_control" not in blocks[1]
    assert blocks[1]["text"].startswith("The scene right now") and blocks[1]["text"].endswith('{"objects": 3}')
    # No scene (the catalog has no get_scene_info): one cached block.
    assert system_blocks("RULES") == [{"type": "text", "text": "RULES", "cache_control": {"type": "ephemeral"}}]


def test_the_turn_builds_its_prompt_with_the_shared_marker():
    import inspect

    from blender_mcp.chat import turn
    assert "SCENE_MARKER" in inspect.getsource(turn.Turn.system_prompt)


# ---- advisor -----------------------------------------------------------------

ADVICE = [{"type": "server_tool_use", "id": "srvtoolu_1", "name": "advisor", "input": {}},
          {"type": "advisor_tool_result", "tool_use_id": "srvtoolu_1",
           "content": {"type": "advisor_redacted_result", "encrypted_content": "enc"}}]


async def test_advisor_tool_beta_and_note_when_set():
    api = FakeAPI(message([{"type": "text", "text": "ok"}], model="claude-sonnet-5"))
    await run(api, model="claude-sonnet-5", advisor="claude-opus-5", advisor_max_tokens=2048)
    body = api.body()
    assert body["tools"][-1] == {"type": "advisor_20260301", "name": "advisor",
                                 "model": "claude-opus-5", "max_tokens": 2048}
    assert "advisor-tool-2026-03-01" in api.requests[0].headers["anthropic-beta"]
    assert "Advisor: keep your guidance under 80 words" in body["system"][0]["text"]


async def test_no_advisor_for_the_same_model_or_without_tools():
    api = FakeAPI(message([{"type": "text", "text": "a"}]), message([{"type": "text", "text": "b"}]))
    await run(api, advisor="claude-opus-5")  # executor is claude-opus-5 already
    await run(api, model="claude-sonnet-5", tools=[], advisor="claude-opus-5")
    for i in (0, 1):
        assert all(t.get("type") != "advisor_20260301" for t in api.body(i).get("tools", []))
        assert "advisor" not in api.requests[i].headers.get("anthropic-beta", "")


async def test_paused_turn_is_resumed_with_its_partial_content():
    api = FakeAPI(message([{"type": "text", "text": "Let me plan."}, ADVICE[0]],
                          stop_reason="pause_turn", model="claude-sonnet-5"),
                  message([ADVICE[1], {"type": "tool_use", "id": "t1", "name": "create_mesh",
                                       "input": {"name": "B"}}],
                          stop_reason="tool_use", model="claude-sonnet-5"))
    c = await run(api, model="claude-sonnet-5", advisor="claude-opus-5")
    second = api.body(1)["messages"]
    assert second[-1]["role"] == "assistant"
    assert second[-1]["content"][-1]["type"] == "server_tool_use"
    assert c.tool_calls[0].name == "create_mesh"


async def test_advice_blocks_are_kept_for_replay():
    api = FakeAPI(message([*ADVICE, {"type": "tool_use", "id": "t1", "name": "create_mesh",
                                     "input": {}}], stop_reason="tool_use", model="claude-sonnet-5"))
    c = await run(api, model="claude-sonnet-5", advisor="claude-opus-5")
    assert [b["type"] for b in c.raw] == ["server_tool_use", "advisor_tool_result", "tool_use"]


async def test_advisor_caching_switch():
    api = FakeAPI(message([{"type": "text", "text": "ok"}], model="claude-haiku-4-5"))
    await run(api, model="claude-haiku-4-5", advisor="claude-opus-5", advisor_cache=True)
    assert api.body()["tools"][-1]["caching"] == {"type": "ephemeral", "ttl": "5m"}


# ---- usage --------------------------------------------------------------------

def _iteration(kind, inp, out, **extra):
    return {"type": kind, "input_tokens": inp, "output_tokens": out,
            "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, **extra}


ADVISOR_USAGE = {
    "input_tokens": 120, "output_tokens": 40,
    "cache_read_input_tokens": 3000, "cache_creation_input_tokens": 500,
    "iterations": [
        _iteration("message", 60, 10),
        _iteration("advisor_message", 3400, 90, model="claude-opus-5"),
        _iteration("message", 60, 30),
        _iteration("advisor_message", 3500, 70, model="claude-opus-5"),
    ],
}


async def test_usage_carries_cache_and_advisor_totals():
    api = FakeAPI(message([{"type": "text", "text": "ok"}], model="claude-sonnet-5",
                          usage=ADVISOR_USAGE))
    c = await run(api, model="claude-sonnet-5", advisor="claude-opus-5")
    u = c.usage
    assert (u.input_tokens, u.output_tokens) == (120, 40)
    assert (u.cache_read_input_tokens, u.cache_creation_input_tokens) == (3000, 500)
    assert (u.advisor_calls, u.advisor_input_tokens, u.advisor_output_tokens) == (2, 6900, 160)


async def test_usage_without_advisor_leaves_advisor_fields_unset():
    api = FakeAPI(message([{"type": "text", "text": "ok"}]))
    u = (await run(api)).usage
    assert (u.input_tokens, u.output_tokens) == (10, 5)
    assert u.cache_read_input_tokens is None
    assert u.advisor_calls is None and u.advisor_input_tokens is None


async def test_usage_is_summed_across_a_paused_turn():
    first = {**ADVISOR_USAGE, "iterations": ADVISOR_USAGE["iterations"][:2]}
    api = FakeAPI(message([{"type": "text", "text": "Let me plan."}, ADVICE[0]],
                          stop_reason="pause_turn", model="claude-sonnet-5", usage=first),
                  message([ADVICE[1], {"type": "tool_use", "id": "t1", "name": "create_mesh",
                                       "input": {"name": "B"}}],
                          stop_reason="tool_use", model="claude-sonnet-5",
                          usage={"input_tokens": 200, "output_tokens": 25,
                                 "cache_read_input_tokens": 3100}))
    u = (await run(api, model="claude-sonnet-5", advisor="claude-opus-5")).usage
    assert (u.input_tokens, u.output_tokens) == (320, 65)
    assert u.cache_read_input_tokens == 6100
    assert u.cache_creation_input_tokens == 500  # only the first call reported one
    assert (u.advisor_calls, u.advisor_input_tokens, u.advisor_output_tokens) == (1, 3400, 90)


async def test_usage_counts_the_empty_attempt_that_was_retried():
    api = FakeAPI(message([]), message([{"type": "text", "text": "fine"}]))
    u = (await run(api)).usage
    assert (u.input_tokens, u.output_tokens) == (20, 10)


def test_parse_message_reads_usage_from_sdk_objects():
    from anthropic.types.beta import BetaMessage

    msg = BetaMessage.model_validate(message([{"type": "text", "text": "ok"}], usage=ADVISOR_USAGE))
    u = an.parse_message(msg, set()).usage
    assert u.input_tokens == 120 and u.advisor_calls == 2 and u.advisor_output_tokens == 160
