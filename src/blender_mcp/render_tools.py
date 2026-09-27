"""View framing, renders, orthographic elevations and image comparison.

Thin dispatch wrappers over the addon's RenderHandlersMixin
(addon/executor/handlers/render.py). Every image path is on the Blender
host, not the caller's machine; omitted output paths go to a unique file in
Blender's temp dir, and each result says where the file landed.
"""

from __future__ import annotations

import json

from fastmcp import Context
from fastmcp.contrib.mcp_mixin import MCPMixin, mcp_tool

from .dispatch_component import (
    DEFAULT_TIMEOUT_S,
    TIMEOUT_LONG,
    BlenderDispatchComponent,
)
from .object_storage import output_name

# Mirrors addon/render_geometry.py (the installed server can't import addon/).
AXES = ("front", "back", "left", "right", "top", "bottom")
ANGLE_PRESETS = ("front", "back", "left", "right", "top", "bottom", "iso")
ENGINES = ("EEVEE", "WORKBENCH", "CYCLES", "BLENDER_EEVEE", "BLENDER_EEVEE_NEXT",
           "BLENDER_WORKBENCH")
COMPARE_MODES = ("stack", "overlay", "diff")
FITS = ("width", "height", "contain", "none")


def _err(error: str, **extra) -> str:
    return json.dumps({"status": "error", "error": error, **extra})


def _names(value) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise ValueError("expected an object name or a list of object names")
    return value or None


def check_angle(angle):
    if angle is None:
        return None
    if isinstance(angle, str):
        if angle.lower() not in ANGLE_PRESETS:
            raise ValueError(f"angle must be one of {', '.join(ANGLE_PRESETS)} or [yaw, elevation]")
        return angle.lower()
    if isinstance(angle, list) and len(angle) == 2 and all(isinstance(v, (int, float)) for v in angle):
        if not -90 <= angle[1] <= 90:
            raise ValueError("elevation must be between -90 and 90 degrees")
        return angle
    raise ValueError("angle must be a preset name or [yaw, elevation] degrees")


def check_engine(engine: str) -> str:
    e = str(engine or "").upper()
    if e not in ENGINES:
        raise ValueError("engine must be EEVEE, WORKBENCH or CYCLES")
    return e


def check_resolution(resolution):
    if resolution is None:
        return None
    if (not isinstance(resolution, list) or len(resolution) != 2
            or not all(isinstance(v, int) and v > 0 for v in resolution)):
        raise ValueError("resolution must be [width, height] positive integers")
    return resolution


class BlenderRenderComponent(MCPMixin):
    """set_view, render, render_view, compare_images."""

    _call = BlenderDispatchComponent._call

    @mcp_tool()
    async def set_view(
        self,
        frame: list[str] | str | None = None,
        angle: str | list[float] | None = None,
        perspective: str | None = None,
        shading: str | dict | None = None,
        all_viewports: bool = False,
        target_uuid: str | None = None,
        _timeout: float = DEFAULT_TIMEOUT_S,
        bus_id: str | None = None,
        ctx: Context = None,
    ) -> str:
        """Frame the 3D viewport on objects at a chosen angle, in one call.

        ``frame``: object names, or omit for every visible object.
        ``angle``: front, back, left, right, top, bottom, iso, or
        [yaw, elevation] in degrees (yaw 0 looks from -Y, 90 from +X;
        elevation is above the horizon), or omit to keep the current angle.
        ``perspective``: PERSP or ORTHO. ``shading``: a shading type (SOLID,
        MATERIAL, RENDERED, WIREFRAME) or a dict of blender_set_viewport_shading
        arguments. Pair with blender_get_viewport_screenshot (which can also
        frame and crop by itself). Needs a GUI viewport.
        """
        try:
            args = {"frame": _names(frame), "angle": check_angle(angle),
                    "all_viewports": all_viewports}
            if perspective is not None:
                p = str(perspective).upper()
                if p not in ("PERSP", "ORTHO"):
                    raise ValueError("perspective must be PERSP or ORTHO")
                args["perspective"] = p
            if shading is not None:
                args["shading"] = shading
        except ValueError as e:
            return _err("invalid_argument", detail=str(e))
        return await self._call(ctx, "set_view", args, target_uuid, _timeout, bus_id=bus_id)

    @mcp_tool()
    async def render(
        self,
        camera: str | None = None,
        camera_pos: list[float] | None = None,
        look_at: list[float] | None = None,
        lens: float | None = None,
        engine: str = "EEVEE",
        resolution: list[int] | None = None,
        samples: int | None = None,
        filepath: str | None = None,
        transparent: bool = False,
        store: bool = False,
        target_uuid: str | None = None,
        _timeout: float = TIMEOUT_LONG,
        bus_id: str | None = None,
        ctx: Context = None,
    ) -> str:
        """Render a still image to a PNG on the Blender host; returns its path.

        ``store``: also upload the PNG to object storage; the result's
        ``stored`` entry has an ``object_key`` and a short-lived
        ``download_url`` (see blender_create_download_url to re-sign).

        Camera, in order of precedence: ``camera_pos`` (world coordinates)
        aimed at ``look_at`` (default: the centre of the visible objects) with
        an optional ``lens`` in mm, using a temporary camera that is removed
        afterwards; else ``camera`` by name; else the scene camera.
        ``engine``: EEVEE, WORKBENCH (fast, flat shading) or CYCLES.
        ``resolution`` [w, h] and ``samples`` override the scene only for
        this render; all scene render settings are restored afterwards.
        ``filepath`` omitted: a unique file in Blender's temp dir.

        A render that outlives ``_timeout`` isn't lost: the call returns a
        job_id to follow with blender_job_status / blender_job_result. For
        renders you know are long, use blender_submit with command "render".
        """
        try:
            args = {"camera": camera, "engine": check_engine(engine),
                    "resolution": check_resolution(resolution), "transparent": transparent}
            if camera_pos is not None:
                if len(camera_pos) != 3:
                    raise ValueError("camera_pos must be [x, y, z]")
                args["camera_pos"] = camera_pos
            if look_at is not None:
                if len(look_at) != 3:
                    raise ValueError("look_at must be [x, y, z]")
                args["look_at"] = look_at
            if lens is not None:
                if lens <= 0:
                    raise ValueError("lens must be positive (mm)")
                args["lens"] = lens
            if samples is not None:
                if samples <= 0:
                    raise ValueError("samples must be positive")
                args["samples"] = samples
            if filepath:
                args["filepath"] = filepath
        except (ValueError, TypeError) as e:
            return _err("invalid_argument", detail=str(e))
        return await self._call(ctx, "render", args, target_uuid, _timeout, bus_id=bus_id,
                                store_as=output_name(filepath, "render.png") if store else None)

    @mcp_tool()
    async def render_view(
        self,
        axis: str = "front",
        objects: list[str] | str | None = None,
        size: int = 1024,
        resolution: list[int] | None = None,
        margin: float = 0.05,
        engine: str = "WORKBENCH",
        background: str | list[float] | None = None,
        isolate: bool = False,
        filepath: str | None = None,
        store: bool = False,
        target_uuid: str | None = None,
        _timeout: float = TIMEOUT_LONG,
        bus_id: str | None = None,
        ctx: Context = None,
    ) -> str:
        """Orthographic elevation (front/back/left/right/top/bottom) as a tight PNG.

        ``store``: also upload the PNG to object storage (result ``stored``
        has ``object_key`` and ``download_url``).

        A temporary orthographic camera is sized to the world bounds of
        ``objects`` (default: every visible object) and removed afterwards.
        With no ``resolution`` the image takes the subject's own aspect ratio,
        ``size`` pixels on its long edge, so the subject fills the frame
        except for ``margin`` (a fraction of its size) on every side. Good
        for checking a model against a drawing: pair with
        blender_compare_images. ``background``: omit for transparent, or
        [r, g, b] 0-1 (applied through the world colour, which the Workbench
        engine uses). ``isolate`` hides everything else from the render.
        The front view looks along +Y with +X right and +Z up, like
        Blender's numpad views.
        """
        try:
            if str(axis).lower() not in AXES:
                raise ValueError(f"axis must be one of {', '.join(AXES)}")
            if not isinstance(size, int) or size <= 0:
                raise ValueError("size must be a positive integer")
            if margin < 0:
                raise ValueError("margin must be >= 0")
            args = {"axis": str(axis).lower(), "objects": _names(objects), "size": size,
                    "resolution": check_resolution(resolution), "margin": margin,
                    "engine": check_engine(engine), "isolate": isolate}
            if background is not None and background != "transparent":
                if not (isinstance(background, list) and len(background) >= 3):
                    raise ValueError('background must be "transparent" or [r, g, b]')
                args["background"] = background
            if filepath:
                args["filepath"] = filepath
        except (ValueError, TypeError) as e:
            return _err("invalid_argument", detail=str(e))
        return await self._call(
            ctx, "render_view", args, target_uuid, _timeout, bus_id=bus_id,
            store_as=output_name(filepath, f"{args['axis']}.png") if store else None)

    @mcp_tool()
    async def compare_images(
        self,
        render_path: str,
        reference_path: str,
        mode: str = "overlay",
        opacity: float = 0.5,
        fit: str = "width",
        scale: float = 1.0,
        offset: list[int] | None = None,
        filepath: str | None = None,
        target_uuid: str | None = None,
        _timeout: float = DEFAULT_TIMEOUT_S,
        bus_id: str | None = None,
        ctx: Context = None,
    ) -> str:
        """Compose a render and a reference image (e.g. a plan drawing) into one PNG.

        Both paths are on the Blender host. ``mode``: stack (render above the
        reference), overlay (reference drawn over the render at ``opacity``)
        or diff (per-pixel absolute difference, brighter = more different;
        the result also reports mean and max difference). The reference is
        first scaled to the render by ``fit`` (width, height, contain, none),
        then by ``scale``, then moved by ``offset`` [x, y] pixels (x right,
        y up). Transparent areas are treated as white.
        """
        try:
            m = str(mode).lower()
            if m not in COMPARE_MODES:
                raise ValueError("mode must be stack, overlay or diff")
            f = str(fit).lower()
            if f not in FITS:
                raise ValueError("fit must be width, height, contain or none")
            if not 0 <= opacity <= 1:
                raise ValueError("opacity must be between 0 and 1")
            if scale <= 0:
                raise ValueError("scale must be positive")
            if offset is not None and (len(offset) != 2):
                raise ValueError("offset must be [x, y] pixels")
            args = {"render_path": render_path, "reference_path": reference_path, "mode": m,
                    "opacity": opacity, "fit": f, "scale": scale}
            if offset is not None:
                args["offset"] = offset
            if filepath:
                args["filepath"] = filepath
        except (ValueError, TypeError) as e:
            return _err("invalid_argument", detail=str(e))
        return await self._call(ctx, "compare_images", args, target_uuid, _timeout,
                                bus_id=bus_id)
