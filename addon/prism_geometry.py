"""Pure geometry for extruding 2D polygons (outer ring + holes) into prisms.

No bpy here, so it can be tested outside Blender. The caller supplies a
tessellator that triangulates a polygon with holes; inside Blender that is
``mathutils.geometry.tessellate_polygon``.

Conventions: the outer ring is made counter-clockwise and holes clockwise
(seen from +Z), so with the face orders below every face normal points out
of the solid. A prism without holes gets single n-gon caps; with holes the
caps are triangulated, because a Blender face can't contain a hole.
"""

from __future__ import annotations

from typing import Callable, Sequence

Point2 = tuple[float, float]
Tessellator = Callable[[list[list[tuple[float, float, float]]]], list[tuple[int, int, int]]]

_EPS = 1e-12


def signed_area(ring: Sequence[Point2]) -> float:
    """Shoelace area: positive for counter-clockwise rings."""
    a = 0.0
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        a += x1 * y2 - x2 * y1
    return a / 2.0


def clean_ring(points, *, what: str = "ring") -> list[Point2]:
    """Coerce to float pairs, drop consecutive duplicates and a closing point
    that repeats the first. Raises ValueError on anything that can't form a
    polygon."""
    if not isinstance(points, (list, tuple)):
        raise ValueError(f"{what} must be a list of [x, y] points")
    out: list[Point2] = []
    for p in points:
        if not isinstance(p, (list, tuple)) or len(p) < 2:
            raise ValueError(f"{what} has a point that isn't [x, y]: {p!r}")
        try:
            q = (float(p[0]), float(p[1]))
        except (TypeError, ValueError):
            raise ValueError(f"{what} has a non-numeric point: {p!r}") from None
        if out and abs(q[0] - out[-1][0]) <= _EPS and abs(q[1] - out[-1][1]) <= _EPS:
            continue
        out.append(q)
    if len(out) > 1 and abs(out[0][0] - out[-1][0]) <= _EPS and abs(out[0][1] - out[-1][1]) <= _EPS:
        out.pop()
    if len(out) < 3:
        raise ValueError(f"{what} needs at least 3 distinct points, got {len(out)}")
    if abs(signed_area(out)) <= _EPS:
        raise ValueError(f"{what} has zero area")
    return out


def oriented(ring: list[Point2], ccw: bool) -> list[Point2]:
    is_ccw = signed_area(ring) > 0
    return ring if is_ccw == ccw else list(reversed(ring))


def transform_ring(ring: list[Point2], origin: Point2, scale: float) -> list[Point2]:
    ox, oy = origin
    return [((x - ox) * scale, (y - oy) * scale) for x, y in ring]


def build_prism(
    outer: list[Point2],
    holes: list[list[Point2]],
    z0: float,
    z1: float,
    tessellate: Tessellator,
) -> tuple[list[tuple[float, float, float]], list[tuple[int, ...]]]:
    """Vertices and faces of a closed prism between z0 and z1 (z1 > z0).

    ``outer``/``holes`` must already be cleaned. Vertex layout: all ring
    points at z0 (outer first, then each hole), then the same points at z1.
    """
    if z1 <= z0:
        raise ValueError("height must be positive")
    outer = oriented(outer, ccw=True)
    holes = [oriented(h, ccw=False) for h in holes]
    rings = [outer] + holes
    flat: list[Point2] = [p for r in rings for p in r]
    n = len(flat)
    verts = [(x, y, z0) for x, y in flat] + [(x, y, z1) for x, y in flat]

    faces: list[tuple[int, ...]] = []
    if not holes:
        # Single n-gon caps: bottom reversed so its normal points down.
        faces.append(tuple(reversed(range(n))))
        faces.append(tuple(i + n for i in range(n)))
    else:
        polys = [[(x, y, 0.0) for x, y in r] for r in rings]
        tris = tessellate(polys)
        if not tris:
            raise ValueError("could not triangulate the polygon (self-intersecting or degenerate?)")
        for a, b, c in tris:
            # Orient each cap triangle by its own winding so the result
            # doesn't depend on the tessellator's convention.
            (ax, ay), (bx, by), (cx, cy) = flat[a], flat[b], flat[c]
            if (bx - ax) * (cy - ay) - (by - ay) * (cx - ax) < 0:
                b, c = c, b
            faces.append((a, c, b))              # bottom, facing -Z
            faces.append((a + n, b + n, c + n))  # top, facing +Z

    start = 0
    for r in rings:
        m = len(r)
        for i in range(m):
            a = start + i
            b = start + (i + 1) % m
            faces.append((a, b, b + n, a + n))
        start += m
    return verts, faces


def edge_face_counts(faces: list[tuple[int, ...]]) -> dict[tuple[int, int], int]:
    """How many faces use each undirected edge (2 everywhere = closed manifold)."""
    counts: dict[tuple[int, int], int] = {}
    for f in faces:
        for i in range(len(f)):
            a, b = f[i], f[(i + 1) % len(f)]
            key = (a, b) if a < b else (b, a)
            counts[key] = counts.get(key, 0) + 1
    return counts


def parse_polygon_spec(spec, index: int) -> dict:
    """Normalise one polygon entry: ``{"outer": [...], "holes": [[...]],
    "height"?, "base_z"?, "name"?}`` or a bare ring ``[[x, y], ...]``."""
    if isinstance(spec, list):
        spec = {"outer": spec}
    if not isinstance(spec, dict):
        raise ValueError(f"polygon {index}: expected an object with 'outer' or a list of points")
    if "outer" not in spec:
        raise ValueError(f"polygon {index}: missing 'outer'")
    out = {"outer": clean_ring(spec["outer"], what=f"polygon {index} outer ring")}
    holes = spec.get("holes") or []
    if not isinstance(holes, list):
        raise ValueError(f"polygon {index}: 'holes' must be a list of rings")
    out["holes"] = [clean_ring(h, what=f"polygon {index} hole {j}") for j, h in enumerate(holes)]
    for key in ("height", "base_z"):
        if spec.get(key) is not None:
            try:
                out[key] = float(spec[key])
            except (TypeError, ValueError):
                raise ValueError(f"polygon {index}: {key} must be a number") from None
    if spec.get("name") is not None:
        out["name"] = str(spec["name"])
    return out
