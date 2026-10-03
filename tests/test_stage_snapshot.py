"""The stage snapshot at driver_namespace["blender_mcp.stage"] (addon/stage_snapshot.py)."""

import sys
from collections import deque
from types import SimpleNamespace

import pytest

from addon import stage_snapshot as ss
from addon import state
from addon._version import __version__
from addon.chat.state import APPROVAL_PREFIX, approval_action, chat_state

KEYS = {"v", "source", "addon_version", "connected", "lock", "pending_request",
        "chat", "activity", "updated_at"}
ACTIVITY_KEYS = {"command", "ok", "ms", "at", "error"}


def build(**over):
    args = {"connected": False, "lock_holder": None, "lock_expires_at": None,
            "lock_reason": None, "pending": None, "chat_busy": False,
            "awaiting_approval": None, "activity": [], "now": 100.0}
    args.update(over)
    return ss.build_snapshot(**args)


def entry(i, ok=True):
    return {"command": f"cmd{i}", "ok": ok, "ms": 12.5, "at": 1000.0 + i,
            "error": "" if ok else "Boom: bad"}


@pytest.fixture
def fake_bpy(monkeypatch):
    ns = {}
    monkeypatch.setitem(sys.modules, "bpy", SimpleNamespace(app=SimpleNamespace(driver_namespace=ns)))
    return ns


@pytest.fixture
def clean_state(monkeypatch):
    monkeypatch.setattr(state, "_client", None)
    monkeypatch.setattr(state, "_lock_holder_uuid", None)
    monkeypatch.setattr(state, "_lock_holder_label", None)
    monkeypatch.setattr(state, "_lock_expires_at", None)
    monkeypatch.setattr(state, "_lock_reason", None)
    monkeypatch.setattr(state, "_pending_control_request", None)
    monkeypatch.setattr(state, "_activity", deque(maxlen=20))
    monkeypatch.setattr(state, "_stage_live", False)
    monkeypatch.setattr(chat_state, "busy", False)
    monkeypatch.setattr(chat_state, "pending_approval", None)


# ---- build_snapshot ------------------------------------------------------------

def test_shape_and_types():
    snap = build(connected=1, activity=[entry(1)])
    assert set(snap) == KEYS
    assert snap["v"] == 1 and snap["source"] == "blender_mcp"
    assert snap["addon_version"] == __version__
    assert snap["connected"] is True
    assert snap["lock"] is None and snap["pending_request"] is None
    assert snap["chat"] == {"busy": False, "awaiting_approval": None}
    assert snap["updated_at"] == 100.0 and isinstance(snap["updated_at"], float)
    (a,) = snap["activity"]
    assert set(a) == ACTIVITY_KEYS
    assert isinstance(a["command"], str) and isinstance(a["ok"], bool)
    assert isinstance(a["ms"], float) and isinstance(a["at"], float)
    assert isinstance(a["error"], str)


def test_lock_none_without_holder():
    assert build(lock_expires_at=500.0, lock_reason="x")["lock"] is None
    assert build(lock_holder="claude", lock_expires_at=None)["lock"] is None


def test_lock_set():
    lock = build(lock_holder="claude", lock_expires_at=500, lock_reason=None)["lock"]
    assert lock == {"holder": "claude", "expires_at": 500.0, "reason": ""}
    assert isinstance(lock["expires_at"], float)


def test_expired_lock_is_still_published():
    # Readers treat a past expires_at as no lock; we don't hide it ourselves.
    lock = build(lock_holder="claude", lock_expires_at=50.0, lock_reason="r", now=100.0)["lock"]
    assert lock["expires_at"] == 50.0


def test_pending_request():
    pending = {"job_id": "j", "requester_uuid": "abcdef0123456789", "requester_label": None,
               "reason": "build a scene", "duration_s": 90.0}
    assert build(pending=pending)["pending_request"] == {
        "requester": "abcdef012345", "reason": "build a scene", "duration_s": 90.0}
    pending["requester_label"] = "Claude Code"
    assert build(pending=pending)["pending_request"]["requester"] == "Claude Code"


def test_activity_is_copied_and_capped():
    source = deque((entry(i) for i in range(25)), maxlen=30)
    snap = build(activity=source)
    assert len(snap["activity"]) == ss.ACTIVITY_MAX == 20
    assert snap["activity"][0]["command"] == "cmd5"
    assert snap["activity"][-1]["command"] == "cmd24"  # newest last
    snap["activity"][-1]["command"] = "mutated"
    assert source[-1]["command"] == "cmd24"
    assert all(a is not e for a, e in zip(snap["activity"], source))


def test_activity_drops_extra_keys():
    snap = build(activity=[{**entry(1), "secret": "x"}])
    assert set(snap["activity"][0]) == ACTIVITY_KEYS


# ---- approval_action ------------------------------------------------------------

def test_approval_action_from_server_prompt():
    prompt = f"{APPROVAL_PREFIX} the assistant wants to run this Python.\n\nimport bpy\nprint(1)"
    assert approval_action(prompt) == "run this Python"
    prompt = (f"{APPROVAL_PREFIX} the assistant wants to use add_cube from your "
              "'tools' tool server.\n\n{}")
    assert approval_action(prompt) == "use add_cube from your 'tools' tool server"


def test_approval_action_fallbacks():
    assert approval_action(f"{APPROVAL_PREFIX} something else\nmore") == "something else"
    assert approval_action(APPROVAL_PREFIX) == "approve a step"
    assert approval_action(None) == "approve a step"
    long = f"{APPROVAL_PREFIX} the assistant wants to {'x' * 500}."
    assert len(approval_action(long)) == 120


# ---- publish / clear ------------------------------------------------------------

def test_publish_reads_state(fake_bpy, clean_state, monkeypatch):
    monkeypatch.setattr(state, "_client", SimpleNamespace(running=True, connected=True))
    monkeypatch.setattr(state, "_lock_holder_uuid", "u-123")
    monkeypatch.setattr(state, "_lock_holder_label", "Claude Code")
    monkeypatch.setattr(state, "_lock_expires_at", 2000.0)
    monkeypatch.setattr(state, "_lock_reason", "modelling")
    state._activity.append(entry(1, ok=False))
    monkeypatch.setattr(chat_state, "busy", True)
    monkeypatch.setattr(chat_state, "pending_approval", {
        "prompt": f"{APPROVAL_PREFIX} the assistant wants to run this Python.\n\nx = 1"})

    ss.start()
    snap = fake_bpy[ss.KEY]
    assert snap["connected"] is True
    assert snap["lock"] == {"holder": "Claude Code", "expires_at": 2000.0, "reason": "modelling"}
    assert snap["chat"] == {"busy": True, "awaiting_approval": "run this Python"}
    assert snap["activity"][0]["ok"] is False


def test_connected_needs_running_and_registered(fake_bpy, clean_state, monkeypatch):
    ss.start()
    assert fake_bpy[ss.KEY]["connected"] is False
    monkeypatch.setattr(state, "_client", SimpleNamespace(running=True, connected=False))
    ss.publish()
    assert fake_bpy[ss.KEY]["connected"] is False


def test_publish_replaces_whole_dict(fake_bpy, clean_state):
    ss.start()
    first = fake_bpy[ss.KEY]
    frozen = {**first, "activity": list(first["activity"])}
    state._activity.append(entry(1))
    ss.publish()
    second = fake_bpy[ss.KEY]
    assert second is not first
    assert first == frozen  # the old one was never touched
    assert len(second["activity"]) == 1


def test_publish_is_noop_until_started(fake_bpy, clean_state):
    ss.publish()
    assert ss.KEY not in fake_bpy


def test_clear_removes_key_and_stops_publishing(fake_bpy, clean_state):
    ss.start()
    assert ss.KEY in fake_bpy
    ss.clear()
    assert ss.KEY not in fake_bpy
    ss.publish()  # e.g. a redraw timer queued before unregister
    assert ss.KEY not in fake_bpy
    ss.clear()  # idempotent


def test_publish_never_raises(fake_bpy, clean_state, monkeypatch, capsys):
    ss.start()

    def boom():
        raise RuntimeError("nope")

    monkeypatch.setattr(ss, "_snapshot_from_state", boom)
    ss.publish()
    assert "not published" in capsys.readouterr().out
