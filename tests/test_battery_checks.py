"""Chat battery: judging checks against observed data (scripts/battery/checks.py)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "battery"))

from checks import (
    color_matches,
    judge,
    judge_all,
    normalise_check,
    python_exprs,
    select,
)


def obj(name, loc=(0, 0, 0), size=(1, 1, 1), type_="MESH", mats=(), bottom=None):
    """An object snapshot with its AABB centred on loc (or resting at ``bottom``)."""
    lo = [loc[i] - size[i] / 2 for i in range(3)]
    hi = [loc[i] + size[i] / 2 for i in range(3)]
    if bottom is not None:
        lo[2], hi[2] = bottom, bottom + size[2]
    return {"name": name, "type": type_, "loc": list(loc), "dims": list(size), "bmin": lo, "bmax": hi,
            "mats": list(mats)}


def snap(*objs, materials=None, annotations=None, python=None):
    return {"objects": list(objs), "materials": materials or {}, "annotations": annotations or {},
            "python": python or {}}


def run(raw, after, before=None, turn=None, index=0):
    return judge(normalise_check(raw), after, before, turn or {}, index)


def test_object_exists_absent_and_count():
    s = snap(obj("Crate"), obj("Light", type_="LIGHT"))
    assert run({"object_exists": "Crate"}, s)["ok"]
    assert run({"object_exists": "crate"}, s)["ok"]  # case-insensitive fallback
    assert not run({"object_exists": "Box"}, s)["ok"]
    assert run({"object_absent": "Box"}, s)["ok"]
    assert not run({"object_exists": {"name_contains": "Cr", "count": 2}}, s)["ok"]
    r = run({"count_type": {"type": "mesh", "eq": 1}}, s)
    assert r["ok"] and r["observed"] == 1
    assert not run({"count_type": {"type": "MESH", "min": 2}}, s)["ok"]


def test_new_selector_uses_before_snapshot():
    before = snap(obj("Old"))
    after = snap(obj("Old"), obj("Fresh"))
    assert [o["name"] for o in select({"new": True}, after, before)] == ["Fresh"]
    assert run({"count_type": {"type": "MESH", "new": True, "eq": 1}}, after, before)["ok"]


def test_pick_tallest():
    s = snap(obj("A", size=(1, 1, 1)), obj("B", size=(0.2, 0.2, 3)), obj("C", size=(4, 4, 0.1)))
    assert select({"type": "MESH", "pick": "tallest"}, s)[0]["name"] == "B"
    assert select({"type": "MESH", "pick": "largest"}, s)[0]["name"] == "C"


def test_dims_and_any_order():
    s = snap(obj("T", size=(0.8, 1.2, 0.04)))
    assert not run({"dims_approx": {"name": "T", "dims": [1.2, 0.8, 0.04]}}, s)["ok"]
    r = run({"dims_approx": {"name": "T", "dims": [1.2, 0.8, 0.04], "any_order": True}}, s)
    assert r["ok"] and r["observed"]["dims"] == [0.8, 1.2, 0.04]
    assert run({"dims_approx": {"name": "T", "dims": [None, None, 0.05], "tol": 0.02}}, s)["ok"]
    assert run({"dims_approx": {"name": "Missing", "dims": [1, 1, 1]}}, s)["observed"] == "no match"


def test_location_points():
    s = snap(obj("Crate", loc=(3, -1, 0.25), size=(2, 1, 0.5)))
    assert run({"location_approx": {"name": "Crate", "point": "bottom", "location": [3, -1, 0]}}, s)["ok"]
    assert run({"location_approx": {"name": "Crate", "point": "top", "location": [None, None, 0.5]}}, s)["ok"]
    r = run({"location_approx": {"name": "Crate", "location": [0, 0, 0], "tol": 0.1}}, s)
    assert not r["ok"] and r["observed"]["origin"] == [3, -1, 0.25]


def test_rests_on():
    table = obj("Table", loc=(1.5, 0.5, 0.75), size=(1.2, 0.8, 0.05))
    top = table["bmax"][2]
    ball = obj("Ball", loc=(1.5, 0.5, top + 0.15), size=(0.3, 0.3, 0.3))
    assert run({"rests_on": {"name": "Ball", "base": "Table"}}, snap(table, ball))["ok"]
    floating = obj("Ball", loc=(1.5, 0.5, top + 0.5), size=(0.3, 0.3, 0.3))
    r = run({"rests_on": {"name": "Ball", "base": "Table"}}, snap(table, floating))
    assert not r["ok"] and r["observed"]["gap"] == pytest.approx(0.35, abs=1e-3)
    beside = obj("Ball", loc=(5, 0.5, top + 0.15), size=(0.3, 0.3, 0.3))
    r = run({"rests_on": {"name": "Ball", "base": "Table"}}, snap(table, beside))
    assert not r["ok"] and r["observed"]["center_over_base"] is False


@pytest.mark.parametrize("rgb, name, ok", [
    ((0.8, 0.0, 0.0), "red", True),
    ((1.0, 0.21, 0.0), "orange", True),   # sRGB (1, .5, 0) stored linear
    ((1.0, 0.21, 0.0), "red", False),
    ((0.0, 0.1, 0.9), "blue", True),
    ((0.02, 0.45, 0.05), "green", True),
    ((0.05, 0.05, 0.05), "grey", True),
    ((0.05, 0.05, 0.05), "black", False),
    ((0.005, 0.005, 0.005), "black", True),
    ((0.9, 0.9, 0.9), "white", True),
    ((1.0, 0.766, 0.336), ["yellow", "orange"], True),  # gold
    ((0.5, 0.5, 0.5), [0.5, 0.5, 0.5], True),
])
def test_color_matches(rgb, name, ok):
    assert color_matches(list(rgb), name) is ok


def test_has_material_params_and_all():
    mats = {"Red": {"color": [0.8, 0, 0, 1], "metallic": 0.0, "roughness": 0.5},
            "Gold": {"color": [1.0, 0.766, 0.336, 1], "metallic": 1.0, "roughness": 0.2},
            "Tex": {"color": None, "metallic": 0.0, "roughness": 0.5}}
    s = snap(obj("A", mats=["Tex", "Red"]), obj("B", mats=["Gold"]), obj("C"), materials=mats)
    assert run({"has_material": {"name": "A", "color": "red"}}, s)["ok"]
    assert run({"has_material": {"name": "B", "metallic_min": 0.8, "roughness_max": 0.4}}, s)["ok"]
    assert not run({"has_material": {"name": "A", "metallic_min": 0.8}}, s)["ok"]
    assert run({"has_material": {"name": "A", "material": "tex"}}, s)["ok"]
    r = run({"has_material": {"name": "C"}}, s)
    assert not r["ok"] and r["observed"]["materials"] == []
    r = run({"has_material": {"type": "MESH", "all": True, "color": "red"}}, s)
    assert not r["ok"] and len(r["observed"]) == 3


def test_annotations():
    pillar = obj("Pillar", loc=(3, 0, 1.5), size=(0.6, 0.6, 3))
    near = {"LLM": {"strokes": 4, "bmin": [2.5, -0.5, -0.1], "bmax": [3.5, 0.5, 3.1]}}
    s = snap(pillar, obj("Short", loc=(-3, 0, 0.25)), annotations=near)
    assert run({"annotation_strokes_min": 1}, s)["ok"]
    assert not run({"annotation_strokes_min": {"min": 1, "layer": "Other"}}, s)["ok"]
    assert run({"annotation_near": "Pillar"}, s)["ok"]
    assert not run({"annotation_near": "Short"}, s)["ok"]
    everything = {"LLM": {"strokes": 1, "bmin": [-50, -50, -50], "bmax": [50, 50, 50]}}
    assert not run({"annotation_near": "Pillar"}, snap(pillar, annotations=everything))["ok"]


def test_transcript_checks():
    turn = {"reply": "I made a RED crate named Crate.", "errors": [],
            "tools": [{"name": "create_mesh", "ok": True}, {"name": "mesh_health", "ok": False}]}
    s = snap()
    assert run({"reply_contains": "red crate"}, s, turn=turn)["ok"]
    assert run({"reply_contains": {"text": ["blue", "red"]}}, s, turn=turn)["ok"]
    assert not run({"reply_contains": {"text": ["blue", "red"], "all": True}}, s, turn=turn)["ok"]
    assert not run({"reply_not_contains": ["crate"]}, s, turn=turn)["ok"]
    assert run({"max_tool_calls": 2}, s, turn=turn)["ok"]
    assert not run({"max_tool_calls": 1}, s, turn=turn)["ok"]
    assert run({"tool_used": "create_mesh"}, s, turn=turn)["ok"]
    assert not run({"tool_not_used": "create_mesh"}, s, turn=turn)["ok"]
    assert run({"no_errors": True}, s, turn=turn)["ok"]
    r = run({"no_errors": {"tools_ok": True}}, s, turn=turn)
    assert not r["ok"] and r["observed"] == ["tool mesh_health failed"]
    assert not run({"reply_contains": "x"}, s, turn={})["ok"]
    quoted = {"reply": 'Two: "Sphere_A" and **Sphere_B**.\nI can’t   do that.'}
    assert run({"reply_contains": "sphere_a and sphere_b"}, s, turn=quoted)["ok"]
    assert run({"reply_contains": "can't do that"}, s, turn=quoted)["ok"]


def test_scene_unchanged():
    before = snap(obj("A"), obj("B", loc=(1, 0, 0)))
    assert run({"scene_unchanged": True}, before, before)["ok"]
    moved = snap(obj("A"), obj("B", loc=(2, 0, 0)), obj("C"))
    r = run({"scene_unchanged": True}, moved, before)
    assert not r["ok"] and r["observed"] == {"added": ["C"], "removed": [], "changed": ["B"]}
    assert not run({"scene_unchanged": True}, before, None)["ok"]


def test_python_checks_use_their_index():
    checks = [normalise_check({"no_errors": True}), normalise_check({"python": "1 + 1 == 2"})]
    assert python_exprs(checks) == {"1": "1 + 1 == 2"}
    after = snap(python={"1": {"value": True}})
    results = judge_all(checks, after, None, {})
    assert [r["ok"] for r in results] == [True, True]
    assert not judge(checks[1], snap(python={"1": {"error": "NameError: x"}}), None, {}, 1)["ok"]
    assert judge(checks[1], snap(), None, {}, 1)["observed"] == "not evaluated"


def test_broken_data_is_a_failed_check_not_a_crash():
    bad = {"objects": [{"name": "A", "type": "MESH"}]}  # no bounds
    r = run({"rests_on": {"name": "A", "base": "A"}}, bad)
    assert not r["ok"] and "check error" in r["observed"]
