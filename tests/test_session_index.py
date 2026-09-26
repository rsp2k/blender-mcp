"""BusManager session index: a dead session's entry must not match a new session
that happens to reuse its id (the lookup the job_update spoof check trusts)."""

import gc
import uuid
import weakref

from blender_mcp.message_bus import BusManager


class Session:
    """Stand-in for a FastMCP session (weak-referenceable, default identity)."""


def test_lookup_and_forget():
    m = BusManager()
    s = Session()
    bus = uuid.uuid4()
    m.index_session(s, bus, "blender-a")
    assert m.lookup_session(s) == (bus, "blender-a")
    m.forget_session(s)
    assert m.lookup_session(s) is None


def test_stale_entry_with_reused_id_does_not_match():
    m = BusManager()
    dead = Session()
    dead_ref = weakref.ref(dead)
    del dead
    gc.collect()
    assert dead_ref() is None

    new = Session()
    # What an unregistered-then-garbage-collected session leaves behind when a
    # new session object lands at the same address.
    m._session_index[id(new)] = (dead_ref, uuid.uuid4(), "blender-dead")
    assert m.lookup_session(new) is None
    assert id(new) not in m._session_index


def test_non_weakrefable_sessions_still_work():
    m = BusManager()
    s = object()
    bus = uuid.uuid4()
    m.index_session(s, bus, "blender-b")
    assert m.lookup_session(s) == (bus, "blender-b")
    assert m.lookup_session(object()) is None


def test_removing_a_bus_purges_its_sessions():
    m = BusManager()
    s = Session()
    bus = uuid.uuid4()
    m.get_or_create(bus, name="t")
    m.index_session(s, bus, "blender-c")
    m.remove(bus)
    assert m.lookup_session(s) is None
