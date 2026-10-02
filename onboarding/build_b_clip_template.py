"""Build the B. Clip application template (app_template/B_Clip/).

Runs inside Blender; onboarding/b_clip_template.sh drives it. Three steps,
each its own Blender run:

    scene  OUT PREVIEWS   headless: B, camera, lights, viewport; saves
                          OUT/startup.blend and renders PREVIEWS/camera.png
    layout                GUI (under Xvnc), with OUT/startup.blend open:
                          closes the timeline and properties editor, shrinks
                          the outliner, saves, then patches the sidebar's
                          width and tab (see blend_patch.py)
    splash OUT PREVIEWS   headless: renders OUT/splash.png (1000x500)

B himself comes from build_clip_mascot.py (HERO pose, rig), reading the STL
from build_clip_mascot.CLIP_STL.
"""

import importlib.util
import math
import os
import sys

import bpy
from bpy_extras.object_utils import world_to_camera_view
from mathutils import Vector

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import blend_patch  # noqa: E402

SIDEBAR_WIDTH = 360  # px at UI scale 1; wide enough for the Chat panel
SIDEBAR_TAB = "BlenderMCP"
OUTLINER_WIDTH = 240

SPLASH_TEXT = "Tell Claude what to make"


def load_mascot():
    spec = importlib.util.spec_from_file_location(
        "build_clip_mascot", os.path.join(HERE, "build_clip_mascot.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def mesh_points(objects):
    dg = bpy.context.evaluated_depsgraph_get()
    pts = []
    for obj in objects:
        ev = obj.evaluated_get(dg)
        pts += [ev.matrix_world @ v.co for v in ev.data.vertices]
    return pts


def view_direction(azimuth, elevation):
    """Unit vector from the subject toward the viewer (see framed_camera)."""
    az, el = math.radians(azimuth), math.radians(elevation)
    return Vector((math.sin(az) * math.cos(el), -math.cos(az) * math.cos(el),
                   math.sin(el)))


def fit_camera(cam, pts, target, size, centre=(0.5, 0.5), iterations=8):
    """Aim cam at pts and move it so their box is `size` (NDC, w h) at `centre`.

    The camera keeps its direction; distance sets the scale, shift the place.
    """
    scene = bpy.context.scene
    lo = Vector([min(p[i] for p in pts) for i in range(3)])
    hi = Vector([max(p[i] for p in pts) for i in range(3)])
    mid = (lo + hi) / 2
    d = target
    dist = 15.0
    aspect = scene.render.resolution_y / scene.render.resolution_x
    for _ in range(iterations):
        cam.location = mid + d * dist
        cam.rotation_euler = (-d).to_track_quat("-Z", "Y").to_euler()
        bpy.context.view_layer.update()
        ndc = [world_to_camera_view(scene, cam, p) for p in pts]
        xs, ys = [c.x for c in ndc], [c.y for c in ndc]
        k = max((max(xs) - min(xs)) / size[0], (max(ys) - min(ys)) / size[1])
        cam.data.shift_x += (max(xs) + min(xs)) / 2 - centre[0]
        cam.data.shift_y += ((max(ys) + min(ys)) / 2 - centre[1]) * aspect
        dist *= k
    return mid, dist


# ---------------------------------------------------------------- scene

# Both look from B's front left, so he faces the sidebar. The camera uses the
# mascot's hero angle and stands farther back than the viewport's eye, so it
# stays out of that first view.
VIEW_ANGLE = (-30.0, 16.0)  # azimuth, elevation in degrees
CAMERA_ANGLE = (-38.0, 12.0)
CURSOR = (2.6, 0.0, 0.0)  # beside B, so Add puts new things next to him
# The 3D Viewport area at 1920x1080 once the layout step has run. The
# sidebar overlaps its right edge, so B is centred in what's left.
VIEWPORT_PX = (1674, 1031)
VISIBLE_CENTRE = ((VIEWPORT_PX[0] - SIDEBAR_WIDTH) / 2 / VIEWPORT_PX[0], 0.47)


def frame_viewport(space, pts):
    """Point the viewport at B so he fills about half its height, centred in
    the part the sidebar leaves visible. A throwaway camera with the
    viewport's optics (50 mm lens, and Blender's 2x zoom on a 36 mm sensor)
    does the measuring."""
    scene = bpy.context.scene
    scene.render.resolution_x, scene.render.resolution_y = VIEWPORT_PX
    data = bpy.data.cameras.new("measure")
    data.lens, data.sensor_width = 50, 72
    probe = bpy.data.objects.new("measure", data)
    scene.collection.objects.link(probe)
    d = view_direction(*VIEW_ANGLE)
    mid, dist = fit_camera(probe, pts, d, (0.6, 0.55), centre=VISIBLE_CENTRE)
    rot = (-d).to_track_quat("-Z", "Y")
    width = dist * data.sensor_width / data.lens
    right, up = rot @ Vector((1, 0, 0)), rot @ Vector((0, 1, 0))
    centre = mid + (right * data.shift_x + up * data.shift_y) * width
    r3d = space.region_3d
    r3d.view_perspective = 'PERSP'
    r3d.view_rotation = rot
    r3d.view_distance = dist
    r3d.view_location = centre
    bpy.data.objects.remove(probe)
    bpy.data.cameras.remove(data)


def build_scene(mascot):
    scene = bpy.context.scene
    subjects = mascot.build(mascot.HERO)
    coll = bpy.data.collections["Clip mascot"]
    rig = mascot.rig(coll)
    coll.name = "B. Clip"
    rig.name = "B. Clip"  # move this to move B; everything rides on its bones

    # Solid view draws each material's viewport colour; the render steel is
    # nearly black there, so lift it a little (renders are unaffected).
    steel = bpy.data.materials["Clip steel"]
    steel.diffuse_color = mascot.rgb("#2b3f6b")
    steel.metallic = 0.4
    steel.roughness = 0.35

    # The hero lights, smaller so their outlines don't crowd the viewport.
    lights = bpy.data.collections.new("Lights")
    scene.collection.children.link(lights)
    for name, size in (("Key", 1.2), ("Fill", 1.5), ("Rim L", 0.5), ("Rim R", 0.5)):
        light = bpy.data.objects[name]
        light.data.size = size
        mascot.link(light, lights)

    # Only the Layout workspace; fewer tabs for a first look.
    keep = bpy.data.workspaces["Layout"]
    bpy.data.batch_remove([w for w in bpy.data.workspaces if w != keep])
    for window in bpy.context.window_manager.windows:
        window.workspace = keep

    pts = mesh_points(subjects)
    for area in bpy.data.screens["Layout"].areas:
        if area.type != 'VIEW_3D':
            continue
        space = area.spaces.active
        space.show_region_ui = True
        space.show_region_toolbar = True
        space.shading.type = 'SOLID'
        space.shading.light = 'STUDIO'
        space.shading.color_type = 'MATERIAL'
        space.overlay.show_floor = True
        frame_viewport(space, pts)

    scene.render.film_transparent = False
    scene.render.resolution_x, scene.render.resolution_y = 1920, 1080
    scene.render.resolution_percentage = 100
    cam_data = bpy.data.cameras.new("Camera")
    cam_data.lens = 50
    cam = bpy.data.objects.new("Camera", cam_data)
    scene.collection.objects.link(cam)
    scene.camera = cam
    fit_camera(cam, pts, view_direction(*CAMERA_ANGLE), (0.8, 0.72))

    scene.cursor.location = CURSOR
    for obj in bpy.data.objects:
        obj.select_set(False)
    rig.hide_set(True)  # bones stay usable, they just don't clutter the view
    bpy.context.view_layer.objects.active = None
    scene.frame_set(1)
    return cam


def scene_step(out_dir, preview_dir):
    mascot = load_mascot()
    cam = build_scene(mascot)
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "startup.blend")
    bpy.ops.wm.save_as_mainfile(filepath=path, compress=False, relative_remap=False)
    print(f"[b_clip] saved {path}")

    if preview_dir:
        os.makedirs(preview_dir, exist_ok=True)
        scene = bpy.context.scene
        scene.render.resolution_percentage = 50
        scene.render.engine = "BLENDER_EEVEE"
        scene.eevee.taa_render_samples = 64
        scene.camera = cam
        scene.render.filepath = os.path.join(preview_dir, "camera.png")
        bpy.ops.render.render(write_still=True)
        print(f"[b_clip] rendered {scene.render.filepath}")


# ---------------------------------------------------------------- layout (GUI)

def layout_step():
    """Close editors and save; must run with a real window (not -b)."""
    path = bpy.data.filepath
    steps = iter(range(5))

    def area_of(screen, kind):
        return next((a for a in screen.areas if a.type == kind), None)

    def tick():
        try:
            step = next(steps)
            win = bpy.context.window_manager.windows[0]
            screen = win.screen
            if step == 1:
                for kind in ('PROPERTIES', 'DOPESHEET_EDITOR'):
                    area = area_of(screen, kind)
                    if area:
                        with bpy.context.temp_override(window=win, screen=screen, area=area):
                            bpy.ops.screen.area_close()
            elif step == 2:
                outliner = area_of(screen, 'OUTLINER')
                if outliner and outliner.width > OUTLINER_WIDTH:
                    with bpy.context.temp_override(window=win, screen=screen):
                        bpy.ops.screen.area_move(
                            x=outliner.x - 1, y=outliner.y + outliner.height // 2,
                            delta=outliner.width - OUTLINER_WIDTH)
            elif step == 4:
                kinds = sorted(a.type for a in screen.areas)
                print(f"[b_clip] areas {kinds}")
                if kinds != ['OUTLINER', 'VIEW_3D']:
                    raise RuntimeError(f"unexpected areas {kinds}")
                bpy.ops.wm.save_as_mainfile(filepath=path, compress=False)
                n = blend_patch.patch_sidebar(path, SIDEBAR_WIDTH, SIDEBAR_TAB)
                print(f"[b_clip] patched {n} sidebar(s): {blend_patch.read_sidebar(path)}")
                sys.stdout.flush()
                os._exit(0)
            return 0.5  # let the window redraw between steps
        except Exception:
            import traceback
            traceback.print_exc()
            sys.stdout.flush()
            os._exit(1)

    bpy.app.timers.register(tick, first_interval=1.5)


# ---------------------------------------------------------------- splash

def hex_lin(mascot, h):
    return mascot.rgb(h)


def splash_world(mascot, glow_at, shadow_at, shadow_size):
    """Camera rays see a calm navy backdrop with a soft glow behind B and a
    contact shadow at his feet, drawn in screen space; everything else (the
    lighting) sees the mascot's usual world."""
    world = bpy.context.scene.world
    nt = world.node_tree
    nodes, links = nt.nodes, nt.links
    for n in list(nodes):
        nodes.remove(n)
    out = nodes.new("ShaderNodeOutputWorld")
    bg = nodes.new("ShaderNodeBackground")
    links.new(bg.outputs[0], out.inputs[0])

    def math_node(op, a, b=None):
        n = nodes.new("ShaderNodeMath")
        n.operation = op
        for i, v in enumerate((a, b)):
            if v is None:
                continue
            if isinstance(v, (int, float)):
                n.inputs[i].default_value = v
            else:
                links.new(v, n.inputs[i])
        return n.outputs[0]

    def mix(fac, a, b):
        n = nodes.new("ShaderNodeMix")
        n.data_type = 'RGBA'
        sockets = {s.identifier: s for s in n.inputs}
        for key, v in (("Factor_Float", fac), ("A_Color", a), ("B_Color", b)):
            if isinstance(v, (int, float, tuple)):
                sockets[key].default_value = v
            else:
                links.new(v, sockets[key])
        return next(s for s in n.outputs if s.identifier == "Result_Color")

    def smooth(value, lo, hi):
        n = nodes.new("ShaderNodeMapRange")
        n.interpolation_type = 'SMOOTHERSTEP'
        links.new(value, n.inputs["Value"])
        n.inputs["From Min"].default_value = lo
        n.inputs["From Max"].default_value = hi
        n.inputs["To Min"].default_value = 1.0
        n.inputs["To Max"].default_value = 0.0
        return n.outputs[0]

    def ellipse(cx, cy, rx, ry):
        dx = math_node('DIVIDE', math_node('SUBTRACT', sep.outputs[0], cx), rx)
        dy = math_node('DIVIDE', math_node('SUBTRACT', sep.outputs[1], cy), ry)
        return math_node('SQRT', math_node('ADD', math_node('MULTIPLY', dx, dx),
                                           math_node('MULTIPLY', dy, dy)))

    coords = nodes.new("ShaderNodeTexCoord")
    sep = nodes.new("ShaderNodeSeparateXYZ")
    links.new(coords.outputs["Window"], sep.inputs[0])

    base = mix(sep.outputs[1], hex_lin(mascot, "#0b1322"), hex_lin(mascot, "#15233f"))
    glow = smooth(ellipse(glow_at[0], glow_at[1], 0.42, 0.84), 0.0, 1.0)
    lit = mix(math_node('MULTIPLY', glow, 0.85), base, hex_lin(mascot, "#24467d"))
    shadow = smooth(ellipse(shadow_at[0], shadow_at[1], *shadow_size), 0.0, 1.0)
    backdrop = mix(math_node('MULTIPLY', shadow, 0.75), lit, hex_lin(mascot, "#03050a"))

    path = nodes.new("ShaderNodeLightPath")
    final = mix(path.outputs["Is Camera Ray"], (0.18 * 0.6, 0.2 * 0.6, 0.24 * 0.6, 1.0),
                backdrop)
    links.new(final, bg.inputs["Color"])
    bg.inputs["Strength"].default_value = 1.0


def camera_frame(cam, depth):
    """Camera-local centre, width and height of the frame at `depth`."""
    scene = bpy.context.scene
    data = cam.data
    width = depth * data.sensor_width / data.lens
    height = width * scene.render.resolution_y / scene.render.resolution_x
    return Vector((data.shift_x * width, data.shift_y * width, -depth)), width, height


def add_text(mascot, cam, depth, left, middle, px, max_px):
    """One line of Inter, flat to the camera; left/middle are NDC, px the em size."""
    scene = bpy.context.scene
    curve = bpy.data.curves.new("Splash text", "FONT")
    curve.body = SPLASH_TEXT
    font_path = os.path.join(bpy.utils.system_resource('DATAFILES', path="fonts"), "Inter.woff2")
    curve.font = bpy.data.fonts.load(font_path)
    curve.align_x = 'LEFT'
    curve.align_y = 'CENTER'
    obj = bpy.data.objects.new("Splash text", curve)
    scene.collection.objects.link(obj)
    obj.parent = cam
    obj.visible_shadow = False

    mat = bpy.data.materials.new("Splash text")
    mat.use_nodes = True
    nt = mat.node_tree
    for n in list(nt.nodes):
        nt.nodes.remove(n)
    emit = nt.nodes.new("ShaderNodeEmission")
    emit.inputs["Color"].default_value = mascot.rgb("#eef3fb")
    emit.inputs["Strength"].default_value = 3.0
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    nt.links.new(emit.outputs[0], out.inputs[0])
    curve.materials.append(mat)

    centre, w, h = camera_frame(cam, depth)
    res_y = scene.render.resolution_y
    res_x = scene.render.resolution_x
    curve.size = px / res_y * h
    curve.offset = curve.size * 0.005  # a touch heavier than Inter Regular
    bpy.context.view_layer.update()
    text_px = obj.dimensions.x / w * res_x
    if text_px > max_px:
        curve.size *= max_px / text_px
        curve.offset = curve.size * 0.005
        bpy.context.view_layer.update()
    obj.location = (centre.x + (left - 0.5) * w, centre.y + (middle - 0.5) * h, centre.z)
    print(f"[b_clip] splash text {obj.dimensions.x / w * res_x:.0f} px wide")
    return obj


def splash_step(out_dir, preview_dir):
    mascot = load_mascot()
    scene = bpy.context.scene
    pose = {**mascot.HERO, "look": (0.07, 0.02)}  # glancing toward the words
    subjects = mascot.build(pose)
    scene.render.resolution_x, scene.render.resolution_y = 1000, 500
    scene.render.resolution_percentage = 100
    scene.render.film_transparent = False

    cam_data = bpy.data.cameras.new("Splash camera")
    cam_data.lens = 70
    cam = bpy.data.objects.new("Splash camera", cam_data)
    scene.collection.objects.link(cam)
    scene.camera = cam
    d = view_direction(-34.0, 12.0)
    pts = mesh_points(subjects)
    # B fills the left of the frame; the text sits to his right; the
    # top-right corner stays empty for Blender's version label.
    mid, dist = fit_camera(cam, pts, d, (0.36, 0.8), centre=(0.25, 0.47))

    base = [p for p in pts if p.z < 0.05]
    feet = [world_to_camera_view(scene, cam, p) for p in base]
    fx = sum(c.x for c in feet) / len(feet)
    fy = min(c.y for c in feet)
    splash_world(mascot, glow_at=(0.25, 0.5), shadow_at=(fx, fy + 0.01),
                 shadow_size=(0.2, 0.06))

    add_text(mascot, cam, dist, left=0.5, middle=0.45, px=64, max_px=430)

    scene.render.engine = "BLENDER_EEVEE"
    scene.eevee.taa_render_samples = 128
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGB"
    os.makedirs(out_dir, exist_ok=True)
    scene.render.filepath = os.path.join(out_dir, "splash.png")
    bpy.ops.render.render(write_still=True)
    print(f"[b_clip] rendered {scene.render.filepath}")


def main():
    args = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    step = args[0] if args else ""
    if step == "scene":
        scene_step(args[1], args[2] if len(args) > 2 else None)
    elif step == "layout":
        layout_step()
    elif step == "splash":
        splash_step(args[1], args[2] if len(args) > 2 else None)
    else:
        raise SystemExit("usage: ... -- scene OUT [PREVIEWS] | layout | splash OUT")


main()
