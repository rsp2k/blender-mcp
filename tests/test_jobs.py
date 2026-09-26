"""Resumable jobs: store, timeout-to-job, late delivery, spoofing, long-poll, cancel."""

import asyncio
import json
import time
import uuid
from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from blender_mcp import job_tools, jobs
from blender_mcp import job_waiter as jw_mod
from blender_mcp.bus_tools import _pending_jobs
from blender_mcp.dispatch_component import _dispatch
from blender_mcp.message_bus import ClientInfo, bus_manager
from blender_mcp.storage import job_repo
from blender_mcp.storage.models import BusJob

TARGET = "blender-target-0001"
OTHER = "blender-other-0002"


def run(coro):
    return asyncio.run(coro)


class Env:
    """A bus with one target Blender, an in-memory job store, fake sessions."""

    def __init__(self, sessionmaker):
        self.db = sessionmaker
        self.bus_uuid = uuid.uuid4()
        self.bus_id = str(self.bus_uuid)
        self.bus = bus_manager.get_or_create(self.bus_uuid, name="test")
        self.target_session = object()
        self.other_session = object()
        self.bus.register(ClientInfo(uuid=TARGET, client_type="blender", is_persistent=True,
                                     session=self.target_session))
        self.routed = []
        self.on_route = None

        def fake_route(payload, **kw):
            self.routed.append((payload, kw))
            if self.on_route:
                self.on_route(payload)

        self.bus.route = fake_route


@pytest.fixture
def env(monkeypatch):
    async def setup():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(BusJob.__table__.create)
        return async_sessionmaker(engine, expire_on_commit=False)

    sm = run(setup())
    monkeypatch.setattr(jobs, "_session_factory", sm)

    async def member(user_id, bus_id):
        return user_id == "u1"

    async def member_buses(user_id):
        return [e.bus_id] if user_id == "u1" else []

    monkeypatch.setattr(jobs, "_membership", member)
    monkeypatch.setattr(jobs, "_member_buses", member_buses)
    monkeypatch.setattr(jobs, "_last_prune", 0.0)
    jobs._waiters.clear()
    _pending_jobs.clear()
    e = Env(sm)
    yield e
    bus_manager.remove(e.bus_uuid)


def row(env, job_id):
    async def go():
        async with env.db() as s:
            return await job_repo.get_job(s, job_id)
    return run(go())


# ---- store ---------------------------------------------------------------

def test_store_lifecycle_and_duplicates(env):
    async def go():
        async with env.db() as s:
            await job_repo.create_job(s, "j-1", env.bus_id, TARGET, "execute_code", {"code": "x"})
            r = await job_repo.mark_running(s, "j-1")
            assert r.status == "running" and r.started_at is not None
            r, changed = await job_repo.finish_job(s, "j-1", "completed", "out", "")
            assert changed and r.status == "completed" and r.result == "out"
            r, changed = await job_repo.finish_job(s, "j-1", "failed", "other", "boom")
            assert not changed and r.status == "completed" and r.result == "out"
            r = await job_repo.mark_running(s, "j-1")
            assert r.status == "completed"
    run(go())


def test_store_caps_and_list(env):
    async def go():
        async with env.db() as s:
            await job_repo.create_job(s, "j-big", env.bus_id, TARGET, "execute_code",
                                      {"code": "x" * 200_000})
            r, _ = await job_repo.finish_job(s, "j-big", "completed", "y" * 1_200_000)
            assert len(r.result) < 1_100_000 and "truncated by BlenderMCP" in r.result
            assert r.params.get("_truncated") is True
            await job_repo.create_job(s, "j-2", env.bus_id, TARGET, "get_scene_info", {})
            rows = await job_repo.list_jobs(s, [env.bus_id])
            assert {x.job_id for x in rows} == {"j-big", "j-2"}
            assert [x.job_id for x in await job_repo.list_jobs(s, [env.bus_id], status="queued")] == ["j-2"]
            assert await job_repo.list_jobs(s, ["other-bus"]) == []
    run(go())


def test_retention_prunes_expired_and_marks_orphans_lost(env):
    async def go():
        async with env.db() as s:
            await job_repo.create_job(s, "j-old", env.bus_id, TARGET, "c", {})
            await job_repo.create_job(s, "j-orphan", env.bus_id, "blender-gone", "c", {})
            await job_repo.create_job(s, "j-live", env.bus_id, TARGET, "c", {})
            now = job_repo.utcnow()
            old = await job_repo.get_job(s, "j-old")
            old.expires_at = now - timedelta(minutes=1)
            for jid in ("j-orphan", "j-live"):
                r = await job_repo.get_job(s, jid)
                r.created_at = now - timedelta(hours=2)
            await s.commit()
        await jobs.maybe_prune(force=True)
    run(go())
    assert row(env, "j-old") is None
    assert row(env, "j-orphan").status == "lost"
    assert row(env, "j-live").status == "queued"  # target still registered


# ---- dispatch -------------------------------------------------------------

def test_reply_within_timeout_keeps_old_shape(env):
    async def go():
        def reply(payload):
            async def later():
                await asyncio.sleep(0.01)
                await jobs.handle_update(payload["job_id"], "completed", "hi", "",
                                         session=env.target_session)
            asyncio.get_running_loop().create_task(later())
        env.on_route = reply
        return json.loads(await _dispatch(env.bus, env.bus_id, "get_scene_info", {},
                                          TARGET, 2.0, caller_sub="u1"))
    out = run(go())
    assert out["status"] == "completed" and out["result"] == "hi" and out["job_id"]
    assert row(env, out["job_id"]).status == "completed"


def test_timeout_becomes_a_job_then_late_result_lands(env):
    async def go():
        out = json.loads(await _dispatch(env.bus, env.bus_id, "execute_code",
                                         {"code": "sleep"}, TARGET, 0.05, caller_sub="u1"))
        assert out["status"] == "queued" and out["job_id"].startswith("j-")
        assert "blender_job_status" in out["hint"]
        # Simulate a server restart: every in-memory map is gone.
        _pending_jobs.clear()
        jw_mod.job_waiter._futures.clear()
        jobs._waiters.clear()
        await jobs.handle_update(out["job_id"], "running", "", "", session=env.target_session)
        res = await jobs.handle_update(out["job_id"], "completed", "job-ok", "",
                                       session=env.target_session)
        assert res["recorded"] == "completed"
        return out["job_id"]
    job_id = run(go())
    r = row(env, job_id)
    assert r.status == "completed" and r.result == "job-ok" and r.caller_sub == "u1"


def test_update_from_non_target_is_rejected(env):
    env.bus.register(ClientInfo(uuid=OTHER, client_type="blender", is_persistent=True,
                                session=env.other_session))

    async def go():
        out = json.loads(await _dispatch(env.bus, env.bus_id, "execute_code", {},
                                         TARGET, 0.05, caller_sub="u1"))
        res = await jobs.handle_update(out["job_id"], "completed", "forged", "",
                                       session=env.other_session)
        assert res["error"] == "not_job_target"
        res = await jobs.handle_update(out["job_id"], "completed", "forged", "",
                                       session=object())
        assert res["error"] == "not_job_target"
        return out["job_id"]
    job_id = run(go())
    assert row(env, job_id).status == "queued"


def test_untracked_job_falls_back_to_legacy_path(env):
    assert run(jobs.handle_update("j-unknown", "completed", "", "", session=env.target_session)) is None


def test_long_poll_returns_early_on_update(env):
    async def go():
        async with env.db() as s:
            await job_repo.create_job(s, "j-poll", env.bus_id, TARGET, "c", {})

        async def update_soon():
            await asyncio.sleep(0.05)
            await jobs.handle_update("j-poll", "running", "", "", session=env.target_session)

        t0 = time.monotonic()
        task = asyncio.create_task(update_soon())
        changed = await jobs.wait_for_change("j-poll", 5)
        await task
        return changed, time.monotonic() - t0
    changed, elapsed = run(go())
    assert changed and elapsed < 1.0
    assert row(env, "j-poll").status == "running"


# ---- tools ----------------------------------------------------------------

@pytest.fixture
def tools(env, monkeypatch):
    monkeypatch.setattr(job_tools, "_resolve_user_id", lambda ctx: "u1")

    async def fake_resolve_bus(user_id, bus_id=None):
        return {"ok": True, "bus": env.bus, "bus_id": env.bus_uuid, "name": "test"}

    monkeypatch.setattr(job_tools, "resolve_bus", fake_resolve_bus)
    return job_tools.BlenderJobComponent()


def test_submit_status_result(env, tools):
    async def go():
        out = json.loads(await tools.submit(command="execute_code", params={"code": "x"}))
        assert out["status"] == "queued"
        job_id = out["job_id"]

        async def finish_soon():
            await asyncio.sleep(0.05)
            await jobs.handle_update(job_id, "completed", "done", "", session=env.target_session)

        task = asyncio.create_task(finish_soon())
        status = json.loads(await tools.job_status(job_id=job_id, wait_seconds=5))
        await task
        result = json.loads(await tools.job_result(job_id=job_id))
        listed = json.loads(await tools.list_jobs())
        return status, result, listed
    status, result, listed = run(go())
    assert status["status"] == "completed" and status["done"] is True
    assert result["result"] == "done"
    assert listed["jobs"] and listed["jobs"][0]["status"] == "completed"


def test_non_member_cannot_see_job(env, tools, monkeypatch):
    async def go():
        out = json.loads(await tools.submit(command="get_scene_info"))
        monkeypatch.setattr(job_tools, "_resolve_user_id", lambda ctx: "intruder")
        return json.loads(await tools.job_result(job_id=out["job_id"]))
    assert run(go())["error"] == "job_not_found"


def test_cancel_queued_job(env, tools):
    def addon_acks(payload):
        if payload.get("message_type") == "job_cancel":
            async def ack():
                await asyncio.sleep(0.01)
                await jobs.handle_update(payload["job_id"], "cancelled", "", "",
                                         session=env.target_session)
            asyncio.get_running_loop().create_task(ack())

    async def go():
        out = json.loads(await tools.submit(command="execute_code", params={"code": "x"}))
        env.on_route = addon_acks
        return json.loads(await tools.job_cancel(job_id=out["job_id"]))
    res = run(go())
    assert res["status"] == "ok" and res["job_status"] == "cancelled"


def test_cannot_cancel_running_job(env, tools):
    async def go():
        out = json.loads(await tools.submit(command="execute_code", params={"code": "x"}))
        await jobs.handle_update(out["job_id"], "running", "", "", session=env.target_session)
        return json.loads(await tools.job_cancel(job_id=out["job_id"]))
    res = run(go())
    assert res["error"] == "job_running"


def test_cancel_when_target_gone_marks_cancelled(env, tools):
    async def go():
        out = json.loads(await tools.submit(command="execute_code", params={"code": "x"}))
        env.bus.unregister(TARGET)
        return out["job_id"], json.loads(await tools.job_cancel(job_id=out["job_id"]))
    job_id, res = run(go())
    assert res["job_status"] == "cancelled" and "note" in res
    assert row(env, job_id).status == "cancelled"


# ---- pull fallback for dispatches the event stream missed ---------------

def _age(env, job_id, seconds):
    async def go():
        async with env.db() as s:
            r = await job_repo.get_job(s, job_id)
            r.created_at = job_repo.utcnow() - timedelta(seconds=seconds)
            await s.commit()
    run(go())


def test_pending_dispatches_returns_missed_queued_jobs(env, tools):
    out = json.loads(run(tools.submit(command="execute_code", params={"code": "print(1)"})))
    _age(env, out["job_id"], 10)
    res = run(jobs.pending_dispatches(env.target_session, TARGET, []))
    assert res["status"] == "ok" and res["bus_id"] == env.bus_id
    assert [d["job_id"] for d in res["dispatches"]] == [out["job_id"]]
    assert res["dispatches"][0]["command"] == "execute_code"
    assert res["dispatches"][0]["params"] == {"code": "print(1)"}


def test_pending_dispatches_skips_young_running_and_truncated(env, tools):
    young = json.loads(run(tools.submit(command="get_scene_info")))["job_id"]
    running = json.loads(run(tools.submit(command="get_scene_info")))["job_id"]
    run(jobs.handle_update(running, "running", "", "", session=env.target_session))
    _age(env, running, 10)
    big = json.loads(run(tools.submit(command="execute_code",
                                      params={"code": "x" * (job_repo.PARAMS_CAP + 10)})))["job_id"]
    _age(env, big, 10)
    res = run(jobs.pending_dispatches(env.target_session, TARGET, []))
    ids = [d["job_id"] for d in res["dispatches"]]
    assert young not in ids and running not in ids and big not in ids


def test_pending_dispatches_only_for_the_calling_client(env, tools):
    run(tools.submit(command="get_scene_info"))
    assert run(jobs.pending_dispatches(env.other_session, TARGET, []))["error"] == "not_registered_client"
    assert run(jobs.pending_dispatches(env.target_session, OTHER, []))["error"] == "not_registered_client"


def test_pending_dispatches_reports_cancelled_held_jobs(env, tools):
    job_id = json.loads(run(tools.submit(command="get_scene_info")))["job_id"]
    run(jobs.finish(job_id, "cancelled", error="x"))
    res = run(jobs.pending_dispatches(env.target_session, TARGET, [job_id, "j-unknown"]))
    assert res["cancelled"] == [job_id]


def test_unacknowledged_cancel_marks_job_cancelled(env, tools, monkeypatch):
    # The cancel notification can be lost like a dispatch; the job must not
    # stay queued, or the pull fallback would deliver it after all.
    monkeypatch.setattr(job_tools, "CANCEL_ACK_WAIT_S", 0.05)

    async def go():
        out = json.loads(await tools.submit(command="execute_code", params={"code": "x"}))
        return out["job_id"], json.loads(await tools.job_cancel(job_id=out["job_id"]))
    job_id, res = run(go())
    assert res["status"] == "ok" and res["job_status"] == "cancelled" and "note" in res
    assert row(env, job_id).status == "cancelled"
    _age(env, job_id, 10)
    pend = run(jobs.pending_dispatches(env.target_session, TARGET, []))
    assert job_id not in [d["job_id"] for d in pend["dispatches"]]


def test_keepalive_reaches_every_blender_session(env):
    from blender_mcp import stream_keepalive

    class FakeSession:
        def __init__(self):
            self.sent = []

        async def send_log_message(self, level, data, logger):
            self.sent.append((level, data, logger))

    s = FakeSession()
    env.bus.register(ClientInfo(uuid=OTHER, client_type="blender", is_persistent=True, session=s))
    run(stream_keepalive.send_keepalives())
    assert s.sent, "keepalive not sent"
    level, data, logger_name = s.sent[0]
    assert level == "debug" and logger_name == "_message_bus"
    assert data["payload"]["message_type"] == "bus_keepalive" and data["target_uuid"] == OTHER
