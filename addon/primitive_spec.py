"""Argument maths for add_primitive, with no bpy dependency.

Turns kind/size/radius/depth into full extents, and anchor/location into
where the geometry sits relative to the object's origin, so the handler
only has to build a unit shape with bmesh and fit it to these numbers.

The object's origin is the anchor point: anchor="center" puts the origin at
the middle of the shape, anchor="bottom" at the centre of its bottom face,
so an object standing on the floor at (x, y) is anchor="bottom",
location=[x, y, 0], and raising it later moves that point.
"""

from __future__ import annotations

KINDS = {
    "box": "box", "cube": "box", "cuboid": "box",
    "cylinder": "cylinder",
    "cone": "cone",
    "sphere": "sphere", "uv_sphere": "sphere", "uvsphere": "sphere", "ball": "sphere",
    "plane": "plane", "rectangle": "plane",
}
ROUND = ("cylinder", "cone", "sphere")
ANCHORS = ("center", "bottom")
DEFAULT_SEGMENTS = 32
MIN_SEGMENTS, MAX_SEGMENTS = 3, 256
DEFAULT_EXTENT = 1.0


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _positive(v, what: str) -> float:
    if not _num(v) or v <= 0:
        raise ValueError(f"{what} must be a positive number (metres)")
    return float(v)


def normalize_kind(kind) -> str:
    k = str(kind or "").strip().lower().replace(" ", "_").replace("-", "_")
    if k not in KINDS:
        raise ValueError(f"kind must be one of box, cylinder, cone, sphere, plane (got {kind!r})")
    return KINDS[k]


def normalize_anchor(anchor) -> str:
    a = str(anchor or "center").strip().lower()
    a = {"centre": "center", "middle": "center", "base": "bottom", "floor": "bottom"}.get(a, a)
    if a not in ANCHORS:
        raise ValueError("anchor must be 'center' (location is the middle) or 'bottom' "
                         "(location is the centre of the bottom face)")
    return a


def vec3(value, what: str, default=(0.0, 0.0, 0.0)) -> tuple[float, float, float]:
    if value is None:
        return tuple(float(v) for v in default)
    if not isinstance(value, (list, tuple)) or len(value) != 3 or not all(_num(v) for v in value):
        raise ValueError(f"{what} must be [x, y, z] numbers")
    return tuple(float(v) for v in value)


def extents(kind: str, size=None, radius=None, depth=None) -> tuple[float, float, float]:
    """Full [x, y, z] extents of the finished shape.

    ``size``: full extents, a number (a cube / equal extents) or [x, y, z]
    ([x, y] for a plane). Round kinds may instead take ``radius`` (and
    ``depth`` = height for cylinder and cone). A plane is flat: z is 0.
    """
    kind = normalize_kind(kind)
    if size is not None and (radius is not None or depth is not None):
        raise ValueError("pass either size (full extents) or radius/depth, not both")
    if kind in ("box", "plane") and radius is not None:
        raise ValueError(f"a {kind} takes size, not radius")
    if kind in ("box", "plane", "sphere") and depth is not None and size is None:
        raise ValueError(f"a {kind} takes {'size' if kind != 'sphere' else 'radius or size'}, "
                         "not depth")

    if size is not None:
        if _num(size):
            s = _positive(size, "size")
            dims = [s, s, s]
        elif isinstance(size, (list, tuple)) and len(size) in (2, 3):
            if kind != "plane" and len(size) != 3:
                raise ValueError("size must be [x, y, z] full extents (or one number)")
            dims = [_positive(v, "each size component") if i < 2 or kind != "plane" else 0.0
                    for i, v in enumerate(size)]
            if len(dims) == 2:
                dims.append(0.0)
        else:
            raise ValueError("size must be a number or [x, y, z] full extents in metres")
        if kind == "plane":
            dims[2] = 0.0
        return tuple(dims)

    if kind == "box":
        return (DEFAULT_EXTENT,) * 3
    if kind == "plane":
        return (DEFAULT_EXTENT, DEFAULT_EXTENT, 0.0)
    r = _positive(radius, "radius") if radius is not None else DEFAULT_EXTENT / 2
    if kind == "sphere":
        return (2 * r, 2 * r, 2 * r)
    d = _positive(depth, "depth") if depth is not None else DEFAULT_EXTENT
    return (2 * r, 2 * r, d)


def segments_for(kind: str, segments=None) -> int | None:
    if kind not in ROUND:
        if segments is not None:
            raise ValueError(f"segments only applies to cylinder, cone and sphere, not {kind}")
        return None
    if segments is None:
        return DEFAULT_SEGMENTS
    if not isinstance(segments, int) or isinstance(segments, bool) or not (
            MIN_SEGMENTS <= segments <= MAX_SEGMENTS):
        raise ValueError(f"segments must be a whole number from {MIN_SEGMENTS} to {MAX_SEGMENTS}")
    return segments


def geometry_offset(dims, anchor: str) -> tuple[float, float, float]:
    """Shift from a shape centred on its origin to one whose origin is the anchor."""
    return (0.0, 0.0, dims[2] / 2.0) if normalize_anchor(anchor) == "bottom" else (0.0, 0.0, 0.0)


def fit_scale(actual, target) -> tuple[float, float, float]:
    """Per-axis factors taking measured extents to the requested ones (1 for a flat axis)."""
    return tuple((t / a) if a > 1e-12 and t > 0 else 1.0 for a, t in zip(actual, target))


def expected_bounds(dims, anchor: str, location) -> dict:
    """World bounds of an unrotated, unparented primitive."""
    loc = vec3(location, "location")
    off = geometry_offset(dims, anchor)
    centre = [loc[i] + off[i] for i in range(3)]
    mn = [centre[i] - dims[i] / 2.0 for i in range(3)]
    mx = [centre[i] + dims[i] / 2.0 for i in range(3)]
    return {"min": mn, "max": mx, "size": list(dims), "center": centre}


def plan(kind, size=None, radius=None, depth=None, anchor="center", location=None,
         rotation=None, segments=None) -> dict:
    """Validated arguments for the handler (raises ValueError)."""
    k = normalize_kind(kind)
    a = normalize_anchor(anchor)
    dims = extents(k, size, radius, depth)
    return {
        "kind": k,
        "anchor": a,
        "dims": dims,
        "segments": segments_for(k, segments),
        "location": vec3(location, "location"),
        "rotation": vec3(rotation, "rotation"),  # degrees, XYZ Euler
        "offset": geometry_offset(dims, a),
    }
