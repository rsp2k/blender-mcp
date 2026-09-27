"""Pure helpers for Poly Haven texturing (no bpy, so they unit-test anywhere).

Poly Haven's texture map keys vary in case and naming ("Diffuse", "Rough",
"nor_gl", "nor_dx", "arm", "AO", "Displacement"), and its /info and /assets
responses carry ``dimensions`` as [width_mm, height_mm] for textures (HDRIs
have none). These functions turn that into material roles and real-world
mapping scales.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence

POLYHAVEN_DISABLED_HINT = (
    "Poly Haven is switched off in the Blender MCP addon. Ask the user to "
    "enable 'Use assets from Poly Haven' (Edit > Preferences > Add-ons > "
    "Blender MCP, or the BlenderMCP tab in the 3D Viewport sidebar). It takes "
    "effect immediately; no reconnect is needed. blender_search_polyhaven_assets "
    "and blender_polyhaven_info also work without it."
)

# Files in /files/<id> that are not texture images.
NON_IMAGE_KEYS = {"blend", "gltf", "mtlx", "usd", "fbx"}


def classify_map(name: str) -> str | None:
    """Map a Poly Haven map key (or an image-name suffix) to a material role.

    Returns one of: base, rough, metal, normal_gl, normal_dx, normal, disp,
    ao, arm, or None for keys that aren't image maps.
    """
    key = (name or "").strip().lower()
    if key in NON_IMAGE_KEYS:
        return None
    if key in ("diffuse", "color", "col", "albedo", "basecolor", "base_color", "diff"):
        return "base"
    if key in ("rough", "roughness"):
        return "rough"
    if key in ("metal", "metallic", "metalness"):
        return "metal"
    if key in ("nor_gl", "gl", "normal_gl"):
        return "normal_gl"
    if key in ("nor_dx", "dx", "normal_dx"):
        return "normal_dx"
    if key in ("nor", "normal"):
        return "normal"
    if key in ("displacement", "disp", "height"):
        return "disp"
    if key in ("ao", "ambientocclusion", "ambient_occlusion"):
        return "ao"
    if key == "arm":
        return "arm"
    return None


def pick_maps(names: Iterable[str]) -> dict[str, str]:
    """Choose one source map per role from available keys.

    Prefers the OpenGL normal map (Blender's convention) over DirectX, and a
    dedicated map over the packed ARM channel (callers use ARM only for roles
    that have no dedicated map).
    """
    by_role: dict[str, str] = {}
    for n in names:
        role = classify_map(n)
        if role and role not in by_role:
            by_role[role] = n
    if "normal_gl" in by_role:
        by_role.setdefault("normal_pick", by_role["normal_gl"])
    elif "normal" in by_role:
        by_role.setdefault("normal_pick", by_role["normal"])
    elif "normal_dx" in by_role:
        by_role.setdefault("normal_pick", by_role["normal_dx"])
    return by_role


def dimensions_m(info: dict | None) -> tuple[float, float] | None:
    """Real-world texture size in metres from a /info or /assets entry, or None."""
    if not isinstance(info, dict):
        return None
    dims = info.get("dimensions")
    if not isinstance(dims, (list, tuple)) or len(dims) < 2:
        return None
    try:
        w, h = float(dims[0]), float(dims[1])
    except (TypeError, ValueError):
        return None
    if w <= 0 or h <= 0:
        return None
    return (round(w / 1000.0, 4), round(h / 1000.0, 4))


def mapping_scale(
    tile_size_m: Sequence[float] | float,
    object_scale: Sequence[float] = (1.0, 1.0, 1.0),
) -> tuple[float, float, float]:
    """Mapping-node Scale that tiles a texture at ``tile_size_m`` in world units.

    With Object texture coordinates, a coordinate of 1.0 is one unit of the
    object's *local* space, which the object's scale stretches in the world.
    To repeat every ``tile`` world metres, the mapping scale per axis is
    object_scale / tile. With world (Geometry.Position) coordinates pass
    object_scale=(1, 1, 1). A single number means a square tile; a pair is
    (width, height) and the height is reused for the third box-projection axis.
    """
    if isinstance(tile_size_m, (int, float)):
        tw = th = float(tile_size_m)
    else:
        vals = [float(v) for v in tile_size_m]
        if not vals:
            raise ValueError("tile_size_m is empty")
        tw = vals[0]
        th = vals[1] if len(vals) > 1 else vals[0]
    if tw <= 0 or th <= 0:
        raise ValueError("tile_size_m must be positive")
    sx, sy, sz = (float(v) for v in object_scale)
    # Box projection samples each face with two of the three axes; using the
    # tile height for Z keeps vertical faces (walls) at true height.
    return (sx / tw, sy / tw, sz / th)


def _unit(v: Sequence[float]) -> tuple[float, float, float]:
    x, y, z = (float(c) for c in v)
    n = math.sqrt(x * x + y * y + z * z)
    if n == 0:
        raise ValueError("zero-length direction")
    return (x / n, y / n, z / n)


def normal_direction(spec) -> tuple[tuple[float, float, float], str | None]:
    """Resolve a normal rule to (direction, mode). mode 'side' means horizontal."""
    if isinstance(spec, str):
        s = spec.strip().lower()
        if s in ("up", "top", "+z"):
            return (0.0, 0.0, 1.0), None
        if s in ("down", "bottom", "-z"):
            return (0.0, 0.0, -1.0), None
        if s in ("side", "sides", "wall", "walls", "horizontal"):
            return (0.0, 0.0, 1.0), "side"
        raise ValueError(f"unknown normal '{spec}'; use up, down, side, or [x, y, z]")
    return _unit(spec), None


def normal_matches(face_normal: Sequence[float], spec, tolerance_deg: float = 30.0) -> bool:
    """Whether a world-space face normal satisfies a normal rule.

    'side' matches faces whose normal is within ``tolerance_deg`` of the
    horizontal plane (walls); other rules match within ``tolerance_deg`` of
    the given direction.
    """
    direction, mode = normal_direction(spec)
    try:
        n = _unit(face_normal)
    except ValueError:
        return False
    dot = n[0] * direction[0] + n[1] * direction[1] + n[2] * direction[2]
    if mode == "side":
        # angle from horizontal = asin(|n.z|)
        return math.degrees(math.asin(min(1.0, abs(dot)))) <= tolerance_deg
    return math.degrees(math.acos(max(-1.0, min(1.0, dot)))) <= tolerance_deg


def height_matches(center_z: float, min_z: float | None, max_z: float | None) -> bool:
    """Whether a face centre's world Z lies inside an optional [min_z, max_z] band."""
    if min_z is not None and center_z < float(min_z):
        return False
    return max_z is None or center_z <= float(max_z)
