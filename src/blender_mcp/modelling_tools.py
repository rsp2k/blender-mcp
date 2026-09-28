"""Everyday modelling tools: primitives, plain colours, and a full object list.

Added after a chat battery on local models showed them building boxes from
hand-written vertices (and getting the centring wrong), reaching for Python
to make something red, and guessing at scenes with more than the ten objects
get_scene_info lists. The add-on side is addon/executor/handlers/modelling.py;
arguments are checked here first so a malformed call fails without a bus
round trip. Colour names are resolved in the add-on (addon/color_names.py),
which answers unknown names with suggestions.
"""

from __future__ import annotations

import json

from fastmcp import Context
from fastmcp.contrib.mcp_mixin import MCPMixin, mcp_tool

from .data_tools import _resolve
from .dispatch_component import DEFAULT_TIMEOUT_S, _dispatch

PRIMITIVE_KINDS = ("box", "cube", "cylinder", "cone", "sphere", "plane")
ANCHORS = ("center", "bottom")
MAX_LIST_LIMIT = 500


def _err(error: str, **extra) -> str:
    return json.dumps({"status": "error", "error": error, **extra})


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _xyz(value, what: str):
    if value is None:
        return None
    if not isinstance(value, (list, tuple)) or len(value) != 3 or not all(_num(v) for v in value):
        raise ValueError(f"{what} must be [x, y, z] numbers")
    return [float(v) for v in value]


def _positive(value, what: str):
    if value is None:
        return None
    if not _num(value) or value <= 0:
        raise ValueError(f"{what} must be a positive number (metres)")
    return float(value)


def primitive_params(kind, name=None, size=None, radius=None, depth=None, location=None,
                     anchor="center", rotation=None, segments=None, collection=None,
                     parent=None) -> dict:
    """Validated add_primitive dispatch params (raises ValueError)."""
    k = str(kind or "").strip().lower()
    if k not in PRIMITIVE_KINDS:
        raise ValueError(f"kind must be one of {', '.join(PRIMITIVE_KINDS)}")
    a = str(anchor or "center").strip().lower()
    a = "center" if a == "centre" else a
    if a not in ANCHORS:
        raise ValueError("anchor must be 'center' or 'bottom'")
    if size is not None:
        if _num(size):
            size = _positive(size, "size")
        elif (isinstance(size, (list, tuple)) and len(size) in (2, 3)
              and all(_num(v) for v in size)):
            if len(size) == 2 and k != "plane":
                raise ValueError("size must be [x, y, z] full extents (or one number)")
            if any(v <= 0 for v in size[:2]) or (len(size) == 3 and k != "plane" and size[2] <= 0):
                raise ValueError("size components must be positive (metres)")
            size = [float(v) for v in size]
        else:
            raise ValueError("size must be a number or [x, y, z] full extents in metres")
        if radius is not None or depth is not None:
            raise ValueError("pass either size (full extents) or radius/depth, not both")
    if k in ("box", "cube", "plane") and (radius is not None or depth is not None):
        raise ValueError(f"a {k} takes size ([x, y, z] full extents), not radius/depth")
    if k == "sphere" and depth is not None:
        raise ValueError("a sphere takes radius (or size), not depth")
    if segments is not None:
        if k not in ("cylinder", "cone", "sphere"):
            raise ValueError("segments only applies to cylinder, cone and sphere")
        if not isinstance(segments, int) or isinstance(segments, bool) or not 3 <= segments <= 256:
            raise ValueError("segments must be a whole number from 3 to 256")
    return {
        "kind": k, "name": name, "size": size,
        "radius": _positive(radius, "radius"), "depth": _positive(depth, "depth"),
        "location": _xyz(location, "location"), "anchor": a,
        "rotation": _xyz(rotation, "rotation"), "segments": segments,
        "collection": collection, "parent": parent,
    }


def color_params(objects, color, roughness=None, metallic=None, name=None) -> dict:
    """Validated set_color dispatch params (raises ValueError). Names are
    resolved by the add-on; only the shape is checked here."""
    if isinstance(objects, str):
        if not objects:
            raise ValueError("objects must name at least one object")
    elif not (isinstance(objects, list) and objects and all(isinstance(o, str) and o for o in objects)):
        raise ValueError("objects must be an object name, a list of names, or 'selected' / 'active'")
    if isinstance(color, str):
        if not color.strip():
            raise ValueError("color is empty")
    elif not (isinstance(color, (list, tuple)) and len(color) in (3, 4) and all(_num(c) for c in color)):
        raise ValueError("color must be a name ('red', 'warm white', 'brass'), a hex code, "
                         "or [r, g, b] from 0 to 1")
    for value, what in ((roughness, "roughness"), (metallic, "metallic")):
        if value is not None and (not _num(value) or not 0 <= value <= 1):
            raise ValueError(f"{what} must be a number from 0 to 1")
    return {"objects": objects, "color": list(color) if not isinstance(color, str) else color,
            "roughness": roughness, "metallic": metallic, "name": name}


MAX_NAME_CHARS = 255  # Blender caps names at 63 bytes; this only rejects nonsense


def scene_param(scene) -> str | None:
    """A scene name to inspect, or None for the active scene (raises ValueError)."""
    if scene is None:
        return None
    if not isinstance(scene, str):
        raise ValueError("scene must be a scene name (a string)")  # noqa: TRY004 - reported as invalid_argument
    name = scene.strip()
    if not name:
        return None
    if len(name) > MAX_NAME_CHARS:
        raise ValueError("scene name is too long")
    return scene


def list_params(type=None, name_contains=None, collection=None, limit=100, offset=0,
                scene=None) -> dict:
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_LIST_LIMIT:
        raise ValueError(f"limit must be a whole number from 1 to {MAX_LIST_LIMIT}")
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise ValueError("offset must be a whole number, 0 or more")
    params = {"type": type.upper() if isinstance(type, str) and type else None,
              "name_contains": name_contains or None, "collection": collection or None,
              "limit": limit, "offset": offset}
    name = scene_param(scene)
    if name is not None:
        # Only sent when given: an older add-on would drop it and answer for
        # the active scene, which its "scene" field then shows.
        params["scene"] = name
    return params


class BlenderModellingComponent(MCPMixin):
    """add_primitive, set_color, list_scene_objects."""

    async def _send(self, ctx, command, params, target_uuid, timeout, bus_id):
        bus, bus_id_str, user_id = await _resolve(ctx, f"blender_{command}", bus_id)
        if bus is None:
            return bus_id_str
        return await _dispatch(bus, bus_id_str, command, params, target_uuid, timeout,
                               caller_sub=user_id)

    @mcp_tool()
    async def add_primitive(
        self,
        kind: str,
        name: str | None = None,
        size: float | list[float] | None = None,
        radius: float | None = None,
        depth: float | None = None,
        location: list[float] | None = None,
        anchor: str = "center",
        rotation: list[float] | None = None,
        segments: int | None = None,
        collection: str | None = None,
        parent: str | None = None,
        target_uuid: str | None = None,
        _timeout: float = DEFAULT_TIMEOUT_S,
        bus_id: str | None = None,
        ctx: Context = None,
    ) -> str:
        """Add a box, cylinder, cone, sphere or plane with exact dimensions.

        Use this instead of writing vertices for simple shapes. Units are
        metres, Z is up. ``kind``: box (or cube), cylinder, cone, sphere,
        plane. Sizes are FULL extents, never half: ``size`` = [x, y, z] for a
        box (one number for a cube), [x, y] for a plane. Cylinder and cone
        take ``radius`` and ``depth`` (the height along Z); a sphere takes
        ``radius``. Round kinds may use ``size`` [x, y, z] instead to stretch
        them. ``anchor`` says what ``location`` means and becomes the
        object's origin: "center" (default) = the middle of the shape;
        "bottom" = the centre of its bottom face. Something standing on the
        floor at (x, y) is anchor="bottom", location=[x, y, 0]; to put it on
        top of another object, use that object's top z. ``rotation``:
        [rx, ry, rz] degrees. ``segments``: roundness for cylinder, cone and
        sphere (default 32). With ``parent``, location is in the parent's
        frame. Returns the world bounds (min/max/size) so you can check the
        result, and whether the mesh is a closed solid.
        """
        try:
            params = primitive_params(kind, name, size, radius, depth, location, anchor,
                                      rotation, segments, collection, parent)
        except ValueError as e:
            return _err("invalid_argument", detail=str(e))
        return await self._send(ctx, "add_primitive", params, target_uuid, _timeout, bus_id)

    @mcp_tool()
    async def set_color(
        self,
        objects: str | list[str],
        color: str | list[float],
        roughness: float | None = None,
        metallic: float | None = None,
        name: str | None = None,
        target_uuid: str | None = None,
        _timeout: float = DEFAULT_TIMEOUT_S,
        bus_id: str | None = None,
        ctx: Context = None,
    ) -> str:
        """Colour objects with a plain material: "make it red", "gold", "warm white quartz".

        ``objects``: an object name, a list of names, or "selected" /
        "active" for what the user has picked. ``color``: a colour name
        (any CSS colour such as "red", "navy", "tomato", or a material word:
        "warm white", "off white", "charcoal", "walnut", "oak", "gold",
        "brass", "copper", "chrome", "steel", "concrete", "quartz",
        "marble"...), a hex code like "#d8c39a", or [r, g, b] from 0 to 1 as
        in a colour picker. Metal words set metallic=1 with a fitting
        roughness; ``roughness`` and ``metallic`` (0-1) override that
        (defaults 0.5 and 0). Creates a Principled BSDF material (or reuses
        one with the same name and values; ``name`` picks the name) and
        makes it the object's only material. Unknown colour names come
        back with suggestions. For image textures use make_pbr_material.
        """
        try:
            params = color_params(objects, color, roughness, metallic, name)
        except ValueError as e:
            return _err("invalid_argument", detail=str(e))
        return await self._send(ctx, "set_color", params, target_uuid, _timeout, bus_id)

    @mcp_tool()
    async def list_scene_objects(
        self,
        type: str | None = None,
        name_contains: str | None = None,
        collection: str | None = None,
        limit: int = 100,
        offset: int = 0,
        scene: str | None = None,
        target_uuid: str | None = None,
        _timeout: float = DEFAULT_TIMEOUT_S,
        bus_id: str | None = None,
        ctx: Context = None,
    ) -> str:
        """List every object in a scene (get_scene_info shows only the first 10).

        ``scene``: a scene name to list without switching the user's active
        scene (default: the active one). get_scene_info names every scene.

        Each row: name, type, parent, collections, world location,
        dimensions (metres) and material names. Filters: ``type`` (MESH,
        LIGHT, CAMERA, EMPTY, CURVE...), ``name_contains`` (case-insensitive)
        and ``collection`` (includes its child collections). Sorted by name.
        Also returns ``total_objects`` and ``type_counts`` for the whole
        scene, and ``matching`` for the filtered set, so counting questions
        need one call. Page with ``offset``: when ``next_offset`` is not
        null, call again with offset=next_offset.
        """
        try:
            params = list_params(type, name_contains, collection, limit, offset, scene)
        except ValueError as e:
            return _err("invalid_argument", detail=str(e))
        return await self._send(ctx, "list_scene_objects", params, target_uuid, _timeout, bus_id)
