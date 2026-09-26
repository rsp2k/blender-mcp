"""Viewport, timeline and placement helpers.

Covers the calls a remote reviewer otherwise hand-writes through
execute_code on every session: stepping a marker-bound camera tour,
putting the viewport on a camera, placing objects in a parent's frame,
and flipping shading/overlay switches that pollute screenshots.
"""

from __future__ import annotations

import bpy
import mathutils

from ..registry import command

_SHADING_TYPES = ("WIREFRAME", "SOLID", "MATERIAL", "RENDERED")


def _view3d_targets(all_viewports: bool = False) -> list[tuple]:
    """(window, area, space, region) for 3D viewports across all windows.

    Walks window_manager.windows rather than context.screen because
    commands run from a bpy.app.timers callback, where context.screen
    can be None.
    """
    found = []
    wm = bpy.context.window_manager
    for window in getattr(wm, "windows", []):
        screen = window.screen
        if screen is None:
            continue
        for area in screen.areas:
            if area.type != "VIEW_3D":
                continue
            region = next((r for r in area.regions if r.type == "WINDOW"), None)
            found.append((window, area, area.spaces.active, region))
            if not all_viewports:
                return found
    return found


def _require_view3d(all_viewports: bool = False) -> list[tuple]:
    targets = _view3d_targets(all_viewports)
    if not targets:
        if bpy.app.background:
            raise RuntimeError("No 3D viewport in --background mode")
        raise RuntimeError("No 3D viewport open in any window")
    return targets


def _get_object(name: str):
    obj = bpy.data.objects.get(name)
    if obj is None:
        raise ValueError(f"Object not found: {name!r}")
    return obj


def _vec3(value, label: str) -> mathutils.Vector:
    try:
        vec = mathutils.Vector([float(v) for v in value])
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a list of 3 numbers, got {value!r}")
    if len(vec) != 3:
        raise ValueError(f"{label} must have exactly 3 components, got {len(vec)}")
    return vec


def _marker_camera_for_frame(scene, frame: int):
    """Mirror BKE_scene_camera_switch_find: the camera marker with the
    largest frame <= current frame, else the earliest camera marker."""
    markers = [m for m in scene.timeline_markers if m.camera is not None]
    if not markers:
        return None
    at_or_before = [m for m in markers if m.frame <= frame]
    if at_or_before:
        return max(at_or_before, key=lambda m: m.frame)
    return min(markers, key=lambda m: m.frame)


def _tag_view3d_redraw() -> None:
    for _w, area, _s, _r in _view3d_targets(all_viewports=True):
        area.tag_redraw()


class ViewControlHandlersMixin:
    """set_frame, look_through, place_object, world_from_local,
    set_viewport_shading, set_viewport_overlays."""

    @command("set_frame")
    def set_frame(self, frame: int, apply_markers: bool = True):
        """Jump to a frame and apply timeline-marker camera binding.

        scene.frame_set() evaluates the depsgraph but does not run the
        editor-level camera switch that binds markers to cameras; only
        the UI frame operators do, and those lag a step when scripted.
        So after frame_set we resolve the bound camera the same way
        Blender does and assign scene.camera directly.
        """
        scene = bpy.context.scene
        previous = scene.camera
        scene.frame_set(int(frame))

        marker = None
        if apply_markers:
            marker = _marker_camera_for_frame(scene, scene.frame_current)
            if marker is not None and scene.camera is not marker.camera:
                scene.camera = marker.camera
        _tag_view3d_redraw()

        return {
            "frame": scene.frame_current,
            "camera": scene.camera.name if scene.camera else None,
            "previous_camera": previous.name if previous else None,
            "switched": scene.camera is not previous,
            "marker": (
                {"name": marker.name, "frame": marker.frame} if marker else None
            ),
        }

    @command("look_through")
    def look_through(self, camera: str, lock: bool = True, shading: str = None):
        """Put the first 3D viewport into camera view through ``camera``.

        ``lock`` sets space.lock_camera ("Lock Camera to View"): while on,
        navigating the viewport moves the camera itself. ``shading``
        optionally sets the viewport shading type in the same call.
        """
        cam = _get_object(camera)
        if cam.type != "CAMERA":
            raise ValueError(f"{camera!r} is type {cam.type}, not CAMERA")
        if shading is not None and shading.upper() not in _SHADING_TYPES:
            raise ValueError(f"shading must be one of {list(_SHADING_TYPES)}")

        scene = bpy.context.scene
        scene.camera = cam
        _window, area, space, _region = _require_view3d()[0]
        space.region_3d.view_perspective = "CAMERA"
        space.lock_camera = bool(lock)
        if shading is not None:
            space.shading.type = shading.upper()
        area.tag_redraw()

        return {
            "camera": cam.name,
            "view_perspective": space.region_3d.view_perspective,
            "lock_camera": space.lock_camera,
            "shading": space.shading.type,
        }

    @command("place_object")
    def place_object(
        self,
        object: str,
        location,
        target=None,
        parent: str = None,
        frame: str = "parent",
        track_axis: str = "-Z",
        up_axis: str = "Y",
        lens: float = None,
    ):
        """Place ``object`` at ``location`` given in a reference frame.

        ``lens`` sets a camera's focal length in mm; the camera's existing
        lens is kept when omitted.

        frame="parent": location (and target) are in the parent's local
        coordinates; the parent is ``parent`` if given, else the object's
        current parent, else world. frame="world": raw world coordinates.

        ``parent`` also re-parents the object (parent inverse reset to
        identity, so its local location equals the parent-frame location).
        ``target`` orients the object to look at that point; the default
        track axis -Z with up Y is what cameras and lights expect.
        Without a target the current world rotation and scale are kept.
        """
        obj = _get_object(object)
        loc = _vec3(location, "location")
        tgt = _vec3(target, "target") if target is not None else None
        if frame not in ("parent", "world"):
            raise ValueError("frame must be 'parent' or 'world'")
        if lens is not None:
            if obj.type != 'CAMERA':
                raise ValueError(f"lens only applies to cameras; {obj.name!r} is {obj.type}")
            if lens <= 0:
                raise ValueError("lens must be positive (mm)")

        new_parent = _get_object(parent) if parent else None
        if new_parent is obj:
            raise ValueError("An object cannot be its own parent")
        ref = new_parent or obj.parent
        ref_matrix = (
            ref.matrix_world.copy()
            if frame == "parent" and ref is not None
            else mathutils.Matrix.Identity(4)
        )

        world_loc = ref_matrix @ loc
        _old_loc, rot, scale = obj.matrix_world.decompose()
        if tgt is not None:
            direction = (ref_matrix @ tgt) - world_loc
            if direction.length < 1e-9:
                raise ValueError("target coincides with location")
            rot = direction.to_track_quat(track_axis, up_axis)

        if new_parent is not None:
            obj.parent = new_parent
            obj.matrix_parent_inverse = mathutils.Matrix.Identity(4)

        obj.matrix_world = mathutils.Matrix.LocRotScale(world_loc, rot, scale)
        if lens is not None:
            obj.data.lens = float(lens)
        bpy.context.view_layer.update()

        result = {
            "object": obj.name,
            "parent": obj.parent.name if obj.parent else None,
            "world_location": list(obj.matrix_world.translation),
            "local_location": list(obj.location),
            "rotation_euler": list(obj.rotation_euler),
        }
        if obj.type == 'CAMERA':
            result["lens"] = obj.data.lens
        return result

    @command("world_from_local")
    def world_from_local(self, object: str, point, inverse: bool = False):
        """Convert a point between ``object``'s local frame and world.

        inverse=False: local -> world (matrix_world @ point).
        inverse=True:  world -> local (matrix_world.inverted() @ point).
        Pass the parent empty to convert model coordinates for children.
        """
        obj = _get_object(object)
        p = _vec3(point, "point")
        mw = obj.matrix_world
        result = (mw.inverted() @ p) if inverse else (mw @ p)
        return {
            "object": obj.name,
            "direction": "world_to_local" if inverse else "local_to_world",
            "input": list(p),
            "result": list(result),
        }

    @command("set_viewport_shading")
    def set_viewport_shading(
        self,
        type: str = None,
        scene_world: bool = None,
        scene_lights: bool = None,
        studio_light: str = None,
        raytracing: bool = None,
        all_viewports: bool = False,
    ):
        """Set 3D viewport shading; omitted arguments are left unchanged.

        scene_world / scene_lights apply to both Material Preview and
        Rendered modes (use_scene_world + use_scene_world_render), so the
        built-in studio HDRI stops showing up in reflective surfaces.
        raytracing toggles the scene's EEVEE ray tracing (a scene-level
        setting, not per-viewport). Returns the resulting state.
        """
        if type is not None and type.upper() not in _SHADING_TYPES:
            raise ValueError(f"type must be one of {list(_SHADING_TYPES)}")
        targets = _require_view3d(all_viewports)

        for _w, area, space, _r in targets:
            sh = space.shading
            if type is not None:
                sh.type = type.upper()
            if scene_world is not None:
                sh.use_scene_world = bool(scene_world)
                sh.use_scene_world_render = bool(scene_world)
            if scene_lights is not None:
                sh.use_scene_lights = bool(scene_lights)
                sh.use_scene_lights_render = bool(scene_lights)
            if studio_light is not None:
                try:
                    sh.studio_light = studio_light
                except TypeError as exc:
                    # Dynamic enum: Blender's own message lists the
                    # choices valid for the current shading.light mode.
                    raise ValueError(f"Unknown studio_light: {exc}")
            area.tag_redraw()

        eevee = getattr(bpy.context.scene, "eevee", None)
        rt_supported = eevee is not None and hasattr(eevee, "use_raytracing")
        if raytracing is not None:
            if not rt_supported:
                raise ValueError("EEVEE ray tracing is not available in this Blender")
            eevee.use_raytracing = bool(raytracing)

        sh = targets[0][2].shading
        return {
            "viewports": len(targets),
            "type": sh.type,
            "scene_world": sh.use_scene_world,
            "scene_world_render": sh.use_scene_world_render,
            "scene_lights": sh.use_scene_lights,
            "scene_lights_render": sh.use_scene_lights_render,
            "studio_light": sh.studio_light,
            "raytracing": eevee.use_raytracing if rt_supported else None,
            "render_engine": bpy.context.scene.render.engine,
        }

    @command("set_viewport_overlays")
    def set_viewport_overlays(self, flags: dict = None, all_viewports: bool = False):
        """Toggle View3DOverlay booleans by name, e.g.
        {"show_relationship_lines": false, "show_extras": false}.

        Unknown names are rejected before anything changes. An empty or
        missing ``flags`` just reports current values.
        """
        flags = flags or {}
        valid = sorted(
            p.identifier
            for p in bpy.types.View3DOverlay.bl_rna.properties
            if p.type == "BOOLEAN" and not p.is_readonly
        )
        unknown = [k for k in flags if k not in valid]
        if unknown:
            raise ValueError(f"Unknown overlay flag(s) {unknown}; valid: {valid}")

        targets = _require_view3d(all_viewports)
        for _w, area, space, _r in targets:
            for name, value in flags.items():
                setattr(space.overlay, name, bool(value))
            area.tag_redraw()

        overlay = targets[0][2].overlay
        common = (
            "show_overlays",
            "show_relationship_lines",
            "show_extras",
            "show_floor",
            "show_axis_x",
            "show_axis_y",
            "show_wireframes",
            "show_outline_selected",
            "show_object_origins",
            "show_cursor",
            "show_text",
            "show_stats",
        )
        state = {n: getattr(overlay, n) for n in common if hasattr(overlay, n)}
        state.update({n: getattr(overlay, n) for n in flags})
        return {"viewports": len(targets), "changed": list(flags), "state": state}
