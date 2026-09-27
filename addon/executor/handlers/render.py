"""View framing, renders, orthographic elevations and image comparison.

set_view frames the 3D viewport on objects at a chosen angle. render uses a
named, scene or temporary camera. render_view places a temporary ortho camera
sized to the subject so elevation checks come back as a tight PNG.
compare_images stacks, overlays or diffs two images on the Blender host.
"""

from __future__ import annotations

import os
import time

import bpy
import mathutils

from ... import render_geometry as geo
from ...screenshot_paths import default_screenshot_path
from ..registry import command
from .view_controls import _get_object, _require_view3d, _vec3

ENGINE_ALIASES = {
    "EEVEE": ("BLENDER_EEVEE_NEXT", "BLENDER_EEVEE"),
    "BLENDER_EEVEE": ("BLENDER_EEVEE_NEXT", "BLENDER_EEVEE"),
    "BLENDER_EEVEE_NEXT": ("BLENDER_EEVEE_NEXT", "BLENDER_EEVEE"),
    "WORKBENCH": ("BLENDER_WORKBENCH",),
    "BLENDER_WORKBENCH": ("BLENDER_WORKBENCH",),
    "CYCLES": ("CYCLES",),
}


def _resolve_engine(scene, name: str) -> str:
    """Map a friendly engine name onto whatever this Blender registers.

    Blender 4.2-4.x calls EEVEE 'BLENDER_EEVEE_NEXT'; 5.x calls it
    'BLENDER_EEVEE' again. Engines register dynamically, so try each.
    """
    key = str(name or "").strip().upper()
    candidates = ENGINE_ALIASES.get(key)
    if candidates is None:
        raise ValueError(f"engine must be EEVEE, WORKBENCH or CYCLES, got {name!r}")
    previous = scene.render.engine
    for cand in candidates:
        try:
            scene.render.engine = cand
            return cand
        except TypeError:
            continue
        finally:
            scene.render.engine = previous
    raise ValueError(f"engine {name!r} isn't available in this Blender")


def _world_corners(obj) -> list[tuple]:
    mw = obj.matrix_world
    return [tuple(mw @ mathutils.Vector(c)) for c in obj.bound_box]


def _frame_objects(names) -> list:
    """Objects to frame: the named ones, else every visible mesh-like object."""
    # matrix_world is stale until the depsgraph updates, so bounds read
    # straight after a scripted move/scale (same job) would be wrong.
    bpy.context.view_layer.update()
    if names:
        if isinstance(names, str):
            names = [names]
        return [_get_object(n) for n in names]
    kinds = {"MESH", "CURVE", "SURFACE", "META", "FONT", "GREASEPENCIL", "POINTCLOUD", "VOLUME"}
    objs = [
        o for o in bpy.context.view_layer.objects
        if o.type in kinds and o.visible_get()
    ]
    if not objs:
        raise ValueError("nothing visible to frame; pass object names")
    return objs


def _output_path(filepath, fmt="png") -> str:
    if filepath:
        os.makedirs(os.path.dirname(os.path.abspath(filepath)), exist_ok=True)
        return filepath
    return default_screenshot_path(fmt, base_dir=bpy.app.tempdir or None)


def _camera_rotation(basis) -> mathutils.Quaternion:
    m = mathutils.Matrix((basis["right"], basis["up"], basis["back"])).transposed()
    return m.to_quaternion()


class _RenderSettings:
    """Snapshot and restore the scene settings a render touches."""

    def __init__(self, scene):
        self.scene = scene
        r = scene.render
        self.saved = {
            "engine": r.engine,
            "res": (r.resolution_x, r.resolution_y, r.resolution_percentage),
            "film_transparent": r.film_transparent,
            "filepath": r.filepath,
            "file_format": r.image_settings.file_format,
            "color_mode": r.image_settings.color_mode,
            "camera": scene.camera,
            "eevee_samples": getattr(scene.eevee, "taa_render_samples", None),
            "cycles_samples": getattr(getattr(scene, "cycles", None), "samples", None),
            "world_color": tuple(scene.world.color) if scene.world else None,
            "workbench_bg": scene.display.shading.background_type,
        }

    def restore(self):
        s, r, v = self.scene, self.scene.render, self.saved
        r.engine = v["engine"]
        r.resolution_x, r.resolution_y, r.resolution_percentage = v["res"]
        r.film_transparent = v["film_transparent"]
        r.filepath = v["filepath"]
        r.image_settings.file_format = v["file_format"]
        r.image_settings.color_mode = v["color_mode"]
        s.camera = v["camera"]
        if v["eevee_samples"] is not None:
            s.eevee.taa_render_samples = v["eevee_samples"]
        if v["cycles_samples"] is not None:
            s.cycles.samples = v["cycles_samples"]
        if v["world_color"] is not None and s.world:
            s.world.color = v["world_color"]
        s.display.shading.background_type = v["workbench_bg"]


def _new_temp_camera(scene, name="blendermcp_temp_camera"):
    data = bpy.data.cameras.new(name)
    obj = bpy.data.objects.new(name, data)
    scene.collection.objects.link(obj)
    return obj


def _remove_temp_camera(obj):
    data = obj.data
    bpy.data.objects.remove(obj, do_unlink=True)
    if data is not None and data.users == 0:
        bpy.data.cameras.remove(data)


def _render_to(scene, path: str) -> dict:
    r = scene.render
    r.filepath = path
    r.image_settings.file_format = "PNG"
    r.image_settings.color_mode = "RGBA" if r.film_transparent else "RGB"
    started = time.monotonic()
    bpy.ops.render.render(write_still=True)
    if not os.path.exists(path):
        raise RuntimeError(f"render finished but no file at {path}")
    width = int(r.resolution_x * r.resolution_percentage / 100)
    height = int(r.resolution_y * r.resolution_percentage / 100)
    return {"filepath": path, "width": width, "height": height,
            "seconds": round(time.monotonic() - started, 2)}


def _load_rgba(path: str):
    """Image file -> float32 array (height, width, 4), rows bottom-up."""
    import numpy as np

    if not os.path.exists(path):
        raise ValueError(f"image not found: {path}")
    img = bpy.data.images.load(path, check_existing=False)
    try:
        w, h = img.size
        if w == 0 or h == 0:
            raise ValueError(f"could not read image: {path}")
        arr = np.empty(w * h * 4, dtype=np.float32)
        img.pixels.foreach_get(arr)
        return arr.reshape(h, w, 4)
    finally:
        bpy.data.images.remove(img)


def _resize(arr, scale: float):
    """Nearest-neighbour resize; deterministic and dependency-free."""
    import numpy as np

    if abs(scale - 1.0) < 1e-9:
        return arr
    h, w = arr.shape[:2]
    nh, nw = max(1, round(h * scale)), max(1, round(w * scale))
    ys = np.minimum((np.arange(nh) / scale).astype(int), h - 1)
    xs = np.minimum((np.arange(nw) / scale).astype(int), w - 1)
    return arr[ys][:, xs]


def _flatten_on_white(arr):
    import numpy as np

    out = arr.copy()
    alpha = out[..., 3:4]
    out[..., :3] = out[..., :3] * alpha + (1.0 - alpha)
    out[..., 3] = 1.0
    return out.astype(np.float32)


def _save_rgba(arr, path: str) -> None:
    import numpy as np

    h, w = arr.shape[:2]
    img = bpy.data.images.new("blendermcp_compose", width=w, height=h, alpha=True)
    try:
        img.pixels.foreach_set(np.ascontiguousarray(arr, dtype=np.float32).ravel())
        img.filepath_raw = path
        img.file_format = "PNG"
        img.save()
    finally:
        bpy.data.images.remove(img)


def crop_image_file(path: str, box) -> tuple[int, int]:
    """Crop a PNG in place to ``box`` (x0, y0, x1, y1), y=0 at the bottom."""
    arr = _load_rgba(path)
    x0, y0, x1, y1 = box
    _save_rgba(arr[y0:y1, x0:x1], path)
    return x1 - x0, y1 - y0


def _redraw(window, area, region) -> None:
    """Draw the region now so region_3d's matrices match the current view.

    window_matrix and perspective_matrix are only recomputed when the region
    draws, and a bus job that changes the view runs between redraws.
    """
    try:
        with bpy.context.temp_override(window=window, area=area, region=region):
            bpy.ops.wm.redraw_timer(type="DRAW_WIN_SWAP", iterations=1)
    except Exception as e:  # noqa: BLE001 - a stale matrix beats failing the call
        print(f"[BlenderMCP] viewport redraw failed: {e}")


def _view_selected(objs, window, area, region) -> None:
    """Frame objs with view3d.view_selected, leaving the selection as it was."""
    view_layer = bpy.context.view_layer
    prev_selected = [o for o in view_layer.objects if o.select_get()]
    prev_active = view_layer.objects.active
    try:
        for o in prev_selected:
            o.select_set(False)
        for o in objs:
            o.select_set(True)
        with bpy.context.temp_override(window=window, area=area, region=region):
            bpy.ops.view3d.view_selected(use_all_regions=False)
    finally:
        for o in view_layer.objects:
            if o.select_get() and o not in prev_selected:
                o.select_set(False)
        for o in prev_selected:
            try:
                o.select_set(True)
            except RuntimeError:
                pass
        view_layer.objects.active = prev_active


def viewport_bbox(obj_names, margin: float = 0.05):
    """Projected pixel bbox of objects in the first 3D viewport, or None."""
    window, area, space, region = _require_view3d()[0]
    rv3d = space.region_3d
    if not bpy.app.background:
        _redraw(window, area, region)
    points = [p for o in _frame_objects(obj_names) for p in _world_corners(o)]
    matrix = [list(row) for row in rv3d.perspective_matrix]
    return geo.project_bbox(points, matrix, (region.width, region.height), margin)


class RenderHandlersMixin:
    """set_view, render, render_view, compare_images."""

    @command("set_view")
    def set_view(self, frame=None, angle=None, perspective=None, shading=None,
                 all_viewports: bool = False, margin: float = 0.05):
        """Frame the 3D viewport on objects at an angle.

        frame: object names, or None for every visible object. angle: a preset
        (front, back, left, right, top, bottom, iso), [yaw, elevation]
        degrees, or None to keep the current rotation. perspective: PERSP or
        ORTHO. shading: a type name (SOLID, MATERIAL, ...) or a dict of
        set_viewport_shading arguments. margin: free space kept around the
        subject, as a fraction of its projected size per side.
        """
        yaw_el = geo.resolve_angle(angle)
        if perspective is not None:
            perspective = str(perspective).upper()
            if perspective not in ("PERSP", "ORTHO"):
                raise ValueError("perspective must be PERSP or ORTHO")
        objs = _frame_objects(frame)
        targets = _require_view3d(all_viewports)

        for _window, _area, space, _region in targets:
            rv3d = space.region_3d
            if rv3d.view_perspective == "CAMERA":
                rv3d.view_perspective = "PERSP"
            if yaw_el is not None:
                rv3d.view_rotation = mathutils.Euler(geo.view_euler(*yaw_el), "XYZ").to_quaternion()
            if perspective is not None:
                rv3d.view_perspective = perspective

        framed, skipped = [], []
        for o in objs:
            (framed if o.visible_get() else skipped).append(o)
        if not framed:
            raise ValueError(
                f"none of the objects can be framed (hidden?): {[o.name for o in skipped]}")
        points = [p for o in framed for p in _world_corners(o)]

        fits = []
        for window, area, space, region in targets:
            if bpy.app.background:
                # No drawn region, so no valid window matrix to fit against.
                _view_selected(framed, window, area, region)
                continue
            # view3d.view_selected sizes the view from the largest bbox side,
            # not the projected silhouette, so a box seen corner-on overflows
            # (the bottom of the default cube was cut off). Fit against the
            # region's own projection instead (closed form, see fit_view).
            _redraw(window, area, region)  # refresh window_matrix after persp/ortho changes
            rv3d = space.region_3d
            rot3 = [list(row) for row in rv3d.view_rotation.to_matrix()]
            window_matrix = [list(row) for row in rv3d.window_matrix]
            ortho = rv3d.view_perspective == "ORTHO"
            fit = geo.fit_view(
                points, window_matrix, rot3, margin=margin, ortho=ortho, ref_distance=rv3d.view_distance,
            )
            rv3d.view_location = fit["location"]
            rv3d.view_distance = fit["distance"]
            _redraw(window, area, region)
            fits.append({"contained": fit["contained"]})
        skipped = [o.name for o in skipped]
        framed = [o.name for o in framed]

        shading_result = None
        if shading:
            kwargs = dict(shading) if isinstance(shading, dict) else {"type": shading}
            kwargs.pop("all_viewports", None)
            shading_result = self.set_viewport_shading(all_viewports=all_viewports, **kwargs)

        rv3d = targets[0][2].region_3d
        return {
            "framed": framed,
            "skipped": skipped,
            "angle": list(yaw_el) if yaw_el else None,
            "perspective": rv3d.view_perspective,
            "view_distance": rv3d.view_distance,
            "viewports": len(targets),
            "fit": fits or None,
            "shading": shading_result,
        }

    @command("render")
    def render(self, camera=None, camera_pos=None, look_at=None, lens=None,
               engine: str = "EEVEE", resolution=None, samples=None, filepath=None,
               transparent: bool = False):
        """Render a still to a PNG on this machine and return its path.

        Camera: camera_pos (+ optional look_at, default the scene's visible
        bounds centre, and lens) uses a temporary camera; else ``camera``
        by name; else the scene camera. Scene render settings are restored
        afterwards.
        """
        scene = bpy.context.scene
        eng = _resolve_engine(scene, engine)
        settings = _RenderSettings(scene)
        temp = None
        try:
            if camera_pos is not None:
                pos = _vec3(camera_pos, "camera_pos")
                if look_at is not None:
                    target = _vec3(look_at, "look_at")
                else:
                    bmin, bmax = geo.union_bounds(_world_corners(o) for o in _frame_objects(None))
                    target = mathutils.Vector([(a + b) / 2 for a, b in zip(bmin, bmax)])
                basis = geo.look_at_basis(tuple(pos), tuple(target))
                temp = _new_temp_camera(scene)
                temp.location = pos
                temp.rotation_mode = "QUATERNION"
                temp.rotation_quaternion = _camera_rotation(basis)
                if lens:
                    temp.data.lens = float(lens)
                scene.camera = temp
            elif camera is not None:
                cam = _get_object(camera)
                if cam.type != "CAMERA":
                    raise ValueError(f"{camera!r} is a {cam.type}, not a camera")
                scene.camera = cam
            elif scene.camera is None:
                raise ValueError("scene has no camera; pass camera or camera_pos")
            used_camera = scene.camera.name if temp is None else None

            r = scene.render
            r.engine = eng
            if resolution:
                r.resolution_x, r.resolution_y = int(resolution[0]), int(resolution[1])
                r.resolution_percentage = 100
            r.film_transparent = bool(transparent)
            if samples:
                if eng == "CYCLES":
                    scene.cycles.samples = int(samples)
                elif "EEVEE" in eng:
                    scene.eevee.taa_render_samples = int(samples)
            out = _render_to(scene, _output_path(filepath))
        finally:
            settings.restore()
            if temp is not None:
                _remove_temp_camera(temp)
        return {**out, "engine": eng, "camera": used_camera or "temporary",
                "transparent": bool(transparent)}

    @command("render_view")
    def render_view(self, axis: str = "front", objects=None, size: int = 1024,
                    resolution=None, margin: float = 0.05, engine: str = "WORKBENCH",
                    background=None, isolate: bool = False, filepath=None):
        """Orthographic elevation of objects along an axis, as a tight PNG.

        A temporary ortho camera is sized to the subject's world bounds; with
        no ``resolution`` the image takes the subject's aspect, ``size``
        pixels on the long edge. background: None/"transparent", or [r, g, b]
        (0-1, used by the Workbench engine via the world colour). isolate
        hides every other object from the render.
        """
        if size <= 0:
            raise ValueError("size must be positive")
        scene = bpy.context.scene
        eng = _resolve_engine(scene, engine)
        objs = _frame_objects(objects)
        points = [p for o in objs for p in _world_corners(o)]
        fit = geo.ortho_camera_fit(points, axis, margin=margin,
                                   resolution=tuple(resolution) if resolution else None,
                                   long_edge=int(size))
        settings = _RenderSettings(scene)
        temp = _new_temp_camera(scene, "blendermcp_ortho_camera")
        hidden = []
        try:
            temp.data.type = "ORTHO"
            temp.data.ortho_scale = fit["ortho_scale"]
            temp.data.sensor_fit = "AUTO"
            temp.data.clip_start = fit["clip_start"]
            temp.data.clip_end = fit["clip_end"]
            temp.location = fit["location"]
            temp.rotation_mode = "QUATERNION"
            temp.rotation_quaternion = _camera_rotation(fit)
            scene.camera = temp

            r = scene.render
            r.engine = eng
            r.resolution_x, r.resolution_y = fit["resolution"]
            r.resolution_percentage = 100
            if background in (None, "transparent"):
                r.film_transparent = True
            else:
                rgb = [float(c) for c in background][:3]
                r.film_transparent = False
                if scene.world is not None:
                    scene.world.color = rgb
                scene.display.shading.background_type = "WORLD"

            if isolate:
                keep = {o.name for o in objs} | {temp.name}
                for o in scene.objects:
                    if o.name not in keep and not o.hide_render and o.type not in ("LIGHT",):
                        o.hide_render = True
                        hidden.append(o)
            out = _render_to(scene, _output_path(filepath))
        finally:
            for o in hidden:
                o.hide_render = False
            settings.restore()
            _remove_temp_camera(temp)
        w, h, d = fit["subject_size"]
        return {**out, "engine": eng, "axis": str(axis).lower(),
                "objects": [o.name for o in objs],
                "ortho_scale": fit["ortho_scale"],
                "subject_size": [w, h, d], "margin": margin}

    @command("compare_images")
    def compare_images(self, render_path: str, reference_path: str, mode: str = "overlay",
                       opacity: float = 0.5, fit: str = "width", scale: float = 1.0,
                       offset=None, filepath=None):
        """Compose a render and a reference drawing into one PNG.

        mode: stack (render above the reference), overlay (reference drawn
        over the render at ``opacity``) or diff (per-pixel absolute
        difference, brighter = more different). The reference is scaled by
        ``fit`` (width, height, contain, none) to the render, then by
        ``scale``, then shifted by ``offset`` [x, y] pixels (x right, y up).
        """
        import numpy as np

        mode = str(mode).lower()
        if mode not in ("stack", "overlay", "diff"):
            raise ValueError("mode must be stack, overlay or diff")
        if not 0.0 <= float(opacity) <= 1.0:
            raise ValueError("opacity must be between 0 and 1")
        if scale <= 0:
            raise ValueError("scale must be positive")
        base = _flatten_on_white(_load_rgba(render_path))
        ref = _flatten_on_white(_load_rgba(reference_path))
        bh, bw = base.shape[:2]
        rh, rw = ref.shape[:2]
        factor = geo.fit_scale((rw, rh), (bw, bh), fit) * float(scale)
        ref = _resize(ref, factor)

        if mode == "stack":
            width = max(bw, ref.shape[1])
            canvas = np.ones((bh + ref.shape[0], width, 4), dtype=np.float32)
            # Rows are bottom-up: the reference goes at the bottom rows,
            # the render above it.
            canvas[: ref.shape[0], : ref.shape[1]] = ref
            canvas[ref.shape[0]:, :bw] = base
            out_arr = canvas
        else:
            dx, dy = (int(v) for v in (offset or (0, 0)))
            placed = np.ones_like(base)
            mask = np.zeros(base.shape[:2], dtype=bool)
            ys0, xs0 = max(0, dy), max(0, dx)
            ys1 = min(bh, dy + ref.shape[0])
            xs1 = min(bw, dx + ref.shape[1])
            if ys1 > ys0 and xs1 > xs0:
                placed[ys0:ys1, xs0:xs1] = ref[ys0 - dy: ys1 - dy, xs0 - dx: xs1 - dx]
                mask[ys0:ys1, xs0:xs1] = True
            if mode == "overlay":
                out_arr = base.copy()
                a = float(opacity)
                out_arr[mask, :3] = base[mask, :3] * (1 - a) + placed[mask, :3] * a
            else:
                diff = np.abs(base[..., :3] - placed[..., :3]).max(axis=2)
                out_arr = np.ones_like(base)
                out_arr[..., 0] = diff
                out_arr[..., 1] = diff
                out_arr[..., 2] = diff
                out_arr[..., 3] = 1.0
        path = _output_path(filepath)
        _save_rgba(out_arr, path)
        stats = {}
        if mode == "diff":
            stats = {"mean_difference": round(float(out_arr[..., 0].mean()), 4),
                     "max_difference": round(float(out_arr[..., 0].max()), 4)}
        return {"filepath": path, "mode": mode, "width": int(out_arr.shape[1]),
                "height": int(out_arr.shape[0]), "reference_scale": round(factor, 4), **stats}
