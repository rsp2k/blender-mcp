"""File, scene-cleanup, interior-face and glTF-export commands.

Covers model-home gaps #5 (save/open), #16 (startup leftovers), #17
(interior skins after self-unions) and #27 (glTF exporter silently dropping
colours). The decisions live in addon/file_scene_checks.py; this module only
gathers data from bpy and applies the result.
"""

from __future__ import annotations

import json
import os
import struct

import bmesh
import bpy
from mathutils import Vector
from mathutils.bvhtree import BVHTree

from ... import file_scene_checks as checks
from ..registry import command
from .booleans import mesh_health

_RAY_DIRECTIONS = (
    Vector((0.5773, 0.5774, 0.5775)).normalized(),
    Vector((-0.6, 0.3, 0.742)).normalized(),
    Vector((0.21, -0.83, 0.52)).normalized(),
)
_MAX_RAY_HITS = 10_000
_SAMPLE = 20


def _abspath(path: str) -> str:
    return os.path.abspath(os.path.expanduser(bpy.path.abspath(path)))


def _require_blend(path: str) -> str:
    if not isinstance(path, str) or not path.strip():
        raise ValueError("path is required")
    path = _abspath(path)
    if not path.lower().endswith(".blend"):
        raise ValueError(f"not a .blend path: {path}")
    return path


def _file_state() -> dict:
    return {
        "filepath": bpy.data.filepath or None,
        "is_dirty": bool(bpy.data.is_dirty),
        "scene": bpy.context.scene.name if bpy.context.scene else None,
        "objects": len(bpy.data.objects),
    }


def _refuse_if_dirty(discard_unsaved: bool, action: str) -> None:
    refusal = checks.dirty_refusal(bool(bpy.data.is_dirty), discard_unsaved, action)
    if refusal:
        raise ValueError(refusal)


# --- startup defaults (#16) ---------------------------------------------------

def _object_props(obj) -> dict:
    props = {
        "name": obj.name, "type": obj.type,
        "parent": obj.parent.name if obj.parent else None,
        "children": [c.name for c in obj.children],
        "modifiers": [m.type for m in obj.modifiers],
        "location": tuple(obj.location), "rotation": tuple(obj.rotation_euler),
        "scale": tuple(obj.scale),
        "materials": [s.material.name for s in obj.material_slots if s.material],
    }
    if obj.type == "MESH":
        props["vertices"] = [tuple(v.co) for v in obj.data.vertices]
        props["face_count"] = len(obj.data.polygons)
    elif obj.type == "LIGHT":
        props["light_type"] = obj.data.type
        props["energy"] = obj.data.energy
    elif obj.type == "CAMERA":
        props["lens"] = obj.data.lens
    return props


def find_startup_defaults(scene) -> list[dict]:
    found = []
    for obj in scene.objects:
        kind, mismatches = checks.default_object_reasons(_object_props(obj))
        if kind:
            found.append({"object": obj.name, "kind": kind,
                          "untouched": not mismatches, "differences": mismatches})
    return found


# --- interior faces (#17) -------------------------------------------------------

def _face_shells(bm) -> list[list]:
    seen, shells = set(), []
    for f in bm.faces:
        if f.index in seen:
            continue
        stack, shell = [f], []
        seen.add(f.index)
        while stack:
            cur = stack.pop()
            shell.append(cur)
            for e in cur.edges:
                for nb in e.link_faces:
                    if nb.index not in seen:
                        seen.add(nb.index)
                        stack.append(nb)
        shells.append(shell)
    return shells


def _shell_closed(shell) -> bool:
    ids = {f.index for f in shell}
    for f in shell:
        for e in f.edges:
            if sum(1 for lf in e.link_faces if lf.index in ids) != 2:
                return False
    return True


class _Shell:
    def __init__(self, index, faces, coords):
        self.index = index
        self.faces = faces
        self.closed = _shell_closed(faces)
        polys = [[v.index for v in f.verts] for f in faces]
        self.bvh = BVHTree.FromPolygons(coords, polys, all_triangles=False)
        pts = [coords[i] for p in polys for i in p]
        self.lo = Vector((min(p.x for p in pts), min(p.y for p in pts), min(p.z for p in pts)))
        self.hi = Vector((max(p.x for p in pts), max(p.y for p in pts), max(p.z for p in pts)))

    def contains(self, point, step: float) -> bool:
        """Odd crossing count along a ray means inside; majority of 3 rays."""
        if not self.closed:
            return False
        if any(point[i] < self.lo[i] - step or point[i] > self.hi[i] + step for i in range(3)):
            return False
        votes = 0
        for d in _RAY_DIRECTIONS:
            origin, hits = Vector(point), 0
            for _ in range(_MAX_RAY_HITS):
                loc, _n, _i, _dist = self.bvh.ray_cast(origin, d)
                if loc is None:
                    break
                hits += 1
                origin = loc + d * step
            votes += hits % 2
        return votes >= 2


def _classify_interior(bm, method: str) -> dict:
    bm.faces.ensure_lookup_table()
    bm.verts.ensure_lookup_table()
    coords = [v.co.copy() for v in bm.verts]
    lo = Vector((min(c.x for c in coords), min(c.y for c in coords), min(c.z for c in coords)))
    hi = Vector((max(c.x for c in coords), max(c.y for c in coords), max(c.z for c in coords)))
    eps = max((hi - lo).length * 1e-5, 1e-6)
    shells = [_Shell(i, s, coords) for i, s in enumerate(_face_shells(bm))]

    def inside_any(point, exclude=None):
        return any(s.contains(point, eps) for s in shells if s is not exclude)

    flip, delete, delete_shells = [], [], []
    if method == "shells":
        for s in shells:
            samples = [f.calc_center_median() for f in s.faces[:3]]
            others = [o for o in shells if o is not s]
            if others and all(inside_any(p, exclude=s) for p in samples):
                delete_shells.append(s.index)
                delete.extend(f.index for f in s.faces)
    else:
        for s in shells:
            for f in s.faces:
                n = f.normal
                if n.length < 0.5:
                    continue  # degenerate face; leave it
                c = f.calc_center_median()
                action = checks.face_action(inside_any(c + n * eps * 10),
                                            inside_any(c - n * eps * 10))
                if action == "flip":
                    flip.append(f.index)
                elif action == "delete":
                    delete.append(f.index)
    return {
        "shells": len(shells), "closed_shells": sum(1 for s in shells if s.closed),
        "flip": flip, "delete": delete, "delete_shells": delete_shells,
    }


# --- glTF audit (#27) -------------------------------------------------------------

def _image_missing(node) -> bool:
    img = getattr(node, "image", None)
    if img is None:
        return True
    if img.source == "FILE" and not img.packed_file:
        return not os.path.exists(bpy.path.abspath(img.filepath))
    return False


def _material_output(nt):
    outs = [n for n in nt.nodes if n.type == "OUTPUT_MATERIAL"]
    return next((n for n in outs if n.is_active_output), outs[0] if outs else None)


def material_info(mat) -> dict:
    nt = getattr(mat, "node_tree", None)
    if nt is None or not getattr(mat, "use_nodes", True):
        return {"use_nodes": False}
    out = _material_output(nt)
    links = out.inputs["Surface"].links if out else []
    surface = links[0].from_node if links else None
    info = {"use_nodes": True, "surface": surface.type if surface else None}
    if surface is not None and surface.type == "BSDF_PRINCIPLED":
        bc = surface.inputs.get("Base Color")
        if bc is not None and bc.links:
            src = bc.links[0].from_node
            info["base_color_source"] = src.type
            if src.type == "TEX_IMAGE":
                info["image_missing"] = _image_missing(src)
            elif src.type == "MIX":
                images = _mix_multiply_images(src)
                if images is not None:
                    info["mix_multiply_image"] = True
                    info["image_missing"] = _image_missing(images)
    return info


def _mix_multiply_images(mix):
    """The Image Texture node when ``mix`` is image x constant (MULTIPLY, fixed factor)."""
    if getattr(mix, "data_type", None) != "RGBA" or getattr(mix, "blend_type", None) != "MULTIPLY":
        return None
    color_inputs = [s for s in mix.inputs if s.enabled and s.type == "RGBA"][:2]
    factor = next((s for s in mix.inputs if s.enabled and s.name == "Factor"), None)
    if factor is not None and factor.links:
        return None
    linked = [s for s in color_inputs if s.links]
    if len(linked) != 1 or linked[0].links[0].from_node.type != "TEX_IMAGE":
        return None
    return linked[0].links[0].from_node


def _export_materials(objs) -> list:
    mats, seen = [], set()
    for obj in objs:
        for slot in getattr(obj, "material_slots", []):
            if slot.material and slot.material.name not in seen:
                seen.add(slot.material.name)
                mats.append(slot.material)
    return mats


def _swap_diffuse(mat):
    """Temporarily route a Diffuse BSDF's colour through a Principled BSDF.

    Returns a restore callable, or None when the material isn't a plain
    Diffuse case this can reproduce exactly.
    """
    nt = mat.node_tree
    out = _material_output(nt)
    link = out.inputs["Surface"].links[0]
    diffuse = link.from_node
    if diffuse.type != "BSDF_DIFFUSE":
        return None
    principled = nt.nodes.new("ShaderNodeBsdfPrincipled")
    color_in = diffuse.inputs["Color"]
    if color_in.links:
        nt.links.new(color_in.links[0].from_socket, principled.inputs["Base Color"])
    else:
        principled.inputs["Base Color"].default_value = tuple(color_in.default_value)
    principled.inputs["Roughness"].default_value = diffuse.inputs["Roughness"].default_value
    original_socket = link.from_socket
    nt.links.new(principled.outputs["BSDF"], out.inputs["Surface"])

    def restore():
        nt.links.new(original_socket, out.inputs["Surface"])
        nt.nodes.remove(principled)
    return restore


def _glb_materials_without_color(path: str) -> list[str] | None:
    """Read the written GLB's JSON chunk: materials with no base colour at all."""
    try:
        with open(path, "rb") as fh:
            magic, _ver, _len = struct.unpack("<4sII", fh.read(12))
            if magic != b"glTF":
                return None
            clen, ctype = struct.unpack("<II", fh.read(8))
            if ctype != 0x4E4F534A:  # "JSON"
                return None
            doc = json.loads(fh.read(clen))
    except (OSError, ValueError, struct.error):
        return None
    empty = []
    for m in doc.get("materials", []):
        pbr = m.get("pbrMetallicRoughness") or {}
        if "baseColorFactor" not in pbr and "baseColorTexture" not in pbr:
            empty.append(m.get("name", "?"))
    return empty


class FileSceneHandlersMixin:

    # --- #5 save / open ---------------------------------------------------------

    @command("save_file")
    def save_file(self, path: str | None = None, copy: bool = False, compress: bool = False,
                  create_dirs: bool = False):
        """Save the open .blend (to its own path, or to ``path``)."""
        if path:
            target = _require_blend(path)
            parent = os.path.dirname(target)
            if not os.path.isdir(parent):
                if not create_dirs:
                    raise ValueError(f"folder does not exist: {parent} (pass create_dirs=true)")
                os.makedirs(parent, exist_ok=True)
        elif bpy.data.filepath:
            target = bpy.data.filepath
        else:
            raise ValueError("this file has never been saved; pass a path")
        if copy and target == bpy.data.filepath:
            raise ValueError("copy=true needs a path different from the open file")
        bpy.ops.wm.save_as_mainfile(filepath=target, copy=copy, compress=compress)
        return {"saved": target, "copy": copy, "size_bytes": os.path.getsize(target),
                **_file_state()}

    @command("open_file")
    def open_file(self, path: str, discard_unsaved: bool = False, load_ui: bool = False):
        """Open a .blend, refusing to drop unsaved work unless told to."""
        target = _require_blend(path)
        if not os.path.isfile(target):
            raise ValueError(f"file not found: {target}")
        before = _file_state()
        _refuse_if_dirty(discard_unsaved, "opening another file")
        bpy.ops.wm.open_mainfile(filepath=target, load_ui=load_ui)
        return {"opened": target, "discarded_unsaved": before["is_dirty"],
                "scenes": [s.name for s in bpy.data.scenes], **_file_state()}

    @command("revert_file")
    def revert_file(self, discard_unsaved: bool = False):
        """Reload the open file from disk."""
        if not bpy.data.filepath:
            raise ValueError("this file has never been saved; nothing to revert to")
        _refuse_if_dirty(discard_unsaved, "reverting")
        bpy.ops.wm.revert_mainfile()
        return {"reverted": bpy.data.filepath, **_file_state()}

    @command("new_file")
    def new_file(self, empty: bool = True, discard_unsaved: bool = False):
        """Start a new file: an empty scene, or the user's startup file."""
        _refuse_if_dirty(discard_unsaved, "starting a new file")
        bpy.ops.wm.read_homefile(use_empty=empty, load_ui=False)
        return {"new_file": True, "empty": empty,
                "startup_defaults": find_startup_defaults(bpy.context.scene), **_file_state()}

    # --- #16 startup leftovers ----------------------------------------------------

    @command("scene_defaults")
    def scene_defaults(self, remove: bool = False, kinds=None):
        """Find (and optionally remove) untouched startup cube/light/camera."""
        if isinstance(kinds, str):
            kinds = [kinds]
        kinds = set(kinds or ("cube", "light", "camera"))
        bad = kinds - {"cube", "light", "camera"}
        if bad:
            raise ValueError(f"unknown kinds {sorted(bad)}; use cube, light, camera")
        found = find_startup_defaults(bpy.context.scene)
        removed = []
        if remove:
            for item in found:
                if item["untouched"] and item["kind"] in kinds:
                    obj = bpy.data.objects.get(item["object"])
                    data = obj.data
                    bpy.data.objects.remove(obj, do_unlink=True)
                    if data is not None and data.users == 0:
                        for coll in (bpy.data.meshes, bpy.data.lights, bpy.data.cameras):
                            if data.name in coll and coll[data.name] == data:
                                coll.remove(data)
                                break
                    removed.append(item["object"])
        return {"scene": bpy.context.scene.name, "defaults": found, "removed": removed,
                "kept_modified": [i["object"] for i in found if not i["untouched"]]}

    @command("new_scene")
    def new_scene(self, name: str = "Scene", make_active: bool = True, copy_world: bool = True):
        """Add an empty scene (no startup objects) and switch to it."""
        current = bpy.context.scene
        scene = bpy.data.scenes.new(name)
        if copy_world and current is not None and current.world is not None:
            scene.world = current.world
        activated = False
        if make_active:
            for window in bpy.context.window_manager.windows:
                window.scene = scene
                activated = True
        return {"scene": scene.name, "active": activated,
                "note": None if activated or not make_active else
                "no Blender window (background mode); the scene exists but isn't active"}

    # --- #17 interior faces --------------------------------------------------------

    @command("remove_interior")
    def remove_interior(self, object: str, method: str = "faces", dry_run: bool = False):
        """Flip inside-out faces and delete buried faces or enclosed shells."""
        if method not in ("faces", "shells"):
            raise ValueError("method must be 'faces' or 'shells'")
        obj = bpy.data.objects.get(object)
        if obj is None or obj.type != "MESH":
            raise ValueError(f"no mesh object named {object!r}")
        if obj.mode != "OBJECT":
            raise ValueError(f"{object!r} is in {obj.mode} mode; switch to Object mode first")
        before = mesh_health(obj)
        bm = bmesh.new()
        bm.from_mesh(obj.data)
        try:
            plan = _classify_interior(bm, method)
            result = {
                "object": obj.name, "method": method, "dry_run": dry_run,
                "shells": plan["shells"], "closed_shells": plan["closed_shells"],
                "faces_to_flip": len(plan["flip"]), "faces_to_delete": len(plan["delete"]),
                "shells_to_delete": len(plan["delete_shells"]),
                "sample_flip": plan["flip"][:_SAMPLE], "sample_delete": plan["delete"][:_SAMPLE],
                "health_before": before,
                "note": ("Only closed shells count as solid; open parts are never used to "
                         "decide what's inside." if plan["closed_shells"] < plan["shells"] else None),
                "base_mesh_only": bool(obj.modifiers),
            }
            if dry_run or not (plan["flip"] or plan["delete"]):
                result["health_after"] = before
                return result
            bm.faces.ensure_lookup_table()
            if plan["flip"]:
                bmesh.ops.reverse_faces(bm, faces=[bm.faces[i] for i in plan["flip"]])
            if plan["delete"]:
                bmesh.ops.delete(bm, geom=[bm.faces[i] for i in plan["delete"]], context="FACES")
                loose = [v for v in bm.verts if not v.link_faces]
                if loose:
                    bmesh.ops.delete(bm, geom=loose, context="VERTS")
            bm.to_mesh(obj.data)
            obj.data.update()
        finally:
            bm.free()
        result["health_after"] = mesh_health(obj)
        return result

    # --- #27 glTF export -------------------------------------------------------------

    @command("export_gltf")
    def export_gltf(self, path: str, objects=None, format: str = "glb", audit: bool = True,
                    auto_fix: bool = False, create_dirs: bool = False):
        """Export glTF after auditing materials the exporter would empty out."""
        fmt = {"glb": "GLB", "gltf": "GLTF_SEPARATE"}.get(str(format).lower())
        if fmt is None:
            raise ValueError("format must be 'glb' or 'gltf'")
        target = os.path.abspath(os.path.expanduser(path))
        ext = ".glb" if fmt == "GLB" else ".gltf"
        if not target.lower().endswith(ext):
            target += ext
        parent = os.path.dirname(target)
        if not os.path.isdir(parent):
            if not create_dirs:
                raise ValueError(f"folder does not exist: {parent} (pass create_dirs=true)")
            os.makedirs(parent, exist_ok=True)

        if isinstance(objects, str):
            objects = [objects]
        if objects:
            objs = []
            for name in objects:
                o = bpy.data.objects.get(name)
                if o is None:
                    raise ValueError(f"object not found: {name!r}")
                objs.append(o)
        else:
            objs = [o for o in bpy.context.scene.objects if o.visible_get()]

        issues = []
        if audit or auto_fix:
            for mat in _export_materials(objs):
                why = checks.gltf_material_issue(material_info(mat))
                if why:
                    issues.append({"material": mat.name, "problem": why})

        restores, fixed = [], []
        if auto_fix:
            for item in issues:
                restore = _swap_diffuse(bpy.data.materials[item["material"]])
                if restore is not None:
                    restores.append(restore)
                    fixed.append(item["material"])
        view_layer = bpy.context.view_layer
        prev_sel = [o for o in view_layer.objects if o.select_get()]
        prev_active = view_layer.objects.active
        try:
            if objects:
                for o in view_layer.objects:
                    o.select_set(o in objs)
            bpy.ops.export_scene.gltf(filepath=target, export_format=fmt,
                                      use_selection=bool(objects))
        finally:
            for restore in restores:
                restore()
            if objects:
                for o in view_layer.objects:
                    o.select_set(o in prev_sel)
                view_layer.objects.active = prev_active

        remaining = [i for i in issues if i["material"] not in fixed]
        result = {
            "exported": target, "format": fmt, "size_bytes": os.path.getsize(target),
            "objects": len(objs), "material_issues": remaining, "auto_fixed": fixed,
        }
        if fmt == "GLB":
            result["materials_without_color_in_file"] = _glb_materials_without_color(target)
        if remaining:
            result["guidance"] = (
                "These materials export without their colour. Use a Principled BSDF with "
                "Base Color set to a constant or wired straight from an Image Texture; bake "
                "node chains to an image first. auto_fix=true handles plain Diffuse BSDFs."
            )
        return result
