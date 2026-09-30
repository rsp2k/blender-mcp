"""add_primitive / set_color / list_scene_objects / place_object offsets:
colour table, primitive maths, server validation and dispatch shape, catalog."""

import asyncio
import json

import pytest
from fastmcp import FastMCP

from addon import color_names as cn
from addon import primitive_spec as ps
from blender_mcp import dispatch_component as dc
from blender_mcp import modelling_tools as mt
from blender_mcp.chat.catalog import DEFAULT_TOOLS, build_catalog


def run(coro):
    return asyncio.run(coro)


# ---- colour table -----------------------------------------------------------

@pytest.mark.parametrize("name, hex_", [
    ("red", "#ff0000"), ("Red", "#ff0000"), ("  navy ", "#000080"),
    ("dark slate gray", "#2f4f4f"), ("DarkSlateGray", "#2f4f4f"), ("rebecca-purple", "#663399"),
    ("#F00", "#ff0000"), ("c8a2c8", "#c8a2c8"),
])
def test_css_names_and_hex(name, hex_):
    assert cn.resolve(name)["hex"] == hex_


@pytest.mark.parametrize("name", ["warm white", "Warm-White", "off_white", "OffWhite", "charcoal",
                                  "walnut", "oak", "concrete", "quartz", "warm white quartz"])
def test_material_words_are_not_metal(name):
    r = cn.resolve(name)
    assert r["metallic"] == 0.0
    assert r["label"] in cn.MATERIALS


@pytest.mark.parametrize("name", ["gold", "brass", "copper", "chrome", "stainless steel", "aluminum"])
def test_metals_preset_metallic_and_roughness(name):
    r = cn.resolve(name)
    assert r["metallic"] == 1.0 and r["roughness"] < 0.5 and r["preset"]


def test_material_word_beats_css_paint_colour():
    # CSS gold is #ffd700 paint; the word means the metal.
    assert cn.resolve("gold")["metallic"] == 1.0
    assert cn.resolve("silver")["metallic"] == 1.0


def test_explicit_values_override_presets_and_defaults():
    r = cn.resolve("gold", roughness=0.8, metallic=0.2)
    assert (r["roughness"], r["metallic"]) == (0.8, 0.2)
    r = cn.resolve("red")
    assert (r["roughness"], r["metallic"]) == (cn.DEFAULT_ROUGHNESS, cn.DEFAULT_METALLIC)
    assert cn.resolve("concrete")["roughness"] == 0.9


def test_suffixes_are_tolerated():
    assert cn.resolve("walnut wood")["label"] == "walnut"
    assert cn.resolve("red paint")["hex"] == "#ff0000"


def test_numbers_are_srgb_and_converted_to_linear():
    r = cn.resolve([1, 0.5, 0])
    assert r["srgb"] == [1.0, 0.5, 0.0]
    assert r["linear"][0] == 1.0 and r["linear"][2] == 0.0
    assert r["linear"][1] == pytest.approx(0.214, abs=1e-3)
    assert cn.resolve([255, 128, 0])["hex"] == "#ff8000"  # 0..255 accepted
    assert cn.resolve([1, 0, 0, 1])["hex"] == "#ff0000"   # alpha ignored


def test_srgb_linear_round_trip():
    for c in (0.0, 0.02, 0.2, 0.5, 0.9, 1.0):
        assert cn.linear_to_srgb(cn.srgb_to_linear(c)) == pytest.approx(c, abs=1e-6)


@pytest.mark.parametrize("bad, fragment", [
    ("plaid", "unknown colour"), ([1, 0], "color must"), ([-1, 0, 0], "negative"),
    ([300, 0, 0], "0 to 1"), (None, "color must"), ([True, 0, 0], "color must"),
])
def test_bad_colours(bad, fragment):
    with pytest.raises(ValueError, match=fragment):
        cn.resolve(bad)


def test_unknown_name_suggests_close_ones():
    with pytest.raises(ValueError, match="did you mean") as e:
        cn.resolve("walnutt")
    assert "walnut" in str(e.value)


def test_roughness_metallic_bounds():
    with pytest.raises(ValueError, match="roughness"):
        cn.resolve("red", roughness=2)
    with pytest.raises(ValueError, match="metallic"):
        cn.resolve("red", metallic=-0.1)


def test_default_material_names():
    assert cn.material_name(cn.resolve("warm white")) == "Warm White"
    assert cn.material_name(cn.resolve([0.1, 0.2, 0.3])) == "#1a334c"


def test_battery_reads_the_colours_as_named():
    """The battery's colour judge must agree with what set_color writes."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "battery"))
    from checks import color_matches

    assert color_matches(cn.resolve("red")["linear"], "red")
    assert color_matches(cn.resolve("warm white quartz")["linear"], "white")
    assert color_matches(cn.resolve("gold")["linear"], ["yellow", "orange"])
    assert color_matches(cn.resolve("navy")["linear"], "blue")


# ---- primitive maths --------------------------------------------------------

def test_box_bottom_anchor_sits_on_floor():
    p = ps.plan("box", size=[2, 1, 0.9], anchor="bottom", location=[0, 0, 0])
    b = ps.expected_bounds(p["dims"], p["anchor"], p["location"])
    assert b["min"] == [-1.0, -0.5, 0.0] and b["max"] == [1.0, 0.5, 0.9]
    assert p["offset"] == (0.0, 0.0, 0.45)


def test_box_center_anchor_is_centred_on_location():
    b = ps.expected_bounds(ps.extents("box", [2, 1, 0.5]), "center", [3, -1, 1])
    assert b["min"] == [2.0, -1.5, 0.75] and b["max"] == [4.0, -0.5, 1.25]


def test_round_kinds_from_radius_and_depth():
    assert ps.extents("cylinder", radius=0.25, depth=3) == (0.5, 0.5, 3.0)
    assert ps.extents("cone", radius=1, depth=2) == (2.0, 2.0, 2.0)
    assert ps.extents("sphere", radius=0.15) == (0.3, 0.3, 0.3)
    assert ps.extents("sphere", size=[1, 2, 3]) == (1.0, 2.0, 3.0)


def test_defaults_and_cube_number():
    assert ps.extents("box") == (1.0, 1.0, 1.0)
    assert ps.extents("cube", 2) == (2.0, 2.0, 2.0)
    assert ps.extents("cylinder") == (1.0, 1.0, 1.0)
    assert ps.extents("plane") == (1.0, 1.0, 0.0)
    assert ps.extents("plane", [4, 3]) == (4.0, 3.0, 0.0)
    assert ps.extents("plane", [4, 3, 9]) == (4.0, 3.0, 0.0)


def test_plane_bottom_anchor_is_flat_on_location():
    b = ps.expected_bounds(ps.extents("plane", [4, 3]), "bottom", [1, 1, 0])
    assert b["min"][2] == b["max"][2] == 0.0


@pytest.mark.parametrize("kwargs, fragment", [
    ({"kind": "torus"}, "kind must"),
    ({"kind": "box", "radius": 1}, "not radius"),
    ({"kind": "box", "size": [1, 2]}, r"\[x, y, z\]"),
    ({"kind": "box", "size": [1, 0, 1]}, "positive"),
    ({"kind": "cylinder", "size": [1, 1, 1], "radius": 1}, "not both"),
    ({"kind": "sphere", "depth": 1}, "not depth"),
    ({"kind": "box", "anchor": "top"}, "anchor"),
    ({"kind": "box", "segments": 8}, "segments only"),
    ({"kind": "sphere", "segments": 2}, "segments must"),
    ({"kind": "box", "location": [1, 2]}, "location"),
])
def test_plan_rejects(kwargs, fragment):
    with pytest.raises(ValueError, match=fragment):
        ps.plan(**kwargs)


def test_plan_aliases():
    p = ps.plan("Cube", anchor="centre")
    assert p["kind"] == "box" and p["anchor"] == "center" and p["segments"] is None
    assert ps.plan("uv sphere")["segments"] == ps.DEFAULT_SEGMENTS
    assert ps.plan("box", anchor="floor")["anchor"] == "bottom"


def test_fit_scale_skips_flat_axis():
    assert ps.fit_scale([1.0, 1.0, 0.0], [2.0, 4.0, 0.0]) == (2.0, 4.0, 1.0)
    assert ps.fit_scale([0.98, 1.0, 1.0], [1.0, 1.0, 1.0])[0] == pytest.approx(1 / 0.98)


# ---- server tools: validation and dispatch ------------------------------------

@pytest.fixture
def comp(monkeypatch):
    c = mt.BlenderModellingComponent()
    sent = []

    async def fake_send(ctx, command, params, target_uuid, timeout, bus_id):
        sent.append((command, params, target_uuid))
        return json.dumps({"status": "completed", "command": command})

    monkeypatch.setattr(c, "_send", fake_send)
    c.sent = sent
    return c


def test_add_primitive_dispatches_params(comp):
    run(comp.add_primitive(kind="box", name="Crate", size=[2, 1, 0.9], anchor="bottom",
                           location=[0, 0, 0]))
    command, params, _ = comp.sent[-1]
    assert command == "add_primitive"
    assert params == {"kind": "box", "name": "Crate", "size": [2.0, 1.0, 0.9], "radius": None,
                      "depth": None, "location": [0.0, 0.0, 0.0], "anchor": "bottom",
                      "rotation": None, "segments": None, "collection": None, "parent": None}


def test_add_primitive_round_kind(comp):
    run(comp.add_primitive(kind="cylinder", radius=0.25, depth=3, segments=48))
    params = comp.sent[-1][1]
    assert (params["radius"], params["depth"], params["segments"]) == (0.25, 3.0, 48)


@pytest.mark.parametrize("kwargs", [
    {"kind": "pyramid"}, {"kind": "box", "size": -1}, {"kind": "box", "size": [1, 1]},
    {"kind": "box", "radius": 1}, {"kind": "sphere", "radius": 0}, {"kind": "box", "anchor": "top"},
    {"kind": "box", "location": [0, 0]}, {"kind": "box", "segments": 12},
    {"kind": "cylinder", "segments": 1000}, {"kind": "cylinder", "size": 1, "depth": 2},
])
def test_add_primitive_rejects_before_dispatch(comp, kwargs):
    out = json.loads(run(comp.add_primitive(**kwargs)))
    assert out["error"] == "invalid_argument" and comp.sent == []


def test_set_color_dispatches(comp):
    run(comp.set_color(objects="selected", color="warm white quartz"))
    assert comp.sent[-1][:2] == ("set_color", {"objects": "selected", "color": "warm white quartz",
                                               "roughness": None, "metallic": None, "name": None})
    run(comp.set_color(objects=["A", "B"], color=[1, 0, 0], roughness=0.2, name="Red"))
    assert comp.sent[-1][1]["color"] == [1, 0, 0] and comp.sent[-1][1]["objects"] == ["A", "B"]


def test_set_color_glow_sent_only_when_asked(comp):
    run(comp.set_color(objects="Tower 4", color="orange", glow=3))
    assert comp.sent[-1][1]["glow"] == 3.0
    run(comp.set_color(objects="Tower 4", color="orange", glow=0))
    assert comp.sent[-1][1]["glow"] == 0.0  # 0 turns a glow off, so it must be sent


@pytest.mark.parametrize("kwargs", [
    {"objects": "", "color": "red"}, {"objects": [], "color": "red"},
    {"objects": ["A", 3], "color": "red"}, {"objects": "A", "color": ""},
    {"objects": "A", "color": [1, 0]}, {"objects": "A", "color": "red", "roughness": 1.5},
    {"objects": "A", "color": "red", "metallic": "1"},
    {"objects": "A", "color": "red", "glow": -1}, {"objects": "A", "color": "red", "glow": 5000},
    {"objects": "A", "color": "red", "glow": "bright"},
])
def test_set_color_rejects_before_dispatch(comp, kwargs):
    out = json.loads(run(comp.set_color(**kwargs)))
    assert out["error"] == "invalid_argument" and comp.sent == []


def test_list_scene_objects_params(comp):
    run(comp.list_scene_objects())
    assert comp.sent[-1][:2] == ("list_scene_objects", {"type": None, "name_contains": None,
                                                        "collection": None, "limit": 100, "offset": 0})
    run(comp.list_scene_objects(type="mesh", name_contains="box", limit=5, offset=10))
    assert comp.sent[-1][1] == {"type": "MESH", "name_contains": "box", "collection": None,
                                "limit": 5, "offset": 10}
    for bad in ({"limit": 0}, {"limit": 501}, {"offset": -1}):
        assert json.loads(run(comp.list_scene_objects(**bad)))["error"] == "invalid_argument"


# ---- place_object relative moves --------------------------------------------

def test_place_object_absolute_call_shape_unchanged():
    assert dc.placement_params("Crate", [1, 2, 3]) == {
        "object": "Crate", "location": [1.0, 2.0, 3.0], "target": None, "parent": None,
        "frame": "parent", "track_axis": "-Z", "up_axis": "Y", "lens": None}


def test_place_object_offset_only():
    p = dc.placement_params("Lid", offset=[0, 0, 0.5])
    assert p["location"] is None and p["offset"] == [0.0, 0.0, 0.5]
    assert p["parent"] is None  # a relative move never names a parent
    p = dc.placement_params("Lid", rotate_by=[0, 0, 90])
    assert p["rotate_by"] == [0.0, 0.0, 90.0] and "offset" not in p


@pytest.mark.parametrize("kwargs, fragment", [
    ({}, "pass location"), ({"offset": [0, 0]}, "offset"), ({"rotate_by": "90"}, "rotate_by"),
    ({"location": [0, 0, 0], "frame": "local"}, "frame"),
])
def test_place_object_rejects(kwargs, fragment):
    with pytest.raises(ValueError, match=fragment):
        dc.placement_params("Crate", **kwargs)


def test_place_object_tool_validates_before_dispatch(monkeypatch):
    comp = dc.BlenderDispatchComponent()
    sent = []

    async def fake_call(ctx, command, params, target_uuid, timeout, bus_id=None):
        sent.append((command, params))
        return "{}"

    monkeypatch.setattr(comp, "_call", fake_call)
    assert json.loads(run(comp.place_object(object="Lid")))["error"] == "invalid_argument"
    assert sent == []
    run(comp.place_object(object="Lid", offset=[0, 0, 0.5]))
    assert sent[-1] == ("place_object", dc.placement_params("Lid", offset=[0, 0, 0.5]))


# ---- registration and chat catalog ------------------------------------------

NEW = ("add_primitive", "set_color", "list_scene_objects")


def test_tools_register_under_blender_prefix_without_clashing():
    from blender_mcp.object_tools import BlenderObjectStorageComponent

    server = FastMCP("t")
    mt.BlenderModellingComponent().register_tools(mcp_server=server, prefix="blender")
    BlenderObjectStorageComponent().register_tools(mcp_server=server, prefix="blender")
    names = {t.name for t in run(server.list_tools(run_middleware=False))}
    assert {f"blender_{n}" for n in NEW} <= names
    assert "blender_list_objects" in names  # the object-storage tool keeps its name


def test_catalog_offers_the_new_tools_without_approval():
    for n in (*NEW, "place_object"):
        assert n in DEFAULT_TOOLS and not DEFAULT_TOOLS[n].needs_confirm({})

    server = FastMCP("t")
    mt.BlenderModellingComponent().register_tools(mcp_server=server, prefix="blender")
    dc_comp = dc.BlenderDispatchComponent()
    dc_comp.register_tools(mcp_server=server, prefix="blender")
    cat = {e.name: e for e in run(build_catalog(server))}
    assert set(NEW) | {"place_object"} <= set(cat)
    assert "offset" in cat["place_object"].parameters["properties"]
    assert cat["add_primitive"].parameters["required"] == ["kind"]
    assert "target_uuid" not in cat["set_color"].parameters["properties"]
