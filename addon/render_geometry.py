"""View, framing and projection maths for the view/render tools.

Kept free of bpy/mathutils so it can be unit-tested outside Blender. Vectors
are plain 3-tuples; matrices are row-major nested sequences.
"""

from __future__ import annotations

import math

# Orthographic elevation axes: (forward = direction the camera looks,
# up = camera up). Screen right is forward x up, which keeps +X to the right
# for front/top/bottom, as Blender's own numpad views do.
AXES: dict[str, tuple[tuple[float, float, float], tuple[float, float, float]]] = {
    "front": ((0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
    "back": ((0.0, -1.0, 0.0), (0.0, 0.0, 1.0)),
    "right": ((-1.0, 0.0, 0.0), (0.0, 0.0, 1.0)),
    "left": ((1.0, 0.0, 0.0), (0.0, 0.0, 1.0)),
    "top": ((0.0, 0.0, -1.0), (0.0, 1.0, 0.0)),
    "bottom": ((0.0, 0.0, 1.0), (0.0, -1.0, 0.0)),
}

# Viewport angle presets as (yaw, elevation) in degrees. yaw 0 looks from -Y
# (front), positive yaw orbits counter-clockwise seen from above (90 = from
# +X, the right view). elevation is degrees above the horizon.
ANGLE_PRESETS: dict[str, tuple[float, float]] = {
    "front": (0.0, 0.0),
    "back": (180.0, 0.0),
    "right": (90.0, 0.0),
    "left": (-90.0, 0.0),
    "top": (0.0, 90.0),
    "bottom": (0.0, -90.0),
    # Classic isometric from front-right-top: elevation atan(1/sqrt(2)).
    "iso": (45.0, math.degrees(math.atan(1 / math.sqrt(2)))),
}


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a, b):
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _scale(a, s):
    return (a[0] * s, a[1] * s, a[2] * s)


def _add(*vs):
    return (sum(v[0] for v in vs), sum(v[1] for v in vs), sum(v[2] for v in vs))


def _norm(a):
    length = math.sqrt(_dot(a, a))
    if length < 1e-12:
        raise ValueError("zero-length vector")
    return _scale(a, 1.0 / length)


def resolve_angle(angle) -> tuple[float, float] | None:
    """Accept a preset name, [yaw, elevation] degrees, or None (keep current)."""
    if angle is None:
        return None
    if isinstance(angle, str):
        key = angle.strip().lower()
        if key not in ANGLE_PRESETS:
            raise ValueError(
                f"angle must be one of {', '.join(ANGLE_PRESETS)} or [yaw, elevation], got {angle!r}"
            )
        return ANGLE_PRESETS[key]
    try:
        yaw, elevation = (float(v) for v in angle)
    except (TypeError, ValueError):
        raise ValueError(f"angle must be a preset name or [yaw, elevation] degrees, got {angle!r}")
    if not -90.0 <= elevation <= 90.0:
        raise ValueError("elevation must be between -90 and 90 degrees")
    return yaw, elevation


def view_euler(yaw_deg: float, elevation_deg: float) -> tuple[float, float, float]:
    """XYZ Euler (radians) for a 3D viewport's view_rotation.

    The view looks along its local -Z. Rotating X by (90 - elevation) tilts it
    from straight down to the horizon; rotating Z by yaw orbits it around.
    """
    return (math.radians(90.0 - elevation_deg), 0.0, math.radians(yaw_deg))


def view_direction(yaw_deg: float, elevation_deg: float) -> tuple[float, float, float]:
    """Unit vector the view looks along, for the same yaw/elevation."""
    yaw, el = math.radians(yaw_deg), math.radians(elevation_deg)
    # Horizontal component starts at +Y (front view looks along +Y) and
    # rotates counter-clockwise by yaw; elevation tilts it downward.
    horiz = math.cos(el)
    return (-math.sin(yaw) * horiz, math.cos(yaw) * horiz, -math.sin(el))


def bounds_corners(bmin, bmax) -> list[tuple[float, float, float]]:
    return [
        (x, y, z)
        for x in (bmin[0], bmax[0])
        for y in (bmin[1], bmax[1])
        for z in (bmin[2], bmax[2])
    ]


def union_bounds(point_sets) -> tuple[tuple, tuple]:
    pts = [p for ps in point_sets for p in ps]
    if not pts:
        raise ValueError("no points to bound")
    bmin = tuple(min(p[i] for p in pts) for i in range(3))
    bmax = tuple(max(p[i] for p in pts) for i in range(3))
    return bmin, bmax


def ortho_camera_fit(points, axis: str, margin: float = 0.05,
                     resolution: tuple[int, int] | None = None,
                     long_edge: int = 1024) -> dict:
    """Place an orthographic camera that frames ``points`` tightly along ``axis``.

    Returns the camera location, its basis (right, up, back), ortho_scale,
    clip range and the render resolution. With no ``resolution`` the image
    takes the subject's aspect ratio, ``long_edge`` pixels on its longer side,
    so the PNG is tight. ``margin`` is a fraction of the subject's size added
    on every side.

    Blender's ortho_scale spans the larger render dimension (sensor fit
    AUTO), which is why the scale depends on the aspect.
    """
    key = str(axis).strip().lower()
    if key not in AXES:
        raise ValueError(f"axis must be one of {', '.join(AXES)}, got {axis!r}")
    if margin < 0:
        raise ValueError("margin must be >= 0")
    forward, up = AXES[key]
    right = _cross(forward, up)
    pts = list(points)
    if not pts:
        raise ValueError("no points to frame")

    rs = [_dot(p, right) for p in pts]
    us = [_dot(p, up) for p in pts]
    fs = [_dot(p, forward) for p in pts]
    width = max(rs) - min(rs)
    height = max(us) - min(us)
    depth = max(fs) - min(fs)
    # A flat subject seen edge-on still needs a non-zero frame.
    span = max(width, height, depth, 1e-6)
    width = max(width, span * 1e-3)
    height = max(height, span * 1e-3)
    pad = 1.0 + 2.0 * margin
    need_w, need_h = width * pad, height * pad

    if resolution is None:
        if need_w >= need_h:
            res_x, res_y = long_edge, max(1, round(long_edge * need_h / need_w))
        else:
            res_x, res_y = max(1, round(long_edge * need_w / need_h)), long_edge
    else:
        res_x, res_y = int(resolution[0]), int(resolution[1])
        if res_x <= 0 or res_y <= 0:
            raise ValueError("resolution must be positive")
    aspect = res_x / res_y
    if res_x >= res_y:
        ortho_scale = max(need_w, need_h * aspect)
    else:
        ortho_scale = max(need_h, need_w / aspect)

    center = _add(
        _scale(right, (max(rs) + min(rs)) / 2),
        _scale(up, (max(us) + min(us)) / 2),
        _scale(forward, (max(fs) + min(fs)) / 2),
    )
    standoff = depth / 2 + max(span * 0.5, 0.1)
    location = _sub(center, _scale(forward, standoff))
    return {
        "location": location,
        "right": right,
        "up": up,
        "back": _scale(forward, -1.0),  # camera local +Z
        "ortho_scale": ortho_scale,
        "clip_start": max(standoff - depth / 2 - span * 0.25, 1e-4),
        "clip_end": standoff + depth / 2 + span * 0.25,
        "resolution": (res_x, res_y),
        "subject_size": (width, height, depth),
    }


def look_at_basis(location, target, world_up=(0.0, 0.0, 1.0)) -> dict:
    """Camera basis (right, up, back) for a camera at ``location`` aimed at ``target``."""
    forward = _norm(_sub(target, location))
    up_hint = world_up if abs(_dot(forward, _norm(world_up))) < 0.999 else (0.0, 1.0, 0.0)
    right = _norm(_cross(forward, up_hint))
    up = _cross(right, forward)
    return {"right": right, "up": up, "back": _scale(forward, -1.0)}


def project_bbox(points, perspective_matrix, region_size, margin: float = 0.05):
    """Pixel bbox (x0, y0, x1, y1) of ``points`` in a region, or None.

    ``perspective_matrix`` is region_3d.perspective_matrix (window @ view),
    row-major. Pixel coordinates have y=0 at the bottom, like Blender's
    region and image pixel buffers. Points behind the view are ignored; the
    box is padded by ``margin`` of its size and clamped to the region.
    """
    width, height = region_size
    xs, ys = [], []
    for p in points:
        v = (p[0], p[1], p[2], 1.0)
        clip = [sum(perspective_matrix[r][c] * v[c] for c in range(4)) for r in range(4)]
        w = clip[3]
        if w <= 1e-9:
            continue
        ndc_x, ndc_y = clip[0] / w, clip[1] / w
        xs.append((ndc_x + 1.0) * 0.5 * width)
        ys.append((ndc_y + 1.0) * 0.5 * height)
    if not xs:
        return None
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    pad_x = (x1 - x0) * margin
    pad_y = (y1 - y0) * margin
    x0 = max(0, math.floor(x0 - pad_x))
    y0 = max(0, math.floor(y0 - pad_y))
    x1 = min(width, math.ceil(x1 + pad_x))
    y1 = min(height, math.ceil(y1 + pad_y))
    if x1 - x0 < 1 or y1 - y0 < 1:
        return None
    return (x0, y0, x1, y1)


def view_space(point, rot3, location, distance):
    """World point -> 3D-view space for a viewport orbiting ``location``.

    Mirrors Blender's view matrix: viewinv = T(location) @ R @ T(0, 0, distance),
    where ``rot3`` is view_rotation as a row-major 3x3 (view -> world). The
    viewer sits ``distance`` along +Z of view space and looks down -Z.
    """
    d = _sub(point, location)
    # R^T @ d
    x = rot3[0][0] * d[0] + rot3[1][0] * d[1] + rot3[2][0] * d[2]
    y = rot3[0][1] * d[0] + rot3[1][1] * d[1] + rot3[2][1] * d[2]
    z = rot3[0][2] * d[0] + rot3[1][2] * d[1] + rot3[2][2] * d[2]
    return (x, y, z - distance)


def project_ndc(points, window_matrix, rot3, location, distance,
                ortho: bool = False, ref_distance: float | None = None):
    """Project world points to normalised device coords through a viewport.

    Returns (ndc_points, n_behind). ``window_matrix`` is region_3d.window_matrix
    (row-major). For an orthographic view that matrix scales with the view
    distance it was built at (``ref_distance``), so NDC is rescaled by
    ref_distance / distance; a perspective window matrix doesn't depend on it.
    """
    scale = 1.0
    if ortho and ref_distance and distance > 0:
        scale = ref_distance / distance
    out, behind = [], 0
    W = window_matrix
    for p in points:
        v = view_space(p, rot3, location, distance)
        v4 = (v[0], v[1], v[2], 1.0)
        clip = [sum(W[r][c] * v4[c] for c in range(4)) for r in range(4)]
        w = clip[3]
        if w <= 1e-9:
            behind += 1
            continue
        out.append((clip[0] / w * scale, clip[1] / w * scale))
    return out, behind


def fit_view(points, window_matrix, rot3, margin: float = 0.05,
             ortho: bool = False, ref_distance: float | None = None, fill: float = 0.98):
    """Place a viewport so every point projects inside it with ``margin``.

    ``view3d.view_selected`` sizes the view from the largest bounding-box side,
    not the projected silhouette, so a box seen corner-on (the default
    viewport angle) overflows the region, most at the near corner. This keeps
    the view rotation and solves for the eye position directly: in view axes
    each point gives the linear constraint ``|x - ex| * W00 <= a * (ez - z)``
    (and the same for y), where ``a`` is the NDC half-size the subject may
    use after ``margin`` (a fraction of its own size per side, as the
    screenshot crop pads it). Making the extreme constraints tight gives ex,
    ey and ez in closed form; the larger ez of the two axes wins, which only
    adds slack on the other. The orbit point lands at the subject's middle
    depth. For ORTHO, ``ref_distance`` is the view distance the window matrix
    was built at, since that matrix scales with distance.

    Returns a dict with location, distance, ndc bounds (x0, y0, x1, y1) and
    ``contained`` (every point inside with the margin, checked by projection).
    """
    if not points:
        raise ValueError("no points to frame")
    a = fill / (1.0 + 2.0 * max(0.0, margin))
    w00, w11 = window_matrix[0][0], window_matrix[1][1]
    if not w00 or not w11:
        raise ValueError("window matrix has no scale")
    # Points in view axes (columns of rot3 are the view's right, up, back).
    axes = [[rot3[r][c] for r in range(3)] for c in range(3)]
    vp = [tuple(_dot(p, ax) for ax in axes) for p in points]
    xs, ys, zs = zip(*vp)
    zc = (min(zs) + max(zs)) / 2.0

    if ortho:
        if not ref_distance:
            raise ValueError("ortho fit needs ref_distance")
        ref = ref_distance
        ex, ey = (min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0
        # NDC x = W00 * ref / dist * (x - ex); keep it within a.
        dist = max(
            (max(xs) - min(xs)) / 2.0 * w00 * ref / a,
            (max(ys) - min(ys)) / 2.0 * w11 * ref / a,
            1e-4,
        )
    else:
        ref = None

        def solve(coord, w):
            t = a / w  # half-width of the allowed frustum per unit depth
            hi = max(c + t * z for c, z in zip(coord, zs))
            lo = max(-c + t * z for c, z in zip(coord, zs))
            return (hi - lo) / 2.0, (hi + lo) / (2.0 * t)

        def balance(coord, ez):
            # At the chosen depth, centre the looser axis: the offset where the
            # widest ray on each side is equal minimises the larger of the two,
            # so it can only tighten the fit solve() found.
            lo, hi = min(coord), max(coord)
            for _ in range(60):
                mid = (lo + hi) / 2.0
                right = max((c - mid) / (ez - z) for c, z in zip(coord, zs))
                left = max((mid - c) / (ez - z) for c, z in zip(coord, zs))
                lo, hi = (mid, hi) if right > left else (lo, mid)
            return (lo + hi) / 2.0

        ex, ez_x = solve(xs, w00)
        ey, ez_y = solve(ys, w11)
        ez = max(ez_x, ez_y, max(zs) + 1e-6)  # a lone point would put the eye on it
        ex, ey = balance(xs, ez), balance(ys, ez)
        dist = max(ez - zc, 1e-4)

    loc = tuple(ex * axes[0][i] + ey * axes[1][i] + zc * axes[2][i] for i in range(3))
    ndc, behind = project_ndc(points, window_matrix, rot3, loc, dist, ortho, ref)
    box = None
    if ndc and not behind:
        box = (min(p[0] for p in ndc), min(p[1] for p in ndc),
               max(p[0] for p in ndc), max(p[1] for p in ndc))
    contained = False
    if box is not None:
        x0, y0, x1, y1 = box
        px, py = (x1 - x0) * margin, (y1 - y0) * margin
        contained = (x0 - px >= -1.0 and x1 + px <= 1.0
                     and y0 - py >= -1.0 and y1 + py <= 1.0)
    return {"location": loc, "distance": dist, "ndc_bounds": box, "contained": contained}


def fit_scale(src_size, dst_size, fit: str) -> float:
    """Scale factor to fit ``src`` onto ``dst`` by width, height, or not at all."""
    fit = (fit or "none").lower()
    if fit == "width":
        return dst_size[0] / src_size[0]
    if fit == "height":
        return dst_size[1] / src_size[1]
    if fit == "contain":
        return min(dst_size[0] / src_size[0], dst_size[1] / src_size[1])
    if fit == "none":
        return 1.0
    raise ValueError("fit must be width, height, contain or none")
