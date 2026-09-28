"""Paint annotation strokes onto a viewport capture.

render.opengl leaves annotations out of its image, so get_viewport_screenshot
draws them back on itself. No bpy here: the handler collects strokes and the
view matrix, and this module projects and rasterizes them into an RGBA array
(rows bottom-up, as Blender stores pixels).
"""

from __future__ import annotations

import math
from itertools import pairwise

NEAR_W = 1e-6  # clip-space w below this is at or behind the eye


def _clip(p, m):
    v = (p[0], p[1], p[2], 1.0)
    return [sum(m[r][c] * v[c] for c in range(4)) for r in range(4)]


def _to_pixel(clip, size):
    w = clip[3]
    return ((clip[0] / w + 1.0) * 0.5 * size[0], (clip[1] / w + 1.0) * 0.5 * size[1])


def project_polyline(points, perspective_matrix, size):
    """World-space polyline -> pixel polylines.

    Segments crossing behind the eye are cut at the near plane, so a stroke
    that passes behind the viewer splits into pieces instead of wrapping
    around the screen.
    """
    clips = [_clip(p, perspective_matrix) for p in points]
    runs, current = [], []
    for i, c in enumerate(clips):
        if c[3] > NEAR_W:
            if not current and i > 0 and clips[i - 1][3] <= NEAR_W:
                current.append(_to_pixel(_cut(clips[i - 1], c), size))
            current.append(_to_pixel(c, size))
        else:
            if current:
                current.append(_to_pixel(_cut(clips[i - 1], c), size))
                runs.append(current)
                current = []
    if current:
        runs.append(current)
    return [r for r in runs if len(r) >= 2]


def _cut(a, b):
    """Point on segment a-b (clip space) where w == NEAR_W."""
    t = (NEAR_W - a[3]) / (b[3] - a[3])
    return [a[k] + (b[k] - a[k]) * t for k in range(4)]


def view_polyline(points, size):
    """View-placed stroke (x, y as 0-100 % of the region) -> one pixel polyline."""
    return [[(p[0] / 100.0 * size[0], p[1] / 100.0 * size[1]) for p in points]]


def paint_polylines(arr, polylines, color, thickness, opacity=1.0):
    """Draw pixel polylines into ``arr`` (h, w, 4) in place. Returns pixels hit."""
    import numpy as np

    h, w = arr.shape[:2]
    radius = max(0.5, float(thickness) / 2.0)
    r = math.ceil(radius)
    oy, ox = np.mgrid[-r:r + 1, -r:r + 1]
    disk = (ox * ox + oy * oy) <= radius * radius + 0.25
    offsets = np.stack([ox[disk], oy[disk]], axis=1)  # (K, 2)

    centers = []
    for line in polylines:
        for (x0, y0), (x1, y1) in pairwise(line):
            # Skip segments nowhere near the image; a projected point far
            # outside would otherwise produce millions of samples.
            if (max(x0, x1) < -r or min(x0, x1) > w + r
                    or max(y0, y1) < -r or min(y0, y1) > h + r):
                continue
            x0, y0, x1, y1 = _clip_to_box(x0, y0, x1, y1, -r, -r, w + r, h + r)
            if x0 is None:
                continue
            n = max(1, int(math.hypot(x1 - x0, y1 - y0) / max(radius * 0.5, 0.5)))
            t = np.linspace(0.0, 1.0, n + 1)
            centers.append(np.stack([x0 + (x1 - x0) * t, y0 + (y1 - y0) * t], axis=1))
    if not centers:
        return 0
    c = np.rint(np.concatenate(centers)).astype(np.int64)  # (N, 2)
    pts = (c[:, None, :] + offsets[None, :, :]).reshape(-1, 2)
    keep = (pts[:, 0] >= 0) & (pts[:, 0] < w) & (pts[:, 1] >= 0) & (pts[:, 1] < h)
    pts = np.unique(pts[keep], axis=0)
    if not len(pts):
        return 0
    xs, ys = pts[:, 0], pts[:, 1]
    a = max(0.0, min(1.0, float(opacity)))
    rgb = np.asarray(color[:3], dtype=arr.dtype)
    arr[ys, xs, :3] = arr[ys, xs, :3] * (1.0 - a) + rgb * a
    if arr.shape[2] > 3:
        arr[ys, xs, 3] = np.maximum(arr[ys, xs, 3], a)
    return len(pts)


def _clip_to_box(x0, y0, x1, y1, xmin, ymin, xmax, ymax):
    """Liang-Barsky: the part of a segment inside a box, or (None,)*4."""
    dx, dy = x1 - x0, y1 - y0
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, x0 - xmin), (dx, xmax - x0), (-dy, y0 - ymin), (dy, ymax - y0)):
        if p == 0:
            if q < 0:
                return None, None, None, None
            continue
        t = q / p
        if p < 0:
            t0 = max(t0, t)
        else:
            t1 = min(t1, t)
        if t0 > t1:
            return None, None, None, None
    return x0 + dx * t0, y0 + dy * t0, x0 + dx * t1, y0 + dy * t1
