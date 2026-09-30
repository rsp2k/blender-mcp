"""Everyday modelling commands a chat model reaches for first.

- ``add_primitive``: a box, cylinder, cone, sphere or plane with exact
  extents, built at data level with bmesh (no bpy.ops, so it runs from the
  timer that executes commands) and reported with its world bounds.
- ``set_color``: a plain Principled BSDF material from a colour name, hex
  or [r, g, b], assigned as the object's only material.
- ``list_scene_objects``: every object in the scene, filterable and paged,
  where get_scene_info only lists the first few.

Argument maths lives in the bpy-free ``primitive_spec`` and ``color_names``
modules so it can be unit-tested.
"""

from __future__ import annotations

import math

import bmesh
import bpy
import mathutils

from ... import color_names as cn
from ... import primitive_spec as ps
from ..registry import command
from .annotations import resolve_targets
from .booleans import mesh_health
from .mesh_data import _collection, _parent
from .scene import scene_named

MAX_LIST_LIMIT = 500
_MATERIAL_TYPES = ("MESH", "CURVE", "SURFACE", "META", "FONT")


def _r(v, nd=4):
    return [round(float(x), nd) for x in v]


def _build_unit(bm, kind, segments):
    """Create the raw shape in ``bm``, roughly unit sized and centred."""
    if kind == "box":
        bmesh.ops.create_cube(bm, size=1.0)
    elif kind == "plane":
        bmesh.ops.create_grid(bm, x_segments=1, y_segments=1, size=0.5)
    elif kind == "sphere":
        bmesh.ops.create_uvsphere(bm, u_segments=segments, v_segments=max(3, segments // 2),
                                  radius=0.5)
    else:
        bmesh.ops.create_cone(bm, cap_ends=True, cap_tris=False, segments=segments,
                              radius1=0.5, radius2=0.5 if kind == "cylinder" else 0.0,
                              depth=1.0)
        if kind == "cone":
            # radius2=0 stacks the top ring on one point; merge it into an apex.
            bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=1e-6)
            bmesh.ops.dissolve_degenerate(bm, dist=1e-6, edges=bm.edges)


def _fit(bm, dims, offset):
    """Scale the shape to exact extents, centre it, then shift by ``offset``."""
    bm.verts.ensure_lookup_table()
    lo = [min(v.co[i] for v in bm.verts) for i in range(3)]
    hi = [max(v.co[i] for v in bm.verts) for i in range(3)]
    centre = mathutils.Vector([(lo[i] + hi[i]) / 2 for i in range(3)])
    bmesh.ops.translate(bm, vec=-centre, verts=bm.verts)
    scale = ps.fit_scale([hi[i] - lo[i] for i in range(3)], dims)
    bmesh.ops.scale(bm, vec=scale, verts=bm.verts)
    bmesh.ops.translate(bm, vec=mathutils.Vector(offset), verts=bm.verts)
    if bm.faces:
        bmesh.ops.recalc_face_normals(bm, faces=bm.faces)


def _principled(mat):
    """The material's Principled BSDF node, created and wired if missing."""
    try:
        mat.use_nodes = True
    except (AttributeError, TypeError):
        pass  # always on in newer Blender
    tree = mat.node_tree
    node = next((n for n in tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
    if node is None:
        node = tree.nodes.new("ShaderNodeBsdfPrincipled")
        out = next((n for n in tree.nodes if n.type == "OUTPUT_MATERIAL"), None)
        if out is None:
            out = tree.nodes.new("ShaderNodeOutputMaterial")
        tree.links.new(node.outputs[0], out.inputs["Surface"])
    return node


def _matches(mat, spec) -> bool:
    node = next((n for n in (mat.node_tree.nodes if mat.node_tree else [])
                 if n.type == "BSDF_PRINCIPLED"), None)
    if node is None:
        return False
    col = list(node.inputs["Base Color"].default_value)[:3]
    return (all(abs(a - b) < 1e-3 for a, b in zip(col, spec["linear"]))
            and abs(node.inputs["Roughness"].default_value - spec["roughness"]) < 1e-3
            and abs(node.inputs["Metallic"].default_value - spec["metallic"]) < 1e-3
            and abs(_glow_of(node) - spec.get("glow", 0.0)) < 1e-3)


def _emission_inputs(node):
    """(colour, strength) sockets. Blender 4.0 renamed "Emission" to
    "Emission Color"; models writing Python still guess the old name."""
    col = node.inputs.get("Emission Color") or node.inputs.get("Emission")
    return col, node.inputs.get("Emission Strength")


def _glow_of(node) -> float:
    """Effective emission strength: 0 when the emission colour is black,
    which is how Blender 3.x ships a Principled BSDF (strength 1, black)."""
    col, strength = _emission_inputs(node)
    if col is None or strength is None:
        return 0.0
    if max(list(col.default_value)[:3]) <= 1e-6:
        return 0.0
    return float(strength.default_value)


def _material_for(spec, name):
    """(material, created). An explicit name is created or updated; the
    default name is reused only when that material already has these values."""
    if name:
        mat = bpy.data.materials.get(name)
        return (mat, False) if mat is not None else (bpy.data.materials.new(name), True)
    base = cn.material_name(spec)
    if spec.get("glow"):
        base += " Glow"
    candidates = [base] + [f"{base}.{i:03d}" for i in range(1, 100)]
    for cand in candidates:
        mat = bpy.data.materials.get(cand)
        if mat is None:
            return bpy.data.materials.new(cand), True
        if _matches(mat, spec):
            return mat, False
    return bpy.data.materials.new(base), True


def _apply(mat, spec):
    node = _principled(mat)
    rgba = (*spec["linear"], 1.0)
    node.inputs["Base Color"].default_value = rgba
    node.inputs["Roughness"].default_value = spec["roughness"]
    node.inputs["Metallic"].default_value = spec["metallic"]
    glow = spec.get("glow", 0.0)
    col, strength = _emission_inputs(node)
    if col is not None and strength is not None:
        col.default_value = rgba if glow > 0 else (0.0, 0.0, 0.0, 1.0)
        strength.default_value = glow
    # Solid-mode viewport colour, so the change shows without Material Preview.
    mat.diffuse_color = rgba
    mat.roughness = spec["roughness"]
    mat.metallic = spec["metallic"]


def _targets(objects):
    specs = [objects] if isinstance(objects, str) else list(objects or [])
    if not specs or not all(isinstance(s, str) and s for s in specs):
        raise ValueError("objects must be an object name, a list of names, or 'selected' / 'active'")
    out = []
    for s in specs:
        for obj in resolve_targets(s):
            if obj not in out:
                out.append(obj)
    return out


def _object_row(obj, view_layer=None):
    mw = obj.matrix_world
    row = {
        "name": obj.name,
        "type": obj.type,
        "parent": obj.parent.name if obj.parent else None,
        "collections": [c.name for c in obj.users_collection],
        "location": _r(mw.translation),
        "dimensions": _r(obj.dimensions),
        "materials": list(dict.fromkeys(
            s.material.name for s in obj.material_slots if s.material is not None)),
    }
    try:
        visible = obj.visible_get(view_layer=view_layer) if view_layer else obj.visible_get()
    except (TypeError, RuntimeError):
        visible = True
    if not visible:
        row["hidden"] = True
    return row


class ModellingHandlersMixin:
    """add_primitive, set_color, list_scene_objects."""

    @command("add_primitive")
    def add_primitive(self, kind, name=None, size=None, radius=None, depth=None,
                      location=None, anchor="center", rotation=None, segments=None,
                      collection=None, parent=None):
        """Add a primitive mesh with exact extents; the origin is the anchor point."""
        p = ps.plan(kind, size=size, radius=radius, depth=depth, anchor=anchor,
                    location=location, rotation=rotation, segments=segments)
        par = _parent(parent)
        coll = _collection(collection)
        obj_name = name or p["kind"].capitalize()

        me = bpy.data.meshes.new(obj_name)
        bm = bmesh.new()
        try:
            _build_unit(bm, p["kind"], p["segments"])
            _fit(bm, p["dims"], p["offset"])
            bm.to_mesh(me)
        finally:
            bm.free()
        me.update()
        obj = bpy.data.objects.new(obj_name, me)
        coll.objects.link(obj)
        if par is not None:
            obj.parent = par
            obj.matrix_parent_inverse.identity()
        obj.location = p["location"]
        obj.rotation_euler = mathutils.Euler([math.radians(a) for a in p["rotation"]], "XYZ")
        bpy.context.view_layer.update()

        h = mesh_health(obj)
        lo, hi = h["bounds_world"]["min"], h["bounds_world"]["max"]
        out = {
            "object": obj.name,
            "kind": p["kind"],
            "anchor": p["anchor"],
            "origin_world": _r(obj.matrix_world.translation),
            "dimensions": _r(p["dims"]),
            "bounds_world": {"min": _r(lo), "max": _r(hi),
                             "size": _r([hi[i] - lo[i] for i in range(3)])},
            "vertices": h["vertices"],
            "faces": h["faces"],
            "closed": h["closed"],
            "volume": h["volume"],
            "parent": obj.parent.name if obj.parent else None,
            "collection": coll.name,
        }
        if p["segments"]:
            out["segments"] = p["segments"]
        if p["kind"] == "plane":
            out["note"] = "a plane is a single flat face, so it is not a closed solid"
        if obj.name != obj_name:
            out["renamed"] = f"{obj_name!r} was taken; the new object is {obj.name!r}"
        return out

    @command("set_color")
    def set_color(self, objects, color, roughness=None, metallic=None, name=None, glow=None):
        """Give objects a plain coloured Principled BSDF material, optionally glowing."""
        spec = cn.resolve(color, roughness=roughness, metallic=metallic)
        spec["glow"] = max(0.0, float(glow or 0.0))
        targets = _targets(objects)
        bad = [o.name for o in targets
               if o.type not in _MATERIAL_TYPES or not hasattr(o.data, "materials")]
        if bad:
            raise ValueError(f"these objects can't take a material: {bad}")
        mat, created = _material_for(spec, name)
        _apply(mat, spec)
        replaced = {}
        for obj in targets:
            old = [s.material.name for s in obj.material_slots if s.material is not None]
            if old and old != [mat.name]:
                replaced[obj.name] = old
            obj.data.materials.clear()
            obj.data.materials.append(mat)
            # An object-linked slot would override the mesh's material.
            obj.material_slots[0].material = mat
        out = {
            "material": mat.name,
            "created": created,
            "objects": [o.name for o in targets],
            "color": spec["label"],
            "hex": spec["hex"],
            "base_color_linear": spec["linear"],
            "roughness": spec["roughness"],
            "metallic": spec["metallic"],
        }
        if spec["glow"]:
            out["glow"] = spec["glow"]
        if replaced:
            out["replaced_materials"] = replaced
        shared = [o.name for o in targets if o.data.users > 1]
        if shared:
            out["note"] = (f"{shared} share mesh data with other objects, which now show "
                           "this material too")
        return out

    @command("list_scene_objects", undo=False)
    def list_scene_objects(self, type=None, name_contains=None, collection=None,
                           limit=100, offset=0, scene=None):
        """Every object in the active scene (or the one named ``scene``, without
        switching to it), filtered and paged, sorted by name."""
        scene = scene_named(scene)
        limit = max(1, min(int(limit), MAX_LIST_LIMIT))
        offset = max(0, int(offset))
        objs = list(scene.objects)
        type_counts = {}
        for o in objs:
            type_counts[o.type] = type_counts.get(o.type, 0) + 1
        if collection:
            coll = bpy.data.collections.get(collection)
            if coll is None and collection != scene.collection.name:
                raise ValueError(f"no collection named {collection!r}")
            members = set((coll or scene.collection).all_objects)
            objs = [o for o in objs if o in members]
        if type:
            want = str(type).upper()
            objs = [o for o in objs if o.type == want]
        if name_contains:
            needle = str(name_contains).lower()
            objs = [o for o in objs if needle in o.name.lower()]
        objs.sort(key=lambda o: o.name)
        page = objs[offset:offset + limit]
        if scene == bpy.context.scene:
            layer = bpy.context.view_layer
        else:
            layer = scene.view_layers[0] if len(scene.view_layers) else None
        nxt = offset + len(page)
        return {
            "scene": scene.name,
            "active_scene": bpy.context.scene.name,
            "total_objects": len(scene.objects),
            "type_counts": dict(sorted(type_counts.items())),
            "matching": len(objs),
            "offset": offset,
            "returned": len(page),
            "next_offset": nxt if nxt < len(objs) else None,
            "objects": [_object_row(o, layer) for o in page],
        }
