"""Pure decision logic for the file/scene tools (no bpy import).

Kept separate from executor/handlers/files_scene.py so CI can unit-test the
rules without Blender: which startup objects count as untouched defaults,
what the glTF exporter can't read in a material, and what to do with a face
given which of its two sides are inside the solid.
"""

from __future__ import annotations

import math

# Blender 5.2 factory startup scene, measured with --factory-startup.
DEFAULT_CUBE_CORNERS = {(x, y, z) for x in (-1.0, 1.0) for y in (-1.0, 1.0) for z in (-1.0, 1.0)}
DEFAULT_LIGHT_LOCATION = (4.0762, 1.0055, 5.9039)
DEFAULT_LIGHT_ENERGY = 1000.0
DEFAULT_CAMERA_LOCATION = (7.3589, -6.9258, 4.9583)
DEFAULT_CAMERA_LENS = 50.0
_TOL = 1e-3


def _close(a, b, tol: float = _TOL) -> bool:
    return len(a) == len(b) and all(math.isclose(x, y, abs_tol=tol) for x, y in zip(a, b))


def _base_name(name: str) -> str:
    """'Cube.001' -> 'Cube'. Names alone never decide; they only pick the rule."""
    head, dot, tail = name.rpartition(".")
    return head if dot and tail.isdigit() else name


def default_object_reasons(props: dict) -> tuple[str | None, list[str]]:
    """Classify one object against the factory startup defaults.

    ``props`` is a plain description of the object (see the handler for the
    keys). Returns ``(kind, mismatches)``: kind is "cube", "light" or "camera"
    when the name and type pick a rule (else None), and mismatches lists every
    way it differs from the untouched default. An object is an untouched
    default only when kind is set and mismatches is empty.
    """
    base, otype = _base_name(props.get("name", "")), props.get("type")
    common = []
    if props.get("parent"):
        common.append("has a parent")
    if props.get("children"):
        common.append("has children")
    if props.get("modifiers"):
        common.append("has modifiers")
    if not _close(props.get("scale", ()), (1.0, 1.0, 1.0)):
        common.append("scaled")

    if base == "Cube" and otype == "MESH":
        m = list(common)
        if not _close(props.get("location", ()), (0.0, 0.0, 0.0)):
            m.append("moved")
        if not _close(props.get("rotation", ()), (0.0, 0.0, 0.0)):
            m.append("rotated")
        corners = {tuple(round(c, 4) for c in v) for v in props.get("vertices", [])}
        if props.get("face_count") != 6 or corners != DEFAULT_CUBE_CORNERS:
            m.append("mesh edited")
        if props.get("materials") not in ([], ["Material"]):
            m.append("materials changed")
        return "cube", m
    if base == "Light" and otype == "LIGHT":
        m = list(common)
        if props.get("light_type") != "POINT":
            m.append("light type changed")
        if not math.isclose(props.get("energy") or 0.0, DEFAULT_LIGHT_ENERGY, abs_tol=_TOL):
            m.append("power changed")
        if not _close(props.get("location", ()), DEFAULT_LIGHT_LOCATION):
            m.append("moved")
        return "light", m
    if base == "Camera" and otype == "CAMERA":
        m = list(common)
        if not math.isclose(props.get("lens") or 0.0, DEFAULT_CAMERA_LENS, abs_tol=_TOL):
            m.append("lens changed")
        if not _close(props.get("location", ()), DEFAULT_CAMERA_LOCATION):
            m.append("moved")
        return "camera", m
    return None, []


# --- glTF material audit ------------------------------------------------------

def gltf_material_issue(info: dict) -> str | None:
    """Why the glTF exporter would lose or misstate this material's colour.

    Returns None when the exported colour will match. ``info`` describes the
    material as the handler walked it:
      use_nodes: bool
      surface: node type linked to the active Material Output's Surface, or None
      base_color_source: for Principled, the node type feeding Base Color, or None
      mix_multiply_image: the Base Color source is a MULTIPLY Mix of one Image
                          Texture and a constant, with an unlinked factor
      image_missing: an Image Texture on that path has no image, or its file
                     is gone and it isn't packed
    Measured against Blender 5.2's exporter by reading the written GLB: a
    non-Principled surface exports as an empty material; a Principled Base
    Color survives as a constant, an RGB node, an Image Texture, or an image
    multiplied by a constant. Any other chain is approximated without a
    warning: a two-colour Mix exports one of its inputs, a Math-driven Mix
    exports flat grey, a Hue/Saturation step is dropped from the texture.
    """
    if not info.get("use_nodes"):
        return None  # exporter uses the material's viewport colour
    surface = info.get("surface")
    if surface is None:
        return "nothing is connected to the Material Output surface"
    if surface != "BSDF_PRINCIPLED":
        return (f"surface shader is {surface}; the glTF exporter only reads Principled BSDF "
                "and exports this as an empty white material")
    src = info.get("base_color_source")
    if src is None or src == "RGB":
        return None  # constant or RGB node exports as baseColorFactor
    if src == "TEX_IMAGE" or (src == "MIX" and info.get("mix_multiply_image")):
        if info.get("image_missing"):
            return "Base Color image texture has no image or its file is missing"
        return None
    return (f"Base Color comes through a {src} node chain; the exporter can't reproduce it "
            "and writes an approximation (one input, a flat grey, or the bare texture), so "
            "the exported colour won't match the render")


# --- unsaved work -----------------------------------------------------------------

def dirty_refusal(is_dirty: bool, discard_unsaved: bool, action: str) -> str | None:
    """The refusal message when ``action`` would drop unsaved changes, else None."""
    if is_dirty and not discard_unsaved:
        return (f"The current file has unsaved changes; {action} would discard them. "
                "Save first (blender_save_file) or pass discard_unsaved=true.")
    return None


# --- interior faces ---------------------------------------------------------------

def face_action(out_inside: bool, in_inside: bool) -> str:
    """What to do with a face given which of its sides lies inside the solid.

    out_inside: a point just off the face along its normal is inside a closed shell.
    in_inside:  a point just off the face against its normal is inside.
      outside/inside -> "keep"   (a correct exterior face)
      inside/outside -> "flip"   (exterior face whose normal points inward)
      inside/inside  -> "delete" (buried: an interior skin or duplicate wall)
      outside/outside-> "keep"   (a sheet; nothing encloses it, leave it alone)
    """
    if out_inside and in_inside:
        return "delete"
    if out_inside and not in_inside:
        return "flip"
    return "keep"
