"""Export B. Clip for the "Meet B" page (blender.bet/b).

Reads the committed template scene, app_template/B_Clip/startup.blend, so
it needs neither the STL nor Docker. B there is exactly what
build_clip_mascot.py builds (hero pose, face, Clip_Rig renamed "B. Clip"),
and the hero and think loops come straight from that script's ANIM and
THINK tracks, so the page, the .blend and the homepage all move alike.

Run from the repo root:

    blender -b --factory-startup --python onboarding/export_b_assets.py

It writes:

    web/public/b/b-clip.glb     B and his joints for three.js: meshes are
                                child nodes of the joint they ride on,
                                joints in rest pose, no camera or lights
    web/public/b/b-clip.blend   standalone rigged B: camera, lights, the
                                "B hello" and "B thinking" actions, and
                                two text blocks (how to pose him, credits)
    web/src/data/b-clip.json    both loops sampled per half frame, plus the
                                two files' sizes for the download link

The glTF exporter keeps each bone's own axes (only armature space turns
Y-up), so a Blender pose value applies to a joint node unchanged:
position = rest + rest_rotation * location, rotation = rest * euler,
scale = scale. The checks at the end make sure that still holds.
"""

import importlib.util
import json
import math
import os
import struct
import sys

import bpy

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SOURCE = os.path.join(ROOT, "app_template", "B_Clip", "startup.blend")
OUT = os.path.join(ROOT, "web", "public", "b")
GLB = os.path.join(OUT, "b-clip.glb")
BLEND = os.path.join(OUT, "b-clip.blend")
DATA = os.path.join(ROOT, "web", "src", "data", "b-clip.json")

RIG = "B. Clip"
BONES = ("root", "body", "handle.front", "handle.back", "face",
         "eye.L", "eye.R", "pupil.L", "pupil.R")
# The mesh each joint must carry in the glb; the hierarchy check uses it.
CARRIES = {"body": "Clip body", "handle.front": "Handle front",
           "handle.back": "Handle back", "face": "Smile",
           "eye.L": "Eye L", "eye.R": "Eye R",
           "pupil.L": "Pupil L", "pupil.R": "Pupil R"}
SAMPLE_STEP = 0.5  # frames between samples written to b-clip.json

CREDITS = """B. Clip ("B" for short), the BlenderMCP mascot.

B's binder clip mesh is by teen-wolf on CGTrader:
https://www.cgtrader.com/designers/teen-wolf
Its licence allows redistribution, so you may share this file.

The face, rig, materials, loops and staging were added for BlenderMCP
(https://blender.bet). Built by onboarding/export_b_assets.py from the
B. Clip app template in https://github.com/rsp2k/blender-mcp
"""

HOW_TO = """How to pose B

B rides on an armature called "B. Clip". Every part of him is parented to
one of its bones, so moving a bone moves that part. Nothing is weight
painted, nothing bends.

1. Click B's armature (the thin lines drawn in front of him), or pick
   "B. Clip" in the Outliner.
2. Press Ctrl+Tab for Pose Mode.
3. Click a bone, then press R to rotate it, G to move it, S to scale it.
   Type a number right after, like "R Y 20", for an exact amount.
   Alt+R, Alt+G and Alt+S put a bone back the way it was.

The bones, and what to do with them:

  handle.front   the wire handle on his face side. Rotate about its Y axis
                 (R Y). Positive swings it forward and down; that is his wave.
  handle.back    the handle behind him. Same axis, same direction.
  body           all of him. Rotate about Z (R Z) to lean side to side,
                 move along Y (G Y) to hop.
  face           eyes and smile together.
  eye.L, eye.R   scale along Y (S Y) to blink. 0.1 is nearly shut.
  pupil.L        move along X (G X) to glance left or right across the
  pupil.R        plate, along Y (G Y) to look up or down.
  root           where he stands. Move this to move B.

Two loops come with him, as actions on the armature:

  B hello      the wave from the blender.bet homepage, 72 frames
  B thinking   the "thinking" loop from the Chat panel, 48 frames

"B hello" is active. Press Space to play it. To switch, open the Dope
Sheet, change its mode to Action Editor, and pick the other action from
the list. Each loop starts and ends at rest, so they repeat cleanly.

Press Numpad 0 to look through the camera, F12 to render.

With BlenderMCP connected you can also just ask in the Chat panel (N in
the 3D Viewport, BlenderMCP tab): "make B wave", "make B look up and to
the right", "make B blink twice".
https://docs.blender.bet/how-to/chat-in-blender/
"""


def load_mascot():
    spec = importlib.util.spec_from_file_location(
        "build_clip_mascot", os.path.join(HERE, "build_clip_mascot.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------- scene

def assemble():
    """Factory scene, emptied, with B, his lights and camera appended."""
    scene = bpy.context.scene
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj)
    for block in (bpy.data.meshes, bpy.data.lights, bpy.data.cameras):
        for item in list(block):
            block.remove(item)

    with bpy.data.libraries.load(SOURCE, link=False) as (src, dst):
        dst.collections = [c for c in src.collections if c in (RIG, "Lights")]
        dst.objects = ["Camera"]
        dst.worlds = list(src.worlds)
    for coll in dst.collections:
        scene.collection.children.link(coll)
    cam = dst.objects[0]
    scene.collection.objects.link(cam)
    scene.camera = cam
    if dst.worlds:
        old = scene.world
        scene.world = dst.worlds[0]
        if old and old.users == 0:
            bpy.data.worlds.remove(old)
        scene.world.name = "World"
    for lib in list(bpy.data.libraries):
        bpy.data.libraries.remove(lib)

    arm = bpy.data.objects[RIG]
    missing = [b for b in BONES if b not in arm.data.bones]
    if missing:
        raise RuntimeError(f"{SOURCE}: rig lacks bones {missing}")
    arm.show_in_front = True  # bones stay clickable through the steel
    for pb in arm.pose.bones:
        pb.rotation_mode = "XYZ"

    # Same look as the mascot renders and the template.
    scene.render.engine = "BLENDER_EEVEE"
    scene.eevee.taa_render_samples = 64
    scene.render.resolution_x, scene.render.resolution_y = 1920, 1080
    scene.render.resolution_percentage = 100
    scene.render.film_transparent = False
    scene.view_settings.view_transform = "AgX"
    scene.view_settings.look = "AgX - Punchy"
    scene.cursor.location = (2.6, 0.0, 0.0)  # beside B, as in the template
    return arm


def add_actions(arm, mascot):
    """"B hello" (ANIM) and "B thinking" (THINK), hello left active."""
    actions = {}
    for name, tracks, loop in (("B thinking", mascot.THINK, mascot.THINK_LOOP),
                               ("B hello", mascot.ANIM, mascot.LOOP)):
        if arm.animation_data:
            arm.animation_data.action = None
        for pb in arm.pose.bones:
            pb.location = (0, 0, 0)
            pb.rotation_euler = (0, 0, 0)
            pb.scale = (1, 1, 1)
        mascot.animate(arm, tracks, loop)
        action = arm.animation_data.action
        action.name = name
        action.use_fake_user = True  # survives while the other one is active
        actions[name] = (action, loop, tracks)
    return actions


def sample(arm, actions, mascot):
    """Each loop's channels, sampled every SAMPLE_STEP frames over one cycle.

    Keys are "bone/property/axis". Rotations are in degrees, the rest as
    Blender stores them. The page interpolates linearly between samples.
    """
    scene = bpy.context.scene
    presets = {}
    for name, (action, loop, tracks) in actions.items():
        arm.animation_data.action = action
        chans = sorted({(b, p, a) for b, p, a, _ in tracks})
        series = {f"{b}/{p}/{a}": [] for b, p, a in chans}
        n = int(loop / SAMPLE_STEP)
        for i in range(n):
            t = 1 + i * SAMPLE_STEP
            scene.frame_set(int(t), subframe=t - int(t))
            for b, p, a in chans:
                v = getattr(arm.pose.bones[b], p)[a]
                if p == "rotation_euler":
                    v = math.degrees(v)
                series[f"{b}/{p}/{a}"].append(round(v, 4))
        key = "hello" if name == "B hello" else "thinking"
        presets[key] = {"action": name, "frames": loop, "step": SAMPLE_STEP,
                        "channels": series}
    return presets


def stage_views(texts):
    """Open on the camera view; the Scripting workspace shows the how-to."""
    for screen in bpy.data.screens:
        for area in screen.areas:
            space = area.spaces.active
            if area.type == "VIEW_3D":
                space.shading.type = "SOLID"
                space.shading.light = "STUDIO"
                space.shading.color_type = "MATERIAL"
                space.overlay.show_extras = False
                space.region_3d.view_perspective = "CAMERA"
            elif area.type == "TEXT_EDITOR":
                space.text = texts["how"]


def add_texts(arm):
    texts = {}
    for key, name, body in (("how", "Read me - posing B", HOW_TO),
                            ("credits", "Credits", CREDITS)):
        txt = bpy.data.texts.get(name) or bpy.data.texts.new(name)
        txt.clear()
        txt.write(body)
        texts[key] = txt
    arm["how_to_pose"] = ("Pose Mode (Ctrl+Tab), then rotate handle.front or "
                          "handle.back about Y, body about Z to lean; scale "
                          "eye.L/eye.R along Y to blink; move pupil.L/pupil.R "
                          "along X and Y to look around. Full notes in the "
                          "'Read me - posing B' text.")
    arm["credits"] = ("Binder clip mesh by teen-wolf on CGTrader, "
                      "https://www.cgtrader.com/designers/teen-wolf "
                      "(licence allows redistribution).")
    return texts


# ---------------------------------------------------------------- glb

def export_glb():
    for obj in bpy.data.objects:
        obj.select_set(False)
    bpy.ops.export_scene.gltf(
        filepath=GLB, export_format="GLB", use_selection=False,
        export_cameras=False, export_lights=False, export_apply=True,
        export_animations=False, export_texcoords=False, export_yup=True,
        export_rest_position_armature=True, export_extras=False)


def glb_json(path):
    with open(path, "rb") as fh:
        blob = fh.read()
    size = struct.unpack("<I", blob[12:16])[0]
    return json.loads(blob[20:20 + size])


def check_glb():
    """Every joint is a node, and carries its mesh as a child node."""
    doc = glb_json(GLB)
    nodes = doc["nodes"]
    by_name = {n.get("name"): n for n in nodes}
    for bone in BONES:
        if bone not in by_name:
            raise RuntimeError(f"glb lost joint node {bone}")
    for bone, mesh in CARRIES.items():
        kids = [nodes[i].get("name") for i in by_name[bone].get("children", [])]
        if mesh not in kids:
            raise RuntimeError(f"glb: {mesh} is not a child of {bone} ({kids})")
    for obj in ("Camera", "Key", "Fill", "Rim L", "Rim R"):
        if obj in by_name:
            raise RuntimeError(f"glb should not carry {obj}")
    print(f"[b] glb hierarchy ok: {len(nodes)} nodes, "
          f"{len(doc.get('meshes', []))} meshes, "
          f"materials {[m['name'] for m in doc.get('materials', [])]}")


# ---------------------------------------------------------------- main

def main():
    if not os.path.exists(SOURCE):
        raise SystemExit(f"missing {SOURCE}; build the template first")
    os.makedirs(OUT, exist_ok=True)
    mascot = load_mascot()
    arm = assemble()

    # The glb goes out at rest, before any action exists.
    bpy.context.scene.frame_set(1)
    export_glb()
    check_glb()

    actions = add_actions(arm, mascot)
    presets = sample(arm, actions, mascot)
    scene = bpy.context.scene
    arm.animation_data.action = actions["B hello"][0]
    scene.frame_start, scene.frame_end = 1, mascot.LOOP
    scene.render.fps = mascot.FPS
    scene.frame_set(1)

    texts = add_texts(arm)
    stage_views(texts)
    for obj in bpy.data.objects:
        obj.select_set(False)
    bpy.context.view_layer.objects.active = None
    bpy.ops.file.pack_all()  # nothing to pack today; keeps it self-contained
    bpy.ops.wm.save_as_mainfile(filepath=BLEND, compress=True,
                                relative_remap=False, copy=True)
    print(f"[b] saved {BLEND}")

    data = {
        "_generated": "onboarding/export_b_assets.py; do not edit by hand",
        "fps": mascot.FPS,
        "presets": presets,
        "files": {"glb": os.path.getsize(GLB), "blend": os.path.getsize(BLEND)},
    }
    with open(DATA, "w") as fh:
        json.dump(data, fh, separators=(",", ":"))
        fh.write("\n")
    for path in (GLB, BLEND, DATA):
        print(f"[b] {os.path.relpath(path, ROOT)}: {os.path.getsize(path):,} bytes")


try:
    main()
except Exception:
    import traceback
    traceback.print_exc()
    sys.exit(1)
