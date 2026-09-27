"""2D polygon operations run in the server's Python (shapely), not in Blender.

Model-home's gaps-log item 3: footprint work (morphological closing, unions,
area checks) needed shapely, which Blender's bundled Python lacks, so it ran
in a separate shell. These helpers run server-side on polygons the caller
sends and return rings in the same ``{"outer", "holes"}`` shape that
``blender_extrude_polygons`` accepts, so the result can go straight to
Blender. Keeping shapely out of the addon avoids a second numpy build
fighting the one Blender ships, and works on remote buses.
"""

from __future__ import annotations

from shapely import make_valid
from shapely.geometry import MultiPolygon, Polygon
from shapely.geometry.polygon import orient
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

OPERATIONS = ("union", "buffer", "closing", "opening", "simplify", "difference",
              "intersection", "validate", "area")
JOIN_STYLES = {"round": "round", "mitre": "mitre", "miter": "mitre", "bevel": "bevel"}
MAX_POINTS = 500_000


def _ring(points, what: str) -> list[tuple[float, float]]:
    if not isinstance(points, (list, tuple)) or len(points) < 3:
        raise ValueError(f"{what} must be a list of at least 3 [x, y] points")
    out = []
    for p in points:
        if not isinstance(p, (list, tuple)) or len(p) < 2:
            raise ValueError(f"{what} has a point that isn't [x, y]: {p!r}")
        out.append((float(p[0]), float(p[1])))
    return out


def to_polygon(spec, index: int = 0) -> Polygon:
    """One polygon from ``{"outer": ring, "holes": [ring, ...]}`` or a bare ring."""
    if isinstance(spec, list):
        spec = {"outer": spec}
    if not isinstance(spec, dict) or "outer" not in spec:
        raise ValueError(f"polygon {index}: expected {{'outer': [...], 'holes': [...]}} or a ring")
    holes = spec.get("holes") or []
    return Polygon(_ring(spec["outer"], f"polygon {index} outer ring"),
                   [_ring(h, f"polygon {index} hole {j}") for j, h in enumerate(holes)])


def to_geometry(polygons) -> BaseGeometry:
    if not isinstance(polygons, list) or not polygons:
        raise ValueError("polygons must be a non-empty list")
    total = sum(len(p.get("outer", p) if isinstance(p, dict) else p) for p in polygons)
    if total > MAX_POINTS:
        raise ValueError(f"too many points ({total}; max {MAX_POINTS})")
    polys = [to_polygon(p, i) for i, p in enumerate(polygons)]
    return MultiPolygon(polys) if len(polys) > 1 else polys[0]


def _polygons_of(geom: BaseGeometry) -> list[Polygon]:
    if geom.is_empty:
        return []
    if isinstance(geom, Polygon):
        return [geom]
    if isinstance(geom, MultiPolygon):
        return list(geom.geoms)
    # GeometryCollection etc. (e.g. from make_valid): keep only areas.
    out = []
    for g in getattr(geom, "geoms", []):
        out.extend(_polygons_of(g))
    return out


def from_geometry(geom: BaseGeometry, ndigits: int = 9) -> list[dict]:
    """Rings with the closing point dropped, outer CCW and holes CW."""
    out = []
    for poly in _polygons_of(geom):
        poly = Polygon(poly.exterior.coords, [h.coords for h in poly.interiors])
        poly = orient(poly, sign=1.0)
        outer = [[round(x, ndigits), round(y, ndigits)] for x, y in list(poly.exterior.coords)[:-1]]
        holes = [[[round(x, ndigits), round(y, ndigits)] for x, y in list(h.coords)[:-1]]
                 for h in poly.interiors]
        out.append({"outer": outer, "holes": holes, "area": round(poly.area, ndigits)})
    return out


def run(operation: str, polygons, *, distance: float | None = None,
        tolerance: float | None = None, join_style: str = "mitre",
        mitre_limit: float = 5.0, other=None, resolution: int = 16) -> dict:
    op = str(operation or "").strip().lower()
    if op not in OPERATIONS:
        raise ValueError(f"operation must be one of {', '.join(OPERATIONS)}, got {operation!r}")
    js = JOIN_STYLES.get(str(join_style).lower())
    if js is None:
        raise ValueError(f"join_style must be one of {', '.join(sorted(set(JOIN_STYLES)))}")
    geom = to_geometry(polygons)
    input_area = geom.area
    invalid_input = not geom.is_valid
    if invalid_input or op == "validate":
        geom = make_valid(geom)

    def need_distance():
        if distance is None or float(distance) <= 0:
            raise ValueError(f"{op} needs a positive distance")
        return float(distance)

    buf = dict(join_style=js, mitre_limit=mitre_limit, quad_segs=resolution)
    if op in ("union", "validate", "area"):
        result = unary_union(_polygons_of(geom))
    elif op == "buffer":
        if distance is None:
            raise ValueError("buffer needs a distance (negative shrinks)")
        result = unary_union(_polygons_of(geom)).buffer(float(distance), **buf)
    elif op == "closing":
        d = need_distance()
        result = unary_union(_polygons_of(geom)).buffer(d, **buf).buffer(-d, **buf)
    elif op == "opening":
        d = need_distance()
        result = unary_union(_polygons_of(geom)).buffer(-d, **buf).buffer(d, **buf)
    elif op == "simplify":
        if tolerance is None or float(tolerance) <= 0:
            raise ValueError("simplify needs a positive tolerance")
        result = unary_union(_polygons_of(geom)).simplify(float(tolerance), preserve_topology=True)
    else:  # difference / intersection
        if other is None:
            raise ValueError(f"{op} needs 'other' polygons")
        b = to_geometry(other)
        if not b.is_valid:
            b = make_valid(b)
        a = unary_union(_polygons_of(geom))
        result = a.difference(b) if op == "difference" else a.intersection(b)

    polys = from_geometry(result)
    out = {
        "operation": op,
        "input_area": round(input_area, 9),
        "area": round(result.area, 9),
        "polygon_count": len(polys),
        "hole_count": sum(len(p["holes"]) for p in polys),
        "input_valid": not invalid_input,
    }
    if op != "area":
        out["polygons"] = polys
    if invalid_input:
        out["note"] = "input was not a valid polygon set (self-intersections?); repaired with make_valid first"
    return out
