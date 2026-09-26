"""Background workers: roles on the bus, spawn/stop/cancel flows, progress, worker mode."""
# ruff: noqa: F811  (env/tools are pytest fixtures imported from test_jobs)

import asyncio
import json
import sys
import types
from types import SimpleNamespace

import pytest

for _name in ("bpy", "bmesh", "mathutils"):
    sys.modules.setdefault(_name, types.ModuleType(_name))

from addon import worker as addon_worker
from addon.client import job_reporter
from blender_mcp import jobs, worker_tools
from blender_mcp.dispatch_component import _pick_blender_target
from blender_mcp.message_bus import ClientInfo
from tests.test_jobs import TARGET, env, row, run, tools  # noqa: F401

WORKER = "blender-worker-aaaaaaaaaaaa"


def add_worker(env, uuid=WORKER, parent=TARGET):
    session = object()
    env.bus.register(ClientInfo(uuid=uuid, client_type="blender", is_persistent=True,
                                session=session, role="worker", parent_uuid=parent, pid=4242))
    return session


# ---- bus roles --------------------------------------------------------------

def test_role_and_parent_are_listed_and_kept_on_reregister(env):
    add_worker(env)
    d = env.bus.get(WORKER).to_dict()
    assert d["role"] == "worker" and d["parent_uuid"] == TARGET
    env.bus.register(ClientInfo(uuid=WORKER, client_type="blender", is_persistent=True))
    assert env.bus.get(WORKER).role == "worker"
    assert "role" not in env.bus.get(TARGET).to_dict()


def test_orphaned_worker(env):
    add_worker(env)
    assert not env.bus.is_orphaned(env.bus.get(WORKER))
    env.bus.unregister(TARGET)
    assert env.bus.is_orphaned(env.bus.get(WORKER))


def test_implicit_pick_skips_workers(env):
    add_worker(env)
    assert _pick_blender_target(env.bus, None) == {"ok": True, "uuid": TARGET}
    assert env.bus.resolve_target("latest").uuid == TARGET
    assert _pick_blender_target(env.bus, WORKER)["uuid"] == WORKER
    assert env.bus.resolve_target("pid:4242").uuid == WORKER


# ---- worker tools -----------------------------------------------------------

@pytest.fixture
def wtools(env, tools, monkeypatch):
    monkeypatch.setattr(worker_tools, "_resolve_user_id", lambda ctx: "u1")

    async def fake_resolve_bus(user_id, bus_id=None):
        return {"ok": True, "bus": env.bus, "bus_id": env.bus_uuid, "name": "test"}

    monkeypatch.setattr(worker_tools, "resolve_bus", fake_resolve_bus)
    monkeypatch.setattr(worker_tools, "POLL_S", 0.01)
    monkeypatch.setattr(worker_tools, "GRACEFUL_EXIT_WAIT_S", 0.5)
    return worker_tools.BlenderWorkerComponent()


def reply_to(env, handler):
    """Answer command_dispatch messages: handler(command, params, target) -> (status, result)."""
    def on_route(payload):
        if payload.get("message_type") != "command_dispatch":
            return
        target = env.routed[-1][1]["routing"]["target_uuid"]
        status, result = handler(payload["command"], payload.get("params") or {}, target)
        session = env.target_session if target == TARGET else env.worker_session

        async def later():
            await asyncio.sleep(0.01)
            await jobs.handle_update(payload["job_id"], status, json.dumps(result), "",
                                     session=session)
        asyncio.get_running_loop().create_task(later())
    env.on_route = on_route


def test_spawn_returns_once_worker_registers(env, wtools):
    def handler(command, params, target):
        assert command == "spawn_worker" and target == TARGET

        async def worker_joins():
            await asyncio.sleep(0.05)
            env.worker_session = add_worker(env)
        asyncio.get_running_loop().create_task(worker_joins())
        return "completed", {"worker_uuid": WORKER, "pid": 4242, "snapshot_path": "/tmp/s.blend",
                             "worker_dir": "/tmp/w", "log_path": "/tmp/w/worker.log",
                             "expires_at": 1.0}
    reply_to(env, handler)
    out = json.loads(run(wtools.spawn_worker(timeout_s=5)))
    assert out["status"] == "ok" and out["worker_uuid"] == WORKER and out["parent_uuid"] == TARGET


def test_spawn_refuses_a_worker_target(env, wtools):
    env.worker_session = add_worker(env)
    out = json.loads(run(wtools.spawn_worker(target_uuid=WORKER)))
    assert out["error"] == "target_is_worker"


def test_offer_reload_refuses_a_worker_target(env, wtools):
    env.worker_session = add_worker(env)
    out = json.loads(run(wtools.offer_reload(path="/tmp/r.blend", target_uuid=WORKER)))
    assert out["error"] == "target_is_worker"


def test_stop_idle_worker_gracefully(env, wtools):
    env.worker_session = add_worker(env)

    def handler(command, params, target):
        assert command == "worker_exit" and target == WORKER
        asyncio.get_running_loop().call_later(0.05, env.bus.unregister, WORKER)
        return "completed", {"exiting": True}
    reply_to(env, handler)
    out = json.loads(run(wtools.stop_worker(worker_uuid=WORKER)))
    assert out["stopped"] and out["how"] == "graceful"
    assert env.bus.get(WORKER) is None


def test_stop_busy_worker_kills_through_parent(env, wtools, tools):
    env.worker_session = add_worker(env)
    seen = []

    def handler(command, params, target):
        seen.append((command, target))
        if command == "stop_worker":
            assert target == TARGET and params["worker_uuid"] == WORKER
            return "completed", {"exited": True, "returncode": -15, "worker_dir": "/tmp/w"}
        return "completed", {}

    async def go():
        out = json.loads(await tools.submit(command="execute_code", params={"code": "x"},
                                            target_uuid=WORKER))
        await jobs.handle_update(out["job_id"], "running", "", "", session=env.worker_session)
        reply_to(env, handler)
        return json.loads(await wtools.stop_worker(worker_uuid=WORKER))
    out = run(go())
    assert out["stopped"] and out["how"] == "kill"
    assert seen == [("stop_worker", TARGET)]
    assert env.bus.get(WORKER) is None


def test_cancel_running_job_on_worker_stops_it(env, wtools, tools):
    env.worker_session = add_worker(env)

    def handler(command, params, target):
        return "completed", {"exited": True, "returncode": -9}

    async def go():
        out = json.loads(await tools.submit(command="execute_code", params={"code": "x"},
                                            target_uuid=WORKER))
        await jobs.handle_update(out["job_id"], "running", "", "", session=env.worker_session)
        reply_to(env, handler)
        return out["job_id"], json.loads(await tools.job_cancel(job_id=out["job_id"]))
    job_id, res = run(go())
    assert res["status"] == "ok" and res["job_status"] == "cancelled"
    assert res["worker_stopped"]["stopped"]
    assert row(env, job_id).status == "cancelled"
    assert env.bus.get(WORKER) is None


def test_running_job_on_gui_still_cannot_be_cancelled(env, tools):
    async def go():
        out = json.loads(await tools.submit(command="execute_code", params={"code": "x"}))
        await jobs.handle_update(out["job_id"], "running", "", "", session=env.target_session)
        return json.loads(await tools.job_cancel(job_id=out["job_id"]))
    assert run(go())["error"] == "job_running"


# ---- progress ---------------------------------------------------------------

def test_progress_is_stored_and_reported(env, tools):
    async def go():
        out = json.loads(await tools.submit(command="execute_code", params={"code": "x"}))
        jid = out["job_id"]
        await jobs.handle_update(jid, "running", "", "", session=env.target_session,
                                 progress=1.7, progress_message="boolean 3/7")
        status = json.loads(await tools.job_status(job_id=jid))
        await jobs.handle_update(jid, "completed", "ok", "", session=env.target_session)
        await jobs.handle_update(jid, "running", "", "", session=env.target_session,
                                 progress=0.1, progress_message="late")
        final = json.loads(await tools.job_status(job_id=jid))
        return status, final
    status, final = run(go())
    assert status["status"] == "running"
    assert status["progress"]["fraction"] == 1.0 and status["progress"]["message"] == "boolean 3/7"
    assert final["status"] == "completed" and final["progress"]["message"] == "boolean 3/7"


def test_progress_reporter_rate_limits(monkeypatch):
    sent = []
    monkeypatch.setattr(job_reporter, "submit_job_update",
                        lambda client, job_id, status, **kw: sent.append(kw))
    t = [0.0]
    report = job_reporter.make_progress_reporter(SimpleNamespace(), "j-1", clock=lambda: t[0])
    assert report(0.1, "a") is True
    assert report(0.2, "b") is False          # within 0.5 s
    assert report(1.0, "done") is True        # completion always goes out
    t[0] = 1.0
    assert report("bad") is False
    assert report(0.5, "c") is True
    assert [s["progress"] for s in sent] == [0.1, 1.0, 0.5]


# ---- worker mode ------------------------------------------------------------

def good_env(**over):
    e = {
        "BLENDER_MCP_WORKER": "1",
        "BLENDER_MCP_WORKER_UUID": WORKER,
        "BLENDER_MCP_WORKER_PARENT": TARGET,
        "BLENDER_MCP_WORKER_PARENT_PID": "123",
        "BLENDER_MCP_WORKER_TOKEN": "tok",
        "BLENDER_MCP_WORKER_TOKEN_EXP": "5000",
        "BLENDER_MCP_SERVER": "https://mcp.example",
        "BLENDER_MCP_WORKER_DEADLINE": "4000",
    }
    e.update(over)
    return e


def test_parse_env():
    cfg = addon_worker.parse_env(good_env())
    assert cfg.uuid == WORKER and cfg.parent_pid == 123 and cfg.deadline == 4000
    assert cfg.bus_id is None and cfg.label == "background worker"
    with pytest.raises(addon_worker.WorkerConfigError):
        addon_worker.parse_env(good_env(BLENDER_MCP_WORKER_TOKEN=""))
    with pytest.raises(addon_worker.WorkerConfigError):
        addon_worker.parse_env(good_env(BLENDER_MCP_WORKER_PARENT_PID="x"))
    with pytest.raises(addon_worker.WorkerConfigError):
        addon_worker.parse_env({})
    assert addon_worker.is_worker_mode(good_env()) and not addon_worker.is_worker_mode({})


def test_exit_reasons_in_priority_order():
    cfg = addon_worker.parse_env(good_env())
    args = {"parent_alive": True, "stop_requested": False, "client_running": True}
    assert addon_worker.exit_reason(cfg, 100, **args) is None
    assert addon_worker.exit_reason(cfg, 4000, **args) == "deadline reached"
    assert "gone" in addon_worker.exit_reason(cfg, 100, **(args | {"parent_alive": False}))
    assert addon_worker.exit_reason(cfg, 100, **(args | {"client_running": False})) == "bus client stopped"
    assert addon_worker.exit_reason(cfg, 4000, **(args | {"stop_requested": True})) == "stop requested"


def test_deadline_and_refresh_rules():
    assert addon_worker.worker_deadline(1000, 0) == 1000 + 7200
    assert addon_worker.worker_deadline(1000, 3000) == 3000 - 300
    assert addon_worker.needs_token_refresh(1000, 1000 + 1000)
    assert not addon_worker.needs_token_refresh(1000, 1000 + 3600)
    assert not addon_worker.needs_token_refresh(1000, 0)


def test_spawn_env_passes_only_the_access_token():
    e = addon_worker.spawn_env(
        {"PATH": "/bin", "BLENDER_MCP_BUS_ID": "stale"},
        worker_uuid=WORKER, parent_uuid=TARGET, parent_pid=7, token="access",
        token_exp=9, server="https://s", bus_id=None, deadline=99.9, label="w",
        addon_module="bl_ext.x.blender_mcp",
    )
    assert e["BLENDER_MCP_WORKER"] == "1" and e["BLENDER_MCP_WORKER_TOKEN"] == "access"
    assert e["BLENDER_MCP_WORKER_DEADLINE"] == "99" and e["PATH"] == "/bin"
    assert "BLENDER_MCP_BUS_ID" not in e
    assert not any("REFRESH" in k for k in e)
    assert addon_worker.parse_env(e).parent_pid == 7


def test_new_worker_uuid_shape():
    u = addon_worker.new_worker_uuid()
    assert u.startswith("blender-worker-") and len(u) == len("blender-worker-") + 12


def test_worker_loop_exits_when_parent_dies(monkeypatch):
    """run_worker_loop with a fake client: parent pid disappears -> clean exit."""
    from addon import state

    stopped = []

    class FakeClient:
        def __init__(self, **kw):
            self.kw, self.running = kw, False

        def start(self):
            self.running = True

        def stop(self):
            stopped.append(True)
            self.running = False

        def _drain_queue(self):
            return 0.1

    cfg = addon_worker.parse_env(good_env())
    t = [0.0]

    def fake_sleep(s):
        t[0] += s

    alive = iter([True, True, False])
    reason = addon_worker.run_worker_loop(cfg, pid_alive=lambda pid: next(alive),
                                          clock=lambda: t[0], sleep=fake_sleep,
                                          client_factory=FakeClient,
                                          executor_factory=lambda: object())
    assert "gone" in reason and stopped
    assert state._client.kw["refresh_token"] == ""
    assert state._client.role == "worker" and state._client.parent_uuid == TARGET


def test_reload_banner_lines():
    assert addon_worker.reload_banner_lines(None, False) == []
    lines = addon_worker.reload_banner_lines({"path": "/w/result.blend", "message": "attic done"}, True)
    assert lines[0] == "Background result ready: attic done"
    assert lines[1] == "File: result.blend"
    assert lines[2].startswith("Reloading discards")
    assert len(addon_worker.reload_banner_lines({"path": "/w/r.blend"}, False)) == 2
