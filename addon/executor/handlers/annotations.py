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


def _extract_stroke_points(stroke):
    """Extract stroke points as raw [x,y,z] Vectors in whatever space
    Blender stored them in — NOT world-transformed here.

    Legacy GP (v2) stroke points expose ``.co``. GPv3 exposes points
    via foreach_get on a flat array. Runtime-detected rather than
    version-branched so the code survives whichever 5.x release.
    """
    raw = []
    try:
        for p in stroke.points:
            co = getattr(p, "co", None)
            if co is not None:
                raw.append(mathutils.Vector(co[:3]))
    except (AttributeError, TypeError):
        pass
    if not raw:
        n = getattr(stroke, "points_num", None)
        if n:
            flat = [0.0] * (n * 3)
            try:
                stroke.points.foreach_get("position", flat)
                for i in range(n):
                    raw.append(mathutils.Vector(flat[i * 3 : i * 3 + 3]))
            except (AttributeError, RuntimeError):
                pass
    return raw


def _detect_stroke_space(stroke, layer, raw_points):
    """Return the coordinate space Blender stored this stroke in.

    Blender annotation strokes carry a ``display_mode`` (or similar)
    attribute with values like ``SCREEN`` / ``3DSPACE`` / ``2DSPACE`` /
    ``2DIMAGE`` — this is the authoritative source. Fall back to a
    Z-variance heuristic if the attribute doesn't exist on that stroke
    shape.

    Returns one of: ``"3D"``, ``"2D-View"``, ``"2D-Image"``, ``"2D-Local"``,
    ``"unknown"``. Callers should ONLY treat matrix_world @ point as
    meaningful when space == "3D".
    """
    for obj in (stroke, layer):
        for attr in ("display_mode", "annotation_placement", "placement"):
            val = getattr(obj, attr, None)
            if val is None:
                continue
            token = str(val).upper()
            if "3D" in token or "SURFACE" in token or "CURSOR" in token or "STROKE" in token:
                return "3D"
            if "SCREEN" in token or token == "VIEW":
                return "2D-View"
            if "IMAGE" in token:
                return "2D-Image"
            if "2D" in token or "LOCAL" in token:
                return "2D-Local"
    # Heuristic fallback: if every point's Z equals the first's, this is
    # a flat stroke — very likely screen/view space. Real 3D annotations
    # have depth variance because the user's viewpoint changes with the
    # pen's cursor position when drawing.
    if not raw_points:
        return "unknown"
    z0 = raw_points[0].z
    if all(abs(p.z - z0) < 1e-6 for p in raw_points):
        return "2D-View"
    return "3D"


def _bbox_from_vectors(vecs):
    """min/max across a list of Vector3s. Returns (None, None) if empty."""
    if not vecs:
        return None, None
    xs = [v.x for v in vecs]
    ys = [v.y for v in vecs]
    zs = [v.z for v in vecs]
    return [min(xs), min(ys), min(zs)], [max(xs), max(ys), max(zs)]


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
                        raw = _extract_stroke_points(stroke)
                        space = _detect_stroke_space(stroke, layer, raw)
                        entry: dict = {
                            "id": stroke_id,
                            "gp_source": source,
                            "gp_name": gp.name,
                            "layer": getattr(layer, "info", None) or getattr(layer, "name", "?"),
                            "layer_index": layer_idx,
                            "frame": frame_num,
                            "stroke_index": stroke_idx,
                            "point_count": len(raw),
                            "color": _stroke_color(stroke, layer),
                            "space": space,
                        }
                        if include_bbox:
                            # World-space bbox is only meaningful for
                            # 3D strokes. For screen/view-space strokes,
                            # applying matrix_world gives nonsense; leave
                            # bbox null and let the LLM know via `space`.
                            if space == "3D":
                                world = [matrix_world @ v for v in raw]
                                bmin, bmax = _bbox_from_vectors(world)
                            else:
                                bmin = bmax = None
                            entry["bbox_min"] = bmin
                            entry["bbox_max"] = bmax
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
        raw = _extract_stroke_points(stroke)
        space = _detect_stroke_space(stroke, layer, raw)

        # World coords + bbox only make sense for 3D strokes; for
        # screen/view/image space the matrix_world transform is
        # meaningless (points are in view-plane units, not scene
        # units) so we return the raw coords with an explicit space
        # tag and let the LLM decide whether to raycast, unproject,
        # or bail out.
        if space == "3D":
            world = [matrix_world @ v for v in raw]
            world_points = [[w.x, w.y, w.z] for w in world]
            bmin, bmax = _bbox_from_vectors(world)
        else:
            world_points = None
            bmin = bmax = None

        return {
            "id": annotation_id,
            "gp_source": source,
            "gp_name": gp_name,
            "layer": getattr(layer, "info", None) or getattr(layer, "name", "?"),
            "frame": frame_num,
            "space": space,
            "point_count": len(raw),
            # raw_points are always safe (in whatever space Blender
            # stored them); world_points are ONLY populated for
            # space == "3D" where the transform is meaningful.
            "raw_points": [[p.x, p.y, p.z] for p in raw],
            "world_points": world_points,
            "color": _stroke_color(stroke, layer),
            "bbox_min": bmin,
            "bbox_max": bmax,
            "hint": (
                None if space == "3D"
                else "Stroke was drawn in " + space + " space; raw_points are "
                     "in view-plane coordinates, not world. To associate with "
                     "scene objects, use a raycast through the viewport at each "
                     "point (bpy.ops.view3d.select or scene.ray_cast) from the "
                     "user's active viewport region."
            ),
        }
