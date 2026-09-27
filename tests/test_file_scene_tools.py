"""File/scene tools: startup-default rules, glTF audit rules, interior decisions,
unsaved-work refusal, and server-side validation."""

import asyncio
import json

import pytest

from addon import file_scene_checks as fc
from blender_mcp import file_scene_tools as ft

CUBE = [(x, y, z) for x in (-1.0, 1.0) for y in (-1.0, 1.0) for z in (-1.0, 1.0)]


def cube(**over):
    props = {"name": "Cube", "type": "MESH", "parent": None, "children": [], "modifiers": [],
             "location": (0, 0, 0), "rotation": (0, 0, 0), "scale": (1, 1, 1),
             "materials": ["Material"], "vertices": CUBE, "face_count": 6}
    props.update(over)
    return props


# ---- #16 startup defaults ------------------------------------------------------

def test_untouched_default_cube():
    assert fc.default_object_reasons(cube()) == ("cube", [])


def test_moved_cube_is_not_untouched():
    kind, why = fc.default_object_reasons(cube(location=(3, 0, 0)))
    assert kind == "cube" and "moved" in why


def test_edited_mesh_is_not_untouched():
    _kind, why = fc.default_object_reasons(cube(vertices=CUBE[:-1] + [(1.0, 1.0, 1.5)]))
    assert "mesh edited" in why


def test_name_alone_never_decides():
    # A user's own mesh called Cube that isn't the default shape is kept.
    assert fc.default_object_reasons(cube(face_count=500))[1]
    # A non-mesh called Cube matches no rule at all.
    assert fc.default_object_reasons(cube(type="EMPTY")) == (None, [])


def test_suffixed_names_use_the_same_rule():
    assert fc.default_object_reasons(cube(name="Cube.001"))[0] == "cube"


def test_default_light_and_changed_power():
    light = {"name": "Light", "type": "LIGHT", "location": fc.DEFAULT_LIGHT_LOCATION,
             "scale": (1, 1, 1), "light_type": "POINT", "energy": 1000.0}
    assert fc.default_object_reasons(light) == ("light", [])
    assert "power changed" in fc.default_object_reasons({**light, "energy": 50.0})[1]


def test_default_camera_with_children_is_kept():
    cam = {"name": "Camera", "type": "CAMERA", "location": fc.DEFAULT_CAMERA_LOCATION,
           "scale": (1, 1, 1), "lens": 50.0, "children": ["Rig"]}
    assert "has children" in fc.default_object_reasons(cam)[1]


# ---- #27 glTF audit -------------------------------------------------------------

@pytest.mark.parametrize("info", [
    {"use_nodes": False},
    {"use_nodes": True, "surface": "BSDF_PRINCIPLED"},
    {"use_nodes": True, "surface": "BSDF_PRINCIPLED", "base_color_source": "RGB"},
    {"use_nodes": True, "surface": "BSDF_PRINCIPLED", "base_color_source": "TEX_IMAGE"},
    {"use_nodes": True, "surface": "BSDF_PRINCIPLED", "base_color_source": "MIX",
     "mix_multiply_image": True},
])
def test_exportable_materials_pass(info):
    assert fc.gltf_material_issue(info) is None


def test_diffuse_surface_is_flagged():
    why = fc.gltf_material_issue({"use_nodes": True, "surface": "BSDF_DIFFUSE"})
    assert "empty" in why


@pytest.mark.parametrize("src", ["MIX", "MATH", "HUE_SAT", "VALTORGB"])
def test_node_chains_are_flagged(src):
    why = fc.gltf_material_issue({"use_nodes": True, "surface": "BSDF_PRINCIPLED",
                                  "base_color_source": src})
    assert "won't match" in why


def test_missing_image_is_flagged():
    why = fc.gltf_material_issue({"use_nodes": True, "surface": "BSDF_PRINCIPLED",
                                  "base_color_source": "TEX_IMAGE", "image_missing": True})
    assert "missing" in why


def test_unconnected_output_is_flagged():
    assert fc.gltf_material_issue({"use_nodes": True, "surface": None})


# ---- #17 interior decisions -------------------------------------------------------

@pytest.mark.parametrize("out_in, expected", [
    ((False, True), "keep"),
    ((True, False), "flip"),
    ((True, True), "delete"),
    ((False, False), "keep"),
])
def test_face_action(out_in, expected):
    assert fc.face_action(*out_in) == expected


# ---- #5 unsaved work -----------------------------------------------------------------

def test_dirty_refusal():
    assert fc.dirty_refusal(True, False, "opening another file")
    assert fc.dirty_refusal(True, True, "opening another file") is None
    assert fc.dirty_refusal(False, False, "opening another file") is None


# ---- server-side validation --------------------------------------------------------

def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def comp(monkeypatch):
    c = ft.BlenderFileSceneComponent()
    calls = []

    async def fake_call(ctx, command, params, target_uuid, timeout, bus_id=None):
        calls.append((command, params))
        return json.dumps({"status": "completed", "command": command})

    monkeypatch.setattr(c, "_call", fake_call)
    c.calls = calls
    return c


def test_open_requires_blend_path(comp):
    out = json.loads(run(comp.open_file(path="/tmp/model.obj")))
    assert out["error"] == "invalid_argument" and not comp.calls


def test_open_forwards_discard_flag(comp):
    run(comp.open_file(path="/tmp/a.blend", discard_unsaved=True))
    assert comp.calls == [("open_file", {"path": "/tmp/a.blend", "discard_unsaved": True,
                                         "load_ui": False})]


def test_save_without_path_is_allowed(comp):
    run(comp.save_file())
    assert comp.calls[0] == ("save_file", {"path": None, "copy": False, "compress": False,
                                           "create_dirs": False})


def test_scene_defaults_rejects_unknown_kind(comp):
    out = json.loads(run(comp.scene_defaults(remove=True, kinds=["sun"])))
    assert out["error"] == "invalid_argument" and not comp.calls


def test_remove_interior_rejects_unknown_method(comp):
    out = json.loads(run(comp.remove_interior(object="Walls", method="voxel")))
    assert out["error"] == "invalid_argument"


def test_export_gltf_validates_format_and_wraps_single_object(comp):
    assert json.loads(run(comp.export_gltf(path="/tmp/x", format="fbx")))["error"] == "invalid_argument"
    run(comp.export_gltf(path="/tmp/x.glb", objects="House"))
    assert comp.calls[-1][1]["objects"] == ["House"]
