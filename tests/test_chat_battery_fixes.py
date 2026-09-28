"""Fixes from the 2026-09-28 chat battery: prompt rules, deselected screenshots,
per-scene inspection, step error text, and the runner's snapshot retry."""

import asyncio
import json
import sys
from pathlib import Path

import pytest

from addon import selection_guard as sg
from addon.chat.state import ChatState, format_log_line
from blender_mcp import dispatch_component as dc
from blender_mcp import modelling_tools as mt
from blender_mcp.chat import vision
from blender_mcp.chat.turn import SYSTEM_PROMPT, step_detail

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "battery"))

import snippets
from checks import claimed_missing, normalise_check, tool_records
from report import render_markdown


def run(coro):
    return asyncio.run(coro)


# ---- system prompt ----------------------------------------------------------

def test_prompt_measures_the_reference_before_placing():
    p = " ".join(SYSTEM_PROMPT.split())
    assert "measure that object first (world_bounds)" in p
    assert "Never assume a table's height" in p


def test_prompt_asks_on_ambiguous_references_and_stays_consistent():
    p = " ".join(SYSTEM_PROMPT.split())
    assert "Finish the whole request in this turn" in p
    assert "Ask a question only when the request is genuinely ambiguous" in p
    # The ambiguity rule is framed as the exception the finish rule allows.
    assert "An ambiguous reference is such a case" in p
    assert "change nothing and ask which one" in p


def test_prompt_confirms_multi_part_builds():
    p = " ".join(SYSTEM_PROMPT.split())
    assert "call list_scene_objects to confirm every part exists" in p
    assert "Never report a part that isn't in the scene" in p


# ---- deselect / restore -----------------------------------------------------

class Obj:
    def __init__(self, name, selected=False, locked=False):
        self.name, self._sel, self.locked = name, selected, locked

    def select_get(self):
        return self._sel

    def select_set(self, v):
        if self.locked:
            raise RuntimeError("not in view layer")
        self._sel = bool(v)


class Objects(list):
    active = None


class Layer:
    def __init__(self, objs, active=None):
        self.objects = Objects(objs)
        self.objects.active = active


def test_deselected_clears_then_restores_exactly():
    a, b, c = Obj("A", True), Obj("B"), Obj("C", True)
    layer = Layer([a, b, c], active=c)
    with sg.deselected(layer) as saved:
        assert [o.select_get() for o in layer.objects] == [False, False, False]
        assert layer.objects.active is None
        assert saved["selected"] == [a, c]
    assert [o.select_get() for o in layer.objects] == [True, False, True]
    assert layer.objects.active is c


def test_deselected_restores_even_when_the_capture_fails():
    a = Obj("A", True)
    layer = Layer([a], active=a)
    with pytest.raises(RuntimeError), sg.deselected(layer):
        raise RuntimeError("render failed")
    assert a.select_get() and layer.objects.active is a


def test_deselected_disabled_touches_nothing():
    a = Obj("A", True)
    layer = Layer([a], active=a)
    with sg.deselected(layer, enabled=False):
        assert a.select_get() and layer.objects.active is a


def test_restore_skips_objects_that_cannot_be_selected():
    a, stuck = Obj("A", True), Obj("Stuck", True)
    layer = Layer([a, stuck], active=a)
    saved = sg.capture(layer)
    stuck.locked = True
    assert sg.clear(layer) == 2  # the stuck one stays selected; no exception
    stuck.locked = False
    stuck._sel = False
    sg.restore(layer, saved)
    assert a.select_get() and stuck.select_get() and layer.objects.active is a


# ---- vision asks for a deselected capture -------------------------------------

class FakeExecutor:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    async def call(self, name, args, extra=None):
        self.calls.append((name, dict(extra or {})))
        return self.replies.pop(0)


def test_vision_capture_sends_deselect():
    ex = FakeExecutor([(True, "{}")])
    assert run(vision.capture(ex)) == (True, "{}")
    assert ex.calls == [(vision.SCREENSHOT_TOOL, {"store": True, "max_size": vision.MAX_SIZE,
                                                  "deselect": True})]


def test_vision_capture_retries_without_deselect_when_refused():
    refusal = "1 validation error: deselect Unexpected keyword argument"
    ex = FakeExecutor([(False, refusal), (True, "shot")])
    assert run(vision.capture(ex)) == (True, "shot")
    assert "deselect" not in ex.calls[1][1] and ex.calls[1][1]["store"] is True


def test_vision_capture_does_not_retry_other_failures():
    ex = FakeExecutor([(False, "No 3D viewport in the active screen")])
    assert run(vision.capture(ex)) == (False, "No 3D viewport in the active screen")
    assert len(ex.calls) == 1


def test_screenshot_tool_sends_deselect_only_when_on(monkeypatch):
    comp = dc.BlenderDispatchComponent()
    sent = []

    async def fake_call(ctx, command, params, *a, **k):
        sent.append((command, params))
        return "{}"

    monkeypatch.setattr(comp, "_call", fake_call)
    run(comp.get_viewport_screenshot())
    run(comp.get_viewport_screenshot(deselect=True))
    assert "deselect" not in sent[0][1] and sent[1][1]["deselect"] is True


# ---- scene parameter ----------------------------------------------------------

def test_scene_param_validation():
    assert mt.scene_param(None) is None
    assert mt.scene_param("   ") is None
    assert mt.scene_param("H1 Closed") == "H1 Closed"
    for bad in (3, ["H1"], "x" * 300):
        with pytest.raises(ValueError):
            mt.scene_param(bad)


def test_list_params_scene_only_sent_when_given():
    assert "scene" not in mt.list_params()
    assert mt.list_params(scene="H2 Open")["scene"] == "H2 Open"
    with pytest.raises(ValueError):
        mt.list_params(scene=7)


def test_list_scene_objects_tool_forwards_scene(monkeypatch):
    c = mt.BlenderModellingComponent()
    sent = []

    async def fake_send(ctx, command, params, target_uuid, timeout, bus_id):
        sent.append(params)
        return "{}"

    monkeypatch.setattr(c, "_send", fake_send)
    run(c.list_scene_objects(scene="H4 Cutaway"))
    assert sent[-1]["scene"] == "H4 Cutaway"
    out = json.loads(run(c.list_scene_objects(scene=5)))
    assert out["error"] == "invalid_argument" and len(sent) == 1


def test_get_scene_info_tool_forwards_scene(monkeypatch):
    comp = dc.BlenderDispatchComponent()
    sent = []

    async def fake_call(ctx, command, params, *a, **k):
        sent.append((command, params))
        return "{}"

    monkeypatch.setattr(comp, "_call", fake_call)
    run(comp.get_scene_info())
    run(comp.get_scene_info(scene="H1 Closed"))
    assert sent == [("get_scene_info", {}), ("get_scene_info", {"scene": "H1 Closed"})]
    assert json.loads(run(comp.get_scene_info(scene=1)))["error"] == "invalid_argument"


# ---- step error text ----------------------------------------------------------

def test_step_detail():
    assert step_detail(False, "bad\n  args " + "x" * 500)["error"].startswith("bad args ")
    assert len(step_detail(False, "y" * 500)["error"]) == 300
    assert step_detail(False, "") == {"error": "failed (no error text)"}
    assert step_detail(True, '{"object": "Crate"}') == {"head": '{"object": "Crate"}'}
    assert step_detail(True, "") == {}


def test_addon_keeps_error_and_head_on_the_tool_row():
    st = ChatState()
    st.begin_turn("go")
    st.apply_event({"t": "tool", "name": "add_primitive", "phase": "start"})
    st.apply_event({"t": "tool", "name": "add_primitive", "phase": "end", "ok": False, "ms": 3,
                    "error": "invalid_argument: size must be positive"})
    st.apply_event({"t": "tool", "name": "list_scene_objects", "phase": "end", "ok": True,
                    "ms": 9, "head": '{"scene": "Scene"', "error": 5})
    rows = [m for m in st.messages if m["role"] == "tool"]
    assert rows[0]["error"] == "invalid_argument: size must be positive"
    assert rows[1]["head"] == '{"scene": "Scene"' and "error" not in rows[1]
    line = format_log_line(rows[0])
    assert line.endswith("failed: invalid_argument: size must be positive")


# ---- runner: tool records, report, snapshot retry -------------------------------

def test_tool_records_keep_error_and_head():
    msgs = [{"role": "user", "text": "hi"},
            {"role": "tool", "name": "get_scene_info", "ok": False, "ms": 22, "turn": 9,
             "error": "unexpected argument 'scene'"},
            {"role": "tool", "name": "add_primitive", "ok": True, "ms": 300, "turn": 9,
             "head": '{"object": "Crate", ' + "z" * 400}]
    recs = tool_records(msgs)
    assert recs[0] == {"name": "get_scene_info", "ok": False, "ms": 22, "turn": 9,
                       "error": "unexpected argument 'scene'"}
    assert len(recs[1]["head"]) == 160 and "error" not in recs[1]


def test_report_shows_failed_tool_error_text():
    r = {"model": "m", "case": "c", "ok": False, "repeat": 1, "seconds": 1.0, "reply": "x",
         "checks": [{"check": "reply_contains", "ok": False, "observed": "x", "expect": "y"}],
         "tools": [{"name": "get_scene_info", "ok": False, "error": "unexpected argument 'scene'"},
                   {"name": "set_color", "ok": False},
                   {"name": "list_scene_objects", "ok": True, "head": "{...}"}]}
    md = render_markdown([r])
    assert "get_scene_info failed: `unexpected argument 'scene'`" in md
    assert "set_color failed: `(no error text recorded)`" in md
    assert "list_scene_objects failed" not in md


CHECKS = [normalise_check(c) for c in (
    {"object_exists": "Crate"}, {"dims_approx": {"name": "Crate", "dims": [2, 1, 0.5]}},
    {"object_absent": "Cube"})]


def test_claimed_missing_from_reply_and_created_head():
    empty = {"objects": [{"name": "Camera"}, {"name": "Light"}]}
    tools = [{"name": "add_primitive", "ok": True, "head": '{"object": "Crate", "kind": "box"'}]
    assert claimed_missing("Added the box Crate.", CHECKS, [], empty) == ["Crate"]
    assert claimed_missing("Done.", CHECKS, tools, empty) == ["Crate"]
    # A failed creation or an unmentioned name claims nothing.
    assert claimed_missing("Done.", CHECKS, [{**tools[0], "ok": False}], empty) == []
    # object_absent names are not claims; present objects are not missing.
    assert claimed_missing("I removed the Cube.", CHECKS, [], empty) == []
    have = {"objects": [{"name": "crate"}]}
    assert claimed_missing("Added Crate.", CHECKS, tools, have) == []


def test_idle_snippet_compiles():
    compile(snippets.IDLE, "snippet", "exec")
