"""Addon side of the event-stream safety net: keepalive stamping, job dedupe,
and queueing dispatches recovered by the pull fallback."""

import asyncio
import sys
import threading
import types
from types import SimpleNamespace

# addon.client's package init imports the Blender runtime; these modules
# under test don't use it, so stand in empty modules outside Blender.
for _name in ("bpy", "bmesh", "mathutils"):
    sys.modules.setdefault(_name, types.ModuleType(_name))

from addon.client import message_pump


def client():
    return SimpleNamespace(job_queue=[], queue_lock=threading.Lock(), client_uuid="blender-me",
                           last_stream_message_at=0.0)


def notification(data, level="info"):
    return SimpleNamespace(method="notifications/message",
                           params={"logger": "_message_bus", "data": data, "level": level})


def dispatch(job_id, command="get_scene_info"):
    return {"target_uuid": "blender-me",
            "payload": {"message_type": "command_dispatch", "job_id": job_id,
                        "command": command, "params": {}}}


def test_keepalive_stamps_stream_and_is_not_queued():
    c = client()
    asyncio.run(message_pump.handle_message(
        c, notification({"payload": {"message_type": "bus_keepalive"}}, level="debug")))
    assert c.last_stream_message_at > 0
    assert c.job_queue == []


def test_dispatch_notification_stamps_and_queues():
    c = client()
    asyncio.run(message_pump.handle_message(c, notification(dispatch("j-1"))))
    assert c.last_stream_message_at > 0
    assert message_pump.held_job_ids(c) == ["j-1"]


def test_same_job_is_queued_once():
    c = client()
    message_pump.enqueue_job(c, 6, dispatch("j-1"))
    message_pump.enqueue_job(c, 6, dispatch("j-1"))
    assert len(c.job_queue) == 1


def test_pulled_dispatch_is_queued_in_wire_shape_and_deduped():
    c = client()
    pulled = [{"job_id": "j-2", "command": "execute_code", "params": {"code": "x"}}]
    assert message_pump.enqueue_pulled(c, pulled, "bus-1") == 1
    _, _, log_data = c.job_queue[0]
    assert log_data["target_uuid"] == "blender-me"
    assert log_data["payload"] == {"message_type": "command_dispatch", "job_id": "j-2",
                                   "command": "execute_code", "params": {"code": "x"}}
    # A late notification for the same job, or a second pull, adds nothing.
    assert message_pump.enqueue_pulled(c, pulled, "bus-1") == 0
    message_pump.enqueue_job(c, 6, dispatch("j-2", "execute_code"))
    assert len(c.job_queue) == 1


def test_pulled_dispatch_skipped_if_already_seen_by_notification():
    c = client()
    message_pump.enqueue_job(c, 6, dispatch("j-3"))
    assert message_pump.enqueue_pulled(c, [{"job_id": "j-3", "command": "get_scene_info"}], "b") == 0


def test_seen_set_is_bounded():
    c = client()
    for i in range(message_pump.SEEN_JOBS_MAX + 50):
        with c.queue_lock:
            message_pump.remember_job(c, f"j-{i}")
    assert len(c._seen_job_ids) == message_pump.SEEN_JOBS_MAX
