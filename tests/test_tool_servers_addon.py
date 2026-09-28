"""Add-on half of "Bring Your Own Tools": pure logic plus a real stdio server.

The runner test starts a tiny FastMCP app with sys.executable, lists its
tools, calls one, survives a crash and restarts on the next call. No bpy.
"""

import json
import shlex
import sys
import textwrap
import threading
import time
from types import SimpleNamespace

import pytest

from addon.tool_servers import config
from addon.tool_servers.config import (
    ConfigError,
    ServerSpec,
    build_launch,
    expand_vars,
    kind_of,
    name_problem,
    parse_env,
    sanitize_name,
    snapshot_problems,
    split_command,
)
from addon.tool_servers.report import (
    DESCRIPTION_LIMIT,
    MAX_TOOLS,
    SCHEMA_LIMIT,
    build_report,
)
from addon.tool_servers.results import TEXT_LIMIT, failure, shape_result
from addon.tool_servers.runner import ERROR, OFF, RUNNING, ToolServerRunner

# --- config ------------------------------------------------------------------


def test_kind_follows_the_start_field():
    assert kind_of("https://tools.example.com/mcp") == "http"
    assert kind_of("  HTTP://localhost:9000/mcp") == "http"
    assert kind_of("uvx mcp-pdf") == "stdio"
    assert kind_of("") == "stdio"


def test_expand_vars_reads_the_environment_and_names_missing_ones():
    env = {"TOKEN": "s3cret", "HOME": "/home/me"}
    assert expand_vars("${TOKEN}", env) == "s3cret"
    assert expand_vars("Bearer-${TOKEN}-${HOME}", env) == "Bearer-s3cret-/home/me"
    assert expand_vars("no vars $TOKEN", env) == "no vars $TOKEN"
    with pytest.raises(ConfigError, match="NOPE"):
        expand_vars("${NOPE}", env)


def test_parse_env_lines_and_semicolons():
    env = {"DOCS": "/srv/docs"}
    assert parse_env("A=1\nB = two words \n\n# comment\nC=${DOCS}/pdf", env) == {
        "A": "1", "B": "two words", "C": "/srv/docs/pdf"}
    assert parse_env("A=1; B=x=y", env) == {"A": "1", "B": "x=y"}
    assert parse_env("", env) == {}
    for bad in ("JUSTTEXT", "1BAD=x", "=x"):
        with pytest.raises(ConfigError):
            parse_env(bad, env)


def test_split_command_handles_quotes():
    assert split_command("uvx mcp-pdf --root '/my docs'") == ("uvx", ["mcp-pdf", "--root", "/my docs"])
    with pytest.raises(ConfigError):
        split_command("   ")
    with pytest.raises(ConfigError):
        split_command("uvx 'unterminated")


def test_name_rules():
    assert name_problem("pdf") is None
    assert name_problem("dwg-tools-2") is None
    assert name_problem("") == "needs a name"
    assert "a-z" in name_problem("PDF")
    assert "a-z" in name_problem("pdf_reader")
    assert "24" in name_problem("x" * 25)
    assert sanitize_name(" My PDF_Reader ") == "my-pdf-reader"
    assert len(sanitize_name("a" * 40)) == config.NAME_MAX


def test_snapshot_problems_flags_bad_and_duplicate_names():
    specs = [ServerSpec("pdf", "a"), ServerSpec("pdf", "b"), ServerSpec("Bad!", "c"),
             ServerSpec("ok", "d")]
    problems = snapshot_problems(specs)
    assert problems["pdf"] == "another server has this name"
    assert "Bad!" in problems
    assert "ok" not in problems


def test_stdio_launch_merges_env_onto_the_environment():
    spec = ServerSpec("pdf", "uvx mcp-pdf", env_text="PDF_ROOT=${HOME}/docs")
    launch = build_launch(spec, {"HOME": "/home/me", "PATH": "/usr/bin"})
    assert launch.kind == "stdio"
    assert (launch.command, launch.args) == ("uvx", ["mcp-pdf"])
    assert launch.env == {"HOME": "/home/me", "PATH": "/usr/bin", "PDF_ROOT": "/home/me/docs"}


def test_http_launch_sends_an_expanded_bearer_token():
    spec = ServerSpec("cad", "https://cad.example.com/mcp", token="${CAD_TOKEN}")
    launch = build_launch(spec, {"CAD_TOKEN": "abc"})
    assert launch.kind == "http"
    assert launch.url == "https://cad.example.com/mcp"
    assert launch.headers == {"Authorization": "Bearer abc"}
    assert build_launch(ServerSpec("cad", "https://cad.example.com/mcp"), {}).headers == {}
    with pytest.raises(ConfigError, match="CAD_TOKEN"):
        build_launch(spec, {})


def test_tokens_never_go_over_plain_http_except_to_localhost():
    with pytest.raises(ConfigError, match="https"):
        build_launch(ServerSpec("x", "http://tools.example.com/mcp", token="t"), {})
    ok = build_launch(ServerSpec("x", "http://localhost:9000/mcp", token="t"), {})
    assert ok.headers["Authorization"] == "Bearer t"


def test_launch_key_ignores_trust_and_timeout():
    a = ServerSpec("pdf", "uvx mcp-pdf", trusted=False, timeout_s=60)
    b = ServerSpec("pdf", "uvx mcp-pdf", trusted=True, timeout_s=10)
    c = ServerSpec("pdf", "uvx mcp-pdf2")
    assert a.launch_key() == b.launch_key() != c.launch_key()


# --- results -------------------------------------------------------------------


def _text(t):
    return SimpleNamespace(type="text", text=t)


def test_shape_joins_text_and_notes_images():
    result = SimpleNamespace(content=[
        _text("page 1"),
        SimpleNamespace(type="image", mimeType="image/png", data="A" * 4096),
        _text("page 2"),
    ], is_error=False, structured_content=None)
    reply = shape_result(result)
    assert reply["ok"] is True and reply["truncated"] is False
    lines = reply["text"].splitlines()
    assert lines[0] == "page 1" and lines[2] == "page 2"
    assert "image omitted" in lines[1] and "image/png" in lines[1] and "3 KB" in lines[1]


def test_shape_truncates_to_the_limit():
    reply = shape_result(SimpleNamespace(content=[_text("x" * (TEXT_LIMIT + 50))], is_error=False))
    assert reply["truncated"] is True and len(reply["text"]) == TEXT_LIMIT
    exact = shape_result(SimpleNamespace(content=[_text("y" * TEXT_LIMIT)], is_error=False))
    assert exact["truncated"] is False


def test_shape_marks_tool_errors_and_falls_back_to_structured_content():
    err = shape_result(SimpleNamespace(content=[_text("file not found")], is_error=True))
    assert err == {"ok": False, "text": "file not found", "truncated": False}
    structured = shape_result(SimpleNamespace(content=[], is_error=False,
                                              structured_content={"pages": 3}))
    assert json.loads(structured["text"]) == {"pages": 3}
    assert failure("boom") == {"ok": False, "text": "boom", "truncated": False}


def test_shape_embedded_resources():
    res_text = SimpleNamespace(type="resource", resource=SimpleNamespace(text="inline", uri="file:///a"))
    res_blob = SimpleNamespace(type="resource", resource=SimpleNamespace(blob="AAAA", uri="file:///b"))
    reply = shape_result(SimpleNamespace(content=[res_text, res_blob], is_error=False))
    assert reply["text"].splitlines() == ["inline", "[binary resource omitted: file:///b]"]


# --- report --------------------------------------------------------------------


def _tool(name, description="", schema=None):
    return SimpleNamespace(name=name, description=description,
                           inputSchema=schema or {"type": "object", "properties": {}})


def test_report_payload_shape():
    tools, dropped = build_report([
        ("pdf", False, [_tool("read_text", "Read a PDF")]),
        ("cad", True, [{"name": "read_dxf", "description": "DXF", "input_schema": {"type": "object"}}],
         120.4),
    ])
    assert dropped == []
    assert tools == [
        {"server": "pdf", "name": "read_text", "description": "Read a PDF",
         "input_schema": {"type": "object", "properties": {}}, "trusted": False, "timeout_s": 60},
        {"server": "cad", "name": "read_dxf", "description": "DXF",
         "input_schema": {"type": "object"}, "trusted": True, "timeout_s": 120},
    ]
    assert build_report([]) == ([], [])


# --- user_tool_call resolution ---------------------------------------------------


def test_resolve_call_refuses_servers_not_in_the_preferences():
    from addon.tool_servers.bridge import resolve_call

    specs = [ServerSpec("pdf", "uvx mcp-pdf", timeout_s=30)]
    for params in ({"server": "evil", "tool": "x"}, {"tool": "x"}, {"server": ""}, None,
                   {"server": "evil", "tool": "x", "command": "rm -rf /", "start": "sh"}):
        spec, *_, refusal = resolve_call(specs, params)
        assert spec is None and refusal == {"ok": False, "text": "unknown tool server", "truncated": False}


def test_resolve_call_takes_only_the_name_from_params():
    from addon.tool_servers.bridge import resolve_call

    specs = [ServerSpec("pdf", "uvx mcp-pdf", timeout_s=30)]
    spec, tool, args, timeout, refusal = resolve_call(specs, {
        "server": "pdf", "tool": "read_text", "arguments": {"path": "a.pdf"},
        "timeout_s": 600, "start": "sh -c evil", "command": "evil"})
    assert refusal is None and spec is specs[0] and spec.start == "uvx mcp-pdf"
    assert (tool, args) == ("read_text", {"path": "a.pdf"})
    assert timeout == 30  # never longer than the user's own setting
    _, _, args, timeout, _ = resolve_call(specs, {"server": "pdf", "tool": "t",
                                                  "arguments": "junk", "timeout_s": 5})
    assert args == {} and timeout == 5


def test_resolve_call_refuses_disabled_and_ambiguous_servers():
    from addon.tool_servers.bridge import resolve_call

    off = resolve_call([ServerSpec("pdf", "x", enabled=False)], {"server": "pdf", "tool": "t"})[-1]
    assert off["ok"] is False and "turned off" in off["text"]
    dup = resolve_call([ServerSpec("pdf", "x"), ServerSpec("pdf", "y")], {"server": "pdf", "tool": "t"})[-1]
    assert dup["ok"] is False and "another server" in dup["text"]
    no_tool = resolve_call([ServerSpec("pdf", "x")], {"server": "pdf"})[-1]
    assert no_tool["ok"] is False


def test_describe_status_lines():
    from addon.tool_servers.runner import describe

    assert describe(None) == ("none", "not started")
    assert describe({"state": "starting"})[1] == "starting…"
    assert describe({"state": "running", "tools": [1, 2, 3]})[1] == "running · 3 tools"
    assert describe({"state": "running", "tools": [1]})[1] == "running · 1 tool"
    assert describe({"state": "error", "error": "boom"}) == ("error", "boom")
    assert describe({"state": "off"}) == ("off", "off")


def test_report_applies_the_contract_limits():
    big_schema = {"type": "object", "description": "z" * (SCHEMA_LIMIT + 1)}
    many = [_tool(f"t{i}", "d" * (DESCRIPTION_LIMIT + 10)) for i in range(MAX_TOOLS + 3)]
    tools, dropped = build_report([
        ("big", False, [_tool("huge", schema=big_schema)]),
        ("many", True, many),
    ])
    assert len(tools) == MAX_TOOLS
    assert all(len(t["description"]) == DESCRIPTION_LIMIT for t in tools)
    reasons = {(d["server"], d["name"]): d["reason"] for d in dropped}
    assert "input schema" in reasons[("big", "huge")]
    assert sum(1 for d in dropped if d["server"] == "many") == 3


# --- chat transcript -------------------------------------------------------------


def test_chat_tool_line_shows_the_server():
    from addon.chat.state import ChatState, tool_line

    assert tool_line({"name": "pdf__read_text", "server": "pdf", "ms": 1200}) == "pdf · read_text  1.20 s"
    assert tool_line({"name": "read_text_2", "server": "pdf"}) == "pdf · read_text_2"
    assert tool_line({"name": "set_view", "ms": 307}) == "set_view  0.31 s"

    st = ChatState()
    st.begin_turn("read the spec")
    st.apply_event({"t": "tool", "name": "pdf__read_text", "server": "pdf", "phase": "start"})
    st.apply_event({"t": "tool", "name": "pdf__read_text", "server": "pdf", "phase": "end",
                    "ok": True, "ms": 1200})
    tools = [m for m in st.messages if m["role"] == "tool"]
    assert len(tools) == 1 and tools[0]["server"] == "pdf" and tools[0]["ok"] is True
    assert st.drain_log()[-1].endswith("tool pdf · read_text  1.20 s ok")

    # An end event without a start still records the server.
    st.apply_event({"t": "tool", "name": "pdf__info", "server": "pdf", "phase": "end", "ok": False})
    assert st.messages[-1]["server"] == "pdf"


# --- runner with a real stdio server ------------------------------------------------

TINY_SERVER = textwrap.dedent('''
    import os
    from fastmcp import FastMCP

    mcp = FastMCP("tiny")

    @mcp.tool
    def echo(text: str) -> str:
        """Echo text back."""
        return os.environ.get("PREFIX", "") + text

    @mcp.tool
    def crash() -> str:
        """Exit the process."""
        os._exit(3)

    @mcp.tool
    def slow(seconds: float) -> str:
        """Sleep, then answer."""
        import time
        time.sleep(seconds)
        return "done"

    if __name__ == "__main__":
        mcp.run(show_banner=False)
''')


def _wait_for(pred, timeout=60.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.1)
    return False


def _call(runner, spec, tool, args, timeout_s=20.0):
    done = threading.Event()
    out = {}

    def cb(reply):
        out["thread"] = threading.current_thread()
        out.update(reply)
        done.set()

    runner.call(spec, tool, args, timeout_s, cb)
    assert done.wait(timeout_s + 30), f"{tool} never answered"
    return out


@pytest.fixture
def tiny(tmp_path):
    path = tmp_path / "tiny_server.py"
    path.write_text(TINY_SERVER)
    changes = []
    runner = ToolServerRunner(on_change=lambda: changes.append(time.monotonic()),
                              start_timeout_s=60)
    spec = ServerSpec("tiny", f"{shlex.quote(sys.executable)} {shlex.quote(str(path))}",
                      env_text="PREFIX=>>", trusted=True, timeout_s=20)
    yield runner, spec, changes
    runner.stop()


def test_runner_starts_lists_calls_and_restarts_after_a_crash(tiny):
    runner, spec, changes = tiny
    runner.apply([spec]).result(10)
    assert _wait_for(lambda: (runner.status("tiny") or {}).get("state") != "starting")
    st = runner.status("tiny")
    assert st["state"] == RUNNING, st
    assert {t.name for t in st["tools"]} == {"echo", "crash", "slow"}
    assert changes, "status changes notify"

    entries = runner.report_entries()
    assert [(n, trusted, t) for n, trusted, _, t in entries] == [("tiny", True, 20.0)]
    tools, dropped = build_report(entries)
    assert {t["name"] for t in tools} == {"echo", "crash", "slow"} and dropped == []
    assert {t["timeout_s"] for t in tools} == {20}
    echo = next(t for t in tools if t["name"] == "echo")
    assert echo["input_schema"]["properties"]["text"]["type"] == "string"

    reply = _call(runner, spec, "echo", {"text": "hi"})
    assert reply["ok"] is True and reply["text"] == ">>hi" and reply["truncated"] is False
    assert reply["thread"] is runner.thread  # the reply comes from the runner's own thread

    crashed = _call(runner, spec, "crash", {})
    assert crashed["ok"] is False and crashed["text"]
    assert _wait_for(lambda: runner.status("tiny")["state"] == ERROR, 10)
    # Its tools stay reported: the next call starts it again.
    assert runner.report_entries()

    again = _call(runner, spec, "echo", {"text": "again"})
    assert again == {"ok": True, "text": ">>again", "truncated": False, "thread": runner.thread}
    assert runner.status("tiny")["state"] == RUNNING


def test_runner_timeouts_unknown_tools_and_disable(tiny):
    runner, spec, _ = tiny
    slow = _call(runner, spec, "slow", {"seconds": 5}, timeout_s=1.0)
    assert slow["ok"] is False and "timed out" in slow["text"]

    missing = _call(runner, spec, "nope", {})
    assert missing["ok"] is False and missing["text"]

    off = ServerSpec(spec.name, spec.start, spec.env_text, enabled=False)
    runner.apply([off]).result(15)
    assert runner.status("tiny")["state"] == OFF
    assert runner.report_entries() == []

    runner.apply([]).result(15)
    assert runner.status("tiny") is None


def test_concurrent_calls_after_a_crash_share_one_restart(tiny):
    runner, spec, _ = tiny
    assert _call(runner, spec, "echo", {"text": "a"})["ok"] is True
    assert _call(runner, spec, "crash", {})["ok"] is False
    replies, threads = [], []
    for word in ("x", "y", "z"):
        t = threading.Thread(target=lambda w=word: replies.append(_call(runner, spec, "echo", {"text": w})))
        t.start()
        threads.append(t)
    for t in threads:
        t.join(60)
    assert sorted(r["text"] for r in replies) == [">>x", ">>y", ">>z"]
    assert all(r["ok"] for r in replies)
    assert len(runner._servers) == 1


def test_restart_is_what_the_test_button_uses(tiny):
    runner, spec, _ = tiny
    status = runner.restart(spec).result(60)
    assert status["state"] == RUNNING and len(status["tools"]) == 3


def _stub_blender_modules():
    # addon.client's package init imports the Blender runtime; job_reporter
    # doesn't use it (same stand-in as test_job_outbox.py).
    import types
    for name in ("bpy", "bmesh", "mathutils"):
        sys.modules.setdefault(name, types.ModuleType(name))


class _StubBusClient:
    """Just enough of BlenderMCPClient for submit_job_update and report()."""

    def __init__(self, loop=None, mcp=None):
        import collections
        _stub_blender_modules()
        self.loop, self.client, self.connected = loop, mcp, mcp is not None
        self.job_outbox = collections.deque()
        self.worker_mode = False


def test_bridge_refuses_unknown_servers_through_the_job_reply():
    from addon.tool_servers.bridge import ToolServerBridge

    bridge = ToolServerBridge()
    try:
        client = _StubBusClient()  # not connected: replies wait in the outbox
        bridge.handle_call(client, "job-1", [ServerSpec("pdf", "uvx mcp-pdf")],
                           {"server": "sh", "tool": "x", "start": "sh -c 'rm -rf ~'"})
        update = client.job_outbox.popleft()
        assert update["job_id"] == "job-1" and update["status"] == "completed"
        assert json.loads(update["result"]) == {"ok": False, "text": "unknown tool server",
                                                "truncated": False}
        assert not bridge.runner.running  # nothing was started for it
    finally:
        bridge.shutdown()


def test_bridge_replies_from_the_runner_thread(tiny):
    from addon.tool_servers.bridge import ToolServerBridge

    runner, spec, _ = tiny
    bridge = ToolServerBridge()
    bridge.runner = runner
    try:
        client = _StubBusClient()
        bridge.handle_call(client, "job-2", [spec], {"server": "tiny", "tool": "echo",
                                                     "arguments": {"text": "hey"}, "timeout_s": 60})
        assert _wait_for(lambda: len(client.job_outbox) == 1, 30)
        assert json.loads(client.job_outbox[0]["result"]) == {"ok": True, "text": ">>hey",
                                                              "truncated": False}
    finally:
        bridge.runner = ToolServerRunner()  # the fixture stops the real one


def test_bridge_report_goes_out_on_the_bus_loop_once_per_change(monkeypatch):
    import asyncio

    from addon import state
    from addon.tool_servers.bridge import REPORT_TOOL, ToolServerBridge

    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    calls = []

    class _Mcp:
        async def call_tool(self, name, args, **kw):
            calls.append((name, args, threading.current_thread()))
            return SimpleNamespace(content=[SimpleNamespace(text=json.dumps(
                {"status": "ok", "accepted": len(args["tools"]), "dropped": []}))], is_error=False)

    client = _StubBusClient(loop, _Mcp())
    monkeypatch.setattr(state, "_client", client)
    bridge = ToolServerBridge()
    try:
        entries = [("pdf", False, [_tool("read_text", "Read")], 45)]
        monkeypatch.setattr(bridge.runner, "report_entries", lambda: entries)
        assert bridge.report() is True
        assert _wait_for(lambda: bridge.last_report.get("accepted") == 1, 5)
        name, args, where = calls[0]
        assert name == REPORT_TOOL and where is thread
        assert args["tools"][0] == {"server": "pdf", "name": "read_text", "description": "Read",
                                    "input_schema": {"type": "object", "properties": {}},
                                    "trusted": False, "timeout_s": 45}
        assert bridge.report() is False  # unchanged: not sent again
        assert bridge.report(force=True) is True  # after a registration it is
        entries[:] = []
        assert bridge.report() is True  # an empty list clears the server's set
        assert _wait_for(lambda: len(calls) == 3, 5) and calls[-1][1] == {"tools": []}
    finally:
        bridge.shutdown()
        loop.call_soon_threadsafe(loop.stop)
        thread.join(5)


def test_runner_reports_a_config_problem_as_an_error():
    runner = ToolServerRunner(start_timeout_s=5)
    try:
        spec = ServerSpec("cad", "https://cad.example.com/mcp", token="${SURELY_NOT_SET_12345}")
        runner.apply([spec]).result(10)
        assert _wait_for(lambda: (runner.status("cad") or {}).get("state") == ERROR, 10)
        assert "SURELY_NOT_SET_12345" in runner.status("cad")["error"]
        reply = _call(runner, spec, "anything", {}, timeout_s=5)
        assert reply["ok"] is False and "SURELY_NOT_SET_12345" in reply["text"]
    finally:
        runner.stop()
