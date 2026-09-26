"""Job-pump health over the bus and the remote pump reset (bug-iDJHVyy4e2Q)."""

import asyncio
import json
import time

import pytest

from blender_mcp import client_health, jobs
from blender_mcp.dispatch_component import _dispatch
from blender_mcp.message_bus import ClientInfo
from tests.test_jobs import TARGET, env, run  # noqa: F401  (env is a fixture)


def client(**health):
    c = ClientInfo(uuid="blender-x", client_type="blender")
    if health:
        client_health.record(c, health)
    return c


def test_sanitize_keeps_known_typed_fields_only():
    clean = client_health.sanitize({
        "queue_depth": 3.0, "drain_timer_registered": True, "last_drain_seconds_ago": 1.234,
        "active_jobs": [{"job_id": "j-1", "running_seconds": 40}, {"nope": 1}, "junk"],
        "evil": "x" * 10_000,
    })
    assert clean == {"queue_depth": 3, "drain_timer_registered": True,
                     "last_drain_seconds_ago": 1.2,
                     "active_jobs": [{"job_id": "j-1", "running_seconds": 40.0}]}
    assert client_health.sanitize("not a dict") is None


def test_describe_busy_job():
    summary, hint = client_health.describe(client(
        queue_depth=2, active_jobs=[{"job_id": "j-long", "running_seconds": 40}],
        drain_timer_registered=True, last_drain_seconds_ago=40))
    assert summary["queue_depth"] == 2 and summary["active_jobs"][0]["running_seconds"] >= 40
    assert "busy running job j-long" in hint and "2 more queued" in hint


def test_describe_stalled_pump():
    _, hint = client_health.describe(client(
        queue_depth=2, active_jobs=[], drain_timer_registered=False))
    assert "stalled" in hint and "blender_reset_pump" in hint
    _, hint = client_health.describe(client(
        queue_depth=1, active_jobs=[], drain_timer_registered=True, last_drain_seconds_ago=30))
    assert "stalled" in hint


def test_describe_empty_queue_points_at_delivery():
    _, hint = client_health.describe(client(
        queue_depth=0, active_jobs=[], drain_timer_registered=True, last_drain_seconds_ago=0.1))
    assert "hasn't reached it" in hint


def test_describe_stale_report_and_no_report():
    c = client(queue_depth=0, active_jobs=[])
    c.health_at = time.time() - 120
    _, hint = client_health.describe(c)
    assert "hasn't reported" in hint
    assert client_health.describe(ClientInfo(uuid="b", client_type="blender")) == (None, None)


def test_pending_dispatches_records_health_and_delivers_reset_once(env):  # noqa: F811
    info = env.bus.get(TARGET)
    info.reset_pump_requested = True
    res = run(jobs.pending_dispatches(env.target_session, TARGET, [],
                                      health={"queue_depth": 4, "active_jobs": []}))
    assert res["reset_pump"] is True and info.health["queue_depth"] == 4
    again = run(jobs.pending_dispatches(env.target_session, TARGET, []))
    assert again["reset_pump"] is False
    assert info.to_dict()["health"]["queue_depth"] == 4


def test_dispatch_timeout_carries_health_diagnosis(env):  # noqa: F811
    client_health.record(env.bus.get(TARGET), {
        "queue_depth": 1, "active_jobs": [], "drain_timer_registered": False})
    out = json.loads(run(_dispatch(env.bus, env.bus_id, "get_scene_info", {}, TARGET, 0.05,
                                   caller_sub="u1")))
    assert out["target_health"]["queue_depth"] == 1
    assert "stalled" in out["hint"]


def test_reset_pump_tool_waits_for_checkin(env, monkeypatch):  # noqa: F811
    monkeypatch.setattr(client_health, "_resolve_user_id", lambda ctx: "u1")
    monkeypatch.setattr(client_health, "RESET_WAIT_S", 2.0)

    async def fake_resolve_bus(user_id, bus_id=None):
        return {"ok": True, "bus": env.bus, "bus_id": env.bus_uuid, "name": "test"}

    monkeypatch.setattr(client_health, "resolve_bus", fake_resolve_bus)
    tool = client_health.BlenderHealthComponent()

    async def go():
        async def addon_checks_in():
            await asyncio.sleep(0.3)
            await jobs.pending_dispatches(env.target_session, TARGET, [],
                                          health={"queue_depth": 0, "active_jobs": []})
        checkin = asyncio.create_task(addon_checks_in())
        out = json.loads(await tool.reset_pump(target_uuid=TARGET))
        await checkin
        return out

    out = run(go())
    assert out["reset_delivered"] is True and out["health"]["queue_depth"] == 0


def test_reset_pump_reports_pending_when_addon_silent(env, monkeypatch):  # noqa: F811
    monkeypatch.setattr(client_health, "_resolve_user_id", lambda ctx: "u1")
    monkeypatch.setattr(client_health, "RESET_WAIT_S", 0.3)

    async def fake_resolve_bus(user_id, bus_id=None):
        return {"ok": True, "bus": env.bus, "bus_id": env.bus_uuid, "name": "test"}

    monkeypatch.setattr(client_health, "resolve_bus", fake_resolve_bus)
    out = json.loads(run(client_health.BlenderHealthComponent().reset_pump(target_uuid=TARGET)))
    assert out["reset_delivered"] is False and "still pending" in out["hint"]
    assert env.bus.get(TARGET).reset_pump_requested is True


@pytest.mark.parametrize("tool", ["world_bounds", "check_interference", "get_keyframes",
                                  "insert_keyframe", "set_interpolation"])
def test_analysis_tools_dispatch_their_command(tool, monkeypatch):
    from blender_mcp import analysis_tools

    seen = {}

    async def fake_call(self, ctx, command, params, target_uuid, timeout, bus_id=None):
        seen.update(command=command, params=params)
        return json.dumps({"status": "completed"})

    monkeypatch.setattr(analysis_tools.BlenderAnalysisComponent, "_call", fake_call)
    comp = analysis_tools.BlenderAnalysisComponent()
    args = {"world_bounds": {}, "check_interference": {"objects": ["A", "B"], "frames": [1, 5]},
            "get_keyframes": {"object": "A"},
            "insert_keyframe": {"object": "A", "data_path": "location", "frame": 3},
            "set_interpolation": {"object": "A", "interpolation": "LINEAR"}}[tool]
    run(getattr(comp, tool)(**args))
    assert seen["command"] == tool
    for k, v in args.items():
        assert seen["params"][k] == v
