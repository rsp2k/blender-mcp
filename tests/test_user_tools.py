# ruff: noqa: F811  (cfg/sessions/addon_identity are pytest fixtures imported from test_chat_flow)
"""Tools from the add-on's own tool servers: report, catalog merge, and calls.

Contract: docs-site/src/content/docs/how-to/tool-servers.mdx. The chat
harness and fake model endpoints come from test_chat_flow.
"""

import json
import uuid
from dataclasses import replace

import pytest
from fastmcp import Client, FastMCP
from test_chat_flow import (  # noqa: F401
    BLENDER,
    BUS,
    USER,
    FakeModels,
    Harness,
    addon_identity,
    cfg,
    oa_call,
    oa_text,
    sessions,
    tool_names,
)

from blender_mcp import bus_tools, client_role, dispatch_component
from blender_mcp.bus_tools import BlenderBusComponent
from blender_mcp.chat import user_tools
from blender_mcp.chat.catalog import DEFAULT_TOOLS, Entry, Policy
from blender_mcp.chat.report_tools import BlenderUserToolsComponent
from blender_mcp.chat.turn import APPROVAL_PREFIX, MAX_RESULT_CHARS
from blender_mcp.instrumentation import META_ONLY_TOOLS
from blender_mcp.job_tools import BlenderJobComponent
from blender_mcp.message_bus import MessageBus


def tool(server="pdf", name="read_text", description="Read a PDF's text.", schema=None,
         trusted=False):
    return {"server": server, "name": name, "description": description,
            "input_schema": schema if schema is not None else {
                "type": "object", "properties": {"path": {"type": "string"}},
                "required": ["path"]},
            "trusted": trusted}


@pytest.fixture(autouse=True)
def clean_registry():
    user_tools.registry.clear()
    yield
    user_tools.registry.clear()


class FakeDispatch:
    """Stands in for resolve_bus + _dispatch; records every user_tool_call."""

    def __init__(self, reply=None):
        self.calls: list[dict] = []
        self.reply = reply if reply is not None else self.completed("Page 1: the desk is 1.6 m wide.")
        self.bus = object()

    @staticmethod
    def completed(text, ok=True, truncated=False):
        return json.dumps({"status": "completed", "job_id": "j-1", "target_uuid": BLENDER,
                           "result": json.dumps({"ok": ok, "text": text, "truncated": truncated})})

    async def resolve_bus(self, user_id, bus_id_str=None):
        self.resolved = (user_id, bus_id_str)
        return {"ok": True, "bus": self.bus, "bus_id": uuid.UUID(bus_id_str), "name": "personal"}

    async def dispatch(self, bus, bus_id_str, command, params, target_uuid, timeout,
                       caller_sub=None):
        self.calls.append({"bus": bus, "bus_id": bus_id_str, "command": command,
                           "params": params, "target_uuid": target_uuid, "timeout": timeout,
                           "caller_sub": caller_sub})
        return self.reply


@pytest.fixture
def fake_dispatch(monkeypatch):
    fake = FakeDispatch()
    monkeypatch.setattr(bus_tools, "resolve_bus", fake.resolve_bus)
    monkeypatch.setattr(dispatch_component, "_dispatch", fake.dispatch)
    return fake


def harness(cfg, models, sessions):
    h = Harness(cfg, models, sessions)
    BlenderUserToolsComponent().register_tools(mcp_server=h.server, prefix="blender")
    return h


async def report(client, tools):
    res = await client.call_tool("blender_report_user_tools", {"tools": tools})
    return json.loads(res.content[0].text)


# ---- validation --------------------------------------------------------------

def test_report_limits_and_reasons():
    big_schema = {"type": "object", "properties": {"x": {"type": "string",
                                                         "description": "y" * 9000}}}
    items = [
        tool(description="d" * 1500),
        tool(name="read_text"),                   # duplicate of the first
        tool(server="pdf reader", name="a"),      # server not [A-Za-z0-9_-]
        tool(server="", name="b"),
        tool(name="has space"),
        tool(name=""),
        tool(name="big", schema=big_schema),
        tool(name="array", schema={"type": "array"}),
        tool(name="noschema", schema={}),
        "not a dict",
        {**tool(name="truthy"), "trusted": "yes"},
        tool(name="yes", trusted=True),
    ]
    accepted, dropped = user_tools.validate_report(items)
    assert [t.name for t in accepted] == ["read_text", "noschema", "truthy", "yes"]
    assert len(accepted[0].description) == 1000
    assert accepted[1].input_schema == {"type": "object", "properties": {}}
    assert [t.trusted for t in accepted] == [False, False, False, True]
    assert [d["reason"] for d in dropped] == [
        "duplicate", "invalid_server", "invalid_server", "invalid_name", "invalid_name",
        "schema_too_large", "invalid_schema", "not_an_object"]
    assert dropped[0] == {"server": "pdf", "name": "read_text", "reason": "duplicate"}


def test_report_timeout_field():
    items = [
        tool(name="default"),                                   # missing -> 60
        {**tool(name="none"), "timeout_s": None},               # null -> 60
        {**tool(name="low"), "timeout_s": 1},
        {**tool(name="high"), "timeout_s": 600},
        {**tool(name="zero"), "timeout_s": 0},
        {**tool(name="over"), "timeout_s": 601},
        {**tool(name="float"), "timeout_s": 30.5},
        {**tool(name="text"), "timeout_s": "60"},
        {**tool(name="bool"), "timeout_s": True},
    ]
    accepted, dropped = user_tools.validate_report(items)
    assert [(t.name, t.timeout_s) for t in accepted] == [
        ("default", 60), ("none", 60), ("low", 1), ("high", 600)]
    assert [(d["name"], d["reason"]) for d in dropped] == [
        ("zero", "invalid_timeout"), ("over", "invalid_timeout"), ("float", "invalid_timeout"),
        ("text", "invalid_timeout"), ("bool", "invalid_timeout")]
    merged = user_tools.merge([], accepted, max_tools=10)
    assert [e.user_timeout_s for e in merged] == [60, 60, 1, 600]


async def test_reported_timeout_is_sent_and_waited_on(cfg, sessions, fake_dispatch):
    models = FakeModels(oa_call("cad__read_dxf", {"path": "plan.dxf"}), oa_text("ok"))
    h = harness(cfg, models, sessions)
    async with h.client() as client:
        out = await report(client, [{**tool(server="cad", name="read_dxf", trusted=True),
                                     "timeout_s": 240}])
        await h.chat(client)
    assert out["accepted"] == 1
    call = fake_dispatch.calls[0]
    assert call["params"]["timeout_s"] == 240
    assert call["timeout"] == 240 + user_tools.DISPATCH_GRACE_S


def test_report_caps_at_80_tools():
    accepted, dropped = user_tools.validate_report([tool(name=f"t{i}") for i in range(83)])
    assert len(accepted) == 80
    assert [d["name"] for d in dropped] == ["t80", "t81", "t82"]
    assert {d["reason"] for d in dropped} == {"too_many_tools"}


def test_report_must_be_a_list():
    accepted, dropped = user_tools.validate_report({"server": "pdf"})
    assert accepted == [] and dropped[0]["reason"] == "not_a_list"


# ---- names and merge ---------------------------------------------------------

def test_chat_names_are_sanitised_bounded_and_unique():
    assert user_tools.chat_name("pdf", "read_text", set()) == "pdf__read_text"
    assert user_tools.chat_name("cad", "dxf.read/layers", set()) == "cad__dxf_read_layers"
    long = user_tools.chat_name("s", "x" * 100, set())
    assert len(long) == 64 and long.startswith("s__x")
    taken = {"pdf__read_text", "pdf__read_text_2"}
    assert user_tools.chat_name("pdf", "read_text", taken) == "pdf__read_text_3"
    long2 = user_tools.chat_name("s", "x" * 100, {long})
    assert len(long2) == 64 and long2.endswith("_2")


def _blender_entry(name):
    return Entry(name=name, server_name=f"blender_{name}", description="", parameters={},
                 policy=Policy())


def test_merge_blender_first_never_replaced_and_capped():
    blender = [_blender_entry("get_scene_info"), _blender_entry("pdf__read_text")]
    reported, _ = user_tools.validate_report([
        tool(), tool(server="pdf", name="read.text"), tool(name="search", trusted=True),
        tool(name="extra")])
    merged = user_tools.merge(blender, reported, max_tools=5)
    assert [e.name for e in merged] == [
        "get_scene_info", "pdf__read_text", "pdf__read_text_2", "pdf__read_text_3", "pdf__search"]
    assert merged[1].server_name == "blender_pdf__read_text"  # the Blender tool kept its slot
    user = merged[2]
    assert (user.user_server, user.user_tool, user.server_name) == ("pdf", "read_text", None)
    assert merged[3].user_tool == "read.text"
    assert user.description.startswith("[From the user's 'pdf' tool server, not BlenderMCP.")
    assert user.description.endswith("Read a PDF's text.")
    assert user.policy.needs_confirm({"path": "a.pdf"})
    assert not merged[4].policy.needs_confirm({})
    assert '"path": "a.pdf"' in user.policy.preview({"path": "a.pdf"})
    # A cap below the Blender list cuts user tools entirely, Blender first.
    assert [e.name for e in user_tools.merge(blender, reported, max_tools=1)] == ["get_scene_info"]


# ---- the report tool ---------------------------------------------------------

async def test_report_tool_stores_and_hides_from_mcp(cfg, sessions):
    h = harness(cfg, FakeModels(), sessions)
    async with h.client() as client:
        out = await report(client, [tool(), tool(server="bad name")])
        listed = {t.name for t in await client.list_tools()}
    assert out == {"status": "ok", "accepted": 1,
                   "dropped": [{"server": "bad name", "name": "read_text", "reason": "invalid_server"}]}
    assert "blender_report_user_tools" in listed
    # Reported tools are never MCP tools, so no external client can list or call them.
    assert not any("read_text" in n for n in listed)
    assert "blender_report_user_tools" in META_ONLY_TOOLS


async def test_report_requires_the_addon_role(cfg, sessions):
    h = harness(cfg, FakeModels(), sessions)
    u = bus_tools.current_user_id.set(USER)
    c = client_role.current_downstream_client_id.set("some-llm-client")
    try:
        async with Client(h.server) as client:
            out = await report(client, [tool()])
    finally:
        client_role.current_downstream_client_id.reset(c)
        bus_tools.current_user_id.reset(u)
    assert out["status"] == "wrong_role"
    assert user_tools.registry._sets == {}


async def test_report_needs_a_registered_blender(cfg, sessions, monkeypatch):
    from blender_mcp.message_bus import bus_manager

    monkeypatch.setattr(bus_manager, "lookup_session", lambda session: None)
    h = harness(cfg, FakeModels(), sessions)
    async with h.client() as client:
        out = await report(client, [tool()])
    assert out["status"] == "error" and out["error"] == "not_registered"


async def test_a_set_belongs_to_the_session_that_reported_it(cfg, sessions):
    models = FakeModels(oa_text("hi"))
    h = harness(cfg, models, sessions)
    async with h.client() as client:
        await report(client, [tool()])
    assert user_tools.registry.get(BUS, BLENDER, object()) == []
    # A reconnect is a new session; until the add-on re-reports, no user tools.
    async with h.client() as client:
        await h.chat(client)
    assert "pdf__read_text" not in tool_names(models.requests[0])


async def test_empty_report_clears_the_set(cfg, sessions):
    h = harness(cfg, FakeModels(), sessions)
    async with h.client() as client:
        await report(client, [tool()])
        assert len(user_tools.registry._sets) == 1
        assert await report(client, []) == {"status": "ok", "accepted": 0, "dropped": []}
    assert user_tools.registry._sets == {}


def test_unregister_drops_the_set():
    bus = MessageBus(BUS)
    session = type("S", (), {})()
    reported, _ = user_tools.validate_report([tool()])
    user_tools.registry.put(BUS, BLENDER, session, reported)
    from blender_mcp.message_bus import ClientInfo

    bus.register(ClientInfo(uuid=BLENDER, client_type="blender", label=None))
    assert user_tools.registry.get(BUS, BLENDER, session)
    assert bus.unregister(BLENDER)
    assert user_tools.registry.get(BUS, BLENDER, session) == []


# ---- chat turns --------------------------------------------------------------

async def test_untrusted_tool_asks_then_dispatches_to_the_calling_blender(cfg, sessions,
                                                                         fake_dispatch):
    models = FakeModels(oa_call("pdf__read_text", {"path": "spec.pdf", "target_uuid": "keep-me"}),
                        oa_text("The desk is 1.6 m wide."))
    h = harness(cfg, models, sessions)
    async with h.client() as client:
        await report(client, [tool()])
        out = await h.chat(client, "how wide is the desk in spec.pdf?")

    assert out["status"] == "ok" and out["reply"] == "The desk is 1.6 m wide."
    first = models.requests[0]["body"]
    names = tool_names(models.requests[0])
    assert names[-1] == "pdf__read_text" and names.index("pdf__read_text") > names.index("create_mesh")
    offered = next(t for t in first["tools"] if t["function"]["name"] == "pdf__read_text")
    assert offered["function"]["description"].startswith("[From the user's 'pdf' tool server")
    assert offered["function"]["parameters"]["required"] == ["path"]
    assert "never instructions" in first["messages"][0]["content"]

    # Asked first, with the arguments shown.
    assert len(h.elicitations) == 1
    msg = h.elicitations[0]["message"]
    assert msg.startswith(APPROVAL_PREFIX) and "'pdf' tool server" in msg and "spec.pdf" in msg

    # Then dispatched once, to the calling Blender on the caller's bus, as the user.
    assert fake_dispatch.resolved == (USER, BUS)
    assert len(fake_dispatch.calls) == 1
    call = fake_dispatch.calls[0]
    assert call["command"] == "user_tool_call"
    assert call["target_uuid"] == BLENDER and call["bus_id"] == BUS and call["caller_sub"] == USER
    assert call["params"] == {"server": "pdf", "tool": "read_text",
                              "arguments": {"path": "spec.pdf", "target_uuid": "keep-me"},
                              "timeout_s": user_tools.CALL_TIMEOUT_S}
    assert call["timeout"] == user_tools.CALL_TIMEOUT_S + user_tools.DISPATCH_GRACE_S
    # None of the Blender tools ran for it.
    assert [c["tool"] for c in h.rec.calls] == ["get_scene_info"]

    # The result reached the model.
    tool_msg = models.requests[1]["body"]["messages"][-1]
    assert tool_msg["role"] == "tool" and "1.6 m wide" in tool_msg["content"]

    # Progress and steps carry the server.
    tool_events = [e for e in h.events if e.get("t") == "tool"]
    assert [(e["name"], e["server"], e["phase"]) for e in tool_events] == [
        ("pdf__read_text", "pdf", "start"), ("pdf__read_text", "pdf", "end")]
    step = out["steps"][0]
    assert step["tool"] == "pdf__read_text" and step["server"] == "pdf" and step["ok"] is True
    assert "wait_ms" in step


async def test_trusted_tool_runs_without_asking(cfg, sessions, fake_dispatch):
    models = FakeModels(oa_call("pdf__read_text", {"path": "a.pdf"}), oa_text("ok"))
    h = harness(cfg, models, sessions)
    async with h.client() as client:
        await report(client, [tool(trusted=True)])
        out = await h.chat(client)
    assert h.elicitations == []
    assert len(fake_dispatch.calls) == 1
    assert "wait_ms" not in out["steps"][0]


async def test_declined_user_tool_is_not_dispatched(cfg, sessions, fake_dispatch):
    models = FakeModels(oa_call("pdf__read_text", {"path": "a.pdf"}), oa_text("skipped"))
    h = harness(cfg, models, sessions)
    h.elicit_answer = "decline"
    async with h.client() as client:
        await report(client, [tool()])
        out = await h.chat(client)
    assert fake_dispatch.calls == []
    assert out["steps"][0]["ok"] is False
    assert "declined" in models.requests[1]["body"]["messages"][-1]["content"]


async def test_unreported_tool_name_is_rejected(cfg, sessions, fake_dispatch):
    models = FakeModels(oa_call("pdf__delete_all", {}), oa_text("can't"))
    h = harness(cfg, models, sessions)
    async with h.client() as client:
        await report(client, [tool()])
        out = await h.chat(client)
    assert fake_dispatch.calls == [] and out["steps"] == []
    assert "Unknown tool" in models.requests[1]["body"]["messages"][-1]["content"]


async def test_without_a_report_no_user_tools_are_offered(cfg, sessions, fake_dispatch):
    models = FakeModels(oa_call("pdf__read_text", {"path": "a.pdf"}), oa_text("no"))
    h = harness(cfg, models, sessions)
    async with h.client() as client:
        await h.chat(client)
    assert not any("__" in n for n in tool_names(models.requests[0]))
    assert fake_dispatch.calls == []


async def test_dispatch_timeout_is_a_failed_step(cfg, sessions, fake_dispatch):
    fake_dispatch.reply = json.dumps({"status": "running", "job_id": "j-2",
                                      "command": "user_tool_call", "hint": "Still running"})
    models = FakeModels(oa_call("pdf__read_text", {"path": "a.pdf"}), oa_text("it timed out"))
    h = harness(cfg, models, sessions)
    async with h.client() as client:
        await report(client, [tool(trusted=True)])
        out = await h.chat(client)
    assert out["steps"][0]["ok"] is False
    msg = models.requests[1]["body"]["messages"][-1]["content"]
    assert "pdf · read_text did not finish in time" in msg


async def test_addon_side_failure_and_long_text(cfg, sessions, fake_dispatch, monkeypatch):
    models = FakeModels(oa_call("pdf__read_text", {"path": "a.pdf"}),
                        oa_call("pdf__read_text", {"path": "b.pdf"}, call_id="call_2"),
                        oa_text("done"))
    replies = iter([FakeDispatch.completed("server crashed", ok=False),
                    FakeDispatch.completed("x" * 20000, truncated=True)])

    async def dispatch(*a, **kw):
        await FakeDispatch.dispatch(fake_dispatch, *a, **kw)
        return next(replies)

    monkeypatch.setattr(dispatch_component, "_dispatch", dispatch)
    h = harness(cfg, models, sessions)
    async with h.client() as client:
        await report(client, [tool(trusted=True)])
        out = await h.chat(client)
    assert [s["ok"] for s in out["steps"]] == [False, True]
    assert "server crashed" in models.requests[1]["body"]["messages"][-1]["content"]
    long = models.requests[2]["body"]["messages"][-1]["content"]
    assert long.startswith("x" * 100) and len(long) < MAX_RESULT_CHARS + 200
    assert "truncated" in long


async def test_chat_max_tools_caps_the_catalog(cfg, sessions, fake_dispatch):
    models = FakeModels(oa_text("hi"))
    h = harness(replace(cfg, max_tools=5), models, sessions)
    async with h.client() as client:
        await report(client, [tool(name=f"t{i}") for i in range(10)])
        await h.chat(client)
    names = tool_names(models.requests[0])
    assert names == ["get_scene_info", "get_object_info", "create_mesh", "execute_code", "pdf__t0"]
    assert len(DEFAULT_TOOLS) > 4  # the harness only has four of the Blender tools


async def test_sanitised_names_that_collide_get_suffixes_in_a_turn(cfg, sessions, fake_dispatch):
    models = FakeModels(oa_call("cad__read_dxf_2", {}), oa_text("hi"))
    h = harness(cfg, models, sessions)
    async with h.client() as client:
        await report(client, [tool(server="cad", name="read.dxf", trusted=True),
                              tool(server="cad", name="read/dxf", trusted=True)])
        await h.chat(client)
    assert tool_names(models.requests[0])[-2:] == ["cad__read_dxf", "cad__read_dxf_2"]
    assert fake_dispatch.calls[0]["params"]["tool"] == "read/dxf"


# ---- chat only ---------------------------------------------------------------

async def test_llm_clients_cannot_send_user_tool_call():
    server = FastMCP("guard-test")
    BlenderJobComponent().register_tools(mcp_server=server, prefix="blender")
    BlenderBusComponent().register_tools(mcp_server=server, prefix="blender")
    u = bus_tools.current_user_id.set(USER)
    c = client_role.current_downstream_client_id.set("some-llm-client")
    try:
        async with Client(server) as client:
            sub = await client.call_tool("blender_submit", {
                "command": "user_tool_call", "params": {"server": "pdf", "tool": "read_text"}})
            msg = await client.call_tool("blender_send_message", {"payload": {
                "message_type": "command_dispatch", "job_id": "j-x",
                "command": "user_tool_call", "params": {}}})
    finally:
        client_role.current_downstream_client_id.reset(c)
        bus_tools.current_user_id.reset(u)
    assert json.loads(sub.content[0].text)["error"] == "chat_only_command"
    assert json.loads(msg.content[0].text)["error"] == "chat_only_command"
