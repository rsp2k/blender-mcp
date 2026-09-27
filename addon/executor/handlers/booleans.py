"""Boolean operations that report what they did, and a mesh-health query.

Exact booleans fail silently in several ways (model-home's feedback items 6, 9,
15, 17, 20, 21, 25 and 26): a DIFFERENCE that does nothing, a UNION that drops
a whole operand, and unions of faces sharing a plane that leave non-manifold
edges which only show up when a later boolean goes wrong. The boolean tool
measures the target before and after every operand, refuses unsafe inputs
unless forced, and warns about coincident faces; the verdicts live in
``addon/boolean_checks.py`` so they can be tested without Blender.
"""

from __future__ import annotations

import bmesh
import bpy
from mathutils.bvhtree import BVHTree

from ... import boolean_checks as checks
from ..registry import command

_ZERO_AREA = 1e-12
_MAX_SHELL_FACES = 500_000
_MAX_COPLANAR_FACES = 20_000
_PARALLEL_DOT = 0.9999


def _get_mesh_object(name: str):
    obj = bpy.data.objects.get(name)
    if obj is None:
        raise ValueError(f"Object not found: {name!r}")
    if obj.type != "MESH":
        raise ValueError(f"{name!r} is a {obj.type}, not a mesh")
    return obj


def _bmesh_world(obj, evaluated: bool) -> bmesh.types.BMesh:
    """A world-space bmesh copy of the object's mesh. Never touches the object."""
    bm = bmesh.new()
    if evaluated:
        depsgraph = bpy.context.evaluated_depsgraph_get()
        ev = obj.evaluated_get(depsgraph)
        me = ev.to_mesh()
        try:
            bm.from_mesh(me)
        finally:
            ev.to_mesh_clear()
    else:
        bm.from_mesh(obj.data)
    bm.transform(obj.matrix_world)
    return bm


def _count_shells(bm, limit: int = _MAX_SHELL_FACES):
    """Connected face islands, or None for very large meshes."""
    if len(bm.faces) > limit:
        return None
    bm.faces.ensure_lookup_table()
    seen = set()
    shells = 0
    for f in bm.faces:
        if f.index in seen:
            continue
        shells += 1
        stack = [f]
        seen.add(f.index)
        while stack:
            cur = stack.pop()
            for e in cur.edges:
                for nf in e.link_faces:
                    if nf.index not in seen:
                        seen.add(nf.index)
                        stack.append(nf)
    return shells


def mesh_health(obj, evaluated: bool = False) -> dict:
    bm = _bmesh_world(obj, evaluated)
    try:
        boundary = multi = wire = non_contiguous = 0
        for e in bm.edges:
            n = len(e.link_faces)
            if n == 0:
                wire += 1
            elif n == 1:
                boundary += 1
            elif n > 2:
                multi += 1
            elif not e.is_contiguous:
                non_contiguous += 1
        loose_verts = sum(1 for v in bm.verts if not v.link_edges)
        zero_area = sum(1 for f in bm.faces if f.calc_area() <= _ZERO_AREA)
        non_manifold = boundary + multi + wire + non_contiguous
        closed = bool(bm.faces) and non_manifold == 0
        volume = bm.calc_volume(signed=True) if closed else None
        coords = [v.co for v in bm.verts]
        if coords:
            mn = [min(c[i] for c in coords) for i in range(3)]
            mx = [max(c[i] for c in coords) for i in range(3)]
        else:
            mn = mx = [0.0, 0.0, 0.0]
        out = {
            "object": obj.name,
            "source": "evaluated (with modifiers)" if evaluated else "original mesh data",
            "vertices": len(bm.verts),
            "edges": len(bm.edges),
            "faces": len(bm.faces),
            "non_manifold_edges": non_manifold,
            "boundary_edges": boundary,
            "multi_face_edges": multi,
            "wire_edges": wire,
            "non_contiguous_edges": non_contiguous,
            "loose_vertices": loose_verts,
            "zero_area_faces": zero_area,
            "shells": _count_shells(bm),
            "closed": closed,
            "volume": round(volume, 9) if volume is not None else None,
            "bounds_world": {"min": [round(x, 6) for x in mn], "max": [round(x, 6) for x in mx]},
        }
        if volume is None:
            out["volume_note"] = "not a closed manifold solid, so volume is meaningless"
        return out
    finally:
        bm.free()


def _plane_samples(bm):
    """Per face: (normal, sample points) where samples are the centre plus the
    corners pulled 25% toward it, so partial overlaps are still caught."""
    out = []
    for f in bm.faces:
        if f.calc_area() <= _ZERO_AREA:
            continue
        c = f.calc_center_median()
        pts = [c] + [v.co.lerp(c, 0.25) for v in f.verts]
        out.append((f.normal.copy(), pts))
    return out


def coincident_faces(a, b, tolerance: float) -> dict:
    """Faces of ``a`` and ``b`` that lie in the same plane and overlap.

    Samples points on each face of one mesh and finds the nearest surface of
    the other within ``tolerance``; a hit whose face normal is parallel means
    the two share a plane there. Same-direction normals are coincident
    surfaces (item 20); opposite normals are abutting solids (item 26). Both
    make exact booleans produce wrong results without an error.
    """
    bm_a = _bmesh_world(a, evaluated=True)
    bm_b = _bmesh_world(b, evaluated=True)
    try:
        if len(bm_a.faces) > _MAX_COPLANAR_FACES or len(bm_b.faces) > _MAX_COPLANAR_FACES:
            return {"checked": False, "reason": f"over {_MAX_COPLANAR_FACES} faces; skipped"}
        same = opposite = 0
        samples = []
        for src, dst in ((bm_a, bm_b), (bm_b, bm_a)):
            dst.faces.ensure_lookup_table()
            tree = BVHTree.FromBMesh(dst)
            for normal, pts in _plane_samples(src):
                for p in pts:
                    loc, _normal, index, _dist = tree.find_nearest(p, tolerance)
                    if loc is None:
                        continue
                    d = normal.dot(dst.faces[index].normal)
                    if d >= _PARALLEL_DOT:
                        same += 1
                    elif d <= -_PARALLEL_DOT:
                        opposite += 1
                    else:
                        continue
                    point = [round(x, 4) for x in loc]
                    if len(samples) < 5 and point not in samples:
                        samples.append(point)
                    break
        return {"checked": True, "coincident_same_direction": same,
                "coincident_opposite": opposite, "sample_locations": samples}
    finally:
        bm_a.free()
        bm_b.free()


def _inflated_copy(obj, distance: float):
    """A temporary copy of ``obj`` pushed outward by ``distance`` along vertex
    normals, used as the boolean operand so no two faces share a plane."""
    me = obj.data.copy()
    bm = bmesh.new()
    bm.from_mesh(me)
    bm.normal_update()
    for v in bm.verts:
        v.co += v.normal * (distance / max(obj.matrix_world.to_scale()))
    bm.to_mesh(me)
    bm.free()
    tmp = bpy.data.objects.new(f"_mcp_nudge_{obj.name}", me)
    tmp.matrix_world = obj.matrix_world.copy()
    bpy.context.scene.collection.objects.link(tmp)
    tmp.hide_set(True)
    tmp.hide_render = True
    return tmp


def _remove_temp(obj):
    me = obj.data
    bpy.data.objects.remove(obj, do_unlink=True)
    if me is not None and me.users == 0:
        bpy.data.meshes.remove(me)


def _apply_modifier(target, mod_name: str):
    with bpy.context.temp_override(object=target, active_object=target,
                                   selected_objects=[target], selected_editable_objects=[target]):
        bpy.ops.object.modifier_move_to_index(modifier=mod_name, index=0)
        bpy.ops.object.modifier_apply(modifier=mod_name)


class BooleanHandlersMixin:

    @command("mesh_health")
    def mesh_health_cmd(self, objects, evaluated: bool = False):
        """Manifoldness, openness, shells and volume for mesh objects."""
        if isinstance(objects, str):
            objects = [objects]
        results = [mesh_health(_get_mesh_object(n), evaluated) for n in objects]
        return {"objects": results}

    @command("boolean")
    def boolean_cmd(
        self,
        target: str,
        operands,
        operation: str = "DIFFERENCE",
        solver: str = "EXACT",
        apply: bool = True,
        check: bool = True,
        force: bool = False,
        use_self: bool = False,
        use_hole_tolerant: bool = False,
        coplanar_tolerance: float = 1e-4,
        coplanar_nudge: float | None = None,
        keep_operands: bool = True,
        hide_operands: bool = True,
    ):
        """Boolean ``operands`` into ``target`` one at a time, measuring each step."""
        op = checks.normalize_operation(operation)
        slv = checks.normalize_solver(solver)
        if isinstance(operands, str):
            operands = [operands]
        if not operands:
            raise ValueError("operands must name at least one mesh object")
        tgt = _get_mesh_object(target)
        ops = [_get_mesh_object(n) for n in operands]
        if tgt in ops:
            raise ValueError("the target can't also be an operand")
        if apply and tgt.data.users > 1:
            raise ValueError(f"{target!r} shares its mesh with other objects; make it single-user first")
        if bpy.context.mode != "OBJECT":
            raise ValueError("switch to Object Mode first")

        before = mesh_health(tgt, evaluated=False)
        operand_health = {o.name: mesh_health(o, evaluated=True) for o in ops}
        coincident = {}
        refusals = []
        if check:
            for name, problems in [(target, checks.input_problems(before))] + [
                (o.name, checks.input_problems(operand_health[o.name])) for o in ops
            ]:
                if problems:
                    refusals.append(f"{name}: {'; '.join(problems)}")
            for o in ops:
                c = coincident_faces(tgt, o, coplanar_tolerance)
                coincident[o.name] = c
                if c.get("checked") and (c["coincident_same_direction"] or c["coincident_opposite"]) \
                        and not coplanar_nudge:
                    refusals.append(
                        f"{o.name}: faces share a plane with {target} "
                        f"({c['coincident_same_direction']} coincident, "
                        f"{c['coincident_opposite']} abutting; e.g. at {c['sample_locations'][:2]}). "
                        "Booleans can corrupt here without an error. Offset the operand, "
                        "or pass coplanar_nudge"
                    )
        if refusals and not force:
            return {
                "status": "refused",
                "target": target,
                "reasons": refusals,
                "target_health": before,
                "operand_health": operand_health,
                "coincident_faces": coincident,
                "hint": "fix the inputs (e.g. use_self union to merge overlapping parts, offset "
                        "abutting faces), or pass force=True to run anyway",
            }

        steps = []
        current = before
        temps = []
        try:
            for o in ops:
                operand_obj = o
                if coplanar_nudge:
                    operand_obj = _inflated_copy(o, float(coplanar_nudge))
                    temps.append(operand_obj)
                mod = tgt.modifiers.new(name=f"MCP_{op.title()}_{o.name}"[:60], type="BOOLEAN")
                mod.operation = op
                mod.solver = slv
                mod.operand_type = "OBJECT"
                mod.object = operand_obj
                if hasattr(mod, "use_self"):
                    mod.use_self = bool(use_self)
                if hasattr(mod, "use_hole_tolerant"):
                    mod.use_hole_tolerant = bool(use_hole_tolerant)
                if apply:
                    _apply_modifier(tgt, mod.name)
                    after = mesh_health(tgt, evaluated=False)
                else:
                    bpy.context.view_layer.update()
                    after = mesh_health(tgt, evaluated=True)
                step = {
                    "operand": o.name,
                    "modifier": None if apply else mod.name,
                    "faces": [current["faces"], after["faces"]],
                    "volume": [current["volume"], after["volume"]],
                    "non_manifold_edges": [current["non_manifold_edges"], after["non_manifold_edges"]],
                    "warnings": checks.verdict(op, current, operand_health[o.name], after),
                }
                steps.append(step)
                current = after
        finally:
            if apply:
                for t in temps:
                    _remove_temp(t)

        for o in ops:
            if not keep_operands and apply:
                _remove_temp(o)
            elif hide_operands:
                o.hide_set(True)
                o.hide_render = True

        warnings = [f"{s['operand']}: {w}" for s in steps for w in s["warnings"]]
        return {
            "status": "completed_with_warnings" if warnings else "completed",
            "target": target,
            "operation": op,
            "solver": slv,
            "applied": bool(apply),
            "forced": bool(refusals and force),
            "refusals_overridden": refusals if force else [],
            "steps": steps,
            "warnings": warnings,
            "target_health_before": before,
            "target_health_after": current,
            "coincident_faces": coincident,
        }
