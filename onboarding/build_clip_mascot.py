"""Build "Clip", the binder-clip mascot for the Blender MCP chat assistant.

Clip is a real binder clip: the sheet-steel body and wire handles come from a
supplied STL, stood on its spine with the jaws up. The handles are rotated
about their hinges into raised arms, and a face (eyes, pupils, glints, a small
chrome smile) is laid onto the front plate by ray casting. Nothing in the clip
itself is sculpted; only the handles' hinge angles change.

Input (keep it out of git):

    CLIP_STL = /home/blender/projects/clip.stl
      (host: onboarding/blender/projects/clip.stl, also ~/Downloads/clip.stl)
      Binder clip model by CGTrader designer "teen-wolf",
      https://www.cgtrader.com/designers/teen-wolf . The STL carries no licence
      text (its header only says "IngeTrazo STL export").

Run headless (from the repo root, with the onboarding instance up):

    cp onboarding/build_clip_mascot.py onboarding/blender/projects/
    docker exec blender-onboarding blender -b --factory-startup \
        --python /home/blender/projects/build_clip_mascot.py

It writes, under /home/blender/projects/clip-mascot/:

    clip-512.png         3/4 hero render, 512x512, transparent
    clip-mascot-128.png  icon render, 128x128, tuned for 16-32 px
    clip-mascot.blend    the rigged, animated hero scene (icon camera too)
    anim/0001..0072.png  the 3 s hero loop, 512x512, transparent

Options after "--": --no-anim skips the 72 loop frames, --anim-only skips
the icon and still renders (it still rebuilds, rigs and saves the scene).

Copy them to web/public/img/mascot/clip-512.png,
addon/icons/clip-mascot.png and onboarding/clip-mascot.blend.
"""

import math
import os
import sys

import bpy
from bpy_extras import anim_utils
from bpy_extras.object_utils import world_to_camera_view
from mathutils import Matrix, Vector

CLIP_STL = "/home/blender/projects/clip.stl"
OUT_DIR = "/home/blender/projects/clip-mascot"

CLIP_LENGTH = 2.2  # the clip's length along its jaws, in scene metres

STEEL = "#18223a"  # deep blue-black steel
CHROME = "#e9edf2"
PUPIL = "#0b1020"
RIM_BLUE = "#3b82f6"  # Supported Systems blue family, for the rim lights

# Poses. Handle leans are degrees from vertical, in the clip's profile plane:
# "front" is the handle hooked into the face-side jaw, leaning toward the
# viewer; "back" leans away. Face sizes are in scene metres on the plate,
# eye_up is how far up the plate (0 = spine, 1 = jaws) the eyes sit.
HERO = {
    "name": "hero", "front": 46.0, "back": 64.0, "wire_boost": 0.0,
    "eye_r": 0.33, "eye_gap": 0.88, "eye_up": 0.48, "pupil": 0.52, "look": (-0.05, 0.03),
    "smile": True, "smile_w": 0.46, "eye_glow": 0.35, "rim": 700,
    "cam_azimuth": -38.0, "cam_elev": 16.0, "lens": 70, "fill": 0.85,
}
ICON = {
    "name": "icon", "front": 44.0, "back": 54.0, "wire_boost": 0.05,
    "eye_r": 0.44, "eye_gap": 1.22, "eye_up": 0.44, "pupil": 0.6, "look": (0.0, -0.01),
    "smile": False, "smile_w": 0.0, "eye_glow": 1.2, "rim": 2400,
    "cam_azimuth": -25.0, "cam_elev": 14.0, "lens": 0, "fill": 0.94,
}


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
    bsdf.inputs["Emission Color"].default_value = col
    bsdf.inputs["Emission Strength"].default_value = emission
    mat.diffuse_color = col
    return mat


def link(obj, coll):
    for c in obj.users_collection:
        c.objects.unlink(obj)
    coll.objects.link(obj)
    return obj


def world_verts(obj) -> list:
    return [obj.matrix_world @ v.co for v in obj.data.vertices]


def centroid(pts) -> Vector:
    return sum(pts, Vector()) / len(pts)


# ---------------------------------------------------------------- the clip

def import_clip(coll):
    """Import the STL and split it into (body, [handle, handle])."""
    bpy.ops.wm.stl_import(filepath=CLIP_STL)
    src = bpy.context.selected_objects[0]
    bpy.context.view_layer.objects.active = src
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")
    bpy.ops.mesh.remove_doubles(threshold=1e-4)  # weld, in case it's unwelded
    bpy.ops.mesh.separate(type="LOOSE")
    bpy.ops.object.mode_set(mode="OBJECT")
    parts = list(bpy.context.selected_objects)

    # The ground plane is a lone quad; the body is the biggest part left; the
    # two handles are the equal-sized pair.
    for p in [p for p in parts if len(p.data.vertices) <= 8]:
        parts.remove(p)
        bpy.data.objects.remove(p)
    parts.sort(key=lambda p: len(p.data.vertices), reverse=True)
    body, handles = parts[0], parts[1:3]
    if len(parts) != 3 or len(handles[0].data.vertices) != len(
            handles[1].data.vertices):
        raise RuntimeError(f"unexpected STL parts: {[p.name for p in parts]}")
    body.name = "Clip body"
    for p in parts:
        link(p, coll)
    return body, handles


def hinge_of(handle, body_centre: Vector):
    """The handle's pivot: the centre of the hook sitting in a jaw roll.

    The handle is long and thin, so take its far ends along its longest axis
    and keep the one nearer the body; the hook is the geometry near that end.
    """
    pts = world_verts(handle)
    ext = [max(p[i] for p in pts) - min(p[i] for p in pts) for i in range(3)]
    axis = max((1, 2), key=lambda i: ext[i])  # y or z: never x, the jaw axis
    lo = min(p[axis] for p in pts)
    hi = max(p[axis] for p in pts)
    end = lo if abs(lo - body_centre[axis]) < abs(hi - body_centre[axis]) else hi
    hook = [p for p in pts if abs(p[axis] - end) < 0.06 * (hi - lo)]
    pivot = centroid(hook)
    tip = max(pts, key=lambda p: (p - pivot).length)
    return pivot, tip


def stand_up(body, handles):
    """Stand the clip on its spine, jaws up, centred, scaled to CLIP_LENGTH."""
    bc = centroid(world_verts(body))
    jaws = centroid([hinge_of(h, bc)[0] for h in handles])
    up = jaws - bc
    up.x = 0.0  # rotate only about X, the jaw axis
    ang = math.atan2(up.y, up.z)
    rot = Matrix.Rotation(ang, 4, "X")
    xs = [p.x for p in world_verts(body)]
    scale = CLIP_LENGTH / (max(xs) - min(xs))
    m = Matrix.Scale(scale, 4) @ rot @ Matrix.Translation(-bc)
    for obj in (body, *handles):
        obj.data.transform(m)
        obj.matrix_world = Matrix.Identity(4)
    pts = world_verts(body)
    lo = Vector([min(p[i] for p in pts) for i in range(3)])
    hi = Vector([max(p[i] for p in pts) for i in range(3)])
    shift = Matrix.Translation((-(lo.x + hi.x) / 2, -(lo.y + hi.y) / 2, -lo.z))
    for obj in (body, *handles):
        obj.data.transform(shift)
        obj.data.update()


def pose_handles(body, handles, pose):
    """Rotate each handle about its hinge (an X-parallel axis) to its lean."""
    bc = centroid(world_verts(body))
    info = [(h, *hinge_of(h, bc)) for h in handles]
    info.sort(key=lambda t: t[1].y)  # smaller y: hooked into the front jaw
    targets = (-pose["front"], pose["back"])  # + leans toward +y (away)
    for (h, pivot, tip), target in zip(info, targets):
        v = tip - pivot
        current = math.degrees(math.atan2(v.y, v.z))
        h.data.transform(Matrix.Translation(-pivot))
        h.location = pivot
        h.rotation_euler = (math.radians(current - target), 0, 0)
    info[0][0].name, info[1][0].name = "Handle front", "Handle back"


def finish_surfaces(body, handles, mats, pose):
    for obj, mat, angle in ((body, mats["steel"], 40),
                            *((h, mats["chrome"], 60) for h in handles)):
        obj.data.materials.clear()
        obj.data.materials.append(mat)
        if obj is not body and pose["wire_boost"]:
            # Icon only: fatten the wire along its normals so it survives 16 px.
            # The handle's shape is untouched; only the tube gets thicker.
            fat = obj.modifiers.new("Thicker wire", "DISPLACE")
            fat.strength = pose["wire_boost"]
            fat.mid_level = 0.0
        with bpy.context.temp_override(selected_editable_objects=[obj],
                                       active_object=obj, object=obj):
            bpy.ops.object.shade_smooth_by_angle(angle=math.radians(angle))


# ---------------------------------------------------------------- the face

def front_plate_frame(body, up_frac: float):
    """Point and axes on the front plate at a fraction of its height.

    Returns (point, normal, right, up) in world space; the normal points out
    of the plate toward the viewer's side (-y), up runs along the slant.
    """
    pts = world_verts(body)
    top = max(p.z for p in pts)
    z = top * up_frac
    bvh_hit = body.ray_cast(Vector((0, -50, z)), Vector((0, 1, 0)))
    ok, loc, normal, _ = bvh_hit
    if not ok:
        raise RuntimeError("face ray missed the front plate")
    if normal.y > 0:
        normal = -normal
    right = Vector((1, 0, 0))
    up = normal.cross(right).normalized()
    if up.z < 0:
        up = -up
    return loc, normal.normalized(), right, up


def on_plate(body, p: Vector, n: Vector):
    """Project p onto the plate along -n; returns (hit, normal)."""
    ok, loc, normal, _ = body.ray_cast(p + n * 2.0, -n)
    if not ok:
        return p, n
    if normal.dot(n) < 0:
        normal = -normal
    return loc, normal.normalized()


def oriented_sphere(coll, name, mat, centre, right, normal, up, radii,
                    segments=48):
    bpy.ops.mesh.primitive_uv_sphere_add(radius=1, segments=segments,
                                         ring_count=segments // 2)
    obj = link(bpy.context.active_object, coll)
    obj.name = name
    basis = Matrix((right, normal, up)).transposed().to_4x4()
    obj.matrix_world = (Matrix.Translation(centre) @ basis
                        @ Matrix.Diagonal((*radii, 1.0)))
    obj.data.materials.append(mat)
    obj.data.shade_smooth()
    return obj


def make_face(coll, mats, body, pose):
    centre, n, right, up = front_plate_frame(body, pose["eye_up"])
    er = pose["eye_r"]
    for side in (-1, 1):
        tag = "L" if side < 0 else "R"
        spot, sn = on_plate(body, centre + right * side * pose["eye_gap"] / 2, n)
        sup = sn.cross(right).normalized()
        oriented_sphere(coll, f"Eye {tag}", mats["eye"], spot + sn * 0.01,
                        right, sn, sup, (er * 0.9, er * 0.35, er * 1.12))
        pr = er * pose["pupil"]
        pc = (spot + right * pose["look"][0]
              + sup * (pose["look"][1] - er * 0.1) + sn * er * 0.33)
        oriented_sphere(coll, f"Pupil {tag}", mats["pupil"], pc, right, sn,
                        sup, (pr, pr * 0.35, pr * 1.1), 32)
        hr = pr * 0.34
        gc = pc - right * pr * 0.35 + sup * pr * 0.42 + sn * pr * 0.36
        oriented_sphere(coll, f"Glint {tag}", mats["glint"], gc, right, sn,
                        sup, (hr, hr, hr), 24)

    if pose["smile"]:
        base, _, _, _ = front_plate_frame(body, pose["eye_up"] - 0.24)
        curve = bpy.data.curves.new("Smile", "CURVE")
        curve.dimensions = "3D"
        curve.bevel_depth = 0.028
        curve.bevel_resolution = 4
        curve.use_fill_caps = True
        spline = curve.splines.new("POLY")
        steps = 20
        spline.points.add(steps)
        w = pose["smile_w"]
        for k in range(steps + 1):
            t = -1 + 2 * k / steps
            p = base + right * t * w / 2 + up * (t * t - 1) * 0.1
            hit, hn = on_plate(body, p, n)
            spline.points[k].co = (*(hit + hn * 0.02), 1.0)
        link(bpy.data.objects.new("Smile", curve), coll)
        curve.materials.append(mats["chrome"])


# ---------------------------------------------------------------- rig

FPS = 24
LOOP = 72  # frames; frame LOOP + 1 is keyed equal to frame 1, so 72 -> 1 flows

# The hero loop. Each track is (bone, property, axis, [(frame, value), ...]);
# every track starts and ends at its rest value so frame 1 is the still.
# Angles are degrees. Bone axes: body Y is up the clip, body Z faces the
# viewer (so Z rotation is a side lean); handle Y is the hinge axis (positive
# swings the handle forward and down); eye Y is the eye's vertical (blink);
# pupil X runs along the plate (glance).
ANIM = [
    # two gentle bobs, each landing a touch low with a small squash
    ("body", "location", 1, [(1, 0), (16, 0.07), (31, -0.02), (37, 0),
                             (52, 0.07), (67, -0.02), (73, 0)]),
    ("body", "scale", 1, [(1, 1), (16, 1.0), (31, 0.955), (37, 1),
                          (52, 1.0), (67, 0.955), (73, 1)]),
    ("body", "scale", 0, [(1, 1), (31, 1.022), (37, 1), (67, 1.022), (73, 1)]),
    ("body", "scale", 2, [(1, 1), (31, 1.022), (37, 1), (67, 1.022), (73, 1)]),
    # slow side-to-side lean over the whole loop
    ("body", "rotation_euler", 2, [(1, 0), (19, 2.5), (37, 0), (55, -2.5),
                                   (73, 0)]),
    # the near handle waves: two swings, then settles
    ("handle.front", "rotation_euler", 1, [(1, 0), (7, -5), (15, 12),
                                           (23, -4), (31, 12), (40, -2),
                                           (48, 0), (73, 0)]),
    # the far handle sways a little
    ("handle.back", "rotation_euler", 1, [(1, 0), (19, -5), (37, 0), (55, 5),
                                          (73, 0)]),
    # one quick blink
    ("eye.L", "scale", 1, [(1, 1), (34, 1), (36, 0.08), (38, 1), (73, 1)]),
    ("eye.R", "scale", 1, [(1, 1), (34, 1), (36, 0.08), (38, 1), (73, 1)]),
    # pupils glance toward the waving hand and back
    ("pupil.L", "location", 0, [(1, 0), (5, 0), (9, 0.085), (26, 0.085),
                                (31, 0), (73, 0)]),
    ("pupil.R", "location", 0, [(1, 0), (5, 0), (9, 0.085), (26, 0.085),
                                (31, 0), (73, 0)]),
]


# The "thinking" loop for the add-on's chat panel, on the icon pose: the
# pupils drift up and aside and hold there, the front handle taps three
# times like drumming fingers, the body sways a little, one slow blink.
# Rendered every THINK_STEP frames, so the add-on plays 16 frames at 8 fps.
THINK_LOOP = 48
THINK_STEP = 3
THINK = [
    ("pupil.L", "location", 0, [(1, 0), (7, 0.07), (41, 0.07), (49, 0)]),
    ("pupil.R", "location", 0, [(1, 0), (7, 0.07), (41, 0.07), (49, 0)]),
    ("pupil.L", "location", 1, [(1, 0), (7, 0.06), (41, 0.06), (49, 0)]),
    ("pupil.R", "location", 1, [(1, 0), (7, 0.06), (41, 0.06), (49, 0)]),
    ("handle.front", "rotation_euler", 1, [(1, 0), (10, 0), (13, 9), (16, 0),
                                           (19, 9), (22, 0), (25, 9), (28, 0),
                                           (49, 0)]),
    ("body", "rotation_euler", 2, [(1, 0), (13, 1.8), (37, -1.8), (49, 0)]),
    ("eye.L", "scale", 1, [(1, 1), (31, 1), (34, 0.1), (37, 1), (49, 1)]),
    ("eye.R", "scale", 1, [(1, 1), (31, 1), (34, 0.1), (37, 1), (49, 1)]),
]


def add_bone(arm, name, head, tail, z_axis, parent=None, inherit_scale="FULL"):
    eb = arm.data.edit_bones.new(name)
    eb.head, eb.tail = head, tail
    eb.align_roll(z_axis)
    eb.parent = arm.data.edit_bones[parent] if parent else None
    eb.use_connect = False
    eb.inherit_scale = inherit_scale
    return eb


def rig(coll):
    """Clip_Rig: rigid bone parenting, rest pose identical to the hero still."""
    objs = {o.name: o for o in coll.objects}
    body = objs["Clip body"]
    top = max(p.z for p in world_verts(body))

    data = bpy.data.armatures.new("Clip_Rig")
    arm = link(bpy.data.objects.new("Clip_Rig", data), coll)
    data.display_type = "STICK"
    bpy.context.view_layer.objects.active = arm
    bpy.ops.object.mode_set(mode="EDIT")

    toward = Vector((0, -1, 0))
    add_bone(arm, "root", (0, 0, 0), (0, -0.6, 0), Vector((0, 0, 1)))
    add_bone(arm, "body", (0, 0, 0), (0, 0, top), toward, "root")
    for tag in ("front", "back"):
        h = objs[f"Handle {tag}"]
        pivot = h.location.copy()
        # head on the hinge axis, bone Y along it (world X), so one rotation
        # about the bone's Y swings the handle exactly as the real hinge does
        add_bone(arm, f"handle.{tag}", pivot, pivot + Vector((0.35, 0, 0)),
                 Vector((0, 0, 1)), "body", inherit_scale="NONE")

    eyes = {s: objs[f"Eye {s}"] for s in "LR"}
    cols = {s: eyes[s].matrix_world.to_3x3() for s in "LR"}
    face_mid = (eyes["L"].matrix_world.translation
                + eyes["R"].matrix_world.translation) / 2
    n = cols["L"].col[1].normalized()
    up = cols["L"].col[2].normalized()
    add_bone(arm, "face", face_mid, face_mid + up * 0.3, n, "body")
    for s in "LR":
        m = eyes[s].matrix_world
        c, e_n, e_up = m.translation, cols[s].col[1].normalized(), \
            cols[s].col[2].normalized()
        add_bone(arm, f"eye.{s}", c, c + e_up * 0.2, e_n, "face")
        pc = objs[f"Pupil {s}"].matrix_world.translation
        add_bone(arm, f"pupil.{s}", pc, pc + e_up * 0.12, e_n, f"eye.{s}")
    bpy.ops.object.mode_set(mode="OBJECT")

    parents = {"Clip body": "body", "Handle front": "handle.front",
               "Handle back": "handle.back", "Smile": "face",
               "Eye L": "eye.L", "Eye R": "eye.R",
               "Pupil L": "pupil.L", "Pupil R": "pupil.R",
               "Glint L": "pupil.L", "Glint R": "pupil.R"}
    for name, bone in parents.items():
        obj = objs.get(name)
        if obj is None:
            continue
        keep = obj.matrix_world.copy()
        obj.parent = arm
        obj.parent_type = "BONE"
        obj.parent_bone = bone
        bpy.context.view_layer.update()
        obj.matrix_world = keep  # rest pose stays exactly the still
    for pb in arm.pose.bones:
        pb.rotation_mode = "XYZ"
    bpy.context.view_layer.update()
    return arm


def animate(arm, tracks=None, loop=LOOP):
    scene = bpy.context.scene
    scene.render.fps = FPS
    scene.frame_start, scene.frame_end = 1, loop
    for bone, prop, axis, keys in (ANIM if tracks is None else tracks):
        pb = arm.pose.bones[bone]
        for frame, value in keys:
            vec = getattr(pb, prop)
            vec[axis] = math.radians(value) if prop == "rotation_euler" \
                else value
            pb.keyframe_insert(prop, index=axis, frame=frame)
            vec[axis] = 0.0 if prop != "scale" else 1.0
    action = arm.animation_data.action
    bag = anim_utils.action_get_channelbag_for_slot(
        action, arm.animation_data.action_slot)
    for fc in bag.fcurves:
        # Cycles first, so Blender computes the auto handles cyclically:
        # the tangent leaving frame 73 matches the one entering frame 1.
        fc.modifiers.new("CYCLES")
        for kp in fc.keyframe_points:
            kp.interpolation = "BEZIER"
            kp.handle_left_type = kp.handle_right_type = "AUTO_CLAMPED"
        fc.update()
    scene.frame_set(1)


def loop_extents(cam, meshes, loop=LOOP):
    """Worst-case NDC box of the subject over the loop (0..1 is in frame)."""
    scene = bpy.context.scene
    lo, hi = [9.0, 9.0], [-9.0, -9.0]
    for f in range(1, loop + 1, 2):
        scene.frame_set(f)
        dg = bpy.context.evaluated_depsgraph_get()
        for o in meshes:
            ev = o.evaluated_get(dg)
            mw = ev.matrix_world
            verts = ev.data.vertices
            for i in range(0, len(verts), 7):
                v = verts[i]
                c = world_to_camera_view(scene, cam, mw @ v.co)
                lo = [min(lo[0], c.x), min(lo[1], c.y)]
                hi = [max(hi[0], c.x), max(hi[1], c.y)]
    scene.frame_set(1)
    return lo, hi


# ---------------------------------------------------------------- staging

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


def framed_camera(name, pose, subjects):
    """A camera at the pose's azimuth/elevation, framing subjects to `fill`.

    Azimuth 0 looks straight at the face (along +y); negative swings round
    toward -x. lens 0 means orthographic.
    """
    scene = bpy.context.scene
    data = bpy.data.cameras.new(name)
    obj = bpy.data.objects.new(name, data)
    scene.collection.objects.link(obj)
    scene.render.resolution_x = scene.render.resolution_y = 512
    pts = []
    for s in subjects:
        pts += world_verts(s)
    mid = (Vector([min(p[i] for p in pts) for i in range(3)])
           + Vector([max(p[i] for p in pts) for i in range(3)])) / 2
    az, el = math.radians(pose["cam_azimuth"]), math.radians(pose["cam_elev"])
    d = Vector((math.sin(az) * math.cos(el), -math.cos(az) * math.cos(el),
                math.sin(el)))
    dist = 20.0
    if pose["lens"]:
        data.lens = pose["lens"]
    else:
        data.type = "ORTHO"
        data.ortho_scale = 6.0
    for _ in range(6):
        obj.location = mid + d * dist
        obj.rotation_euler = (-d).to_track_quat("-Z", "Y").to_euler()
        bpy.context.view_layer.update()
        ndc = [world_to_camera_view(scene, obj, p) for p in pts]
        xs, ys = [c.x for c in ndc], [c.y for c in ndc]
        span = max(max(xs) - min(xs), max(ys) - min(ys))
        cx, cy = (max(xs) + min(xs)) / 2 - 0.5, (max(ys) + min(ys)) / 2 - 0.5
        data.shift_x += cx
        data.shift_y += cy
        k = span / pose["fill"]
        if data.type == "ORTHO":
            data.ortho_scale *= k
        else:
            dist *= k
    return obj


def clear_scene():
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj)
    for coll in list(bpy.data.collections):
        bpy.data.collections.remove(coll)
    for block in (bpy.data.meshes, bpy.data.curves, bpy.data.lights,
                  bpy.data.cameras, bpy.data.armatures, bpy.data.actions):
        for item in list(block):
            block.remove(item)


def build(pose: dict):
    clear_scene()
    scene = bpy.context.scene
    coll = bpy.data.collections.new("Clip mascot")
    scene.collection.children.link(coll)
    mats = {
        "steel": material("Clip steel", STEEL, metallic=0.75, roughness=0.35,
                          coat=0.5),
        "chrome": material("Clip chrome", CHROME, metallic=1.0, roughness=0.12),
        "eye": material("Clip eye", "#ffffff", roughness=0.25,
                        emission=pose["eye_glow"]),
        "pupil": material("Clip pupil", PUPIL, roughness=0.15, coat=1.0),
        "glint": material("Clip glint", "#ffffff", emission=4.0),
    }
    body, handles = import_clip(coll)
    stand_up(body, handles)
    pose_handles(body, handles, pose)
    finish_surfaces(body, handles, mats, pose)
    bpy.context.view_layer.update()
    make_face(coll, mats, body, pose)

    centre = (0, 0, 1.0)
    area_light("Key", (-3.5, -4.5, 5.0), centre, 900, 4.0)
    area_light("Fill", (4.5, -3.5, 1.8), centre, 280, 5.0)
    blue = rgb(RIM_BLUE)[:3]
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
    return [o for o in coll.objects if o.type == "MESH"]


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


def render_loop(cam, folder: str, samples: int, size: int = 512, step: int = 1) -> None:
    scene = bpy.context.scene
    scene.camera = cam
    scene.render.resolution_x = scene.render.resolution_y = size
    scene.frame_step = step
    scene.render.engine = "BLENDER_EEVEE"
    scene.eevee.taa_render_samples = samples
    os.makedirs(folder, exist_ok=True)
    scene.render.filepath = folder + "####"
    bpy.ops.render.render(animation=True)
    print(f"[clip] rendered loop frames {scene.frame_start}..{scene.frame_end}")


def main() -> None:
    args = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    os.makedirs(OUT_DIR, exist_ok=True)

    if "--think-only" in args:
        # Only the add-on's thinking frames; leaves the hero scene alone.
        subjects = build(ICON)
        icon_cam = framed_camera("Icon camera", ICON, subjects)
        arm = rig(bpy.data.collections["Clip mascot"])
        animate(arm, THINK, THINK_LOOP)
        lo, hi = loop_extents(icon_cam, subjects, THINK_LOOP)
        print(f"[clip] think extents x {lo[0]:.3f}..{hi[0]:.3f} "
              f"y {lo[1]:.3f}..{hi[1]:.3f} (must stay inside 0..1)")
        render_loop(icon_cam, f"{OUT_DIR}/think/", 32, size=128, step=THINK_STEP)
        return

    if "--anim-only" not in args:
        subjects = build(ICON)
        icon_cam = framed_camera("Icon camera", ICON, subjects)
        render(icon_cam, f"{OUT_DIR}/clip-mascot-128.png", 128, 64)

    subjects = build(HERO)
    hero_cam = framed_camera("Hero camera", HERO, subjects)  # framed at rest
    coll = bpy.data.collections["Clip mascot"]
    arm = rig(coll)
    animate(arm)
    lo, hi = loop_extents(hero_cam, subjects)
    print(f"[clip] loop extents x {lo[0]:.3f}..{hi[0]:.3f} "
          f"y {lo[1]:.3f}..{hi[1]:.3f} (must stay inside 0..1)")
    if "--anim-only" not in args:
        render(hero_cam, f"{OUT_DIR}/clip-512.png", 512, 128)
    if "--no-anim" not in args:
        render_loop(hero_cam, f"{OUT_DIR}/anim/", 64)

    bpy.context.scene.frame_set(1)
    for obj in bpy.data.objects:
        obj.select_set(False)
    bpy.context.view_layer.objects.active = None
    bpy.ops.wm.save_as_mainfile(filepath=f"{OUT_DIR}/clip-mascot.blend")
    print(f"[clip] saved {OUT_DIR}/clip-mascot.blend")


main()
