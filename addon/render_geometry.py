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
