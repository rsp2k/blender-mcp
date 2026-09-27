"""File, scene-cleanup, interior-face and audited glTF export tools.

Thin dispatch wrappers over the addon's FileSceneHandlersMixin. Covers
model-home gaps #5 (save/open), #16 (startup leftovers in the scene), #17
(interior skins left by self-unions) and #27 (the glTF exporter silently
dropping or approximating colours).
"""

from __future__ import annotations

import json

from fastmcp import Context
from fastmcp.contrib.mcp_mixin import MCPMixin, mcp_tool

from .dispatch_component import (
    DEFAULT_TIMEOUT_S,
    TIMEOUT_LONG,
    TIMEOUT_MEDIUM,
    BlenderDispatchComponent,
)
from .object_storage import output_name

INTERIOR_METHODS = ("faces", "shells")
GLTF_FORMATS = ("glb", "gltf")
DEFAULT_KINDS = ("cube", "light", "camera")


def _err(error: str, **extra) -> str:
    return json.dumps({"status": "error", "error": error, **extra})


def _blend_path(path) -> str:
    if not isinstance(path, str) or not path.strip():
        raise ValueError("path is required")
    if not path.strip().lower().endswith(".blend"):
        raise ValueError(f"not a .blend path: {path!r}")
    return path.strip()


class BlenderFileSceneComponent(MCPMixin):
    """save/open/revert/new file, startup defaults, new scene, interior, glTF."""

    _call = BlenderDispatchComponent._call

    @mcp_tool()
    async def save_file(
        self,
        path: str | None = None,
        copy: bool = False,
        compress: bool = False,
        create_dirs: bool = False,
        target_uuid: str | None = None,
        _timeout: float = TIMEOUT_MEDIUM,
        bus_id: str | None = None,
        ctx: Context = None,
    ) -> str:
        """Save the open .blend: to its own path, or to ``path`` on the Blender machine.

        With ``path`` the open file becomes that file, unless ``copy`` is true,
        which writes a copy and keeps working in the current file. A file that
        has never been saved needs a ``path``. Missing folders are an error
        unless ``create_dirs``. Returns the saved path, size and file state.
        """
        try:
            if path is not None:
                path = _blend_path(path)
        except ValueError as e:
            return _err("invalid_argument", detail=str(e))
        return await self._call(
            ctx, "save_file",
            {"path": path, "copy": copy, "compress": compress, "create_dirs": create_dirs},
            target_uuid, _timeout, bus_id=bus_id,
        )

    @mcp_tool()
    async def open_file(
        self,
        path: str,
        discard_unsaved: bool = False,
        load_ui: bool = False,
        target_uuid: str | None = None,
        _timeout: float = TIMEOUT_MEDIUM,
        bus_id: str | None = None,
        ctx: Context = None,
    ) -> str:
        """Open a .blend on the Blender machine, replacing the current file.

        Refuses when the current file has unsaved changes, so work isn't lost
        without asking; save it first or pass ``discard_unsaved``. The reply
        arrives after the file has loaded, with the scenes and object count of
        the newly open file. ``load_ui`` also loads the file's window layout.
        """
        try:
            path = _blend_path(path)
        except ValueError as e:
            return _err("invalid_argument", detail=str(e))
        return await self._call(
            ctx, "open_file",
            {"path": path, "discard_unsaved": discard_unsaved, "load_ui": load_ui},
            target_uuid, _timeout, bus_id=bus_id,
        )

    @mcp_tool()
    async def revert_file(
        self,
        discard_unsaved: bool = False,
        target_uuid: str | None = None,
        _timeout: float = TIMEOUT_MEDIUM,
        bus_id: str | None = None,
        ctx: Context = None,
    ) -> str:
        """Reload the open file from disk, dropping changes since the last save.

        Refuses when there are unsaved changes unless ``discard_unsaved`` is
        true (reverting is exactly the act of discarding them, so say so).
        """
        return await self._call(
            ctx, "revert_file", {"discard_unsaved": discard_unsaved},
            target_uuid, _timeout, bus_id=bus_id,
        )

    @mcp_tool()
    async def new_file(
        self,
        empty: bool = True,
        discard_unsaved: bool = False,
        target_uuid: str | None = None,
        _timeout: float = TIMEOUT_MEDIUM,
        bus_id: str | None = None,
        ctx: Context = None,
    ) -> str:
        """Start a new file. ``empty`` (default) gives a scene with no objects;
        ``empty=false`` loads the user's startup file (usually cube, light and
        camera; the reply lists any startup defaults found). Refuses to drop
        unsaved changes unless ``discard_unsaved``.
        """
        return await self._call(
            ctx, "new_file", {"empty": empty, "discard_unsaved": discard_unsaved},
            target_uuid, _timeout, bus_id=bus_id,
        )

    @mcp_tool()
    async def scene_defaults(
        self,
        remove: bool = False,
        kinds: list[str] | None = None,
        target_uuid: str | None = None,
        _timeout: float = DEFAULT_TIMEOUT_S,
        bus_id: str | None = None,
        ctx: Context = None,
    ) -> str:
        """Find Blender's startup cube, light and camera left in the scene.

        Startup leftovers are easy to miss: the default 1000 W point light
        once sat above a model and read as a specular glint for four
        diagnostic renders. Each candidate is reported as untouched or with
        what's different (moved, mesh edited, power changed, has children, and
        so on); names alone never decide. ``remove`` deletes only the
        untouched ones, limited to ``kinds`` (cube, light, camera); anything
        modified is kept and listed. Run it before building into a scene.
        """
        if kinds is not None:
            if isinstance(kinds, str):
                kinds = [kinds]
            bad = [k for k in kinds if k not in DEFAULT_KINDS]
            if bad:
                return _err("invalid_argument", detail=f"unknown kinds {bad}; use {list(DEFAULT_KINDS)}")
        return await self._call(
            ctx, "scene_defaults", {"remove": remove, "kinds": kinds},
            target_uuid, _timeout, bus_id=bus_id,
        )

    @mcp_tool()
    async def new_scene(
        self,
        name: str = "Scene",
        make_active: bool = True,
        copy_world: bool = True,
        target_uuid: str | None = None,
        _timeout: float = DEFAULT_TIMEOUT_S,
        bus_id: str | None = None,
        ctx: Context = None,
    ) -> str:
        """Add an empty scene to the open file (no startup objects) and switch to it.

        ``copy_world`` reuses the current scene's World so lighting and
        background carry over. In a Blender without windows (background mode)
        the scene is created but can't be made active; the reply says so.
        """
        if not isinstance(name, str) or not name.strip():
            return _err("invalid_argument", detail="name is required")
        return await self._call(
            ctx, "new_scene", {"name": name, "make_active": make_active, "copy_world": copy_world},
            target_uuid, _timeout, bus_id=bus_id,
        )

    @mcp_tool()
    async def remove_interior(
        self,
        object: str,
        method: str = "faces",
        dry_run: bool = False,
        target_uuid: str | None = None,
        _timeout: float = TIMEOUT_MEDIUM,
        bus_id: str | None = None,
        ctx: Context = None,
    ) -> str:
        """Clean up interior skins left after unions: flip inside-out faces and
        delete buried ones.

        ``method="faces"`` (default) tests each face: a point just off each
        side is checked against every closed shell of the mesh. Outside in
        front and inside behind is a correct face (kept); inside in front and
        outside behind is an inside-out face (flipped); inside on both sides is
        buried (deleted). ``method="shells"`` deletes whole shells that sit
        entirely inside another shell. Only closed shells count as solid, so
        open sheets are never used to decide what's inside. ``dry_run``
        reports what would change without changing anything. Always reports
        counts, sample face indices, and mesh health before and after.
        """
        if method not in INTERIOR_METHODS:
            return _err("invalid_argument", detail=f"method must be one of {list(INTERIOR_METHODS)}")
        if not isinstance(object, str) or not object:
            return _err("invalid_argument", detail="object must be an object name")
        return await self._call(
            ctx, "remove_interior", {"object": object, "method": method, "dry_run": dry_run},
            target_uuid, _timeout, bus_id=bus_id,
        )

    @mcp_tool()
    async def export_gltf(
        self,
        path: str,
        objects: list[str] | str | None = None,
        format: str = "glb",
        audit: bool = True,
        auto_fix: bool = False,
        create_dirs: bool = False,
        store: bool = False,
        target_uuid: str | None = None,
        _timeout: float = TIMEOUT_LONG,
        bus_id: str | None = None,
        ctx: Context = None,
    ) -> str:
        """Export glTF (``glb`` or ``gltf``) on the Blender machine, listing
        materials whose colour won't survive the export.

        ``store`` (glb only): also upload the file to object storage; the
        result's ``stored`` entry has ``object_key`` and ``download_url``.

        The exporter gives no warning: a non-Principled surface (e.g. Diffuse
        BSDF) exports as an empty white material, and a Principled Base Color
        fed by a node chain is approximated (one input of a Mix, a flat grey,
        or the texture without its Hue/Saturation step). Constants, RGB nodes,
        Image Textures and image-times-constant multiplies export correctly.
        ``material_issues`` lists each problem material with the reason, and for
        GLB the reply also reads the written file back
        (``materials_without_color_in_file``). ``auto_fix`` temporarily swaps
        plain Diffuse BSDFs to an equivalent Principled for the export only,
        then restores them; node chains still need baking to an image.
        ``objects`` limits the export to those objects (default: visible
        objects in the scene).
        """
        fmt = str(format).lower()
        if fmt not in GLTF_FORMATS:
            return _err("invalid_argument", detail=f"format must be one of {list(GLTF_FORMATS)}")
        if not isinstance(path, str) or not path.strip():
            return _err("invalid_argument", detail="path is required")
        if store and fmt != "glb":
            return _err("invalid_argument",
                        detail="store needs format=glb (a .gltf export is several files)")
        if isinstance(objects, str):
            objects = [objects]
        return await self._call(
            ctx, "export_gltf",
            {"path": path.strip(), "objects": objects, "format": fmt, "audit": audit,
             "auto_fix": auto_fix, "create_dirs": create_dirs},
            target_uuid, _timeout, bus_id=bus_id,
            store_as=output_name(path.strip(), "export.glb") if store else None,
        )
