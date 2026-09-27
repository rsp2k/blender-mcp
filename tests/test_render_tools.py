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


# ---- fitting the viewport to a subject (item 7 follow-up) --------------------

REGION = (1574, 954)


def persp_window(fov_y_deg=40.0, aspect=REGION[0] / REGION[1], near=0.01, far=1000.0):
    f = 1.0 / math.tan(math.radians(fov_y_deg) / 2)
    return [[f / aspect, 0, 0, 0], [0, f, 0, 0],
            [0, 0, (far + near) / (near - far), 2 * far * near / (near - far)],
            [0, 0, -1, 0]]


def ortho_window(half_h, dist, aspect=REGION[0] / REGION[1]):
    """Ortho window matrix as Blender builds it at view distance ``dist``."""
    return [[1 / (half_h * aspect), 0, 0, 0], [0, 1 / half_h, 0, 0],
            [0, 0, -0.001, 0], [0, 0, 0, 1]], dist


def rot3_for(yaw, elevation):
    ex, ey, ez = geo.view_euler(yaw, elevation)
    cx, sx, cy, sy, cz, sz = (math.cos(ex), math.sin(ex), math.cos(ey), math.sin(ey),
                              math.cos(ez), math.sin(ez))
    rx = [[1, 0, 0], [0, cx, -sx], [0, sx, cx]]
    ry = [[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]]
    rz = [[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]]

    def mm(a, b):
        return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)] for i in range(3)]
    return mm(rz, mm(ry, rx))


def perspective_matrix(window, rot3, loc, dist):
    """window @ view, the matrix region_3d.perspective_matrix would hold."""
    # view = T(0, 0, -dist) @ R^T @ T(-loc)
    rt_ = [[rot3[c][r] for c in range(3)] for r in range(3)]
    t = [-sum(rt_[r][c] * loc[c] for c in range(3)) for r in range(3)]
    t[2] -= dist
    view = [rt_[0] + [t[0]], rt_[1] + [t[1]], rt_[2] + [t[2]], [0, 0, 0, 1]]
    return [[sum(window[i][k] * view[k][j] for k in range(4)) for j in range(4)] for i in range(4)]


SUBJECTS = {
    "cube": CUBE,
    "tall": geo.bounds_corners((-0.5, -0.5, -5), (0.5, 0.5, 5)),
    "flat": geo.bounds_corners((-6, -3, -0.1), (6, 3, 0.1)),
    "offset": geo.bounds_corners((7, -9, 2), (9, -7, 4)),
    "pair": CUBE + geo.bounds_corners((4, 3, -1), (6, 5, 1)),
    # spread in depth: the case an iterative recentre/zoom loop never settled on
    "far_pair": CUBE + geo.bounds_corners((11, -11, 2), (13, -9, 4)),
}


@pytest.mark.parametrize("name", sorted(SUBJECTS))
@pytest.mark.parametrize("margin", [0.05, 0.1, 0.25])
@pytest.mark.parametrize("view", [(45.0, 30.0), (0.0, 0.0), (120.0, -20.0), (30.0, 80.0)])
def test_fit_view_perspective_keeps_every_corner_inside(name, margin, view):
    pts = SUBJECTS[name]
    window, rot3 = persp_window(), rot3_for(*view)
    fit = geo.fit_view(pts, window, rot3, margin=margin)
    assert fit["contained"]
    x0, y0, x1, y1 = fit["ndc_bounds"]
    assert abs(x0 + x1) < 1e-6 and abs(y0 + y1) < 1e-6  # centred, so frame= alone looks right
    # The crop the screenshot takes must not hit the image edge.
    pm = perspective_matrix(window, rot3, fit["location"], fit["distance"])
    x0, y0, x1, y1 = geo.project_bbox(pts, pm, REGION, margin=margin)
    assert x0 > 0 and y0 > 0 and x1 < REGION[0] and y1 < REGION[1]
    # ...and the subject still fills the frame rather than being a speck.
    fx, fy = (x1 - x0) / REGION[0], (y1 - y0) / REGION[1]
    assert max(fx, fy) > 0.85


def test_fit_view_every_point_in_front_of_the_viewer():
    # a slab far wider than it is deep, seen edge-on: the eye must back off
    # until even the nearest corner is in front of it
    window, rot3 = persp_window(), rot3_for(0.0, 0.0)
    big = geo.bounds_corners((-20, -20, -1), (20, 20, 1))
    fit = geo.fit_view(big, window, rot3, margin=0.05)
    _ndc, behind = geo.project_ndc(big, window, rot3, fit["location"], fit["distance"])
    assert fit["contained"] and behind == 0


def test_fit_view_orbit_point_is_mid_depth():
    window, rot3 = persp_window(), rot3_for(45.0, 30.0)
    pts = SUBJECTS["far_pair"]
    fit = geo.fit_view(pts, window, rot3, margin=0.1)
    back = [rot3[r][2] for r in range(3)]
    depths = [sum(p[i] * back[i] for i in range(3)) for p in pts]
    loc_depth = sum(fit["location"][i] * back[i] for i in range(3))
    assert loc_depth == pytest.approx((min(depths) + max(depths)) / 2)


def test_fit_view_ortho_scales_with_distance():
    window, ref = ortho_window(half_h=1.0, dist=3.0)
    rot3 = rot3_for(0.0, 0.0)  # front view
    flat = geo.bounds_corners((-6, -0.1, -1), (6, 0.1, 1))
    fit = geo.fit_view(flat, window, rot3, margin=0.1, ortho=True, ref_distance=ref)
    assert fit["contained"]
    ndc, _ = geo.project_ndc(flat, window, rot3, fit["location"], fit["distance"],
                             ortho=True, ref_distance=ref)
    half_w = max(abs(p[0]) for p in ndc)
    assert 0.98 / 1.2 - 1e-6 <= half_w <= 1 / 1.2 + 1e-6


def test_fit_view_single_point():
    fit = geo.fit_view([(1.0, 2.0, 3.0)], persp_window(), rot3_for(45, 30))
    assert fit["contained"] and fit["distance"] > 0


def test_fit_view_rejects_empty():
    with pytest.raises(ValueError):
        geo.fit_view([], persp_window(), rot3_for(0, 0))
    with pytest.raises(ValueError):
        geo.fit_view(CUBE, ortho_window(1.0, 3.0)[0], rot3_for(0, 0), ortho=True)


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
