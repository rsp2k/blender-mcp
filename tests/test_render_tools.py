"""View/render tools: framing maths, projection, and server-side validation."""

import asyncio
import json
import math

import pytest

from addon import render_geometry as geo
from blender_mcp import render_tools as rt


def run(coro):
    return asyncio.run(coro)


CUBE = geo.bounds_corners((-1, -1, -1), (1, 1, 1))


# ---- orthographic camera fit (item 8) ----------------------------------------

@pytest.mark.parametrize("axis", list(geo.AXES))
def test_ortho_fit_cube_is_square_with_margin(axis):
    fit = geo.ortho_camera_fit(CUBE, axis, margin=0.05, long_edge=512)
    assert fit["resolution"] == (512, 512)
    assert fit["ortho_scale"] == pytest.approx(2.0 * 1.1)
    # The camera sits outside the subject, looking at it.
    back = fit["back"]
    loc = fit["location"]
    assert sum(l * b for l, b in zip(loc, back)) > 1.0
    assert fit["clip_start"] > 0 and fit["clip_end"] > fit["clip_start"]


def test_ortho_fit_wide_subject_gets_tight_aspect():
    pts = geo.bounds_corners((0, -0.5, 0), (6, 0.5, 1))
    fit = geo.ortho_camera_fit(pts, "front", margin=0.0, long_edge=600)
    assert fit["resolution"] == (600, 100)  # front view: width along X, height along Z
    assert fit["ortho_scale"] == pytest.approx(6.0)


def test_ortho_fit_fixed_resolution_never_crops():
    pts = geo.bounds_corners((0, 0, 0), (6, 1, 1))
    fit = geo.ortho_camera_fit(pts, "front", margin=0.0, resolution=(100, 100))
    # Square frame: the 6-wide subject sets the scale.
    assert fit["ortho_scale"] == pytest.approx(6.0)
    fit = geo.ortho_camera_fit(pts, "front", margin=0.0, resolution=(1000, 100))
    # 10:1 frame over a 6:1 subject: height limits, visible width = scale.
    assert fit["ortho_scale"] == pytest.approx(10.0)


def test_axes_keep_x_right_where_blender_does():
    for axis in ("front", "top", "bottom"):
        fwd, up = geo.AXES[axis]
        right = geo._cross(fwd, up)
        assert right == pytest.approx((1.0, 0.0, 0.0))
    fwd, up = geo.AXES["right"]
    assert geo._cross(fwd, up) == pytest.approx((0.0, 1.0, 0.0))


def test_ortho_fit_rejects_bad_input():
    with pytest.raises(ValueError):
        geo.ortho_camera_fit(CUBE, "diagonal")
    with pytest.raises(ValueError):
        geo.ortho_camera_fit(CUBE, "front", margin=-1)
    with pytest.raises(ValueError):
        geo.ortho_camera_fit([], "front")


# ---- viewport angles (item 4) ----------------------------------------------

def test_angle_presets_look_the_right_way():
    assert geo.view_direction(*geo.ANGLE_PRESETS["front"]) == pytest.approx((0, 1, 0), abs=1e-9)
    assert geo.view_direction(*geo.ANGLE_PRESETS["right"]) == pytest.approx((-1, 0, 0), abs=1e-9)
    assert geo.view_direction(*geo.ANGLE_PRESETS["top"]) == pytest.approx((0, 0, -1), abs=1e-9)
    s = 1 / math.sqrt(3)
    assert geo.view_direction(*geo.ANGLE_PRESETS["iso"]) == pytest.approx((-s, s, -s), abs=1e-9)


def test_view_euler_front_is_x90():
    assert geo.view_euler(0, 0) == pytest.approx((math.pi / 2, 0, 0))
    assert geo.view_euler(0, 90) == pytest.approx((0, 0, 0))


def test_resolve_angle():
    assert geo.resolve_angle(None) is None
    assert geo.resolve_angle("ISO") == geo.ANGLE_PRESETS["iso"]
    assert geo.resolve_angle([30, 20]) == (30.0, 20.0)
    with pytest.raises(ValueError):
        geo.resolve_angle("sideways")
    with pytest.raises(ValueError):
        geo.resolve_angle([0, 120])


# ---- screen-space bbox for cropping (item 7) --------------------------------

def ortho_matrix(half):
    """Top-down orthographic view of [-half, half]^2: world XY -> NDC."""
    return [[1 / half, 0, 0, 0], [0, 1 / half, 0, 0], [0, 0, -1 / half, 0], [0, 0, 0, 1]]


def test_project_bbox_centred_object():
    box = geo.project_bbox(CUBE, ortho_matrix(4), (800, 800), margin=0.0)
    assert box == (300, 300, 500, 500)


def test_project_bbox_margin_and_clamp():
    box = geo.project_bbox(CUBE, ortho_matrix(4), (800, 800), margin=0.1)
    assert box == (280, 280, 520, 520)
    big = geo.bounds_corners((-10, -10, 0), (10, 10, 1))
    assert geo.project_bbox(big, ortho_matrix(4), (800, 800), margin=0.0) == (0, 0, 800, 800)


def test_project_bbox_ignores_points_behind_view():
    behind = [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, -1]]  # w < 0 for all points
    assert geo.project_bbox(CUBE, behind, (100, 100)) is None


def test_fit_scale():
    assert geo.fit_scale((200, 100), (400, 400), "width") == 2.0
    assert geo.fit_scale((200, 100), (400, 400), "height") == 4.0
    assert geo.fit_scale((200, 100), (400, 400), "contain") == 2.0
    assert geo.fit_scale((200, 100), (400, 400), "none") == 1.0
    with pytest.raises(ValueError):
        geo.fit_scale((1, 1), (1, 1), "stretch")


def test_look_at_basis_is_orthonormal_and_faces_target():
    b = geo.look_at_basis((6, -6, 5), (0, 0, 0))
    for v in b.values():
        assert math.isclose(sum(c * c for c in v), 1.0, rel_tol=1e-9)
    assert abs(geo._dot(b["right"], b["up"])) < 1e-9
    # -back points at the target
    to_target = geo._norm((-6, 6, -5))
    assert tuple(-c for c in b["back"]) == pytest.approx(to_target)
    # Straight down still works (world-up hint degenerate).
    geo.look_at_basis((0, 0, 10), (0, 0, 0))


# ---- server-side validation --------------------------------------------------

def test_server_lists_mirror_addon():
    assert set(rt.AXES) == set(geo.AXES)
    assert set(rt.ANGLE_PRESETS) == set(geo.ANGLE_PRESETS)


@pytest.fixture
def comp(monkeypatch):
    c = rt.BlenderRenderComponent()
    calls = []

    async def fake_call(ctx, command, params, target_uuid, timeout, bus_id=None):
        calls.append((command, params, timeout))
        return json.dumps({"status": "completed"})

    monkeypatch.setattr(c, "_call", fake_call)
    c.calls = calls
    return c


def test_render_view_passes_args(comp):
    run(comp.render_view(axis="Top", objects="Roof", size=800, margin=0.02))
    command, params, timeout = comp.calls[0]
    assert command == "render_view"
    assert params["axis"] == "top" and params["objects"] == ["Roof"] and params["size"] == 800
    assert timeout == rt.TIMEOUT_LONG
    assert "background" not in params


def test_render_view_rejects_bad_axis(comp):
    out = json.loads(run(comp.render_view(axis="diagonal")))
    assert out["error"] == "invalid_argument" and not comp.calls


def test_render_validates_camera_and_engine(comp):
    assert json.loads(run(comp.render(engine="OPENGL")))["error"] == "invalid_argument"
    assert json.loads(run(comp.render(camera_pos=[1, 2])))["error"] == "invalid_argument"
    assert json.loads(run(comp.render(resolution=[0, 10])))["error"] == "invalid_argument"
    run(comp.render(camera_pos=[6, -6, 5], look_at=[0, 0, 1], lens=35, samples=16))
    params = comp.calls[-1][1]
    assert params["camera_pos"] == [6, -6, 5] and params["lens"] == 35 and "filepath" not in params


def test_set_view_validates(comp):
    assert json.loads(run(comp.set_view(angle="sideways")))["error"] == "invalid_argument"
    assert json.loads(run(comp.set_view(perspective="fisheye")))["error"] == "invalid_argument"
    run(comp.set_view(frame=["Cube"], angle="iso", perspective="ortho", shading="SOLID"))
    params = comp.calls[-1][1]
    assert params == {"frame": ["Cube"], "angle": "iso", "all_viewports": False,
                      "perspective": "ORTHO", "shading": "SOLID"}


def test_compare_images_validates(comp):
    assert json.loads(run(comp.compare_images("a.png", "b.png", mode="blend")))["error"] == "invalid_argument"
    assert json.loads(run(comp.compare_images("a.png", "b.png", opacity=2)))["error"] == "invalid_argument"
    run(comp.compare_images("a.png", "b.png", mode="diff", offset=[3, -4]))
    params = comp.calls[-1][1]
    assert params["mode"] == "diff" and params["offset"] == [3, -4]
