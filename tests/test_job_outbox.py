"""Addon side of jobs: result outbox, size cap, and cancelling queued jobs."""

import asyncio
import heapq
import sys
import threading
import time
import types
from types import SimpleNamespace

# addon.client's package init imports the Blender runtime; these modules
# under test don't use it, so stand in empty modules outside Blender.
for _name in ("bpy", "bmesh", "mathutils"):
    sys.modules.setdefault(_name, types.ModuleType(_name))

from addon.client import job_reporter, message_pump


def disconnected_client():
    return SimpleNamespace(loop=None, client=None, connected=False,
                           job_queue=[], queue_lock=threading.Lock(),
                           client_uuid="blender-me")


class FakeMCP:
    def __init__(self):
        self.calls = []

    async def call_tool(self, name, args):
        self.calls.append((name, args))


def connected_client():
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    c = SimpleNamespace(loop=loop, client=FakeMCP(), connected=True,
                        job_queue=[], queue_lock=threading.Lock(), client_uuid="blender-me")
    return c, t


def wait_for(pred, timeout=2.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


def test_results_queue_while_disconnected_and_running_is_dropped():
    c = disconnected_client()
    job_reporter.submit_job_update(c, "j-1", "running")
    job_reporter.submit_job_update(c, "j-1", "completed", result="ok")
    assert [u["status"] for u in c.job_outbox] == ["completed"]


def test_outbox_is_bounded_dropping_oldest():
    c = disconnected_client()
    for i in range(job_reporter.OUTBOX_MAX + 5):
        job_reporter.submit_job_update(c, f"j-{i}", "completed")
    assert len(c.job_outbox) == job_reporter.OUTBOX_MAX
    assert c.job_outbox[0]["job_id"] == "j-5"


def test_result_is_capped():
    c = disconnected_client()
    job_reporter.submit_job_update(c, "j-big", "completed", result="x" * (job_reporter.RESULT_CAP + 10))
    res = c.job_outbox[0]["result"]
    assert len(res) < job_reporter.RESULT_CAP + 100 and "truncated by BlenderMCP" in res


def test_flush_sends_queued_updates_in_order():
    c = disconnected_client()
    job_reporter.submit_job_update(c, "j-a", "completed", result="a")
    job_reporter.submit_job_update(c, "j-b", "failed", error="b")
    live, _t = connected_client()
    live.job_outbox = c.job_outbox
    try:
        assert job_reporter.flush_outbox(live) == 2
        assert wait_for(lambda: len(live.client.calls) == 2)
        assert [a["job_id"] for _n, a in live.client.calls] == ["j-a", "j-b"]
        assert not live.job_outbox
    finally:
        live.loop.call_soon_threadsafe(live.loop.stop)


def test_cancel_removes_only_the_queued_job(monkeypatch):
    c = disconnected_client()
    reported = []
    monkeypatch.setattr(job_reporter, "submit_job_update",
                        lambda client, job_id, status, result="", error="": reported.append((job_id, status)))
    for jid in ("j-keep", "j-drop"):
        message_pump.enqueue_job(c, 6, {"target_uuid": "blender-me",
                                        "payload": {"message_type": "command_dispatch",
                                                    "job_id": jid, "command": "x"}})
    message_pump.enqueue_job(c, 5, {"target_uuid": "blender-me",
                                    "payload": {"message_type": "job_cancel", "job_id": "j-drop"}})
    remaining = [heapq.heappop(c.job_queue)[2]["payload"]["job_id"] for _ in range(len(c.job_queue))]
    assert remaining == ["j-keep"]
    assert reported == [("j-drop", "cancelled")]


def test_cancel_for_unknown_or_running_job_does_nothing(monkeypatch):
    c = disconnected_client()
    reported = []
    monkeypatch.setattr(job_reporter, "submit_job_update",
                        lambda *a, **k: reported.append(a))
    assert message_pump.cancel_queued_job(c, "j-not-queued") is False
    assert reported == []
