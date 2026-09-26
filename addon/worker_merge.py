"""Merge named collections from a background worker's result into the live scene.

Phase 2 of background workers: instead of reloading the whole result file
(which discards every edit made since the snapshot), append only the named
collections and swap them in where the old ones sat. Edits elsewhere in
the scene survive.

``merge_collections`` is the one entry point; the Merge operator and the
canary both call it. bpy is imported inside functions so the pure helpers
(banner text, name handling) can be unit-tested outside Blender.
"""

from __future__ import annotations

import os
import re

MODES = ("replace", "add")
_SUFFIX = re.compile(r"^(.*)\.\d{3}$")

# ID collections whose members an append can bring in and a replace can
# orphan. Removal order matters: users first, then what they use.
_ID_TYPES = (
    "objects", "collections", "meshes", "curves", "materials", "node_groups",
    "images", "textures", "lights", "cameras", "armatures", "lattices",
    "metaballs", "grease_pencils", "actions",
)


def merge_banner_lines(pending: dict | None) -> list[str]:
    """Text for the merge consent banner; empty = no banner."""
    if not pending:
        return []
    names = ", ".join(pending.get("collections") or []) or "(none)"
    mode = pending.get("mode", "replace")
    msg = pending.get("message") or "a background job finished"
    lines = [f"Background result ready: {msg}"]
    verb = "Replace" if mode == "replace" else "Add"
    lines.append(f"{verb} collections: {names}")
    path = pending.get("path") or ""
    if path:
        lines.append(f"From: {os.path.basename(path)}")
    if mode == "replace":
        lines.append("These collections are replaced; edits elsewhere are kept.")
    return lines


def base_name(name: str) -> str:
    """'Attic.001' -> 'Attic'; names without a numeric suffix are unchanged."""
    m = _SUFFIX.match(name)
    return m.group(1) if m else name


def _ids_by_pointer(bpy) -> dict[str, dict[int, object]]:
    out = {}
    for attr in _ID_TYPES:
        coll = getattr(bpy.data, attr, None)
        if coll is not None:
            out[attr] = {x.as_pointer(): x for x in coll}
    return out


def _subtree(coll) -> list:
    """coll and all its descendant collections."""
    out, stack = [], [coll]
    while stack:
        c = stack.pop()
        out.append(c)
        stack.extend(c.children)
    return out


def _parents_of(bpy, coll) -> list:
    """Collections (including scene master collections) that list coll as a child."""
    parents = [c for c in bpy.data.collections if coll.name in c.children]
    for scene in bpy.data.scenes:
        if coll.name in scene.collection.children:
            parents.append(scene.collection)
    return parents


def _dependencies(objs) -> list:
    """Datablocks the given objects use directly (data, materials, action)."""
    deps = []
    for ob in objs:
        data = getattr(ob, "data", None)
        if data is not None:
            deps.append(data)
            for slot in getattr(data, "materials", []) or []:
                if slot is not None:
                    deps.append(slot)
        for slot in ob.material_slots:
            if slot.material is not None:
                deps.append(slot.material)
        ad = getattr(ob, "animation_data", None)
        if ad is not None and ad.action is not None:
            deps.append(ad.action)
    return deps


def _remove_ids(bpy, ids) -> int:
    """Remove the given datablocks if nothing else uses them. Returns count removed."""
    removed = 0
    for idb in ids:
        try:
            if idb.users != 0:
                continue
            coll = getattr(bpy.data, _collection_attr(bpy, idb), None)
            if coll is not None:
                coll.remove(idb)
                removed += 1
        except (ReferenceError, RuntimeError, KeyError):
            continue
    return removed


def _collection_attr(bpy, idb) -> str:
    rna = idb.bl_rna.identifier
    return {
        "Mesh": "meshes", "Curve": "curves", "Material": "materials",
        "Object": "objects", "Collection": "collections", "Image": "images",
        "Light": "lights", "Camera": "cameras", "Armature": "armatures",
        "Lattice": "lattices", "MetaBall": "metaballs", "Action": "actions",
        "Texture": "textures", "GreasePencil": "grease_pencils",
    }.get(rna, "")


def _delete_subtree(bpy, coll) -> tuple[int, int]:
    """Delete a collection subtree and the objects that live only in it.

    Objects also linked into collections outside the subtree are kept
    (just unlinked). Then the datablocks those deleted objects used are
    removed if nothing else uses them, which limits the purge to what this
    merge orphaned; the user's unrelated orphans are untouched.
    Returns (objects_deleted, datablocks_purged).
    """
    colls = _subtree(coll)
    names = {c.name for c in colls}
    own_objs, shared = [], []
    for c in colls:
        for ob in c.objects:
            if all(uc.name in names for uc in ob.users_collection):
                if ob not in own_objs:
                    own_objs.append(ob)
            elif ob not in shared:
                shared.append(ob)
    deps = _dependencies(own_objs)
    for c in colls:
        for ob in list(c.objects):
            if ob in shared:
                c.objects.unlink(ob)
    n_obj = len(own_objs)
    for ob in own_objs:
        bpy.data.objects.remove(ob, do_unlink=True)
    for c in reversed(colls):
        bpy.data.collections.remove(c)
    purged = 0
    for _ in range(2):  # meshes free their materials on the first pass
        purged += _remove_ids(bpy, deps)
        deps = [d for d in deps if _alive(d)]
    return n_obj, purged


def _alive(idb) -> bool:
    try:
        idb.name  # noqa: B018 - raises ReferenceError once removed
        return True
    except ReferenceError:
        return False


def _restore_names(bpy, new_ids) -> list[str]:
    """Strip .NNN suffixes Blender added on append when the base name is now free."""
    renamed = []
    for attr, items in new_ids.items():
        coll = getattr(bpy.data, attr)
        for idb in items:
            if not _alive(idb):
                continue
            base = base_name(idb.name)
            if base != idb.name and coll.get(base) is None:
                old = idb.name
                idb.name = base
                renamed.append(f"{attr}:{old}->{idb.name}")
    return renamed


def merge_collections(path: str, collections: list[str], mode: str = "replace") -> dict:
    """Append ``collections`` from ``path`` and swap them into the live scene.

    mode="replace": each appended collection is linked under the same
    parents as the live collection of the same name, then the old one (and
    objects that lived only in it) is deleted and the new one takes its
    name. mode="add": appended collections are linked alongside, keeping
    any .NNN suffix Blender gave them.

    A collection missing from the result is skipped and reported; nothing
    is deleted for it. If appending fails, everything appended is removed
    again and the live scene is unchanged.
    """
    import bpy

    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    if not path.lower().endswith(".blend") or not os.path.isfile(path):
        raise FileNotFoundError(f"{path} isn't a .blend file on this machine")
    names = [n for n in dict.fromkeys(collections or []) if n]
    if not names:
        raise ValueError("collections must name at least one collection")

    result: dict = {"path": path, "mode": mode, "replaced": [], "added": [],
                    "skipped": [], "renamed": [], "purged": 0, "ok": True}

    with bpy.data.libraries.load(path, link=False) as (src, _dst):
        available = set(src.collections)
    wanted = [n for n in names if n in available]
    for n in names:
        if n not in available:
            result["skipped"].append({"name": n, "reason": "not in result file"})
    if not wanted:
        return result

    before = _ids_by_pointer(bpy)
    try:
        with bpy.data.libraries.load(path, link=False) as (_src, dst):
            dst.collections = list(wanted)
        appended = list(dst.collections)
    except Exception as e:  # noqa: BLE001 - reported, scene rolled back below
        appended, err = [], str(e)
    else:
        err = None
    after = _ids_by_pointer(bpy)
    new_ids = {attr: [after[attr][p] for p in after[attr] if p not in before.get(attr, {})]
               for attr in after}

    if err is not None or len(appended) != len(wanted) or any(c is None for c in appended):
        _rollback(bpy, new_ids)
        result["ok"] = False
        result["error"] = err or "some collections failed to append"
        result["skipped"] += [{"name": n, "reason": "append failed"} for n in wanted]
        return result

    scene = bpy.context.scene
    for name, new_coll in zip(wanted, appended):
        old = bpy.data.collections.get(name)
        if old is not None and old == new_coll:
            old = None  # no live collection had this name; the append kept it
        parents = _parents_of(bpy, old) if old is not None else []
        if not parents:
            parents = [scene.collection]
        for parent in parents:
            if new_coll.name not in parent.children:
                parent.children.link(new_coll)
        entry = {"name": name, "parents": [p.name for p in parents],
                 "new_objects": len(new_coll.all_objects)}
        if mode == "replace" and old is not None:
            entry["old_objects"] = len(old.all_objects)
            n_obj, purged = _delete_subtree(bpy, old)
            entry["deleted_objects"] = n_obj
            result["purged"] += purged
            new_coll.name = name
            result["replaced"].append(entry)
        else:
            entry["final_name"] = new_coll.name
            result["added"].append(entry)

    if mode == "replace":
        result["renamed"] = _restore_names(bpy, new_ids)
    return result


def _rollback(bpy, new_ids: dict) -> None:
    """Remove everything an append brought in (users before what they use)."""
    for attr in ("objects", "collections", *[a for a in _ID_TYPES
                                             if a not in ("objects", "collections")]):
        coll = getattr(bpy.data, attr, None)
        for idb in new_ids.get(attr, []):
            try:
                if _alive(idb):
                    coll.remove(idb)
            except (ReferenceError, RuntimeError):
                pass
