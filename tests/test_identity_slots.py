"""Slot-lease identity: stable across restarts, distinct across concurrent Blenders."""

import os

import pytest

from addon import identity
from addon.identity import StickyUUIDManager


@pytest.fixture
def procs(monkeypatch):
    """Simulate processes: `alive` is the set of live pids, `as_pid` switches the current one."""
    alive = set()
    monkeypatch.setattr(identity, "_pid_alive", lambda pid: pid in alive)

    def as_pid(pid):
        monkeypatch.setattr(os, "getpid", lambda: pid)
        alive.add(pid)

    return alive, as_pid


def test_restart_keeps_uuid(tmp_path, procs):
    alive, as_pid = procs
    as_pid(100)
    first = StickyUUIDManager(config_dir=str(tmp_path)).get_client_id()
    alive.discard(100)  # Blender exits
    as_pid(200)         # relaunch with a new pid
    assert StickyUUIDManager(config_dir=str(tmp_path)).get_client_id() == first


def test_concurrent_blenders_get_distinct_uuids(tmp_path, procs):
    _alive, as_pid = procs
    as_pid(100)
    a = StickyUUIDManager(config_dir=str(tmp_path)).get_client_id()
    as_pid(200)  # 100 still alive
    b = StickyUUIDManager(config_dir=str(tmp_path)).get_client_id()
    assert a != b


def test_same_process_reconnect_is_stable(tmp_path, procs):
    _alive, as_pid = procs
    as_pid(100)
    a = StickyUUIDManager(config_dir=str(tmp_path)).get_client_id()
    assert StickyUUIDManager(config_dir=str(tmp_path)).get_client_id() == a


def test_second_blender_exits_first_keeps_slot0(tmp_path, procs):
    alive, as_pid = procs
    as_pid(100)
    a = StickyUUIDManager(config_dir=str(tmp_path)).get_client_id()
    as_pid(200)
    b = StickyUUIDManager(config_dir=str(tmp_path)).get_client_id()
    alive.discard(200)
    as_pid(300)  # a third launch while 100 still runs reuses slot 1, not a new uuid
    assert StickyUUIDManager(config_dir=str(tmp_path)).get_client_id() == b
    assert b != a


def test_legacy_single_file_becomes_slot0(tmp_path, procs):
    _alive, as_pid = procs
    legacy = "12345678-1234-1234-1234-123456789abc"
    (tmp_path / "blender_mcp_uuid.txt").write_text(legacy)
    as_pid(100)
    assert StickyUUIDManager(config_dir=str(tmp_path)).get_client_id() == f"blender-{legacy}"


def test_stale_per_pid_files_are_removed(tmp_path, procs):
    alive, as_pid = procs
    (tmp_path / "blender_mcp_uuid_4242.txt").write_text("x" * 36)  # dead pid
    (tmp_path / "blender_mcp_uuid_555.txt").write_text("y" * 36)   # live pid
    alive.add(555)
    as_pid(100)
    StickyUUIDManager(config_dir=str(tmp_path))
    names = set(os.listdir(tmp_path))
    assert "blender_mcp_uuid_4242.txt" not in names
    assert "blender_mcp_uuid_555.txt" in names


def test_lease_from_another_host_is_respected(tmp_path, procs):
    _alive, as_pid = procs
    (tmp_path / "blender_mcp_uuid.txt.lease").write_text("100@some-other-host")
    (tmp_path / "blender_mcp_uuid.txt").write_text("a" * 36)
    as_pid(100)  # same pid number, different machine sharing the config dir
    assert StickyUUIDManager(config_dir=str(tmp_path)).get_client_id() != "blender-" + "a" * 36


def test_real_pid_alive_on_this_os():
    assert identity._pid_alive(os.getpid()) is True
    assert identity._pid_alive(0) is False
