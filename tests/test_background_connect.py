"""connection.stays_connected_in_this_process: background Blenders don't
auto-connect, so they can't rotate the desktop Blender's refresh token."""

import sys
import types

for _name in ("bpy",):
    sys.modules.setdefault(_name, types.ModuleType(_name))

from addon import connection


def test_desktop_blender_stays_connected():
    assert connection.stays_connected_in_this_process(False, env={})


def test_background_blender_does_not_connect_by_default():
    assert not connection.stays_connected_in_this_process(True, env={})


def test_background_blender_connects_only_when_asked():
    on = {connection.BACKGROUND_CONNECT_ENV: "1"}
    assert connection.stays_connected_in_this_process(True, env=on)
    off = {connection.BACKGROUND_CONNECT_ENV: "yes"}
    assert not connection.stays_connected_in_this_process(True, env=off)


def test_supervisor_does_nothing_in_a_background_blender(monkeypatch):
    bpy = sys.modules["bpy"]
    monkeypatch.setattr(bpy, "app", types.SimpleNamespace(background=True), raising=False)
    monkeypatch.delenv(connection.BACKGROUND_CONNECT_ENV, raising=False)
    sup = connection.ConnectionSupervisor()
    called = []
    monkeypatch.setattr(sup, "_evaluate", lambda: called.append(True))
    sup.check_now()
    assert called == []
