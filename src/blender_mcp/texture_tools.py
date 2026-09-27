"""Real-world-scaled Poly Haven materials, per-face assignment, HDRI control.

Dispatch wrappers over the addon's make_pbr_material, assign_material and
set_world_hdri commands (addon/executor/handlers/polyhaven.py), plus
polyhaven_info, which reads Poly Haven directly and needs no Blender.
Arguments are checked here so a typo fails before the bus round trip.
"""

from __future__ import annotations

import json

from fastmcp import Context
from fastmcp.contrib.mcp_mixin import MCPMixin, mcp_tool

from . import polyhaven_api
from .dispatch_component import (
    DEFAULT_TIMEOUT_S,
    BlenderDispatchComponent,
)

COORDINATES = ("object", "world", "uv")
NORMAL_WORDS = ("up", "top", "+z", "down", "bottom", "-z", "side", "sides", "wall", "walls", "horizontal")


def _err(error: str, **extra) -> str:
    return json.dumps({"status": "error", "error": error, **extra})


def normalize_tile(tile_size_m) -> list[float] | None:
    """None, a number (square tile) or [width, height], all positive metres."""
    if tile_size_m is None:
        return None
    vals = [tile_size_m] if isinstance(tile_size_m, (int, float)) else list(tile_size_m)
    if not 1 <= len(vals) <= 2:
        raise ValueError("tile_size_m is a number or [width, height] in metres")
    out = [float(v) for v in vals]
    if any(v <= 0 for v in out):
        raise ValueError("tile_size_m must be positive")
    return out if len(out) == 2 else [out[0], out[0]]


def normalize_normal(normal):
    """A direction word or an [x, y, z] vector; None means no normal rule."""
    if normal is None:
        return None
    if isinstance(normal, str):
        if normal.strip().lower() not in NORMAL_WORDS:
            raise ValueError("normal is up, down, side, or an [x, y, z] direction")
        return normal.strip().lower()
    vec = [float(v) for v in normal]
    if len(vec) != 3 or not any(vec):
        raise ValueError("normal vector must be three numbers, not all zero")
    return vec


class BlenderTextureComponent(MCPMixin):
    """make_pbr_material, assign_material, set_world_hdri, polyhaven_info."""

    _call = BlenderDispatchComponent._call

    @mcp_tool()
    async def polyhaven_info(self, asset_id: str) -> str:
        """Look up a Poly Haven asset: name, type, categories, and real size.

        ``dimensions_m`` is [width, height] of one texture tile in metres
        (textures only; HDRIs and most models have none). Reads the public
        API directly, so it works without Blender and whether or not the
        addon's Poly Haven switch is on.
        """
        if not isinstance(asset_id, str) or not asset_id.strip():
            return _err("invalid_argument", detail="asset_id is required")
        return await polyhaven_api.server_side("info", asset_id=asset_id.strip())

    @mcp_tool()
    async def make_pbr_material(
        self,
        texture_id: str,
        name: str | None = None,
        tile_size_m: float | list[float] | None = None,
        coordinates: str = "object",
        object_name: str | None = None,
        displacement_scale: float = 0.02,
        target_uuid: str | None = None,
        _timeout: float = DEFAULT_TIMEOUT_S,
        bus_id: str | None = None,
        ctx: Context = None,
    ) -> str:
        """Build a material from downloaded Poly Haven maps at real-world size.

        Download first with blender_download_polyhaven_asset(texture_id,
        'textures'). ``tile_size_m`` is how many metres one repeat covers: a
        number or [width, height]; by default it's the asset's Poly Haven
        dimensions (e.g. 3 m for brick_wall_001), else 1 m.

        ``coordinates``:
          - "object" (default): box projection on the object's own
            coordinates. Needs no UVs, the texture moves with the object, and
            ``object_name`` lets the mapping cancel that object's scale so
            bricks come out true size. Rebuild after rescaling the object.
          - "world": box projection on world position. One material can be
            shared by many objects and stays aligned across them (walls that
            meet at a corner), but the pattern stays put if an object moves.
          - "uv": the mesh's UV map, unscaled; real size depends on the unwrap.

        Normal (OpenGL), roughness, AO (multiplied into base colour),
        displacement and the packed ARM map are wired when present. Returns
        the material name, mapping scale and which map feeds what.
        """
        try:
            if coordinates not in COORDINATES:
                raise ValueError(f"coordinates must be one of {', '.join(COORDINATES)}")
            tile = normalize_tile(tile_size_m)
            if displacement_scale < 0:
                raise ValueError("displacement_scale must be >= 0")
        except (ValueError, TypeError) as e:
            return _err("invalid_argument", detail=str(e))
        return await self._call(
            ctx, "make_pbr_material",
            {
                "texture_id": texture_id, "name": name, "tile_size_m": tile,
                "coordinates": coordinates, "object_name": object_name,
                "displacement_scale": displacement_scale,
            },
            target_uuid, _timeout, bus_id=bus_id,
        )

    @mcp_tool()
    async def assign_material(
        self,
        object_name: str,
        material: str,
        faces: list[int] | None = None,
        normal: str | list[float] | None = None,
        normal_tolerance_deg: float = 30.0,
        min_z: float | None = None,
        max_z: float | None = None,
        slot: int | None = None,
        attribute: str | None = None,
        target_uuid: str | None = None,
        _timeout: float = DEFAULT_TIMEOUT_S,
        bus_id: str | None = None,
        ctx: Context = None,
    ) -> str:
        """Put an existing material on some faces of a mesh, not the whole thing.

        Adds a material slot when the object doesn't have this material yet
        and leaves other faces' materials alone. Face rules combine with AND;
        with no rule every face is assigned:
          - ``faces``: polygon indices
          - ``normal``: "up" (floors, roof tops), "down", "side" (walls:
            within ``normal_tolerance_deg`` of horizontal) or an [x, y, z]
            direction, in world space
          - ``min_z`` / ``max_z``: face centre height band in world units
            (e.g. a plinth course below 0.5 m)
          - ``slot``: faces currently using this slot index (swap one material)
          - ``attribute``: a boolean/int face attribute name, faces where true

        Object Mode only. Returns faces assigned and a per-material face
        count; matching nothing changes nothing. set_texture is unchanged
        and still replaces every material on the object.
        """
        try:
            nrm = normalize_normal(normal)
            if not 0 <= normal_tolerance_deg <= 180:
                raise ValueError("normal_tolerance_deg must be between 0 and 180")
            if min_z is not None and max_z is not None and min_z > max_z:
                raise ValueError("min_z is above max_z")
            if faces is not None and any(int(i) < 0 for i in faces):
                raise ValueError("face indices must be >= 0")
        except (ValueError, TypeError) as e:
            return _err("invalid_argument", detail=str(e))
        return await self._call(
            ctx, "assign_material",
            {
                "object_name": object_name, "material": material, "faces": faces,
                "normal": nrm, "normal_tolerance_deg": normal_tolerance_deg,
                "min_z": min_z, "max_z": max_z, "slot": slot, "attribute": attribute,
            },
            target_uuid, _timeout, bus_id=bus_id,
        )

    @mcp_tool()
    async def set_world_hdri(
        self,
        rotation_deg: float | None = None,
        strength: float | None = None,
        background_visible: bool | None = None,
        background_color: list[float] | None = None,
        target_uuid: str | None = None,
        _timeout: float = DEFAULT_TIMEOUT_S,
        bus_id: str | None = None,
        ctx: Context = None,
    ) -> str:
        """Adjust the world HDRI already in the scene. Omitted values stay as they are.

        ``rotation_deg`` turns the environment about Z (moves the sun and
        reflections). ``strength`` is the Background strength (1 = as shot).
        ``background_visible=False`` shows ``background_color`` (RGB 0-1,
        default near black) to the camera while the HDRI still lights and
        reflects in the scene; True restores it. Works in EEVEE and Cycles.
        Returns the resulting rotation, strength and visibility.
        """
        try:
            if strength is not None and strength < 0:
                raise ValueError("strength must be >= 0")
            if background_color is not None:
                if len(background_color) not in (3, 4):
                    raise ValueError("background_color is [r, g, b] in 0-1")
                background_color = [float(c) for c in background_color[:3]]
        except (ValueError, TypeError) as e:
            return _err("invalid_argument", detail=str(e))
        return await self._call(
            ctx, "set_world_hdri",
            {
                "rotation_deg": rotation_deg, "strength": strength,
                "background_visible": background_visible,
                "background_color": background_color,
            },
            target_uuid, _timeout, bus_id=bus_id,
        )
