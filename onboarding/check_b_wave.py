"""Check that B. Clip can wave without his front handle passing through him.

The "Make B wave" chat starter asks for the handle.front bone to swing
between WAVE degrees. This runs what a correct answer does and measures it:

    sweep  each handle bone's rotation Y from -120 to 120 degrees, with a
           BVH overlap test of the handle against B's body, his face parts
           and the other handle; prints the clear range around rest and
           fails unless it covers HANDLE_CLEAR from build_clip_mascot.py
    wave   keys pose.bones["handle.front"].rotation_euler[1] on the rig the
           way insert_keyframe does (frame, value, key), back and forth
           over two seconds at 24 fps, and checks every half frame
    object the mistake a chat model once made: rotating the Handle front
           object instead of the bone (reported, not failed)
    hint   the rig and both handles carry the how_to_pose text

Each hook sits inside its jaw roll, so the handle always touches the body
polygons within ROLL_RADIUS of its own hinge, at rest too. That contact is
ignored; anything else counts.

Headless, against the template (host Blender or the blender-desktop image):

    blender -b app_template/B_Clip/startup.blend --factory-startup \
        --python-exit-code 1 --python onboarding/check_b_wave.py

Pass "-- --no-hint" to check a template built before the hint existed.
Exits 1 on the first failed check.
"""

import importlib.util
import math
import os
import sys

import bpy
from mathutils.bvhtree import BVHTree

HERE = os.path.dirname(os.path.abspath(__file__))
FACE = ("Eye L", "Eye R", "Pupil L", "Pupil R", "Glint L", "Glint R", "Smile")
ROLL_RADIUS = 0.12  # m; the jaw roll around each hinge (measured hook contact stays under 0.1)
SWEEP = range(-120, 121)
WAVE_FRAMES = 48  # two seconds at 24 fps


def load_mascot():
    spec = importlib.util.spec_from_file_location(
        "build_clip_mascot", os.path.join(HERE, "build_clip_mascot.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def say(msg):
    print(f"[wave] {msg}", flush=True)


def check(ok, msg):
    if not ok:
        say(f"FAIL: {msg}")
        sys.stdout.flush()
        os._exit(1)
    say(f"ok: {msg}")


def find_rig():
    for obj in bpy.data.objects:
        if obj.type == "ARMATURE" and "handle.front" in obj.data.bones:
            return obj
    raise RuntimeError("no armature with a handle.front bone in this file")


def world_mesh(obj):
    dg = bpy.context.evaluated_depsgraph_get()
    ev = obj.evaluated_get(dg)
    me = ev.to_mesh()
    try:
        verts = [ev.matrix_world @ v.co for v in me.vertices]
        polys = [tuple(p.vertices) for p in me.polygons]
    finally:
        ev.to_mesh_clear()
    return verts, polys


def bvh(obj):
    verts, polys = world_mesh(obj)
    return BVHTree.FromPolygons(verts, polys, epsilon=0.0)


class Obstacles:
    """B's body (minus the handle's own jaw roll), face parts, the other handle."""

    def __init__(self, rig, tag):
        other = "back" if tag == "front" else "front"
        self.handle = bpy.data.objects[f"Handle {tag}"]
        self.other = bpy.data.objects[f"Handle {other}"]
        bone = rig.pose.bones[f"handle.{tag}"]
        rest = rig.matrix_world @ bone.bone.matrix_local
        head, axis = rest.translation, rest.col[1].xyz.normalized()  # bone Y = hinge
        verts, polys = world_mesh(bpy.data.objects["Clip body"])

        def off_axis(p):
            d = p - head
            return (d - axis * d.dot(axis)).length

        self.roll = {i for i, poly in enumerate(polys)
                     if all(off_axis(verts[j]) < ROLL_RADIUS for j in poly)}
        self.body = BVHTree.FromPolygons(verts, polys, epsilon=0.0)
        self.face = [bvh(bpy.data.objects[n]) for n in FACE if n in bpy.data.objects]

    def hits(self):
        """(body, face, other handle) triangle pairs for the handle as posed now."""
        tree = bvh(self.handle)
        body = sum(1 for _, b in tree.overlap(self.body) if b not in self.roll)
        face = sum(len(tree.overlap(t)) for t in self.face)
        other = len(tree.overlap(bvh(self.other)))
        return body, face, other


def clear_run(rows):
    """The contiguous run of clear angles around 0, as (lo, hi), or None."""
    good = {deg for deg, hit in rows if not any(hit)}
    if 0 not in good:
        return None
    lo = hi = 0
    while lo - 1 in good:
        lo -= 1
    while hi + 1 in good:
        hi += 1
    return lo, hi


def sweep(rig, mascot):
    for tag in ("front", "back"):
        pb = rig.pose.bones[f"handle.{tag}"]
        obs = Obstacles(rig, tag)
        rows = []
        for deg in SWEEP:
            pb.rotation_euler[1] = math.radians(deg)
            bpy.context.view_layer.update()
            rows.append((deg, obs.hits()))
        pb.rotation_euler[1] = 0.0
        bpy.context.view_layer.update()
        first = {deg: hit for deg, hit in rows}
        run = clear_run(rows)
        say(f"handle.{tag} rotation Y clear of body, face and the other handle: "
            f"{run[0] if run else '?'} .. {run[1] if run else '?'} degrees")
        if run:
            for edge in (run[0] - 1, run[1] + 1):
                if edge in first:
                    b, f, o = first[edge]
                    say(f"  at {edge:+d}: body {b}, face {f}, other handle {o} triangle pairs")
        want = mascot.HANDLE_CLEAR[tag]
        check(run is not None and run[0] <= want[0] and run[1] >= want[1],
              f"handle.{tag} is clear over the documented {want[0]}..{want[1]} degrees")


def wave(rig, mascot):
    """Key the wave like a correct chat answer and check every half frame."""
    scene = bpy.context.scene
    lo, hi = mascot.WAVE
    path = 'pose.bones["handle.front"].rotation_euler'
    pb = rig.pose.bones["handle.front"]
    keys = [(1, 0.0), (7, lo), (15, hi), (23, lo), (31, hi), (39, lo), (WAVE_FRAMES + 1, 0.0)]
    for frame, deg in keys:  # insert_keyframe's order: frame, then value, then key
        scene.frame_set(frame)
        pb.rotation_euler[1] = math.radians(deg)
        check(rig.keyframe_insert(data_path=path, index=1, frame=frame), f"keyed {deg:+.0f} deg at {frame}")
    obs = Obstacles(rig, "front")
    worst = 0
    angles = []
    for half in range(2, 2 * (WAVE_FRAMES + 1) + 1):
        scene.frame_set(half // 2, subframe=0.5 * (half % 2))
        angle = math.degrees(pb.rotation_euler[1])
        angles.append(angle)
        hit = obs.hits()
        worst = max(worst, sum(hit))
        if any(hit):
            check(False, f"frame {half / 2:.1f}, handle.front at {angle:.1f} deg: "
                         f"body {hit[0]}, face {hit[1]}, other handle {hit[2]} triangle pairs")
    say(f"wave swung handle.front over {min(angles):.1f} .. {max(angles):.1f} degrees")
    check(min(angles) >= lo - 0.5 and max(angles) <= hi + 0.5, "the curve doesn't overshoot the keys")
    check(worst == 0, f"no frame of the {WAVE_FRAMES}-frame wave intersects B")
    rig.animation_data_clear()
    pb.rotation_euler[1] = 0.0
    scene.frame_set(1)


def object_mistake(rig, mascot):
    """Rotating the Handle front object itself, about each of its axes."""
    obs = Obstacles(rig, "front")
    handle = obs.handle
    rest = handle.rotation_euler.copy()
    for axis in range(3):
        worst = 0
        for deg in range(mascot.WAVE[0], mascot.WAVE[1] + 1):
            handle.rotation_euler = rest
            handle.rotation_euler[axis] = rest[axis] + math.radians(deg)
            bpy.context.view_layer.update()
            worst = max(worst, sum(obs.hits()))
        say(f"control: Handle front object rotated about its {'XYZ'[axis]} over the wave "
            f"range: up to {worst} intersecting triangle pairs")
    handle.rotation_euler = rest
    bpy.context.view_layer.update()


def hint(rig, mascot):
    text = rig.get(mascot.HOW_TO_POSE_KEY)
    check(isinstance(text, str) and "handle.front" in text and "pose" in text.lower(),
          f"{rig.name!r} has its {mascot.HOW_TO_POSE_KEY} text")
    for tag in ("front", "back"):
        obj = bpy.data.objects[f"Handle {tag}"]
        text = obj.get(mascot.HOW_TO_POSE_KEY)
        check(isinstance(text, str) and f"handle.{tag}" in text,
              f"'Handle {tag}' points at the handle.{tag} bone")


def main():
    args = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    mascot = load_mascot()
    rig = find_rig()
    say(f"rig {rig.name!r} in {bpy.data.filepath or '(unsaved)'}")
    sweep(rig, mascot)
    wave(rig, mascot)
    object_mistake(rig, mascot)
    if "--no-hint" not in args:
        hint(rig, mascot)
    say("ALL WAVE CHECKS PASSED")


main()
