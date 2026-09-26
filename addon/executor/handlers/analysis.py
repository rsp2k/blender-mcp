"""Assembly analysis and keyframe helpers.

Covers what a mechanical-assembly session otherwise hand-writes through
execute_code every time (feedback fx-5ZRSYENMByk): real triangle
interference between parts (bounding boxes overlap far more often than
parts intersect), world-space bounds, and keyframes under Blender 5.x
slotted Actions, where ``action.fcurves`` no longer exists.
"""

from __future__ import annotations

import re
from contextlib import contextmanager

import bpy
import mathutils
from mathutils.bvhtree import BVHTree

from ..registry import command

_GEOMETRY_TYPES = {"MESH", "CURVE", "SURFACE", "META", "FONT", "CURVES", "POINTCLOUD", "VOLUME"}
_MESHABLE_TYPES = {"MESH", "CURVE", "SURFACE", "META", "FONT"}
_MAX_INTERFERENCE_OBJECTS = 300
_MAX_SWEEP_FRAMES = 50
_KEY_FRAME_EPS = 1e-4
_CUSTOM_PROP = re.compile(r'^\["(.+)"\]$')


def _r(v, nd: int = 6) -> list[float]:
    return [round(float(x), nd) for x in v]


def _get_object(name: str):
    obj = bpy.data.objects.get(name)
    if obj is None:
        raise ValueError(f"Object not found: {name!r}")
    return obj


def _resolve_objects(objects, include_hidden: bool, types: set[str]):
    """Objects to analyze: a list of names, or all (visible) geometry."""
    if objects in (None, "visible", "all"):
        found = [
            o for o in bpy.context.scene.objects
            if o.type in types and (include_hidden or objects == "all" or o.visible_get())
        ]
        return found, []
    if isinstance(objects, str):
        objects = [objects]
    picked, skipped = [], []
    for name in objects:
        obj = _get_object(name)
        (picked if obj.type in types else skipped).append(obj)
    return picked, [{"object": o.name, "reason": f"type {o.type} has no surface"} for o in skipped]


@contextmanager
def _at_frame(frame):
    """Evaluate at ``frame``, then put the scene back where it was."""
    scene = bpy.context.scene
    if frame is None:
        yield scene.frame_current
        return
    original = (scene.frame_current, scene.frame_subframe)
    scene.frame_set(int(frame))
    try:
        yield int(frame)
    finally:
        scene.frame_set(original[0], subframe=original[1])


def _world_vertices(obj, depsgraph):
    """Evaluated vertex positions in world space, plus polygon index lists.
    None when the object can't produce a mesh."""
    ev = obj.evaluated_get(depsgraph)
    try:
        me = ev.to_mesh()
    except RuntimeError:
        return None
    if me is None:
        return None
    try:
        mw = ev.matrix_world
        verts = [mw @ v.co for v in me.vertices]
        polys = [tuple(p.vertices) for p in me.polygons]
    finally:
        ev.to_mesh_clear()
    return verts, polys


def _aabb(points):
    xs = [p.x for p in points]; ys = [p.y for p in points]; zs = [p.z for p in points]
    return mathutils.Vector((min(xs), min(ys), min(zs))), mathutils.Vector((max(xs), max(ys), max(zs)))


def _boxes_overlap(a, b, pad: float) -> bool:
    (amin, amax), (bmin, bmax) = a, b
    return all(amin[i] - pad <= bmax[i] and bmin[i] - pad <= amax[i] for i in range(3))


def _point_inside(bvh: BVHTree, point) -> bool:
    """Inside a closed, outward-facing surface: the nearest surface point's
    normal faces away from ``point``."""
    hit = bvh.find_nearest(point)
    if hit is None or hit[0] is None:
        return False
    location, normal = hit[0], hit[1]
    return (point - location).dot(normal) < 0


def _animation_fcurves(id_block):
    """F-curves of the action assigned to ``id_block`` and which API holds them.

    Blender 4.4+ layered Actions keep curves in a channelbag per slot
    (layers → strips → channelbags); 5.x removed ``action.fcurves``.
    """
    anim = getattr(id_block, "animation_data", None)
    if anim is None or anim.action is None:
        return [], None, None
    action = anim.action
    try:
        from bpy_extras import anim_utils
        getter = getattr(anim_utils, "animdata_get_channelbag_for_assigned_slot", None)
    except ImportError:
        getter = None
    if getter is not None and getattr(action, "layers", None) is not None:
        cbag = getter(anim)
        slot = getattr(anim, "action_slot", None)
        return (list(cbag.fcurves) if cbag else []), action, (slot.identifier if slot else None)
    return list(getattr(action, "fcurves", [])), action, None


def _id_for(obj, owner: str):
    if owner == "object":
        return obj
    if owner == "data":
        if obj.data is None:
            raise ValueError(f"{obj.name!r} has no data block")
        return obj.data
    if owner == "shape_keys":
        keys = getattr(obj.data, "shape_keys", None)
        if keys is None:
            raise ValueError(f"{obj.name!r} has no shape keys")
        return keys
    raise ValueError("owner must be 'object', 'data' or 'shape_keys'")


def _interpolation_items() -> list[str]:
    return [i.identifier for i in bpy.types.Keyframe.bl_rna.properties["interpolation"].enum_items]


def _key_at(fcurve, frame: float):
    for kp in fcurve.keyframe_points:
        if abs(kp.co.x - frame) < _KEY_FRAME_EPS:
            return kp
    return None


def _set_path_value(id_block, data_path: str, value, index: int):
    """Assign ``value`` to ``data_path`` (or one component when index >= 0)."""
    m = _CUSTOM_PROP.match(data_path)
    if m:
        id_block[m.group(1)] = value
        return
    if "." in data_path:
        base, attr = data_path.rsplit(".", 1)
        target = id_block.path_resolve(base)
    else:
        target, attr = id_block, data_path
    if index >= 0:
        getattr(target, attr)[index] = value
    else:
        setattr(target, attr, value)


class AnalysisHandlersMixin:
    """world_bounds, check_interference, get_keyframes, insert_keyframe, set_interpolation."""

    @command("world_bounds")
    def world_bounds(self, objects=None, frame=None, include_hidden: bool = False, exact: bool = False):
        """World-space axis-aligned bounds per object and their union.

        ``exact`` uses evaluated vertices (tight, slower on dense meshes);
        otherwise the evaluated local bound box corners are transformed,
        which never under-reports but can over-report for rotated parts.
        """
        picked, skipped = _resolve_objects(objects, include_hidden, _GEOMETRY_TYPES)
        rows = []
        with _at_frame(frame) as at:
            dg = bpy.context.evaluated_depsgraph_get()
            for obj in picked:
                method = "bound_box"
                points = None
                if exact and obj.type in _MESHABLE_TYPES:
                    got = _world_vertices(obj, dg)
                    if got and got[0]:
                        points, method = got[0], "vertices"
                if points is None:
                    ev = obj.evaluated_get(dg)
                    points = [ev.matrix_world @ mathutils.Vector(c) for c in ev.bound_box]
                lo, hi = _aabb(points)
                rows.append({
                    "object": obj.name, "min": _r(lo), "max": _r(hi),
                    "size": _r(hi - lo), "center": _r((lo + hi) / 2), "method": method,
                })
        union = None
        if rows:
            lo = mathutils.Vector([min(r["min"][i] for r in rows) for i in range(3)])
            hi = mathutils.Vector([max(r["max"][i] for r in rows) for i in range(3)])
            union = {"min": _r(lo), "max": _r(hi), "size": _r(hi - lo)}
        return {"frame": at, "objects": rows, "union": union, "skipped": skipped}

    @command("check_interference")
    def check_interference(
        self,
        objects=None,
        frame=None,
        frames=None,
        ignore_pairs=None,
        include_hidden: bool = False,
        tolerance: float = 0.0,
        check_containment: bool = True,
        sample_points: int = 3,
    ):
        """Which parts really intersect, not just overlap in bounding box.

        Pairs whose world bounds overlap are tested for triangle
        intersection (BVH overlap on evaluated, world-space meshes). A part
        entirely inside another crosses no triangles, so those pairs are
        also tested for containment (needs closed meshes with outward
        normals). ``frames`` sweeps several frames of a keyframed scene.
        """
        picked, skipped = _resolve_objects(objects, include_hidden, _MESHABLE_TYPES)
        if len(picked) > _MAX_INTERFERENCE_OBJECTS:
            raise ValueError(
                f"{len(picked)} objects is more than {_MAX_INTERFERENCE_OBJECTS}; pass a list, "
                "or run the check in a background worker"
            )
        ignore = {frozenset(p) for p in (ignore_pairs or []) if len(p) == 2}
        sweep = list(frames) if frames else [frame]
        if len(sweep) > _MAX_SWEEP_FRAMES:
            raise ValueError(f"at most {_MAX_SWEEP_FRAMES} frames per sweep")
        results = [
            self._interference_at(picked, f, ignore, float(tolerance), check_containment, int(sample_points))
            for f in sweep
        ]
        if frames:
            return {"frames": results, "skipped": skipped,
                    "objects_checked": len(picked)}
        out = results[0]
        out["skipped"] = skipped
        return out

    def _interference_at(self, picked, frame, ignore, tolerance, check_containment, sample_points):
        with _at_frame(frame) as at:
            dg = bpy.context.evaluated_depsgraph_get()
            geo = {}
            for obj in picked:
                got = _world_vertices(obj, dg)
                if not got or not got[0] or not got[1]:
                    continue
                verts, polys = got
                geo[obj.name] = {
                    "verts": verts, "polys": polys, "box": _aabb(verts),
                    "bvh": BVHTree.FromPolygons(verts, polys, epsilon=tolerance),
                }
        names = sorted(geo)
        box_pairs = 0
        intersecting, contained = [], []
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                if frozenset((a, b)) in ignore:
                    continue
                ga, gb = geo[a], geo[b]
                if not _boxes_overlap(ga["box"], gb["box"], tolerance):
                    continue
                box_pairs += 1
                hits = ga["bvh"].overlap(gb["bvh"])
                if hits:
                    samples = []
                    for pa, _pb in hits[:max(0, sample_points)]:
                        poly = ga["polys"][pa]
                        centroid = sum((ga["verts"][v] for v in poly), mathutils.Vector()) / len(poly)
                        samples.append(_r(centroid, 4))
                    intersecting.append({"a": a, "b": b, "triangle_pairs": len(hits),
                                         "sample_points": samples})
                elif check_containment:
                    if _point_inside(gb["bvh"], ga["verts"][0]):
                        contained.append({"inner": a, "outer": b})
                    elif _point_inside(ga["bvh"], gb["verts"][0]):
                        contained.append({"inner": b, "outer": a})
        return {
            "frame": at,
            "objects_checked": len(names),
            "bbox_overlap_pairs": box_pairs,
            "intersecting": intersecting,
            "contained": contained,
        }

    @command("get_keyframes")
    def get_keyframes(self, object: str, data_path: str | None = None, owner: str = "object"):
        """Keyframes of the action assigned to an object (or its data / shape keys).

        Works with Blender 5.x slotted Actions and older legacy Actions alike.
        """
        obj = _get_object(object)
        fcurves, action, slot = _animation_fcurves(_id_for(obj, owner))
        channels = []
        for fc in fcurves:
            if data_path and fc.data_path != data_path:
                continue
            channels.append({
                "data_path": fc.data_path,
                "index": fc.array_index,
                "group": fc.group.name if fc.group else None,
                "keyframes": [
                    {"frame": round(kp.co.x, 4), "value": round(kp.co.y, 6),
                     "interpolation": kp.interpolation}
                    for kp in fc.keyframe_points
                ],
            })
        return {
            "object": obj.name, "owner": owner,
            "action": action.name if action else None, "slot": slot,
            "api": "slotted" if slot else ("legacy" if action else None),
            "channels": channels,
        }

    @command("insert_keyframe")
    def insert_keyframe(
        self,
        object: str,
        data_path: str,
        frame: float,
        value=None,
        index: int = -1,
        overwrite: bool = False,
        interpolation: str | None = None,
        owner: str = "object",
    ):
        """Key a property at a frame, safely.

        Order is fixed to frame → value → key: setting a value and then
        changing frame re-evaluates the animation and silently discards
        the value. An existing key on the same channel and frame is never
        replaced unless ``overwrite`` is true, and replaced values are
        reported.
        """
        obj = _get_object(object)
        id_block = _id_for(obj, owner)
        try:
            current = id_block.path_resolve(data_path)
        except ValueError as exc:
            raise ValueError(f"{data_path!r} doesn't resolve on {id_block.name!r}: {exc}") from exc
        if interpolation and interpolation not in _interpolation_items():
            raise ValueError(f"interpolation must be one of {_interpolation_items()}")

        indices = [index] if index >= 0 else (
            list(range(len(current))) if hasattr(current, "__len__") and not isinstance(current, str) else [0]
        )
        fcurves, _action, _slot = _animation_fcurves(id_block)
        existing = []
        for fc in fcurves:
            if fc.data_path == data_path and fc.array_index in indices:
                kp = _key_at(fc, float(frame))
                if kp is not None:
                    existing.append({"index": fc.array_index, "value": round(kp.co.y, 6)})
        if existing and not overwrite:
            raise ValueError(
                f"{data_path} already has a key at frame {frame} ({existing}); "
                "pass overwrite=true to replace it"
            )

        scene = bpy.context.scene
        original = (scene.frame_current, scene.frame_subframe)
        try:
            scene.frame_set(int(frame))
            if value is not None:
                _set_path_value(id_block, data_path, value, index)
            if not id_block.keyframe_insert(data_path=data_path, index=index, frame=float(frame)):
                raise ValueError(f"Blender refused to key {data_path!r} (is it animatable?)")
            inserted = []
            for fc in _animation_fcurves(id_block)[0]:
                if fc.data_path == data_path and fc.array_index in indices:
                    kp = _key_at(fc, float(frame))
                    if kp is not None:
                        if interpolation:
                            kp.interpolation = interpolation
                        inserted.append({"index": fc.array_index, "value": round(kp.co.y, 6),
                                         "interpolation": kp.interpolation})
        finally:
            scene.frame_set(original[0], subframe=original[1])
        return {"object": obj.name, "data_path": data_path, "frame": frame,
                "inserted": inserted, "replaced": existing}

    @command("set_interpolation")
    def set_interpolation(
        self,
        object: str,
        interpolation: str,
        data_path: str | None = None,
        frames=None,
        owner: str = "object",
    ):
        """Set interpolation on keys of an object's action, optionally only
        one channel and/or specific frames."""
        if interpolation not in _interpolation_items():
            raise ValueError(f"interpolation must be one of {_interpolation_items()}")
        obj = _get_object(object)
        wanted = [float(f) for f in frames] if frames else None
        changed = 0
        for fc in _animation_fcurves(_id_for(obj, owner))[0]:
            if data_path and fc.data_path != data_path:
                continue
            for kp in fc.keyframe_points:
                if wanted is None or any(abs(kp.co.x - f) < _KEY_FRAME_EPS for f in wanted):
                    kp.interpolation = interpolation
                    changed += 1
        return {"object": obj.name, "interpolation": interpolation, "keys_changed": changed}
