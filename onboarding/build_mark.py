"""Build the Supported Systems mark (the seven-tower icon) as a Blender scene.

Geometry comes straight from https://supported.systems/images/logo-icon.svg:
seven 12-unit-wide towers on a 14-unit pitch, heights 25/35/45/50/45/35/25,
rounded corners (rx=1). Colours are the site's blues, lightest in the middle.

Run headless (from the repo root, with the onboarding instance up):

    docker exec blender-onboarding blender -b --factory-startup \
        --python /home/blender/projects/build_mark.py

It writes /home/blender/projects/supported-systems-mark.blend.
"""

import bpy
from mathutils import Vector

UNIT = 0.045  # SVG units to metres: 4.3 m wide, 2.25 m tall, so Blender's
# default viewpoint frames it the way it frames the default cube
PITCH, WIDTH = 14, 12
HEIGHTS = [25, 35, 45, 50, 45, 35, 25]
# Outer towers deepest (#2563eb), centre lightest (#93c5fd), per the SVG gradients.
COLOURS = ["#2563eb", "#3b82f6", "#60a5fa", "#93c5fd", "#60a5fa", "#3b82f6", "#2563eb"]
OUT = "/home/blender/projects/supported-systems-mark.blend"


def srgb_to_linear(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def material(hex_colour: str) -> bpy.types.Material:
    name = f"Mark {hex_colour}"
    mat = bpy.data.materials.get(name) or bpy.data.materials.new(name)
    srgb = [int(hex_colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    lin = [srgb_to_linear(c) for c in srgb]
    if mat.node_tree is None:
        # use_nodes goes away in Blender 6.0, where materials always have nodes.
        mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    bsdf.inputs["Base Color"].default_value = (*lin, 1.0)
    bsdf.inputs["Roughness"].default_value = 0.35
    mat.diffuse_color = (*lin, 1.0)  # what Solid shading shows
    return mat


def build() -> None:
    for obj in list(bpy.data.objects):
        if obj.type == "MESH":
            bpy.data.objects.remove(obj)

    total = PITCH * (len(HEIGHTS) - 1) + WIDTH
    x0 = -total / 2 + WIDTH / 2
    coll = bpy.data.collections.new("Supported Systems mark")
    bpy.context.scene.collection.children.link(coll)
    for i, (h, hexc) in enumerate(zip(HEIGHTS, COLOURS)):
        bpy.ops.mesh.primitive_cube_add(size=1)
        obj = bpy.context.active_object
        for c in obj.users_collection:
            c.objects.unlink(obj)
        coll.objects.link(obj)
        obj.name = f"Tower {i + 1}"
        obj.scale = (WIDTH * UNIT, WIDTH * UNIT, h * UNIT)
        # Along Y: Blender's default viewpoint sees that axis broadside.
        obj.location = (0.0, (x0 + i * PITCH) * UNIT, h * UNIT / 2)
        bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
        bevel = obj.modifiers.new("Rounded corners", "BEVEL")
        bevel.width = 1 * UNIT
        bevel.segments = 4
        obj.data.materials.append(material(hexc))

    target = Vector((0.0, 0.0, max(HEIGHTS) * UNIT * 0.45))
    cam = bpy.data.objects.get("Camera")
    if cam is not None:
        cam.location = (8.5, 6.0, 3.4)  # upper right on screen, clear of the mark
        look = target - cam.location
        cam.rotation_euler = look.to_track_quat("-Z", "Y").to_euler()
    light = bpy.data.objects.get("Light")
    if light is not None:
        light.location = (5.0, -3.0, 6.5)

    # Nothing selected, so screenshots show the mark without selection outlines.
    for obj in bpy.data.objects:
        obj.select_set(False)
    bpy.context.view_layer.objects.active = None

    bpy.context.scene.frame_set(1)
    bpy.ops.wm.save_as_mainfile(filepath=OUT)
    print(f"[build_mark] saved {OUT}")


build()
