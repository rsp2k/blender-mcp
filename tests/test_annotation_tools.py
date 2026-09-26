"""Annotation write tools: server-side validation, dispatch shape, and stroke geometry."""

import asyncio
import json
import math

import pytest

from addon import annotation_geometry as geo
from blender_mcp import annotation_tools as at


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def comp(monkeypatch):
    c = at.BlenderAnnotationWriteComponent()
    sent = []

    async def fake_dispatch(ctx, command, params, target_uuid, timeout, bus_id):
        sent.append((command, params, target_uuid))
        return json.dumps({"status": "completed", "command": command})

    monkeypatch.setattr(c, "_dispatch_command", fake_dispatch)
    c.sent = sent
    return c


def test_points_3d_and_view_normalized():
    assert at.normalize_points([[0, 0, 0], [1, 2, 3]], "3D") == [[0.0, 0.0, 0.0], [1.0, 2.0, 3.0]]
    assert at.normalize_points([[10, 20], [90, 80]], "view") == [[10.0, 20.0, 0.0], [90.0, 80.0, 0.0]]


@pytest.mark.parametrize("points,space,msg", [
    ([[0, 0, 0]], "3D", "at least 2"),
    ([[0, 0], [1, 1]], "3D", "need [x, y, z]"),
    ([[0, 0, 0], ["a", 1, 2]], "3D", "list of numbers"),
    ([[0, 0, 0], [True, 1, 2]], "3D", "list of numbers"),
    ([[0, 0], [1, 1]], "screen", "space must be"),
])
def test_points_rejected(points, space, msg):
    with pytest.raises(ValueError, match=msg.replace("[", r"\[").replace("]", r"\]")):
        at.normalize_points(points, space)


def test_color_and_thickness_bounds():
    assert at.normalize_color([1, 0.5, 0]) == [1.0, 0.5, 0.0]
    assert at.normalize_color([1, 0, 0, 1]) == [1.0, 0.0, 0.0]
    for bad in ([2, 0, 0], [1, 0], "red"):
        with pytest.raises(ValueError):
            at.normalize_color(bad)
    assert at.normalize_thickness(4) == 4
    for bad in (0, 11, True, "3"):
        with pytest.raises(ValueError):
            at.normalize_thickness(bad)


def test_add_stroke_dispatches_normalized_params(comp):
    run(comp.add_annotation_stroke(points=[[0, 0, 0], [1, 1, 1]], color=[0, 1, 0], thickness=6))
    command, params, _ = comp.sent[-1]
    assert command == "add_annotation_stroke"
    assert params == {"points": [[0.0, 0.0, 0.0], [1.0, 1.0, 1.0]], "layer": "LLM",
                      "color": [0.0, 1.0, 0.0], "thickness": 6, "space": "3D", "cyclic": False}


def test_bad_stroke_never_reaches_blender(comp):
    out = json.loads(run(comp.add_annotation_stroke(points=[[0, 0, 0]])))
    assert out["error"] == "invalid_argument" and comp.sent == []


def test_annotate_object_validates_style_and_padding(comp):
    assert json.loads(run(comp.annotate_object(object="Cube", style="star")))["error"] == "invalid_argument"
    assert json.loads(run(comp.annotate_object(object="Cube", padding=5)))["error"] == "invalid_argument"
    assert comp.sent == []
    run(comp.annotate_object(object="Cube", style="arrow", label="this one"))
    command, params, _ = comp.sent[-1]
    assert command == "annotate_object" and params["style"] == "arrow" and params["label"] == "this one"


def test_clear_defaults_to_llm_layer(comp):
    run(comp.clear_annotations())
    assert comp.sent[-1][:2] == ("clear_annotations", {"layer": "LLM"})


def test_box_is_six_strokes_on_the_bounds():
    strokes = geo.box_strokes((0, 0, 0), (1, 2, 3))
    assert len(strokes) == 6
    pts = {p for s in strokes for p in s}
    assert pts == {(x, y, z) for x in (0, 1) for y in (0, 2) for z in (0, 3)}
    assert strokes[0][0] == strokes[0][-1]  # bottom loop closed


def test_ring_encloses_footprint_at_mid_height():
    (ring,) = geo.ring_stroke((-1, -1, 0), (1, 1, 2))
    assert ring[0] == ring[-1]
    assert all(math.isclose(p[2], 1.0) for p in ring)
    assert all(math.isclose(math.hypot(p[0], p[1]), math.sqrt(2)) for p in ring)


def test_arrow_tip_sits_above_top_center():
    shaft, barb1, barb2 = geo.arrow_strokes((-1, -1, -1), (1, 1, 1))
    tip = shaft[1]
    assert math.isclose(tip[0], 0) and math.isclose(tip[1], 0) and tip[2] > 1
    assert shaft[0][2] > tip[2]  # comes down onto the object
    assert barb1[0] == barb2[0] == tip


def test_padding_grows_flat_objects_too():
    lo, hi = geo.pad_bounds((0, 0, 0), (1, 1, 0), 0.1)
    assert lo[2] < 0 < hi[2]
