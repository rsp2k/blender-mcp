"""Poly Haven texturing: map roles, real-world scale, face rules, server tools."""

import asyncio
import importlib.util
import json
import math
import sys
from pathlib import Path

import pytest

from addon import polyhaven_helpers as ph
from blender_mcp import polyhaven_api as api
from blender_mcp import texture_tools as tt

# addon.executor's __init__ imports bpy; registry.py itself doesn't.
_spec = importlib.util.spec_from_file_location(
    "_registry_under_test", Path(__file__).parent.parent / "addon/executor/registry.py",
)
registry = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = registry  # dataclass() looks its module up here
_spec.loader.exec_module(registry)

# Recorded 2026-09-27 from api.polyhaven.com (trimmed).
INFO_BRICK = {
    "name": "Brick Wall 001", "type": 1, "dimensions": [3000, 3000],
    "categories": ["brick", "man made", "outdoor", "indoor", "clean", "wall"],
    "max_resolution": [8192, 8192],
}
INFO_HDRI = {"name": "Kloppenheim 06", "type": 0, "dimensions": None, "evs_cap": 12}
FILES_BRICK = ["AO", "Diffuse", "Displacement", "Rough", "arm", "blend", "gltf", "mtlx", "nor_dx", "nor_gl"]


def run(coro):
    return asyncio.run(coro)


# ---- map roles ---------------------------------------------------------------

def test_non_image_keys_are_skipped():  # mtlx used to be fetched as an image
    assert [k for k in FILES_BRICK if ph.classify_map(k) is None] == ["blend", "gltf", "mtlx"]


def test_pick_maps_prefers_opengl_normal():
    roles = ph.pick_maps(FILES_BRICK)
    assert roles["base"] == "Diffuse" and roles["rough"] == "Rough" and roles["disp"] == "Displacement"
    assert roles["normal_pick"] == "nor_gl" and roles["arm"] == "arm" and roles["ao"] == "AO"
    assert ph.pick_maps(["nor_dx", "Diffuse"])["normal_pick"] == "nor_dx"


# ---- real-world size -------------------------------------------------------

def test_dimensions_from_recorded_info():
    assert ph.dimensions_m(INFO_BRICK) == (3.0, 3.0)
    assert ph.dimensions_m(INFO_HDRI) is None
    assert ph.dimensions_m({"dimensions": [0, 1]}) is None
    assert ph.dimensions_m(None) is None


def test_server_and_addon_dimensions_agree():
    for info in (INFO_BRICK, INFO_HDRI, {"dimensions": [1234, 567]}, {}):
        a = ph.dimensions_m(info)
        assert api.dimensions_m(info) == (list(a) if a else None)


def test_mapping_scale_cancels_object_scale():
    # A 6 x 0.4 x 3 m wall made by scaling a 2 m cube: local unit = scale/1 m.
    assert ph.mapping_scale((3.0, 3.0), (3.0, 0.2, 1.5)) == pytest.approx((1.0, 0.2 / 3, 0.5))
    assert ph.mapping_scale(2.0) == (0.5, 0.5, 0.5)
    assert ph.mapping_scale([2.0, 1.0]) == (0.5, 0.5, 1.0)  # height drives Z (wall courses)
    with pytest.raises(ValueError):
        ph.mapping_scale(0)


# ---- face rules ---------------------------------------------------------------

@pytest.mark.parametrize("normal,spec,expected", [
    ((0, 0, 1), "up", True),
    ((0, 0, -1), "up", False),
    ((0, 0, -1), "down", True),
    ((1, 0, 0), "side", True),
    ((0.7, 0, 0.7), "side", False),  # 45 degree roof slope is not a wall
    ((0.94, 0, 0.34), "side", True),  # 20 degrees off vertical still counts
    ((0, 1, 0), [0, 2, 0], True),
    ((0, 0, 0), "up", False),
])
def test_normal_matches(normal, spec, expected):
    assert ph.normal_matches(normal, spec, 30.0) is expected


def test_normal_tolerance_is_in_degrees():
    tilted = (math.sin(math.radians(25)), 0, math.cos(math.radians(25)))
    assert ph.normal_matches(tilted, "up", 30) and not ph.normal_matches(tilted, "up", 20)
    with pytest.raises(ValueError):
        ph.normal_direction("sideways")


def test_height_band():
    assert ph.height_matches(0.4, None, 0.5) and not ph.height_matches(0.6, None, 0.5)
    assert ph.height_matches(2.0, 1.0, None) and not ph.height_matches(0.9, 1.0, None)
    assert ph.height_matches(-5, None, None)


# ---- gate hint (#10) -----------------------------------------------------------

def test_disabled_hint_travels_with_the_command():
    @registry.command("_test_gated", gate=lambda p: False, disabled_hint="flip the switch")
    def _h(self):
        return None

    spec = registry.COMMAND_REGISTRY["_test_gated"]
    assert spec.disabled_hint == "flip the switch" and spec.gate(None) is False
    assert registry.CommandSpec("x", _h).disabled_hint is None


def test_hint_says_no_reconnect():
    assert "no reconnect" in ph.POLYHAVEN_DISABLED_HINT
    assert "Use assets from Poly Haven" in ph.POLYHAVEN_DISABLED_HINT


@pytest.mark.parametrize("reply,expected", [
    ({"status": "error", "error": "search_polyhaven_assets is disabled: Poly Haven is switched off"}, True),
    ({"status": "error", "error": "Unknown command type: search_polyhaven_assets"}, True),
    ({"ok": False, "status": "no_client"}, True),
    ({"status": "success", "result": {"assets": {}}}, False),
    ({"status": "timeout"}, False),
])
def test_addon_unavailable(reply, expected):
    assert api.addon_unavailable(json.dumps(reply)) is expected
    assert api.addon_unavailable("not json") is False


def test_summaries_add_metres():
    out = api.summarize_assets({"brick_wall_001": INFO_BRICK, "kloppenheim_06": INFO_HDRI}, limit=1)
    assert out["total_count"] == 2 and out["returned_count"] == 1
    assert out["assets"]["brick_wall_001"]["dimensions_m"] == [3.0, 3.0]
    info = api.summarize_info("kloppenheim_06", INFO_HDRI)
    assert info["type"] == "hdris" and info["dimensions_m"] is None and info["evs_cap"] == 12


# ---- server component ---------------------------------------------------------

@pytest.fixture
def comp(monkeypatch):
    c = tt.BlenderTextureComponent()
    sent = []

    async def fake_call(ctx, command, params, target_uuid, timeout, bus_id=None):
        sent.append((command, params))
        return json.dumps({"status": "completed", "command": command})

    monkeypatch.setattr(c, "_call", fake_call)
    c.sent = sent
    return c


def test_make_pbr_material_normalizes_tile(comp):
    run(comp.make_pbr_material(texture_id="brick_wall_001", tile_size_m=2, object_name="Wall"))
    command, params = comp.sent[-1]
    assert command == "make_pbr_material"
    assert params["tile_size_m"] == [2.0, 2.0] and params["coordinates"] == "object"


def test_assign_material_passes_rules(comp):
    run(comp.assign_material(object_name="Wall", material="Brick", normal="Side", max_z=0.5))
    params = comp.sent[-1][1]
    assert params["normal"] == "side" and params["max_z"] == 0.5 and params["faces"] is None


def test_set_world_hdri_passes_values(comp):
    run(comp.set_world_hdri(rotation_deg=90, strength=0.5, background_visible=False))
    assert comp.sent[-1] == ("set_world_hdri", {
        "rotation_deg": 90, "strength": 0.5, "background_visible": False, "background_color": None,
    })


@pytest.mark.parametrize("call,detail", [
    (lambda c: c.make_pbr_material(texture_id="x", coordinates="planar"), "coordinates"),
    (lambda c: c.make_pbr_material(texture_id="x", tile_size_m=-1), "positive"),
    (lambda c: c.make_pbr_material(texture_id="x", tile_size_m=[1, 2, 3]), "width, height"),
    (lambda c: c.assign_material(object_name="o", material="m", normal="sideways"), "normal"),
    (lambda c: c.assign_material(object_name="o", material="m", normal=[0, 0, 0]), "not all zero"),
    (lambda c: c.assign_material(object_name="o", material="m", min_z=2, max_z=1), "above"),
    (lambda c: c.set_world_hdri(strength=-1), "strength"),
    (lambda c: c.set_world_hdri(background_color=[1, 1]), "background_color"),
])
def test_bad_arguments_never_reach_blender(comp, call, detail):
    out = json.loads(run(call(comp)))
    assert out["error"] == "invalid_argument" and detail in out["detail"] and comp.sent == []


def test_polyhaven_info_needs_no_blender(comp, monkeypatch):
    async def fake_get(path, params=None):
        assert path == "/info/brick_wall_001"
        return INFO_BRICK

    monkeypatch.setattr(api, "_get", fake_get)
    out = json.loads(run(comp.polyhaven_info(asset_id="brick_wall_001")))
    assert out["source"] == "server" and out["result"]["dimensions_m"] == [3.0, 3.0]
    assert comp.sent == []
