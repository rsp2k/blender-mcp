"""Add-on side of the in-Blender chat: pure logic, no bpy."""

import asyncio
import json
import threading
from types import SimpleNamespace

import pytest

from addon.chat import elicitation
from addon.chat import state as chat_mod
from addon.chat.state import (
    ChatState,
    approval_preview,
    build_history,
    decode_tool_result,
    format_log_line,
    is_approval_request,
    parse_progress_event,
    result_message,
    wrap_text,
)


def text_result(obj, is_error=False):
    body = obj if isinstance(obj, str) else json.dumps(obj)
    return SimpleNamespace(content=[SimpleNamespace(text=body)], data=None, is_error=is_error)


# --- progress events ---------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ('{"t": "status", "text": "Thinking…"}', {"t": "status", "text": "Thinking…"}),
    ('{"t": "tool", "name": "create_mesh", "phase": "start"}',
     {"t": "tool", "name": "create_mesh", "phase": "start"}),
    ('{"t": "text", "text": "Done"}', {"t": "text", "text": "Done"}),
])
def test_parse_progress_event_accepts_contract_shapes(raw, expected):
    assert parse_progress_event(raw) == expected


@pytest.mark.parametrize("raw", [
    None, "", "Thinking...", "[1, 2]", '{"t": "weird"}', '{"text": "no type"}',
    '{"t": "tool", "phase": "start"}',
])
def test_parse_progress_event_rejects_other_messages(raw):
    assert parse_progress_event(raw) is None


def test_tool_start_then_end_updates_one_row():
    st = ChatState()
    st.begin_turn("add a cube")
    st.apply_event({"t": "status", "text": "Thinking…"})
    assert st.status == "Thinking…"
    st.apply_event({"t": "tool", "name": "create_mesh", "phase": "start"})
    st.apply_event({"t": "tool", "name": "create_mesh", "phase": "end", "ok": True, "ms": 412})
    tools = [m for m in st.messages if m["role"] == "tool"]
    assert len(tools) == 1
    assert tools[0]["ok"] is True and tools[0]["ms"] == 412
    log = st.drain_log()
    assert any("tool create_mesh  0.41 s ok" in line for line in log)


def test_final_reply_not_duplicated_after_text_event():
    st = ChatState()
    st.begin_turn("add a cube")
    turn = st.turn
    st.apply_event({"t": "text", "text": "Added a cube."})
    payload = {"status": "ok", "reply": "Added a cube.", "steps": [],
               "backend": {"provider": "gateway", "model": "qwen3"}}
    role, text = result_message(payload)
    assert st.finish_turn(turn, role, text, payload)
    assert [m["text"] for m in st.messages if m["role"] == "assistant"] == ["Added a cube."]
    assert st.backend_used == {"provider": "gateway", "model": "qwen3"}
    assert not st.busy


def test_unfinished_tool_marked_failed_and_late_result_ignored():
    st = ChatState()
    st.begin_turn("go")
    turn = st.turn
    st.apply_event({"t": "tool", "name": "execute_code", "phase": "start"})
    assert st.finish_turn(turn, "status", "Stopped.")
    assert next(m for m in st.messages if m["role"] == "tool")["ok"] is False
    # The cancelled call's done callback arrives afterwards and changes nothing.
    assert not st.finish_turn(turn, "assistant", "late reply")
    assert all(m["text"] != "late reply" for m in st.messages)


def test_reply_header_uses_wall_time_not_tool_sum(monkeypatch):
    from addon.chat import reader

    clock = iter([100.0, 172.5])  # begin_turn, then finish_turn
    monkeypatch.setattr(chat_mod.time, "monotonic", lambda: next(clock))
    st = ChatState()
    st.begin_turn("make it glow")
    st.apply_event({"t": "tool", "name": "execute_code", "phase": "start"})
    st.apply_event({"t": "tool", "name": "execute_code", "phase": "end", "ok": True, "ms": 410})
    st.finish_turn(st.turn, "assistant", "Done.")
    turn = reader.turns(st.messages)[-1]
    assert reader.reply_header(turn) == "Claude · 1 step · 1m 12s"


# --- history -----------------------------------------------------------------

def test_history_is_text_turns_only_and_bounded():
    msgs = []
    for i in range(30):
        msgs.append({"role": "user", "text": f"u{i}"})
        msgs.append({"role": "tool", "text": "", "name": "x"})
        msgs.append({"role": "assistant", "text": f"a{i}"})
        msgs.append({"role": "error", "text": "boom"})
    hist = build_history(msgs)
    assert len(hist) == 20
    assert hist[-1] == {"role": "assistant", "content": "a29"}
    assert hist[0] == {"role": "user", "content": "u20"}
    assert {h["role"] for h in hist} == {"user", "assistant"}


def test_begin_turn_history_excludes_the_new_message():
    st = ChatState()
    assert st.begin_turn("first") == []
    st.finish_turn(st.turn, "assistant", "reply one")
    hist = st.begin_turn("second")
    assert hist == [{"role": "user", "content": "first"},
                    {"role": "assistant", "content": "reply one"}]


# --- wrapping ----------------------------------------------------------------

def test_wrap_text_fits_region_and_keeps_paragraphs():
    text = "word " * 40 + "\n\nsecond paragraph"
    lines = wrap_text(text, width_px=320, ui_scale=1.0)
    limit = int((320 - 40) / 7)
    assert all(len(line) <= limit for line in lines)
    assert "" in lines and lines[-1] == "second paragraph"


def test_wrap_text_scales_with_ui_and_has_a_floor():
    text = "x" * 200
    normal = wrap_text(text, 400, 1.0)
    big = wrap_text(text, 400, 2.0)
    assert len(big) > len(normal)
    assert all(len(line) >= 10 for line in wrap_text(text, 20, 1.0)[:-1])


# --- results -----------------------------------------------------------------

@pytest.mark.parametrize("payload,role,needle", [
    ({"status": "ok", "reply": "hi"}, "assistant", "hi"),
    ({"status": "disabled"}, "error", "turned off"),
    ({"status": "no_backend", "hint": "Set a key in prefs"}, "error", "No chat model"),
    ({"status": "no_backend"}, "error", "No chat model"),
    ({"status": "busy"}, "error", "already running"),
    ({"status": "backend_error", "detail": "HTTP 502"}, "error", "HTTP 502"),
    ({"status": "timeout"}, "error", "too long"),
    ({"status": "mystery"}, "error", "mystery"),
    (None, "error", "couldn't read"),
])
def test_result_message(payload, role, needle):
    got_role, text = result_message(payload)
    assert got_role == role
    assert needle in text


def test_decode_tool_result():
    assert decode_tool_result(text_result({"status": "ok"})) == {"status": "ok"}
    assert decode_tool_result(text_result("not json")) is None
    assert decode_tool_result(SimpleNamespace(content=[], data={"a": 1})) == {"a": 1}


def test_features_set_availability():
    st = ChatState()
    assert st.available is None
    st.set_features(["chat", "x"])
    assert st.available is True
    st.set_features(None)
    assert st.available is False


def test_log_line_format():
    line = format_log_line({"role": "user", "text": "two\nlines"}, when=0)
    assert "You: two\n    lines" in line


# --- approvals ---------------------------------------------------------------

def test_approval_routing_decision():
    assert is_approval_request("BlenderMCP approval: run Python?\n\nimport bpy")
    assert not is_approval_request("Please enter your name")
    assert not is_approval_request(" BlenderMCP approval: leading space")
    assert not is_approval_request(None)


def test_approval_preview_strips_prefix_and_counts_cut_lines():
    prompt = "BlenderMCP approval: run this Python?\n" + "\n".join(f"line {i}" for i in range(12))
    lines, cut = approval_preview(prompt, max_lines=8)
    assert lines[0] == "run this Python?"
    assert len(lines) == 8 and cut == 5


def test_approval_preview_collapses_blank_runs():
    prompt = "BlenderMCP approval: run this Python?\n\n\n\nimport bpy\n\n\n# glow\nx = 1"
    lines, cut = approval_preview(prompt, max_lines=8)
    assert lines == ["run this Python?", "", "import bpy", "", "# glow", "x = 1"]
    assert cut == 0


@pytest.fixture
def fresh_state(monkeypatch):
    st = ChatState()
    monkeypatch.setattr(elicitation, "chat_state", st)
    return st


async def test_unrecognised_elicitation_declined(fresh_state):
    result = await elicitation.handle_elicitation("What is your name?", None, None, None)
    assert result.action == "decline"
    assert fresh_state.pending_approval is None


async def test_approval_allowed_from_another_thread(fresh_state):
    task = asyncio.ensure_future(elicitation.handle_elicitation(
        "BlenderMCP approval: delete Cube?", None, None, None))
    while fresh_state.pending_approval is None:
        await asyncio.sleep(0.01)
    threading.Thread(target=elicitation.resolve_approval, args=(True,)).start()
    result = await asyncio.wait_for(task, 2)
    assert result.action == "accept" and result.content == {}
    assert fresh_state.pending_approval is None


async def test_approval_denied(fresh_state):
    task = asyncio.ensure_future(elicitation.handle_elicitation(
        "BlenderMCP approval: run Python?", None, None, None))
    while fresh_state.pending_approval is None:
        await asyncio.sleep(0.01)
    assert elicitation.resolve_approval(False)
    assert (await asyncio.wait_for(task, 2)).action == "decline"


async def test_approval_times_out_to_decline(fresh_state, monkeypatch):
    monkeypatch.setattr(chat_mod, "APPROVAL_TIMEOUT_S", 0.05)
    result = await elicitation.handle_elicitation(
        "BlenderMCP approval: run Python?", None, None, None)
    assert result.action == "decline"
    assert fresh_state.pending_approval is None
    assert any("two minutes" in m["text"] for m in fresh_state.messages)


def test_resolve_without_pending_is_false(fresh_state):
    assert elicitation.resolve_approval(True) is False


async def test_nonblocking_install_runs_elicitations_off_the_receive_loop():
    from mcp.types import ElicitRequest, ElicitRequestFormParams, PingRequest

    started, release = asyncio.Event(), asyncio.Event()
    handled = []

    class FakeGroup:
        def start_soon(self, fn, *args):
            asyncio.ensure_future(fn(*args))

    class FakeSession:
        _task_group = FakeGroup()

        async def _received_request(self, responder):
            handled.append(type(responder.request.root).__name__)
            if isinstance(responder.request.root, ElicitRequest):
                started.set()
                await release.wait()

    session = FakeSession()
    assert elicitation.install_nonblocking_elicitation(session)
    assert elicitation.install_nonblocking_elicitation(session)  # idempotent

    elicit = SimpleNamespace(request=SimpleNamespace(root=ElicitRequest(
        params=ElicitRequestFormParams(message="BlenderMCP approval: x",
                                       requestedSchema={"type": "object", "properties": {}}))))
    ping = SimpleNamespace(request=SimpleNamespace(root=PingRequest()))
    # Returns at once even though the handler is still waiting for a click.
    await asyncio.wait_for(session._received_request(elicit), 0.5)
    await asyncio.wait_for(started.wait(), 0.5)
    await asyncio.wait_for(session._received_request(ping), 0.5)
    assert handled == ["ElicitRequest", "PingRequest"]
    release.set()
