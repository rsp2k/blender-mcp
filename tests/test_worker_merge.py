"""Phase-2 worker results: per-collection merge offer tools and banner text."""
# ruff: noqa: F811  (env/tools/wtools are pytest fixtures)

import json
import sys
import types

for _name in ("bpy", "bmesh", "mathutils"):
    sys.modules.setdefault(_name, types.ModuleType(_name))

from addon.worker_merge import base_name, merge_banner_lines
from tests.test_jobs import TARGET, env, run, tools  # noqa: F401
from tests.test_workers import WORKER, add_worker, reply_to, wtools  # noqa: F401


def test_offer_merge_dispatches_to_gui_blender(env, wtools):
    seen = []

    def handler(command, params, target):
        seen.append((command, params, target))
        return "completed", {"offered": True, "collections": params["collections"]}
    reply_to(env, handler)
    out = json.loads(run(wtools.offer_merge(path="/tmp/r.blend", collections=["Attic", "Roof"],
                                            message="attic rebuild")))
    assert out["status"] == "completed"
    command, params, target = seen[0]
    assert command == "offer_merge" and target == TARGET
    assert params == {"path": "/tmp/r.blend", "collections": ["Attic", "Roof"],
                      "message": "attic rebuild", "mode": "replace"}


def test_offer_merge_validates_before_dispatch(env, wtools):
    out = json.loads(run(wtools.offer_merge(path="/tmp/r.blend", collections=["A"], mode="swap")))
    assert out["error"] == "bad_mode"
    out = json.loads(run(wtools.offer_merge(path="/tmp/r.blend", collections=[])))
    assert out["error"] == "no_collections"


def test_offer_merge_refuses_a_worker_target(env, wtools):
    env.worker_session = add_worker(env)
    out = json.loads(run(wtools.offer_merge(path="/tmp/r.blend", collections=["A"],
                                            target_uuid=WORKER)))
    assert out["error"] == "target_is_worker"


def test_get_merge_result_reads_gui_state(env, wtools):
    report = {"pending": None, "last_result": {"ok": True, "replaced": [{"name": "A"}]}}
    reply_to(env, lambda command, params, target: ("completed", report))
    out = json.loads(run(wtools.get_merge_result()))
    assert out["status"] == "completed" and out["command"] == "get_merge_result"
    assert json.loads(out["result"]) == report


def test_merge_banner_text():
    assert merge_banner_lines(None) == []
    lines = merge_banner_lines({"path": "/w/result.blend", "collections": ["Attic", "Roof"],
                                "mode": "replace", "message": "attic rebuild"})
    assert lines[0] == "Background result ready: attic rebuild"
    assert "Attic, Roof" in lines[1] and lines[1].startswith("Replace")
    assert lines[2] == "From: result.blend"
    assert "edits elsewhere are kept" in lines[3]
    add = merge_banner_lines({"collections": ["A"], "mode": "add"})
    assert add[1].startswith("Add") and not any("replaced" in x for x in add)


def test_base_name():
    assert base_name("Attic.001") == "Attic"
    assert base_name("Attic") == "Attic"
    assert base_name("v1.5") == "v1.5"
    assert base_name("Beam.12") == "Beam.12"
