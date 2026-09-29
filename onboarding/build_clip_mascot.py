"""Build "Clip", the binder-clip mascot for the Blender MCP chat assistant.

A binder clip turned character: the dark steel wedge body wears the face,
the two chrome wire handles are its arms, raised in a V (the hero pose waves
one of them). Everything is procedural, so tweak the constants and rebuild.

Run headless (from the repo root, with the onboarding instance up):

    cp onboarding/build_clip_mascot.py onboarding/blender/projects/
    docker exec blender-onboarding blender -b --factory-startup \
        --python /home/blender/projects/build_clip_mascot.py

It writes, under /home/blender/projects/clip-mascot/:

    clip-512.png         3/4 hero render, 512x512, transparent
    clip-mascot-128.png  front icon render, 128x128, tuned for 16-32 px
    clip-mascot.blend    the hero scene (the icon camera is in it too)

Copy them to web/public/img/mascot/clip-512.png,
addon/icons/clip-mascot.png and onboarding/clip-mascot.blend.
"""

import math
import os

import bmesh
import bpy
from mathutils import Vector

OUT_DIR = "/home/blender/projects/clip-mascot"

# Body: the wedge seen end-on, a trapezoid wide at the jaws, narrow at the spine.
BODY_H = 1.6
BODY_D = 1.0  # depth, front to back
BODY_BEVEL = 0.1

STEEL = "#18223a"  # deep blue-black steel
ACCENT = "#2563eb"  # Supported Systems blue: hinge rolls and rim light
CHROME = "#e9edf2"
PUPIL = "#0b1020"

# Face, on the front plane. Heights are measured from the base of the body.
EYE_Z = 0.8
EYE_GAP = 0.64  # centre to centre
EYE_R = 0.27
SMILE_Z = 0.4
SMILE_W = 0.42

# Pose and proportions per render. Angles are degrees from vertical.
HERO = dict(
    name="hero", left_arm=18.0, right_arm=64.0, arm_len=1.3, wire=0.05,
    loop_face=80.0, pupil_look=(-0.06, 0.03), eye_scale=1.0, smile=True,
    tilt=-6.0, eye_z=EYE_Z, eye_gap=EYE_GAP, body_w=2.2, spine_r=0.4,
    rim=700, steel=STEEL, eye_glow=0.35, pupil=0.52,
)
ICON = dict(
    name="icon", left_arm=34.0, right_arm=34.0, arm_len=1.05, wire=0.095,
    loop_face=85.0, pupil_look=(0.0, -0.01), eye_scale=1.15, smile=False,
    tilt=0.0, eye_z=0.66, eye_gap=0.94, body_w=2.6, spine_r=0.5,
    rim=2400, steel="#1c2b4f", eye_glow=1.2, pupil=0.6,
)


def srgb_to_linear(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def rgb(hex_colour: str) -> tuple:
    srgb = [int(hex_colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    return (*[srgb_to_linear(c) for c in srgb], 1.0)


def material(name: str, hex_colour: str, metallic=0.0, roughness=0.35,
             emission=0.0, coat=0.0) -> bpy.types.Material:
    mat = bpy.data.materials.get(name) or bpy.data.materials.new(name)
    if mat.node_tree is None:
        mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    col = rgb(hex_colour)
    bsdf.inputs["Base Color"].default_value = col
    bsdf.inputs["Metallic"].default_value = metallic
    bsdf.inputs["Roughness"].default_value = roughness
    bsdf.inputs["Coat Weight"].default_value = coat
    if emission:
        bsdf.inputs["Emission Color"].default_value = col
        bsdf.inputs["Emission Strength"].default_value = emission
    mat.diffuse_color = col
    return mat


def link(obj, coll):
    for c in obj.users_collection:
        c.objects.unlink(obj)
    coll.objects.link(obj)
    return obj


def spine_shoulder(pose: dict, side: int) -> Vector:
    """Where a slanted side meets the rounded spine (the tangent point)."""
    r = pose["spine_r"]
    c = Vector((0.0, BODY_H - r))
    foot = Vector((side * pose["body_w"] / 2, 0.0))
    v = foot - c
    alpha = math.acos(r / v.length)
    ang = math.atan2(v.y, v.x) + (alpha if side > 0 else -alpha)
    return c + Vector((math.cos(ang), math.sin(ang))) * r


def profile(pose: dict) -> list:
    """The end-on outline (x, z): flat jaws, slanted sides, round spine."""
    r = pose["spine_r"]
    c = Vector((0.0, BODY_H - r))
    tr, tl = spine_shoulder(pose, 1), spine_shoulder(pose, -1)
    a0 = math.atan2(tr.y - c.y, tr.x - c.x)
    a1 = math.atan2(tl.y - c.y, tl.x - c.x)
    w = pose["body_w"]
    pts = [(-w / 2, 0.0), (w / 2, 0.0)]
    n = 18
    for k in range(n + 1):
        a = a0 + (a1 - a0) * k / n
        pts.append((c.x + r * math.cos(a), c.y + r * math.sin(a)))
    return pts


def make_body(coll, mats, pose):
    bm = bmesh.new()
    y0, y1 = -BODY_D / 2, BODY_D / 2
    outline = profile(pose)
    front = [bm.verts.new((x, y0, z)) for x, z in outline]
    back = [bm.verts.new((x, y1, z)) for x, z in outline]
    n = len(outline)
    bm.faces.new(front[::-1])
    bm.faces.new(back)
    for i in range(n):
        j = (i + 1) % n
        bm.faces.new((front[i], front[j], back[j], back[i]))
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    mesh = bpy.data.meshes.new("Clip body")
    bm.to_mesh(mesh)
    bm.free()
    obj = link(bpy.data.objects.new("Clip body", mesh), coll)
    mesh.materials.append(mats["steel"])
    bev = obj.modifiers.new("Soft edges", "BEVEL")
    bev.width = BODY_BEVEL
    bev.segments = 6
    bev.limit_method = "ANGLE"
    bev.angle_limit = math.radians(30)
    bev.harden_normals = True
    mesh.shade_smooth()
    return obj


def _xz(p2: Vector, inset: float) -> tuple:
    """A profile point, pulled slightly inside the body, as (x, 0, z)."""
    return (p2.x - math.copysign(inset, p2.x), 0.0, p2.y - inset)


def make_hinges(coll, mats, pose):
    """The rolled edges at the spine where the handle wires hook in."""
    for side in (-1, 1):
        bpy.ops.mesh.primitive_cylinder_add(
            radius=0.085, depth=BODY_D * 0.92, vertices=32,
            location=_xz(spine_shoulder(pose, side), 0.02),
            rotation=(math.pi / 2, 0, 0))
        obj = link(bpy.context.active_object, coll)
        obj.name = f"Hinge {'L' if side < 0 else 'R'}"
        obj.data.materials.append(mats["accent"])
        bev = obj.modifiers.new("Round", "BEVEL")
        bev.width = 0.03
        bev.segments = 4
        bev.harden_normals = True
        obj.data.shade_smooth()


def make_arm(coll, mats, pose: dict, side: int, angle_deg: float):
    """One wire handle: a narrow hairpin loop rising from the hinge."""
    length, wire, face_deg = pose["arm_len"], pose["wire"], pose["loop_face"]
    a = math.radians(angle_deg)
    pivot = Vector(_xz(spine_shoulder(pose, side), 0.02))
    d = Vector((side * math.sin(a), 0, math.cos(a)))
    across_xz = Vector((d.z, 0, -d.x)).normalized()
    f = math.radians(face_deg)
    # face 0: the loop's plane runs along the hinge (a real clip); 90: it faces us
    e = (Vector((0, 1, 0)) * math.cos(f) + across_xz * math.sin(f)).normalized()

    foot, top = 0.16, 0.40
    r = top / 2
    pts = [pivot - e * foot / 2 - d * 0.02]
    shoulder = pivot + d * (length - r)
    pts.append(shoulder - e * r)
    for k in range(1, 16):
        t = math.pi * k / 16
        pts.append(shoulder - e * r * math.cos(t) + d * r * math.sin(t))
    pts.append(shoulder + e * r)
    pts.append(pivot + e * foot / 2 - d * 0.02)

    curve = bpy.data.curves.new(f"Arm {'L' if side < 0 else 'R'}", "CURVE")
    curve.dimensions = "3D"
    curve.bevel_depth = wire
    curve.bevel_resolution = 6
    curve.use_fill_caps = True
    spline = curve.splines.new("POLY")
    spline.points.add(len(pts) - 1)
    for p, co in zip(spline.points, pts):
        p.co = (*co, 1.0)
    obj = link(bpy.data.objects.new(curve.name, curve), coll)
    curve.materials.append(mats["chrome"])
    return obj


def make_face(coll, mats, pose: dict):
    look, eye_scale, eye_z = pose["pupil_look"], pose["eye_scale"], pose["eye_z"]
    y_front = -BODY_D / 2
    er = EYE_R * eye_scale
    for side in (-1, 1):
        x = side * pose["eye_gap"] / 2
        bpy.ops.mesh.primitive_uv_sphere_add(
            radius=1, segments=48, ring_count=24,
            location=(x, y_front - 0.01, eye_z))
        eye = link(bpy.context.active_object, coll)
        eye.name = f"Eye {'L' if side < 0 else 'R'}"
        eye.scale = (er * 0.9, er * 0.35, er * 1.12)  # tall ovals
        eye.data.materials.append(mats["eye"])
        eye.data.shade_smooth()

        pr = er * pose["pupil"]
        px, pz = x + look[0], eye_z + look[1] - er * 0.1
        bpy.ops.mesh.primitive_uv_sphere_add(
            radius=1, segments=32, ring_count=16,
            location=(px, y_front - er * 0.33, pz))
        pupil = link(bpy.context.active_object, coll)
        pupil.name = f"Pupil {'L' if side < 0 else 'R'}"
        pupil.scale = (pr, pr * 0.35, pr * 1.1)
        pupil.data.materials.append(mats["pupil"])
        pupil.data.shade_smooth()

        hr = pr * 0.34
        bpy.ops.mesh.primitive_uv_sphere_add(
            radius=hr, segments=24, ring_count=12,
            location=(px - pr * 0.35, y_front - er * 0.33 - pr * 0.36,
                      pz + pr * 0.42))
        glint = link(bpy.context.active_object, coll)
        glint.name = f"Glint {'L' if side < 0 else 'R'}"
        glint.data.materials.append(mats["glint"])
        glint.data.shade_smooth()

    if pose["smile"]:
        curve = bpy.data.curves.new("Smile", "CURVE")
        curve.dimensions = "3D"
        curve.bevel_depth = 0.028
        curve.bevel_resolution = 4
        curve.use_fill_caps = True
        spline = curve.splines.new("POLY")
        n = 20
        spline.points.add(n)
        for k in range(n + 1):
            t = -1 + 2 * k / n
            x = t * SMILE_W / 2
            z = SMILE_Z + (t * t - 1) * 0.1
            spline.points[k].co = (x, y_front - 0.015, z, 1.0)
        link(bpy.data.objects.new("Smile", curve), coll)
        curve.materials.append(mats["chrome"])


def area_light(name, loc, target, energy, size, colour=(1, 1, 1)):
    data = bpy.data.lights.new(name, "AREA")
    data.energy = energy
    data.size = size
    data.color = colour
    obj = bpy.data.objects.new(name, data)
    bpy.context.scene.collection.objects.link(obj)
    obj.location = loc
    look = Vector(target) - Vector(loc)
    obj.rotation_euler = look.to_track_quat("-Z", "Y").to_euler()
    return obj


def camera(name, loc, target, ortho_scale=None, lens=50):
    data = bpy.data.cameras.new(name)
    if ortho_scale:
        data.type = "ORTHO"
        data.ortho_scale = ortho_scale
    else:
        data.lens = lens
    obj = bpy.data.objects.new(name, data)
    bpy.context.scene.collection.objects.link(obj)
    obj.location = loc
    look = Vector(target) - Vector(loc)
    obj.rotation_euler = look.to_track_quat("-Z", "Y").to_euler()
    return obj


def clear_scene():
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj)
    for coll in list(bpy.data.collections):
        bpy.data.collections.remove(coll)
    for block in (bpy.data.meshes, bpy.data.curves, bpy.data.lights,
                  bpy.data.cameras):
        for item in list(block):
            block.remove(item)


def build(pose: dict) -> None:
    clear_scene()
    scene = bpy.context.scene
    coll = bpy.data.collections.new("Clip mascot")
    scene.collection.children.link(coll)
    mats = {
        "steel": material("Clip steel", pose["steel"], metallic=0.75, roughness=0.35,
                          coat=0.5),
        "accent": material("Clip accent", ACCENT, metallic=0.8, roughness=0.3),
        "chrome": material("Clip chrome", CHROME, metallic=1.0, roughness=0.12),
        "eye": material("Clip eye", "#ffffff", roughness=0.25,
                        emission=pose["eye_glow"]),
        "pupil": material("Clip pupil", PUPIL, roughness=0.15, coat=1.0),
        "glint": material("Clip glint", "#ffffff", emission=4.0),
    }
    make_body(coll, mats, pose)
    make_hinges(coll, mats, pose)
    make_arm(coll, mats, pose, -1, pose["left_arm"])
    make_arm(coll, mats, pose, 1, pose["right_arm"])
    make_face(coll, mats, pose)
    # A slight lean for life: everything hangs off one root, pivoting at the jaws.
    root = bpy.data.objects.new("Clip", None)
    coll.objects.link(root)
    for obj in coll.objects:
        if obj is not root:
            obj.parent = root
    root.rotation_euler = (0, math.radians(pose["tilt"]), 0)

    centre = (0, 0, 1.15)
    area_light("Key", (-3.5, -4.5, 5.0), centre, 900, 4.0)
    area_light("Fill", (4.5, -3.5, 1.8), centre, 280, 5.0)
    # Blue rims from behind: they graze the silhouette, not the side faces.
    blue = rgb("#3b82f6")[:3]
    area_light("Rim L", (-1.3, 5.0, 3.0), centre, pose["rim"], 1.6, blue)
    area_light("Rim R", (1.3, 5.0, 3.0), centre, pose["rim"], 1.6, blue)

    world = scene.world or bpy.data.worlds.new("World")
    scene.world = world
    if world.node_tree is None:
        world.use_nodes = True
    bg = world.node_tree.nodes.get("Background")
    bg.inputs["Color"].default_value = (0.18, 0.2, 0.24, 1)
    bg.inputs["Strength"].default_value = 0.6

    scene.render.film_transparent = True
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGBA"
    scene.view_settings.view_transform = "AgX"
    scene.view_settings.look = "AgX - Punchy"


def render(cam, path: str, size: int, samples: int) -> None:
    scene = bpy.context.scene
    scene.camera = cam
    scene.render.resolution_x = scene.render.resolution_y = size
    scene.render.resolution_percentage = 100
    try:
        scene.render.engine = "BLENDER_EEVEE"
        scene.eevee.taa_render_samples = samples
    except TypeError:
        scene.render.engine = "CYCLES"
        scene.cycles.samples = samples
    scene.render.filepath = path
    bpy.ops.render.render(write_still=True)
    print(f"[clip] rendered {path} with {scene.render.engine}")


def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)

    build(ICON)
    icon_cam = camera("Icon camera", (0, -12, 1.3), (0, 0, 1.28),
                      ortho_scale=3.3)
    render(icon_cam, f"{OUT_DIR}/clip-mascot-128.png", 128, 64)

    build(HERO)
    hero_cam = camera("Hero camera", (-4.6, -8.6, 3.0), (0.15, 0, 1.3),
                      lens=78)
    icon_cam = camera("Icon camera", (0, -12, 1.3), (0, 0, 1.25),
                      ortho_scale=3.3)
    render(hero_cam, f"{OUT_DIR}/clip-512.png", 512, 128)

    bpy.context.scene.camera = hero_cam
    for obj in bpy.data.objects:
        obj.select_set(False)
    bpy.context.view_layer.objects.active = None
    bpy.ops.wm.save_as_mainfile(filepath=f"{OUT_DIR}/clip-mascot.blend")
    print(f"[clip] saved {OUT_DIR}/clip-mascot.blend")


main()
