"""addon/undo_steps.py: one undo step per command and the chat's undo_since."""

import importlib.util
import sys
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "addon" / "undo_steps.py"


@pytest.fixture
def us(monkeypatch):
    """A fresh module with a fake clock and a fake Blender undo stack."""
    spec = importlib.util.spec_from_file_location("undo_steps_under_test", _PATH)
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, mod)  # dataclasses look it up
    spec.loader.exec_module(mod)

    now = [100.0]

    def tick():
        now[0] += 1.0
        return now[0]

    mod.clock = tick
    mod.fake = {"stack": [], "undo_calls": 0, "undo_ok": True, "limit": None}

    def push(label):
        mod.fake["stack"].append(label)
        return True

    def undo_once():
        if not mod.fake["undo_ok"] or not mod.fake["stack"]:
            return False
        mod.fake["undo_calls"] += 1
        mod.fake["stack"].pop()
        # Blender fires undo_post and a depsgraph update inside ed.undo.
        mod.note_undo_redo()
        mod.note_scene_change()
        return True

    def run_wrapped(fn, job):
        mod._execute_job(job)

    monkeypatch.setattr(mod, "_undo_available", lambda: True)
    monkeypatch.setattr(mod, "_push_undo", push)
    monkeypatch.setattr(mod, "_undo_once", undo_once)
    monkeypatch.setattr(mod, "_flush_depsgraph", lambda: None)
    monkeypatch.setattr(mod, "_run_wrapped", lambda fn, job: (job.update(fn=fn), run_wrapped(fn, job)))
    monkeypatch.setattr(mod, "_undo_limit", lambda: mod.fake["limit"])
    mod.now = now
    return mod


def _changes(mod, result=None):
    """A handler that touches the scene (the depsgraph fires while suppressed)."""
    def fn():
        mod.note_scene_change()
        return result if result is not None else {"ok": True}
    return fn


def test_labels_name_the_command_and_object(us):
    assert us.label_for("add_primitive") == "BlenderMCP: add_primitive"
    assert us.label_for("add_primitive", {"name": "Cube"}) == "BlenderMCP: add_primitive Cube"
    assert us.label_for("set_color", {"objects": ["Chair"]}) == "BlenderMCP: set_color Chair"
    assert us.label_for("set_color", {"objects": ["A", "B"]}) == "BlenderMCP: set_color"
    assert us.label_for("boolean", {"target": "Box"}, failed=True) == "BlenderMCP: boolean Box (failed)"


def test_labels_are_capped(us):
    long = us.label_for("add_primitive", {"name": "x" * 200})
    assert len(long) <= us.LABEL_CAP
    assert long.endswith("...")
    assert us.label_for("a", {"name": "y" * 200}, failed=True).endswith(" (failed)")
    assert "\n" not in us.label_for("a", {"name": "two\nlines"})


def test_mutating_command_pushes_one_named_step(us):
    t = us.clock()
    out = us.run_command("add_primitive", {"name": "Cube"}, _changes(us))
    assert out == {"ok": True}
    assert us.fake["stack"] == ["BlenderMCP: add_primitive Cube"]
    assert us.steps_since(t) == 1
    assert us.last_steps(5)[0]["command"] == "add_primitive"


def test_read_only_command_pushes_nothing(us):
    t = us.clock()
    us.run_command("get_scene_info", {}, _changes(us), undo=False)
    assert us.fake["stack"] == []
    assert us.steps_since(t) == 0
    # and its depsgraph activity does not count as a foreign change
    us.run_command("add_primitive", {}, _changes(us))
    assert not us.is_dirty()


def test_nested_command_joins_the_outer_step(us):
    def outer():
        us.run_command("add_primitive", {}, _changes(us))
        us.note_scene_change()
    us.run_command("execute_code", {}, outer)
    assert us.fake["stack"] == ["BlenderMCP: execute_code"]


def test_failed_handler_that_changed_nothing_leaves_no_step(us):
    def boom():
        raise ValueError("bad params")
    with pytest.raises(ValueError):
        us.run_command("boolean", {"target": "Box"}, boom)
    us.run_command("set_color", {}, lambda: {"error": "no such object"})
    assert us.fake["stack"] == []
    assert us.steps_since(0) == 0


def test_failed_handler_that_changed_things_is_still_undoable(us):
    def half():
        us.note_scene_change()
        raise RuntimeError("solver blew up")
    with pytest.raises(RuntimeError):
        us.run_command("boolean", {"target": "Box"}, half)
    assert us.fake["stack"] == ["BlenderMCP: boolean Box (failed)"]


def test_no_undo_machinery_in_background(us, monkeypatch):
    monkeypatch.setattr(us, "_undo_available", lambda: False)
    assert us.run_command("add_primitive", {}, lambda: 7) == 7
    assert us.fake["stack"] == []


def test_bounded_list(us):
    t = us.clock()
    for i in range(us.MAX_STEPS + 10):
        us.run_command("add_primitive", {"name": f"C{i}"}, _changes(us))
    assert len(us.last_steps(10_000)) == us.MAX_STEPS
    assert us.last_steps(1)[0]["label"].endswith(f"C{us.MAX_STEPS + 9}")
    # Steps since t were partly dropped, so a block undo would be short.
    summary = us.summary_since(t)
    assert not summary["can_undo"]
    assert "Too many" in summary["reason"]
    # A recent window is still fine.
    assert us.summary_since(us.last_steps(3)[0]["at"])["steps"] == 3


def test_undo_since_undoes_exactly_our_steps(us):
    us.fake["stack"].append("user: move")
    t = us.clock()
    for name in ("A", "B", "C"):
        us.run_command("add_primitive", {"name": name}, _changes(us))
    summary = us.summary_since(t)
    assert summary == {
        "steps": 3,
        "labels": ["BlenderMCP: add_primitive A", "BlenderMCP: add_primitive B",
                   "BlenderMCP: add_primitive C"],
        "can_undo": True,
        "reason": "",
    }
    undone, msg = us.undo_since(t)
    assert (undone, msg) == (3, "Undid 3 steps.")
    assert us.fake["stack"] == ["user: move"]
    assert us.steps_since(t) == 0
    # Our own undo did not mark the scene dirty.
    assert not us.is_dirty()


def test_only_steps_after_t_are_undone(us):
    us.run_command("add_primitive", {"name": "Old"}, _changes(us))
    t = us.clock()
    us.run_command("add_primitive", {"name": "New"}, _changes(us))
    assert us.undo_since(t)[0] == 1
    assert us.fake["stack"] == ["BlenderMCP: add_primitive Old"]


def test_foreign_change_after_our_steps_refuses(us):
    t = us.clock()
    us.run_command("add_primitive", {}, _changes(us))
    us.note_scene_change()  # the user moved something
    assert us.is_dirty()
    summary = us.summary_since(t)
    assert summary["can_undo"] is False
    assert summary["reason"] == us.DIRTY_REASON
    assert us.undo_since(t) == (0, us.DIRTY_REASON)
    assert us.fake["undo_calls"] == 0


def test_foreign_change_between_our_steps_refuses(us):
    t = us.clock()
    us.run_command("add_primitive", {}, _changes(us))
    us.note_scene_change()
    us.run_command("set_color", {}, _changes(us))
    assert not us.is_dirty()  # nothing after the last step...
    assert us.undo_since(t)[0] == 0  # ...but the block would include the user's step


def test_foreign_change_before_the_window_is_fine(us):
    us.note_scene_change()
    t = us.clock()
    us.run_command("add_primitive", {}, _changes(us))
    assert us.summary_since(t)["can_undo"]


def test_user_undo_or_redo_clears_the_record(us):
    t = us.clock()
    us.run_command("add_primitive", {}, _changes(us))
    us.note_scene_change()
    us.note_undo_redo()  # user pressed Ctrl+Z
    assert us.steps_since(t) == 0
    assert not us.is_dirty()
    s = us.summary_since(t)
    assert (s["steps"], s["can_undo"], s["reason"]) == (0, False, "Nothing to undo.")


def test_nothing_to_undo(us):
    assert us.undo_since(us.clock()) == (0, "Nothing to undo.")


def test_blender_undo_limit_refuses(us):
    us.fake["limit"] = 2
    t = us.clock()
    for _ in range(3):
        us.run_command("add_primitive", {}, _changes(us))
    s = us.summary_since(t)
    assert not s["can_undo"] and "keeps only 2" in s["reason"]


def test_partial_undo_is_reported(us):
    t = us.clock()
    for _ in range(2):
        us.run_command("add_primitive", {}, _changes(us))
    real = us._undo_once
    calls = []

    def once():
        calls.append(1)
        return real() if len(calls) == 1 else False
    us._undo_once = once
    undone, msg = us.undo_since(t)
    assert undone == 1 and "1 of 2" in msg
    assert us.steps_since(t) == 1


def test_load_clears(us):
    us.run_command("add_primitive", {}, _changes(us))
    us._on_load_post(None)
    assert us.last_steps(5) == []


def test_registry_undo_flag(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "registry_under_test", _PATH.parent / "executor" / "registry.py")
    registry = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, registry)
    spec.loader.exec_module(registry)
    CommandSpec = registry.CommandSpec

    def f(self):
        pass
    assert CommandSpec("x", f).wants_undo({})
    assert not CommandSpec("x", f, undo=False).wants_undo({})
    dry = CommandSpec("x", f, undo=lambda p: not p.get("dry_run"))
    assert dry.wants_undo({}) and not dry.wants_undo({"dry_run": True})
