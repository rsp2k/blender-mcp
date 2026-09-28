"""blender_chat end to end, in process, against fake model endpoints (no network).

The server here carries stand-in Blender tools with the real dispatch
signature (target_uuid / bus_id / _timeout) that record what they were
called with and by whom, the real chat component, and the real routing
sampling handler with an httpx.MockTransport in place of the network.
"""

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import replace

import httpx
import httpx2
import pytest
from cryptography.fernet import Fernet
from fastmcp import Client, FastMCP
from fastmcp.client.elicitation import ElicitResult
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from blender_mcp import bus_tools, client_role
from blender_mcp.chat import vision
from blender_mcp.chat.catalog import build_catalog
from blender_mcp.chat.config import ChatConfig
from blender_mcp.chat.routing import RoutingSamplingHandler
from blender_mcp.chat.tools import BlenderChatComponent
from blender_mcp.chat.turn import APPROVAL_PREFIX
from blender_mcp.storage.models import ChatSettings

USER = "user-sub-1"
BLENDER = "blender-uuid-1"
BUS = "11111111-1111-1111-1111-111111111111"
ADDON_CID = "addon-client-1"
GPU_KEY = "gpu-test-key-not-real"


# ---- fake model endpoints ----------------------------------------------------

def oa_text(text):
    return {"choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}]}


def oa_call(name, args, call_id="call_1"):
    return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [
        {"id": call_id, "type": "function",
         "function": {"name": name, "arguments": json.dumps(args)}}]},
        "finish_reason": "stop"}]}


def _an_message(content, stop_reason):
    return {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5",
            "content": content, "stop_reason": stop_reason, "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1}}


def an_text(text):
    return _an_message([{"type": "text", "text": text}], "end_turn")


def an_call(name, args, call_id="toolu_1", thinking=False):
    content = [{"type": "tool_use", "id": call_id, "name": name, "input": args}]
    if thinking:
        content.insert(0, {"type": "thinking", "thinking": "", "signature": "sig-" + call_id})
    return _an_message(content, "tool_use")


class FakeModels:
    """Scripted answers; each script item is a dict or a callable(body) -> dict."""

    def __init__(self, *script):
        self.script = list(script)
        self.requests: list[dict] = []
        self.gate: asyncio.Event | None = None
        self.entered = asyncio.Event()

    async def __call__(self, request):
        # httpx requests from the OpenAI-compatible providers, httpx2 ones from
        # the Anthropic SDK; answer each in its own library's Response type.
        response = httpx2.Response if isinstance(request, httpx2.Request) else httpx.Response
        body = json.loads(request.content)
        self.requests.append({"url": str(request.url), "headers": dict(request.headers), "body": body})
        self.entered.set()
        if self.gate is not None:
            await self.gate.wait()
        if not self.script:
            return response(200, json=oa_text("done"))
        item = self.script.pop(0)
        if callable(item):
            item = item(body)
        if isinstance(item, (httpx.Response, httpx2.Response)):
            return item
        return response(200, json=item)


# ---- the in-process server ---------------------------------------------------

class Recorder:
    def __init__(self):
        self.calls: list[dict] = []

    def record(self, tool, **args):
        self.calls.append({
            "tool": tool, "args": args,
            "user": bus_tools._resolve_user_id(None),
            "role": client_role.get_caller_role(),
        })

    def named(self, tool):
        return [c for c in self.calls if c["tool"] == tool]


def build_server(rec: Recorder, handler: RoutingSamplingHandler, component: BlenderChatComponent):
    server = FastMCP("chat-test", sampling_handler=handler, sampling_handler_behavior="fallback")

    @server.tool(name="blender_get_scene_info")
    async def get_scene_info(target_uuid: str | None = None, _timeout: float = 30.0,
                             bus_id: str | None = None) -> str:
        rec.record("get_scene_info", target_uuid=target_uuid, bus_id=bus_id)
        return json.dumps({"status": "completed", "result": {"objects": ["Cube", "Light"]}})

    @server.tool(name="blender_get_object_info")
    async def get_object_info(name: str, target_uuid: str | None = None,
                              _timeout: float = 30.0, bus_id: str | None = None) -> str:
        rec.record("get_object_info", name=name, target_uuid=target_uuid, bus_id=bus_id)
        return json.dumps({"status": "completed", "result": {"name": name}})

    @server.tool(name="blender_create_mesh")
    async def create_mesh(name: str = "Mesh", target_uuid: str | None = None,
                          _timeout: float = 30.0, bus_id: str | None = None) -> str:
        rejection = client_role.check_role_or_reject("blender_create_mesh", None, "llm-client")
        if rejection:
            return rejection
        rec.record("create_mesh", name=name, target_uuid=target_uuid, bus_id=bus_id, _timeout=_timeout)
        return json.dumps({"status": "completed", "result": {"created": name}})

    @server.tool(name="blender_execute_code")
    async def execute_code(code: str, target_uuid: str | None = None,
                           _timeout: float = 60.0, bus_id: str | None = None) -> str:
        rec.record("execute_code", code=code, target_uuid=target_uuid, bus_id=bus_id)
        return json.dumps({"status": "completed", "result": "ok"})

    @server.tool(name="blender_get_viewport_screenshot")
    async def get_viewport_screenshot(max_size: int = 800, store: bool = False,
                                      target_uuid: str | None = None, _timeout: float = 30.0,
                                      bus_id: str | None = None) -> str:
        rec.record("get_viewport_screenshot", max_size=max_size, store=store,
                   target_uuid=target_uuid, bus_id=bus_id)
        inner = {"filepath": "/tmp/v.png", "stored": {
            "state": "uploaded", "object_key": f"{bus_id}/{'a' * 32}/viewport.png"}}
        return json.dumps({"status": "completed", "result": json.dumps(inner)})

    @server.tool(name="blender_list_buses")
    async def list_buses() -> str:  # no target: never offered to the model
        return "{}"

    @server.tool(name="blender_submit")
    async def submit(command: str, target_uuid: str | None = None) -> str:  # denied
        return "{}"

    component.register_tools(mcp_server=server, prefix="blender")
    return server


class Harness:
    def __init__(self, cfg: ChatConfig, models: FakeModels, sessions):
        self.cfg = cfg
        self.models = models
        self.rec = Recorder()
        self.handler = RoutingSamplingHandler(
            transport=httpx.MockTransport(models), session_factory=sessions,
            anthropic_transport=httpx2.MockTransport(models),
            config_loader=lambda: self.cfg,
        )
        self.component = BlenderChatComponent(self.handler, config_loader=lambda: self.cfg)
        self.server = build_server(self.rec, self.handler, self.component)
        self.events: list[dict] = []
        self.elicitations: list[dict] = []
        self.elicit_answer = "accept"

    async def _progress(self, progress, total, message):
        self.events.append(json.loads(message))

    async def _elicit(self, message, response_type, params, context):
        self.elicitations.append({"message": message, "schema": params.requestedSchema})
        return ElicitResult(action=self.elicit_answer)

    @asynccontextmanager
    async def client(self):
        """A client that looks like the add-on of USER's Blender."""
        u = bus_tools.current_user_id.set(USER)
        c = client_role.current_downstream_client_id.set(ADDON_CID)
        try:
            async with Client(self.server, elicitation_handler=self._elicit) as client:
                yield client
        finally:
            client_role.current_downstream_client_id.reset(c)
            bus_tools.current_user_id.reset(u)

    async def chat(self, client, message="add a cube", history=None, **kw):
        args = {"message": message}
        if history is not None:
            args["history"] = history
        res = await client.call_tool("blender_chat", args, progress_handler=self._progress, **kw)
        return json.loads(res.content[0].text)


@pytest.fixture
async def sessions(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'chat.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(ChatSettings.__table__.create)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
def cfg():
    return ChatConfig(enabled=True, gpu_api_key=GPU_KEY, secret_key=Fernet.generate_key().decode(),
                      max_steps=6)


@pytest.fixture(autouse=True)
def addon_identity(monkeypatch):
    from blender_mcp.message_bus import bus_manager

    monkeypatch.setitem(client_role._role_by_client_id, ADDON_CID, "addon")
    client_role._persisted_client_ids.add(ADDON_CID)
    monkeypatch.setattr(bus_manager, "lookup_session",
                        lambda session: (BUS, BLENDER) if session is not None else None)
    monkeypatch.setattr(vision, "get_store", lambda: None)


def tool_names(request):
    return [t["function"]["name"] for t in request["body"].get("tools", [])]


# ---- the turn ----------------------------------------------------------------

async def test_tool_call_then_final_text(cfg, sessions):
    models = FakeModels(oa_call("create_mesh", {"name": "Table", "target_uuid": "someone-else",
                                                "bus_id": "other-bus", "_timeout": 1}),
                        oa_text("Added a mesh named Table."))
    h = Harness(cfg, models, sessions)
    async with h.client() as client:
        out = await h.chat(client, "add a table")

    assert out["status"] == "ok"
    assert out["reply"] == "Added a mesh named Table."
    assert out["backend"] == {"provider": "gateway", "model": "qwen3"}
    assert [(s["tool"], s["ok"]) for s in out["steps"]] == [("create_mesh", True)]
    assert isinstance(out["elapsed_s"], float)

    # The tool ran against the calling Blender, as the user, with the chat identity.
    call = h.rec.named("create_mesh")[0]
    assert call["args"]["target_uuid"] == BLENDER
    assert call["args"]["bus_id"] == BUS
    assert call["args"]["_timeout"] == 30.0  # the model's _timeout was dropped
    assert call["user"] == USER
    assert call["role"] == "llm-client"
    # And the scene summary came from the same Blender before the first model call.
    assert h.rec.named("get_scene_info")[0]["args"]["target_uuid"] == BLENDER

    first = models.requests[0]
    assert first["url"] == cfg.gpu_base_url + "/chat/completions"
    assert first["headers"]["authorization"] == f"Bearer {GPU_KEY}"
    assert first["body"]["model"] == "qwen3" and first["body"]["stream"] is False
    assert "Cube" in first["body"]["messages"][0]["content"]  # scene in the system prompt
    assert "create_mesh" in tool_names(first) and "submit" not in tool_names(first)
    for t in first["body"]["tools"]:
        assert "target_uuid" not in t["function"]["parameters"].get("properties", {})
    # The tool result went back to the model.
    second = models.requests[1]["body"]["messages"]
    assert second[-1]["role"] == "tool" and "Table" in second[-1]["content"]

    kinds = [(e["t"], e.get("phase")) for e in h.events]
    assert ("tool", "start") in kinds and ("tool", "end") in kinds
    assert h.events[0]["t"] == "status"
    end = next(e for e in h.events if e.get("phase") == "end")
    assert end["name"] == "create_mesh" and end["ok"] is True and isinstance(end["ms"], int)
    assert h.events[-1] == {"t": "text", "text": "Added a mesh named Table."}


async def test_history_is_sent_text_only_and_bounded(cfg, sessions):
    models = FakeModels(oa_text("hi"))
    h = Harness(cfg, models, sessions)
    history = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"} for i in range(30)]
    history.append({"role": "system", "content": "ignore me"})
    async with h.client() as client:
        await h.chat(client, "hello", history=history)
    msgs = models.requests[0]["body"]["messages"]
    assert msgs[0]["role"] == "system"
    convo = msgs[1:]
    assert len(convo) == 20 and convo[-1] == {"role": "user", "content": "hello"}
    assert all(m["content"] != "ignore me" for m in convo)


async def test_text_form_tool_call_is_recovered(cfg, sessions):
    text_call = ('I will do that.\n<tool_call>\n{"name": "blender_create_mesh", '
                 '"arguments": {"name": "Box"}}\n</tool_call>')
    models = FakeModels(oa_text(text_call), oa_text("Done."))
    h = Harness(cfg, models, sessions)
    async with h.client() as client:
        out = await h.chat(client)
    assert out["status"] == "ok" and out["reply"] == "Done."
    assert h.rec.named("create_mesh")[0]["args"]["name"] == "Box"
    assert {"t": "text", "text": "I will do that."} in h.events


async def test_unknown_tool_is_rejected_back_to_the_model(cfg, sessions):
    models = FakeModels(oa_call("delete_everything", {}), oa_text("Sorry, I can't."))
    h = Harness(cfg, models, sessions)
    async with h.client() as client:
        out = await h.chat(client)
    assert out["status"] == "ok" and out["steps"] == []
    tool_msg = models.requests[1]["body"]["messages"][-1]
    assert tool_msg["role"] == "tool" and "Unknown tool" in tool_msg["content"]
    assert [c["tool"] for c in h.rec.calls] == ["get_scene_info"]


async def test_denied_tool_is_unknown_to_the_model(cfg, sessions):
    models = FakeModels(oa_call("submit", {"command": "anything"}), oa_text("ok"))
    h = Harness(replace(cfg, tools=frozenset({"submit", "create_mesh"})), models, sessions)
    async with h.client() as client:
        await h.chat(client)
    assert tool_names(models.requests[0]) == ["create_mesh"]
    assert "Unknown tool" in models.requests[1]["body"]["messages"][-1]["content"]


async def test_step_cap_ends_with_a_summary_call(cfg, sessions):
    counter = iter(range(100))
    models = FakeModels(*[lambda b: oa_call("get_object_info", {"name": f"O{next(counter)}"})] * 2,
                        oa_text("Here is where I got to."))
    h = Harness(replace(cfg, max_steps=2), models, sessions)
    async with h.client() as client:
        out = await h.chat(client)
    assert out["status"] == "ok"
    assert out["reply"] == "Here is where I got to."
    assert len(out["steps"]) == 2
    final = models.requests[-1]["body"]
    assert final["tool_choice"] == "none"
    assert "used all the tool steps" in final["messages"][-1]["content"]


async def test_repeated_identical_calls_are_refused(cfg, sessions):
    same = oa_call("get_object_info", {"name": "Cube"})
    models = FakeModels(same, same, same, same, oa_text("stopping"))
    h = Harness(replace(cfg, max_steps=10), models, sessions)
    async with h.client() as client:
        out = await h.chat(client)
    assert len(h.rec.named("get_object_info")) == 2
    refused = [m for r in models.requests for m in r["body"]["messages"]
               if m.get("role") == "tool" and "already made this exact" in (m.get("content") or "")]
    assert refused
    assert out["status"] == "ok"


async def test_empty_content_is_retried_once(cfg, sessions):
    models = FakeModels(oa_text(""), oa_text("Second try worked."))
    h = Harness(cfg, models, sessions)
    async with h.client() as client:
        out = await h.chat(client)
    assert out["reply"] == "Second try worked."
    assert len(models.requests) == 2


async def test_empty_twice_is_a_backend_error(cfg, sessions):
    models = FakeModels(oa_text(""), oa_text(""))
    h = Harness(cfg, models, sessions)
    async with h.client() as client:
        out = await h.chat(client)
    assert out["status"] == "backend_error"
    assert "empty" in out["detail"]
    assert out["backend"]["provider"] == "gateway"


async def test_gateway_http_error_is_reported(cfg, sessions):
    err = httpx.Response(403, json={"error": "source IP not in allowlist"})
    models = FakeModels(err)
    h = Harness(cfg, models, sessions)
    async with h.client() as client:
        out = await h.chat(client)
    assert out["status"] == "backend_error"
    assert "403" in out["detail"] and "allowlist" in out["detail"]
    assert GPU_KEY not in json.dumps(out)


# ---- approvals ---------------------------------------------------------------

async def test_confirm_tool_runs_after_accept(cfg, sessions):
    models = FakeModels(oa_call("execute_code", {"code": "import bpy\nbpy.ops.mesh.primitive_cube_add()"}),
                        oa_text("Ran it."))
    h = Harness(cfg, models, sessions)
    async with h.client() as client:
        out = await h.chat(client)
    assert len(h.elicitations) == 1
    e = h.elicitations[0]
    assert e["message"].startswith(APPROVAL_PREFIX)
    assert "primitive_cube_add" in e["message"]
    assert e["schema"] == {"type": "object", "properties": {}}
    assert h.rec.named("execute_code")[0]["args"]["target_uuid"] == BLENDER
    assert out["steps"][0] == {**out["steps"][0], "tool": "execute_code", "ok": True}


async def test_confirm_tool_decline_is_fed_back(cfg, sessions):
    models = FakeModels(oa_call("execute_code", {"code": "print(1)"}), oa_text("Okay, skipped."))
    h = Harness(cfg, models, sessions)
    h.elicit_answer = "decline"
    async with h.client() as client:
        out = await h.chat(client)
    assert h.rec.named("execute_code") == []
    assert out["steps"][0]["ok"] is False and out["reply"] == "Okay, skipped."
    tool_msg = models.requests[1]["body"]["messages"][-1]
    assert tool_msg["role"] == "tool" and "declined" in tool_msg["content"]


async def test_approval_times_out_as_decline(cfg, sessions):
    models = FakeModels(oa_call("execute_code", {"code": "print(1)"}), oa_text("No answer, skipped."))
    h = Harness(replace(cfg, approval_timeout_s=1), models, sessions)

    async def too_late(message, response_type, params, context):
        # The MCP client runs this inline in its receive loop, so it has to
        # return before the tool result can be read: answer after the
        # server's deadline has already passed.
        await asyncio.sleep(1.5)
        return ElicitResult(action="accept")

    h._elicit = too_late
    async with h.client() as client:
        out = await h.chat(client)
    assert h.rec.named("execute_code") == []
    assert out["status"] == "ok" and out["steps"][0]["ok"] is False


async def test_confirm_only_when_policy_says_so(cfg, sessions):
    models = FakeModels(oa_call("create_mesh", {"name": "A"}), oa_text("ok"))
    h = Harness(cfg, models, sessions)
    async with h.client() as client:
        await h.chat(client)
    assert h.elicitations == []


# ---- flags, busy, backends ---------------------------------------------------

async def test_disabled_flag(cfg, sessions):
    h = Harness(replace(cfg, enabled=False), FakeModels(), sessions)
    async with h.client() as client:
        out = await h.chat(client)
    assert out == {"status": "disabled"}
    assert h.models.requests == []


async def test_llm_clients_cannot_call_chat(cfg, sessions):
    h = Harness(cfg, FakeModels(), sessions)
    c = client_role.current_downstream_client_id.set("some-llm-client")
    try:
        async with Client(h.server) as client:
            res = await client.call_tool("blender_chat", {"message": "hi"})
    finally:
        client_role.current_downstream_client_id.reset(c)
    assert json.loads(res.content[0].text)["status"] == "wrong_role"


async def test_second_turn_while_one_runs_is_busy(cfg, sessions):
    models = FakeModels(oa_text("first done"))
    models.gate = asyncio.Event()
    h = Harness(cfg, models, sessions)
    async with h.client() as client:
        first = asyncio.create_task(h.chat(client, "one"))
        await asyncio.wait_for(models.entered.wait(), 5)
        second = await h.chat(client, "two")
        models.gate.set()
        out = await first
    assert second == {"status": "busy"}
    assert out["status"] == "ok" and out["reply"] == "first done"
    # The slot frees up afterwards.
    models.gate = None
    async with h.client() as client:
        assert (await h.chat(client, "three"))["status"] == "ok"


async def test_turn_timeout(cfg, sessions):
    models = FakeModels(oa_text("too late"))
    models.gate = asyncio.Event()
    h = Harness(replace(cfg, turn_timeout_s=1), models, sessions)
    async with h.client() as client:
        out = await h.chat(client)
    assert out["status"] == "timeout"
    models.gate.set()


async def test_no_backend_when_gateway_is_limited(cfg, sessions):
    h = Harness(replace(cfg, gateway_users=frozenset({"someone-else"})), FakeModels(), sessions)
    async with h.client() as client:
        out = await h.chat(client)
    assert out["status"] == "no_backend" and "Chat backend" in out["hint"]


async def test_per_user_anthropic_backend(cfg, sessions):
    key = "sk-ant-test-" + "x" * 20
    models = FakeModels(an_call("create_mesh", {"name": "Lamp", "target_uuid": "nope"}, thinking=True),
                        an_text("Made a lamp."))
    h = Harness(cfg, models, sessions)
    async with h.client() as client:
        res = await client.call_tool("blender_set_chat_backend",
                                     {"provider": "anthropic", "api_key": key})
        saved = json.loads(res.content[0].text)
        got = json.loads((await client.call_tool("blender_get_chat_backend", {})).content[0].text)
        out = await h.chat(client, "a lamp please")

    assert saved == {"status": "ok", "backend": {"provider": "anthropic", "model": "claude-opus-5",
                                                 "base_url": None, "has_key": True, "source": "user"}}
    assert got == saved
    assert key not in json.dumps(saved) + json.dumps(got) + json.dumps(out)

    # Encrypted at rest.
    async with sessions() as s:
        row = await s.get(ChatSettings, USER)
    assert row.api_key_enc and key not in row.api_key_enc
    assert Fernet(cfg.secret_key.encode()).decrypt(row.api_key_enc.encode()).decode() == key

    assert out["status"] == "ok" and out["reply"] == "Made a lamp."
    assert out["backend"] == {"provider": "anthropic", "model": "claude-opus-5"}
    first = models.requests[0]
    assert first["url"].split("?")[0] == "https://api.anthropic.com/v1/messages"
    assert first["headers"]["x-api-key"] == key
    assert first["headers"]["anthropic-version"] == "2023-06-01"
    assert "authorization" not in first["headers"]
    assert first["body"]["model"] == "claude-opus-5"
    assert first["body"]["max_tokens"] >= 16000 and first["body"]["fallbacks"] == "default"
    assert {t["name"] for t in first["body"]["tools"]} >= {"create_mesh"}
    assert h.rec.named("create_mesh")[0]["args"]["target_uuid"] == BLENDER
    second = models.requests[1]["body"]["messages"]
    # The first round's thinking block goes back unchanged with its tool_use.
    assert second[1]["content"][0] == {"type": "thinking", "thinking": "", "signature": "sig-toolu_1"}
    assert second[-1]["role"] == "user"
    assert second[-1]["content"][0]["type"] == "tool_result"
    assert second[-1]["content"][0]["tool_use_id"] == "toolu_1"


async def test_server_default_anthropic_backend(cfg, sessions):
    server_key = "sk-ant-server-" + "z" * 24
    models = FakeModels(an_call("create_mesh", {"name": "Crate"}), an_text("Made a crate."),
                        oa_text("from their ollama"))
    h = Harness(replace(cfg, default_provider="anthropic", anthropic_api_key=server_key), models, sessions)
    async with h.client() as client:
        got = json.loads((await client.call_tool("blender_get_chat_backend", {})).content[0].text)
        out = await h.chat(client, "a crate")
        # The user's own backend still overrides the server default.
        await client.call_tool("blender_set_chat_backend", {
            "provider": "openai", "base_url": "http://ollama.internal:11434/v1", "model": "llama3"})
        out2 = await h.chat(client)
    assert got == {"status": "ok", "backend": {"provider": "anthropic", "model": "claude-opus-5",
                                               "base_url": None, "has_key": True, "source": "server"}}
    assert server_key not in json.dumps(got) + json.dumps(out) + json.dumps(out2)
    assert out["status"] == "ok" and out["reply"] == "Made a crate."
    assert out["backend"] == {"provider": "anthropic", "model": "claude-opus-5"}
    assert models.requests[0]["headers"]["x-api-key"] == server_key
    assert out2["backend"] == {"provider": "openai", "model": "llama3"}
    assert models.requests[2]["url"] == "http://ollama.internal:11434/v1/chat/completions"


async def test_per_user_openai_backend_and_clear(cfg, sessions):
    models = FakeModels(oa_text("from ollama"), oa_text("from gateway"))
    h = Harness(cfg, models, sessions)
    async with h.client() as client:
        await client.call_tool("blender_set_chat_backend", {
            "provider": "openai", "base_url": "http://ollama.internal:11434/v1", "model": "llama3"})
        out = await h.chat(client)
        await client.call_tool("blender_set_chat_backend", {"provider": "gateway", "clear": True})
        out2 = await h.chat(client)
    assert out["backend"] == {"provider": "openai", "model": "llama3"}
    assert models.requests[0]["url"] == "http://ollama.internal:11434/v1/chat/completions"
    assert "authorization" not in models.requests[0]["headers"]
    assert out2["backend"] == {"provider": "gateway", "model": "qwen3"}


async def test_set_backend_refuses_bad_input(cfg, sessions):
    h = Harness(cfg, FakeModels(), sessions)
    async with h.client() as client:
        async def call(**args):
            res = await client.call_tool("blender_set_chat_backend", args)
            return json.loads(res.content[0].text)

        assert (await call(provider="nope"))["error"] == "invalid_provider"
        assert (await call(provider="anthropic"))["error"] == "api_key_required"
        assert (await call(provider="openai", model="m"))["error"] == "invalid_argument"
        h.cfg = replace(cfg, secret_key="")
        assert (await call(provider="anthropic", api_key="k"))["error"] == "secret_key_not_configured"


# ---- vision --------------------------------------------------------------------

class FakeStore:
    def __init__(self):
        self.keys = []

    async def get_bytes(self, key, max_bytes):
        self.keys.append(key)
        return b"\x89PNG fake"


async def test_look_at_viewport(cfg, sessions, monkeypatch):
    store = FakeStore()
    monkeypatch.setattr(vision, "get_store", lambda: store)
    models = FakeModels(oa_call("look_at_viewport", {"question": "is the cube red?"}),
                        oa_text("A red cube sits on the floor."),
                        oa_text("Yes, it's red."))
    h = Harness(cfg, models, sessions)
    async with h.client() as client:
        out = await h.chat(client)
    assert out["status"] == "ok" and out["reply"] == "Yes, it's red."
    shot = h.rec.named("get_viewport_screenshot")[0]["args"]
    assert shot["store"] is True and shot["max_size"] == 1024 and shot["target_uuid"] == BLENDER
    assert store.keys == [f"{BUS}/{'a' * 32}/viewport.png"]
    vis = models.requests[1]["body"]
    assert vis["model"] == cfg.vision_model
    parts = vis["messages"][-1]["content"]
    assert parts[0] == {"type": "text", "text": "is the cube red?"}
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert "red cube" in models.requests[2]["body"]["messages"][-1]["content"]


async def test_catalog_strips_aiming_args_and_filters(cfg, sessions):
    h = Harness(cfg, FakeModels(), sessions)
    cat = {e.name: e for e in await build_catalog(h.server)}
    assert "look_at_viewport" not in cat  # no storage
    assert set(cat) == {"get_scene_info", "get_object_info", "create_mesh", "execute_code"}
    for e in cat.values():
        props = e.parameters.get("properties", {})
        assert not {"target_uuid", "bus_id", "_timeout", "store", "ctx"} & set(props)
    assert cat["get_object_info"].parameters["required"] == ["name"]
    assert cat["execute_code"].policy.needs_confirm({"code": "x"})
    assert not cat["create_mesh"].policy.needs_confirm({})

    with_vision = {e.name for e in await build_catalog(h.server, vision=True)}
    assert "look_at_viewport" in with_vision
    custom = {e.name for e in await build_catalog(
        h.server, frozenset({"blender_create_mesh", "list_buses", "submit", "missing"}))}
    assert custom == {"create_mesh"}


async def test_time_waiting_for_approval_is_reported_apart(cfg, sessions):
    models = FakeModels(oa_call("execute_code", {"code": "print(1)"}), oa_text("Ran it."))
    h = Harness(cfg, models, sessions)

    async def slow_click(message, response_type, params, context):
        await asyncio.sleep(0.3)  # the user takes a moment
        return ElicitResult(action="accept")

    h._elicit = slow_click
    async with h.client() as client:
        out = await h.chat(client)
    step = out["steps"][0]
    assert step["wait_ms"] >= 300 and step["ms"] < step["wait_ms"]
    end = [e for e in h.events if e.get("t") == "tool" and e.get("phase") == "end"][0]
    assert end["wait_ms"] == step["wait_ms"] and end["ms"] == step["ms"]


async def test_steps_without_approval_carry_no_wait(cfg, sessions):
    models = FakeModels(oa_call("get_scene_info", {}), oa_text("Here it is."))
    h = Harness(cfg, models, sessions)
    async with h.client() as client:
        out = await h.chat(client)
    assert "wait_ms" not in out["steps"][0]
