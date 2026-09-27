"""Boolean tool: outcome verdicts, input checks, and server-side validation."""

import asyncio
import json

import pytest

from addon import boolean_checks as bc
from blender_mcp import boolean_tools as bt


def run(coro):
    return asyncio.run(coro)


def health(faces=6, verts=8, nm=0, volume=8.0, **extra):
    return {"faces": faces, "vertices": verts, "non_manifold_edges": nm, "volume": volume, **extra}


# ---- verdicts: each failure mode from model-home's feedback -----------------

def test_clean_difference_has_no_warnings():
    assert bc.verdict("DIFFERENCE", health(), health(faces=32, volume=1.0), health(faces=40, verts=40, volume=7.0)) == []


def test_noop_difference_warns():  # item 25: EXACT DIFFERENCE silently did nothing
    w = bc.verdict("DIFFERENCE", health(), health(), health())
    assert any("changed nothing" in x for x in w)


def test_union_that_drops_operand_warns():  # item 21: operand eaten, no error
    w = bc.verdict("UNION", health(volume=8.0), health(volume=8.0), health(volume=8.0))
    assert any("dropped" in x for x in w)


def test_union_smaller_than_input_warns():  # item 21: the volume check
    w = bc.verdict("UNION", health(volume=8.0), health(volume=8.0), health(faces=10, verts=12, volume=9.0))
    assert not any("smaller" in x for x in w)
    w = bc.verdict("UNION", health(volume=8.0), health(volume=10.0), health(faces=10, verts=12, volume=9.0))
    assert any("smaller than an input" in x for x in w)


def test_difference_that_grows_warns():  # item 26: DIFFERENCE increased the volume
    w = bc.verdict("DIFFERENCE", health(volume=8.0), health(volume=1.0), health(faces=12, verts=16, volume=9.5))
    assert any("increased the volume" in x for x in w)


def test_new_non_manifold_edges_warn():  # item 26: corruption invisible until later
    w = bc.verdict("UNION", health(), health(), health(faces=11, verts=14, nm=4, volume=None))
    assert any("4 non-manifold edges (was 0)" in x for x in w)
    assert any("no longer a closed solid" in x for x in w)


def test_float_noise_is_not_a_volume_change():
    w = bc.verdict("UNION", health(volume=8.0), health(volume=8.0), health(faces=12, verts=16, volume=16.000001))
    assert w == []


def test_inward_normals_warn():
    w = bc.verdict("DIFFERENCE", health(), health(), health(faces=12, verts=16, volume=-2.0))
    assert any("inward" in x for x in w)


# ---- input checks ------------------------------------------------------------

def test_input_problems_names_the_edge_kinds():
    probs = bc.input_problems(health(nm=6, boundary_edges=4, multi_face_edges=2, volume=None))
    assert probs and "6 non-manifold edges" in probs[0] and "4 open" in probs[0] and "3+ faces" in probs[0]
    assert bc.input_problems(health()) == []
    assert "no faces" in bc.input_problems(health(faces=0))[0]
    assert any("inward" in p for p in bc.input_problems(health(volume=-1.0)))


def test_solver_names():
    assert bc.normalize_solver("manifold") == "MANIFOLD"
    with pytest.raises(ValueError, match="no longer exists"):
        bc.normalize_solver("FAST")
    with pytest.raises(ValueError, match="must be one of"):
        bc.normalize_solver("bogus")
    with pytest.raises(ValueError):
        bc.normalize_operation("SUBTRACT")


def test_server_and_addon_agree_on_names():
    assert bt.SOLVERS == bc.SOLVERS and bt.OPERATIONS == bc.OPERATIONS


# ---- server component ----------------------------------------------------------

@pytest.fixture
def comp(monkeypatch):
    c = bt.BlenderBooleanComponent()
    sent = []

    async def fake_call(ctx, command, params, target_uuid, timeout, bus_id=None):
        sent.append((command, params, target_uuid, timeout))
        return json.dumps({"status": "completed", "command": command})

    monkeypatch.setattr(c, "_call", fake_call)
    c.sent = sent
    return c


def test_boolean_dispatches_normalized_params(comp):
    run(comp.boolean(target="Roof", operands="Wing", operation="union", solver="exact", coplanar_nudge=0.01))
    command, params, _, timeout = comp.sent[-1]
    assert command == "boolean" and timeout == bt.TIMEOUT_MEDIUM
    assert params["operands"] == ["Wing"] and params["operation"] == "UNION" and params["solver"] == "EXACT"
    assert params["check"] is True and params["force"] is False and params["coplanar_nudge"] == 0.01


@pytest.mark.parametrize("kwargs,detail", [
    ({"solver": "FAST"}, "no longer exists"),
    ({"operation": "SUBTRACT"}, "operation must be"),
    ({"operands": []}, "non-empty"),
    ({"coplanar_nudge": -1.0}, "positive"),
    ({"coplanar_tolerance": 0}, "positive"),
])
def test_bad_boolean_never_reaches_blender(comp, kwargs, detail):
    args = {"target": "T", "operands": ["C"], **kwargs}
    out = json.loads(run(comp.boolean(**args)))
    assert out["error"] == "invalid_argument" and detail in out["detail"] and comp.sent == []


def test_mesh_health_dispatch(comp):
    run(comp.mesh_health(objects="Roof", evaluated=True))
    assert comp.sent[-1][:2] == ("mesh_health", {"objects": ["Roof"], "evaluated": True})
    out = json.loads(run(comp.mesh_health(objects=[])))
    assert out["error"] == "invalid_argument"
