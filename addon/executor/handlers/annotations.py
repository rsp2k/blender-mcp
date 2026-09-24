"""Annotation read handlers.

Two @command entries that let an LLM see what the user has drawn in
the viewport (grease-pencil annotations OR user-created grease-pencil
art — the distinction is stored as a ``space`` field on each stroke
so callers can filter).

- ``list_annotations``: enumerate all strokes across every GP datablock
  with lightweight metadata (bbox, point count, color). Meant for the
  "circle-and-reference" flow: the LLM asks "what's the newest thing
  the user drew?" and gets back a small list without pulling every
  stroke point across the bus.

- ``get_annotation``: full stroke geometry for one id (all points in
  world space where meaningful). The LLM pulls this only for the
  strokes it actually cares about, keeping bandwidth reasonable.

Stroke IDs are stable across calls within a session: they encode
``{gp_source}:{gp_name}:{layer_index}:{frame_number}:{stroke_index}``,
which uniquely names a stroke and stays valid until the user edits
that layer. If the user deletes strokes above it, indices shift and
IDs go stale — callers should re-list rather than re-get after any
known user edit.

Write side (LLM creates annotations for the user) is deferred to a
follow-up. This module is intentionally read-only.
"""

from __future__ import annotations

import bpy
import mathutils

from ..registry import command


def _bbox_from_points(world_points):
    """min/max across a list of world-space Vector3s."""
    if not world_points:
        return None, None
    xs = [p.x for p in world_points]
    ys = [p.y for p in world_points]
    zs = [p.z for p in world_points]
    return [min(xs), min(ys), min(zs)], [max(xs), max(ys), max(zs)]


def _stroke_points_world(stroke, matrix_world):
    """Extract stroke points as world-space [x,y,z] lists.

    Legacy GP (v2) stroke points expose ``.co`` as a Vector3. GPv3
    exposes points differently (via foreach_get on a flat array),
    detected here at runtime rather than by version-check so the
    code survives whatever exact 5.x release the user is on.
    """
    world_points = []
    # v2 path: iterable of points, each with .co
    try:
        for p in stroke.points:
            co = getattr(p, "co", None)
            if co is not None:
                world_points.append(matrix_world @ mathutils.Vector(co[:3]))
    except (AttributeError, TypeError):
        pass
    # v3 path: strokes have a flat position array read via foreach_get
    if not world_points:
        n = getattr(stroke, "points_num", None)
        if n:
            flat = [0.0] * (n * 3)
            try:
                stroke.points.foreach_get("position", flat)
                for i in range(n):
                    v = mathutils.Vector(flat[i * 3 : i * 3 + 3])
                    world_points.append(matrix_world @ v)
            except (AttributeError, RuntimeError):
                pass
    return world_points


def _stroke_color(stroke, layer):
    """Best-effort color extraction, tolerating v2/v3 differences.

    Falls back to the layer's tint/color, then to a sentinel gray if
    nothing is available. Returns [r, g, b] in the 0..1 range or None.
    """
    for attr in ("vertex_color_fill", "color", "start_cap_mode"):
        c = getattr(stroke, attr, None)
        if c and len(c) >= 3:
            return [float(c[0]), float(c[1]), float(c[2])]
    for attr in ("color", "tint_color"):
        c = getattr(layer, attr, None)
        if c and len(c) >= 3:
            return [float(c[0]), float(c[1]), float(c[2])]
    return None


class AnnotationHandlersMixin:
    """Read-only annotation handlers (list + get)."""

    @command("list_annotations")
    def list_annotations(
        self,
        include_bbox: bool = True,
        limit: int = 200,
    ):
        """Enumerate every grease-pencil stroke Blender knows about.

        Blender's annotation tool (D-key + drag in the viewport)
        stores strokes on a special ``Annotations`` grease-pencil
        datablock; user-created GP art lives on other datablocks.
        Both are returned, distinguished by ``gp_source`` and
        ``gp_name`` fields so LLMs can filter (typically to
        ``gp_name == "Annotations"`` for the circle-and-reference
        flow).

        Args:
            include_bbox: True (default) computes world-space bbox
                for each stroke. False skips that cost when you just
                want a stroke inventory.
            limit: max strokes returned (default 200). If more exist
                the response is truncated and ``truncated=True`` is
                set — call get_annotation on IDs of interest, or
                narrow by re-listing after user edits.

        Each entry: {id, gp_source, gp_name, layer, layer_index,
        frame, stroke_index, point_count, color, bbox_min, bbox_max}.
        """
        entries: list[dict] = []
        truncated = False
        for source, gp in self._grease_pencil_datablocks():
            layers = getattr(gp, "layers", None) or []
            for layer_idx, layer in enumerate(layers):
                # Layer transform, if any — for v2 annotations layers,
                # this is typically identity; for v3 it can differ.
                matrix_world = getattr(layer, "matrix_world", None)
                if matrix_world is None:
                    matrix_world = mathutils.Matrix.Identity(4)
                frames = getattr(layer, "frames", None) or []
                for frame in frames:
                    frame_num = getattr(frame, "frame_number", None)
                    strokes = getattr(frame, "strokes", None) or getattr(frame, "drawing", None)
                    if strokes is None:
                        continue
                    try:
                        strokes_iter = list(strokes)
                    except TypeError:
                        continue
                    for stroke_idx, stroke in enumerate(strokes_iter):
                        if len(entries) >= limit:
                            truncated = True
                            break
                        stroke_id = f"{source}:{gp.name}:{layer_idx}:{frame_num}:{stroke_idx}"
                        entry: dict = {
                            "id": stroke_id,
                            "gp_source": source,
                            "gp_name": gp.name,
                            "layer": getattr(layer, "info", None) or getattr(layer, "name", "?"),
                            "layer_index": layer_idx,
                            "frame": frame_num,
                            "stroke_index": stroke_idx,
                            "point_count": len(getattr(stroke, "points", []) or []),
                            "color": _stroke_color(stroke, layer),
                        }
                        if include_bbox:
                            pts = _stroke_points_world(stroke, matrix_world)
                            bmin, bmax = _bbox_from_points(pts)
                            entry["bbox_min"] = bmin
                            entry["bbox_max"] = bmax
                            # Overwrite point_count with the number we
                            # actually extracted, which may differ from
                            # len(stroke.points) on v3.
                            entry["point_count"] = len(pts)
                        entries.append(entry)
                    if truncated:
                        break
                if truncated:
                    break
            if truncated:
                break

        return {
            "annotations": entries,
            "count": len(entries),
            "truncated": truncated,
        }

    @command("get_annotation")
    def get_annotation(self, annotation_id: str):
        """Return full geometry for one stroke by id.

        Args:
            annotation_id: from list_annotations. Format
                ``{v2|v3}:{gp_name}:{layer_idx}:{frame}:{stroke_idx}``.

        Returns {id, gp_name, layer, frame, points: [[x,y,z], ...],
        color, bbox_min, bbox_max}. If the id doesn't resolve (user
        edited the layer since the last list_annotations), returns
        ``{error: "not_found", id: ...}`` — re-list rather than
        retry.
        """
        try:
            source, gp_name, layer_idx_s, frame_s, stroke_idx_s = annotation_id.split(":", 4)
            layer_idx = int(layer_idx_s)
            frame_num = int(frame_s)
            stroke_idx = int(stroke_idx_s)
        except (ValueError, AttributeError):
            return {"error": "bad_id_format", "id": annotation_id}

        gp = None
        for src, candidate in self._grease_pencil_datablocks():
            if src == source and candidate.name == gp_name:
                gp = candidate
                break
        if gp is None:
            return {"error": "not_found", "id": annotation_id, "reason": "gp_data_gone"}

        layers = getattr(gp, "layers", None) or []
        if layer_idx >= len(layers):
            return {"error": "not_found", "id": annotation_id, "reason": "layer_gone"}
        layer = layers[layer_idx]

        frame = None
        for f in getattr(layer, "frames", None) or []:
            if getattr(f, "frame_number", None) == frame_num:
                frame = f
                break
        if frame is None:
            return {"error": "not_found", "id": annotation_id, "reason": "frame_gone"}

        strokes = getattr(frame, "strokes", None) or getattr(frame, "drawing", None)
        try:
            strokes_list = list(strokes) if strokes is not None else []
        except TypeError:
            strokes_list = []
        if stroke_idx >= len(strokes_list):
            return {"error": "not_found", "id": annotation_id, "reason": "stroke_gone"}
        stroke = strokes_list[stroke_idx]

        matrix_world = getattr(layer, "matrix_world", None) or mathutils.Matrix.Identity(4)
        pts = _stroke_points_world(stroke, matrix_world)
        bmin, bmax = _bbox_from_points(pts)
        return {
            "id": annotation_id,
            "gp_source": source,
            "gp_name": gp_name,
            "layer": getattr(layer, "info", None) or getattr(layer, "name", "?"),
            "frame": frame_num,
            "point_count": len(pts),
            "points": [[p.x, p.y, p.z] for p in pts],
            "color": _stroke_color(stroke, layer),
            "bbox_min": bmin,
            "bbox_max": bmax,
        }
