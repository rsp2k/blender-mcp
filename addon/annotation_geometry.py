"""Stroke shapes for annotations the addon draws, as plain lists of points.

No bpy or mathutils here so the math can be tested outside Blender.
Every function takes and returns world-space coordinates as 3-tuples,
and returns a list of strokes (each stroke is a list of points).
"""

from __future__ import annotations

import math

Point = tuple[float, float, float]


def bounds_of(points: list[Point]) -> tuple[Point, Point]:
    xs, ys, zs = zip(*points)
    return (min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs))


def pad_bounds(lo: Point, hi: Point, padding: float) -> tuple[Point, Point]:
    """Grow the box by ``padding`` times its largest side on every face
    (at least a small absolute amount, so flat objects still get a box)."""
    size = max(hi[i] - lo[i] for i in range(3))
    pad = max(size * padding, 0.01)
    return (lo[0] - pad, lo[1] - pad, lo[2] - pad), (hi[0] + pad, hi[1] + pad, hi[2] + pad)


def box_strokes(lo: Point, hi: Point) -> list[list[Point]]:
    """Axis-aligned box as six strokes: bottom loop, top loop, four uprights."""
    x0, y0, z0 = lo
    x1, y1, z1 = hi
    bottom = [(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0), (x0, y0, z0)]
    top = [(x, y, z1) for x, y, _ in bottom]
    uprights = [[(x, y, z0), (x, y, z1)] for x, y, _ in bottom[:4]]
    return [bottom, top, *uprights]


def ring_stroke(lo: Point, hi: Point, segments: int = 48) -> list[list[Point]]:
    """Horizontal circle around the box at mid height, enclosing its footprint."""
    cx, cy = (lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2
    cz = (lo[2] + hi[2]) / 2
    r = math.hypot(hi[0] - lo[0], hi[1] - lo[1]) / 2 or 0.1
    pts = [
        (cx + r * math.cos(2 * math.pi * i / segments),
         cy + r * math.sin(2 * math.pi * i / segments), cz)
        for i in range(segments)
    ]
    return [pts + [pts[0]]]


def arrow_strokes(lo: Point, hi: Point) -> list[list[Point]]:
    """Arrow coming down at an angle, tip resting just above the box's top centre."""
    size = max(hi[i] - lo[i] for i in range(3)) or 1.0
    tip = ((lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, hi[2] + 0.05 * size)
    length = 0.8 * size + 0.5
    # Shaft rises up and to one side so it stays readable from most views.
    d = _normalize((1.0, -0.6, 1.4))
    start = (tip[0] + d[0] * length, tip[1] + d[1] * length, tip[2] + d[2] * length)
    head = 0.25 * length
    # Barbs: the shaft direction rotated ±25° around an axis perpendicular to it.
    perp = _normalize(_cross(d, (0.0, 0.0, 1.0)))
    c, s = math.cos(math.radians(25)), math.sin(math.radians(25))
    barbs = []
    for sign in (1, -1):
        v = tuple(d[i] * c + perp[i] * s * sign for i in range(3))
        barbs.append([tip, (tip[0] + v[0] * head, tip[1] + v[1] * head, tip[2] + v[2] * head)])
    return [[start, tip], *barbs]


def _normalize(v):
    n = math.sqrt(sum(c * c for c in v)) or 1.0
    return tuple(c / n for c in v)


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


STYLES = {"box": box_strokes, "circle": ring_stroke, "arrow": arrow_strokes}
