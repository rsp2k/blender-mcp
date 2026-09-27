"""Build meshes from data: raw vertices/faces, or extruded 2D polygons.

Model-home's gaps-log item 1: the Clagstone model's 286 wall prisms had to be
built with bmesh inside execute_code. ``extrude_polygons`` takes polygon rings
(outer + holes) plus heights and builds closed prisms in one call, merged
into one object or one object per prism, and reports each result's mesh
health. Large inputs can come from a file uploaded with blender_upload
(``source``), since dispatch parameters are best kept small.
"""

from __future__ import annotations

import json

import bpy
from mathutils import geometry

from ... import prism_geometry as pg
from ... import upload_store
from ..registry import command
from .booleans import mesh_health
from .uploads import upload_dir

MAX_POLYGONS = 20_000
MAX_VERTICES = 2_000_000


def _tessellate(polys):
    return [tuple(t) for t in geometry.tessellate_polygon(polys)]


def _collection(name):
    if not name:
        return bpy.context.scene.collection
    coll = bpy.data.collections.get(name)
    if coll is None:
        coll = bpy.data.collections.new(name)
        bpy.context.scene.collection.children.link(coll)
    return coll


def _parent(name):
    if not name:
        return None
    obj = bpy.data.objects.get(name)
    if obj is None:
        raise ValueError(f"parent object not found: {name!r}")
    return obj


def _vec3(value, what):
    if value is None:
        return (0.0, 0.0, 0.0)
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError(f"{what} must be [x, y, z]")
    return tuple(float(v) for v in value)


def _make_object(name, verts, faces, coll, parent, location, smooth):
    me = bpy.data.meshes.new(name)
    me.from_pydata(verts, [], faces)
    me.validate(clean_customdata=False)
    me.update()
    if smooth:
        me.shade_smooth()
    obj = bpy.data.objects.new(name, me)
    coll.objects.link(obj)
    if parent is not None:
        obj.parent = parent
        obj.matrix_parent_inverse.identity()
    obj.location = location
    return obj


def _summary(obj):
    h = mesh_health(obj)
    return {
        "object": obj.name,
        "vertices": h["vertices"],
        "faces": h["faces"],
        "closed": h["closed"],
        "non_manifold_edges": h["non_manifold_edges"],
        "shells": h["shells"],
        "volume": h["volume"],
    }


def _read_json_source(source, bus_dir):
    path = upload_store.resolve_source(upload_dir(bus_dir), source)
    with open(path, encoding="utf-8") as fh:
        return json.load(fh), str(path)


class MeshDataHandlersMixin:

    @command("create_mesh")
    def create_mesh(self, name="Mesh", vertices=None, faces=None, source=None,
                    collection=None, parent=None, location=None, scale=1.0,
                    smooth=False, bus_dir=None):
        """Create a mesh object from vertex coordinates and face index lists."""
        src_path = None
        if source:
            data, src_path = _read_json_source(source, bus_dir)
            if not isinstance(data, dict):
                raise ValueError("source JSON must be an object with 'vertices' and 'faces'")
            vertices, faces = data.get("vertices"), data.get("faces")
        if not isinstance(vertices, list) or not vertices:
            raise ValueError("vertices must be a non-empty list of [x, y, z]")
        if len(vertices) > MAX_VERTICES:
            raise ValueError(f"too many vertices ({len(vertices)}; max {MAX_VERTICES})")
        s = float(scale)
        verts = []
        for i, v in enumerate(vertices):
            if not isinstance(v, (list, tuple)) or len(v) != 3:
                raise ValueError(f"vertex {i} must be [x, y, z]")
            verts.append((float(v[0]) * s, float(v[1]) * s, float(v[2]) * s))
        faces = faces or []
        if not isinstance(faces, list):
            raise ValueError("faces must be a list of vertex index lists")
        n = len(verts)
        clean_faces = []
        for i, f in enumerate(faces):
            if not isinstance(f, (list, tuple)) or len(f) < 3:
                raise ValueError(f"face {i} needs at least 3 vertex indices")
            idx = [int(k) for k in f]
            if any(k < 0 or k >= n for k in idx):
                raise ValueError(f"face {i} references a vertex index out of range 0..{n - 1}")
            if len(set(idx)) != len(idx):
                raise ValueError(f"face {i} repeats a vertex index")
            clean_faces.append(idx)
        obj = _make_object(name, verts, clean_faces, _collection(collection),
                           _parent(parent), _vec3(location, "location"), smooth)
        out = {"created": [_summary(obj)]}
        if src_path:
            out["source"] = src_path
        return out

    @command("extrude_polygons")
    def extrude_polygons(self, polygons=None, height=None, base_z=0.0, source=None,
                         name="Prisms", merge=True, collection=None, parent=None,
                         scale=1.0, origin=None, location=None, bus_dir=None):
        """Extrude 2D polygon rings (outer + holes) into closed prisms."""
        src_path = None
        if source:
            data, src_path = _read_json_source(source, bus_dir)
            if isinstance(data, dict):
                polygons = data.get("polygons")
                height = data.get("height", height)
                base_z = data.get("base_z", base_z)
            else:
                polygons = data
        if not isinstance(polygons, list) or not polygons:
            raise ValueError("polygons must be a non-empty list")
        if len(polygons) > MAX_POLYGONS:
            raise ValueError(f"too many polygons ({len(polygons)}; max {MAX_POLYGONS})")
        s = float(scale)
        if s <= 0:
            raise ValueError("scale must be positive")
        org = (0.0, 0.0)
        if origin is not None:
            if not isinstance(origin, (list, tuple)) or len(origin) != 2:
                raise ValueError("origin must be [x, y]")
            org = (float(origin[0]), float(origin[1]))

        specs = [pg.parse_polygon_spec(p, i) for i, p in enumerate(polygons)]
        built = []
        total_verts = 0
        for i, sp in enumerate(specs):
            h = sp.get("height", height)
            if h is None:
                raise ValueError(f"polygon {i}: no height (pass height, or height per polygon)")
            z0 = float(sp.get("base_z", base_z)) * s
            z1 = z0 + float(h) * s
            outer = pg.transform_ring(sp["outer"], org, s)
            holes = [pg.transform_ring(r, org, s) for r in sp["holes"]]
            try:
                verts, faces = pg.build_prism(outer, holes, z0, z1, _tessellate)
            except ValueError as e:
                raise ValueError(f"polygon {i}: {e}") from None
            total_verts += len(verts)
            if total_verts > MAX_VERTICES:
                raise ValueError(f"too many vertices (over {MAX_VERTICES})")
            built.append((sp.get("name") or f"{name}_{i:03d}", verts, faces, bool(sp["holes"])))

        coll = _collection(collection)
        par = _parent(parent)
        loc = _vec3(location, "location")
        if merge:
            all_verts, all_faces = [], []
            for _n, verts, faces, _h in built:
                off = len(all_verts)
                all_verts.extend(verts)
                all_faces.extend(tuple(k + off for k in f) for f in faces)
            objs = [_make_object(name, all_verts, all_faces, coll, par, loc, False)]
        else:
            objs = [_make_object(n, v, f, coll, par, loc, False) for n, v, f, _h in built]

        summaries = [_summary(o) for o in objs]
        bad = [s_["object"] for s_ in summaries if not s_["closed"]]
        out = {
            "prisms": len(built),
            "with_holes": sum(1 for b in built if b[3]),
            "objects": summaries if len(summaries) <= 50 else summaries[:50],
            "object_count": len(summaries),
            "all_closed": not bad,
        }
        if len(summaries) > 50:
            out["objects_note"] = f"first 50 of {len(summaries)} objects listed"
        if bad:
            out["warning"] = f"{len(bad)} object(s) are not closed manifold solids: {bad[:10]}"
        if src_path:
            out["source"] = src_path
        return out
