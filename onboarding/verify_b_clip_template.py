"""Checks for the B. Clip template, run inside Blender by b_clip_template.sh.

Each mode is one Blender run against a throwaway HOME:

    install [stub]   copy app_template/B_Clip into the user templates dir;
                     "stub" also installs a fake BlenderMCP extension (off)
    install-control  the template minus userpref.blend, as Control_NoPrefs
    template         (with --app-template) register, scene, layout, menu
                     placement, File > New > General and back
    enable-nw        stock: turn on node_wrangler, save prefs
    template-save    (with --app-template) save prefs inside the template
    check-nw         stock: node_wrangler must still be on
    check-nw-control stock: report whether the control lost it
    stub             (with --app-template) the template turns BlenderMCP on
    check-stub-off   stock: that stayed inside the template's prefs
    gui MODE         (GUI) screenshot the first look, check the sidebar tab

Exits 1 on the first failed check.
"""

import os
import shutil
import subprocess
import sys

import addon_utils
import bpy

TEMPLATE = "B_Clip"
SRC = "/work/app_template/" + TEMPLATE
PREVIEWS = "/work/onboarding/b_clip_previews"
sys.path.insert(0, "/work/onboarding")


def say(msg):
    print(f"[verify] {msg}", flush=True)


def check(ok, msg):
    if not ok:
        say(f"FAIL: {msg}")
        sys.stdout.flush()
        os._exit(1)
    say(f"ok: {msg}")


def templates_dir():
    return bpy.utils.user_resource('SCRIPTS', path="startup/bl_app_templates_user", create=True)


def user_repo_dir():
    for repo in bpy.context.preferences.extensions.repos:
        if repo.module == "user_default":
            return repo.directory
    raise RuntimeError("no user_default extension repo")


STUB_MANIFEST = """schema_version = "1.0.0"
id = "blender_mcp"
version = "0.0.1"
name = "BlenderMCP (test stub)"
tagline = "Stands in for BlenderMCP in template tests"
maintainer = "test"
type = "add-on"
blender_version_min = "4.2.0"
license = ["SPDX:GPL-3.0-or-later"]
"""

STUB_INIT = '''import bpy

def _sent(wm, context):
    print("[stub] chat input:", wm.blendermcp_chat_input, flush=True)

class STUB_PT_chat(bpy.types.Panel):
    bl_label = "Chat"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'BlenderMCP'
    bl_order = 1
    def draw(self, context):
        self.layout.prop(context.window_manager, "blendermcp_chat_input", text="")

def register():
    bpy.types.WindowManager.blendermcp_chat_input = bpy.props.StringProperty(update=_sent)
    bpy.utils.register_class(STUB_PT_chat)

def unregister():
    bpy.utils.unregister_class(STUB_PT_chat)
    del bpy.types.WindowManager.blendermcp_chat_input
'''


def install(args):
    dst = os.path.join(templates_dir(), TEMPLATE)
    shutil.copytree(SRC, dst, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__"))
    say(f"installed template at {dst}")
    if "stub" in args:
        pkg = os.path.join(user_repo_dir(), "blender_mcp")
        os.makedirs(pkg, exist_ok=True)
        with open(os.path.join(pkg, "blender_manifest.toml"), "w") as fh:
            fh.write(STUB_MANIFEST)
        with open(os.path.join(pkg, "__init__.py"), "w") as fh:
            fh.write(STUB_INIT)
        say(f"installed BlenderMCP stub at {pkg}")


def install_control():
    dst = os.path.join(templates_dir(), "Control_NoPrefs")
    shutil.copytree(SRC, dst, dirs_exist_ok=True, ignore=shutil.ignore_patterns("userpref.blend", "__pycache__"))


def template_module():
    import bl_app_template_utils  # templates live here, not in sys.modules
    return bl_app_template_utils._modules.get(TEMPLATE)


def our_classes_registered():
    return [hasattr(bpy.types, n) for n in ("BCLIP_PT_start", "BCLIP_OT_starter")]


def check_scene():
    objs = bpy.data.objects
    for name in ("Clip body", "Handle front", "Handle back", "Eye L", "Eye R",
                 "Pupil L", "Pupil R", "Smile", "B. Clip", "Camera"):
        check(name in objs, f"startup.blend has '{name}'")
    scene = bpy.context.scene
    check(scene.camera and scene.camera.name == "Camera", "scene camera is set")
    body = objs["Clip body"]
    dg = bpy.context.evaluated_depsgraph_get()
    ev = body.evaluated_get(dg)
    zs = [(ev.matrix_world @ v.co).z for v in ev.data.vertices]
    xs = [(ev.matrix_world @ v.co).x for v in ev.data.vertices]
    check(abs(min(zs)) < 0.02, f"B stands on the ground (lowest z {min(zs):.3f})")
    check(abs((min(xs) + max(xs)) / 2) < 0.05, "B is centred at the origin")
    check(1.5 < max(xs) - min(xs) < 3.0, f"B is about cube-sized ({max(xs) - min(xs):.2f} m wide)")


def check_layout():
    from blend_patch import read_sidebar
    screen = bpy.data.screens["Layout"]
    kinds = sorted(a.type for a in screen.areas)
    check(kinds == ['OUTLINER', 'VIEW_3D'], f"only a 3D Viewport and Outliner ({kinds})")
    view = next(a for a in screen.areas if a.type == 'VIEW_3D')
    check(view.spaces.active.show_region_ui, "the sidebar is open")
    check(not view.spaces.active.overlay.show_extras, "light and camera overlays are off")
    ui = next(r for r in view.regions if r.type == 'UI')
    check(ui.width >= 300, f"the sidebar is wide ({ui.width} px)")
    check([w.name for w in bpy.data.workspaces] == ["Layout"], "only the Layout workspace")
    rows = read_sidebar(os.path.join(SRC, "startup.blend"))
    check(rows and all(r["categories"][:1] == ["BlenderMCP"] for r in rows),
          f"the saved sidebar tab is BlenderMCP ({rows})")


def check_menu_placement():
    paths = bpy.types.TOPBAR_MT_file_new.app_template_paths()
    limit = 6  # TOPBAR_MT_file_new.draw_ex splash_limit
    splash = paths[:limit - 2] if len(paths) > limit - 1 else paths
    say(f"templates {paths}; splash quick-start {splash}")
    check(TEMPLATE in splash, f"{TEMPLATE} is on the splash's quick-start list")
    check(bpy.path.display_name(TEMPLATE) == "B Clip", "menu label is 'B Clip'")


def template_checks():
    prefs = bpy.context.preferences
    check(prefs.app_template == TEMPLATE, f"running in the {TEMPLATE} template")
    check(all(our_classes_registered()), "register() added the panel and operator")
    mod = template_module()
    check(mod is not None, "template module imported")
    check(bpy.app.timers.is_registered(mod._after_startup), "startup timer queued")
    check(mod.blender_mcp_status() == ("missing", None), "BlenderMCP reported missing")
    check(not [k for k in prefs.addons.keys() if k.startswith("bl_ext.")],
          "template prefs list no extensions")
    check_scene()
    check_layout()
    check_menu_placement()

    # File > New > General: unregister() must undo everything.
    bpy.ops.wm.read_homefile(app_template="")
    check(prefs.app_template == "", "File > New > General left the template")
    check(not any(our_classes_registered()), "unregister() removed the panel and operator")
    check(not bpy.app.timers.is_registered(mod._after_startup), "unregister() dropped the timer")
    ws_factory = len(bpy.data.workspaces)
    check(ws_factory > 1, f"General has its usual workspaces ({ws_factory})")

    # And back again.
    bpy.ops.wm.read_homefile(app_template=TEMPLATE)
    check(prefs.app_template == TEMPLATE, "File > New > B Clip re-entered the template")
    check(all(our_classes_registered()), "register() ran again")
    mod = template_module()
    mod.unregister()
    mod.unregister()  # twice must be harmless
    check(not any(our_classes_registered()), "direct unregister() is clean and repeatable")
    mod.register()
    check(all(our_classes_registered()), "direct register() works after unregister()")


def enable_nw():
    addon_utils.enable("node_wrangler", default_set=True)
    bpy.ops.wm.save_userpref()
    check("node_wrangler" in bpy.context.preferences.addons, "stock: node_wrangler on, saved")


def template_save():
    on = "node_wrangler" in bpy.context.preferences.addons
    say(f"inside template {bpy.context.preferences.app_template}: node_wrangler on = {on}")
    bpy.ops.wm.save_userpref()
    cfg = bpy.utils.user_resource('CONFIG')
    tpl_prefs = os.path.join(cfg, bpy.context.preferences.app_template, "userpref.blend")
    say(f"template prefs file exists: {os.path.exists(tpl_prefs)}")


def check_nw():
    check("node_wrangler" in bpy.context.preferences.addons,
          "main prefs still have node_wrangler after a save inside the template")


def check_nw_control():
    lost = "node_wrangler" not in bpy.context.preferences.addons
    say("control: main prefs " + ("LOST node_wrangler (the bug userpref.blend prevents)"
                                  if lost else "kept node_wrangler (bug not reproduced)"))


def stub_checks():
    cfg = bpy.utils.user_resource('CONFIG')
    mod = template_module()
    status, name = mod.blender_mcp_status()
    check(status == "off" and name == "bl_ext.user_default.blender_mcp",
          f"stub found installed but off ({status}, {name})")
    mod._after_startup()  # what the startup timer does
    status, name = mod.blender_mcp_status()
    check(status == "ready", f"the template turned BlenderMCP on ({name})")
    bpy.ops.bclip.starter(prompt="Make B wave")
    check(bpy.context.window_manager.blendermcp_chat_input == "Make B wave",
          "a starter prompt lands in the chat field")
    bpy.ops.wm.save_userpref()
    check(os.path.exists(os.path.join(cfg, TEMPLATE, "userpref.blend")),
          "saving inside the template writes config/B_Clip/userpref.blend")


def check_stub_off():
    # Blender also rewrites the main file on that save, but only with the
    # shared settings; the add-on list there must stay as it was.
    check("bl_ext.user_default.blender_mcp" not in bpy.context.preferences.addons,
          "BlenderMCP turned on inside the template stays out of the main prefs")


# ---------------------------------------------------------------- GUI

def grab(name):
    path = os.path.join(PREVIEWS, name)
    code = (f"from PIL import ImageGrab; "
            f"ImageGrab.grab(xdisplay={os.environ['DISPLAY']!r}).save({path!r})")
    subprocess.run(["/usr/bin/python3", "-c", code], check=True)
    say(f"screenshot {path}")


def gui(mode):
    os.makedirs(PREVIEWS, exist_ok=True)
    steps = iter(range(10))

    def tick():
        try:
            step = next(steps)
            if step == 0:
                return 3.0  # let startup timers run and the splash settle
            if step == 1:
                grab(f"gui-{mode}-splash.png")
                subprocess.run(["xdotool", "mousemove", "960", "540", "key", "Escape"])
                return 1.5
            win = bpy.context.window_manager.windows[0]
            view = next(a for a in win.screen.areas if a.type == 'VIEW_3D')
            ui = next(r for r in view.regions if r.type == 'UI')
            grab(f"gui-{mode}.png")
            cat = ui.active_panel_category
            check(cat == "BlenderMCP", f"sidebar opens on the BlenderMCP tab ({cat!r})")
            check(ui.width >= 300, f"sidebar is {ui.width} px wide")
            mod = template_module()
            want = "missing" if mode == "missing" else "ready"
            check(mod.blender_mcp_status()[0] == want, f"BlenderMCP status is {want}")
            sys.stdout.flush()
            os._exit(0)
        except Exception:
            import traceback
            traceback.print_exc()
            sys.stdout.flush()
            os._exit(1)

    bpy.app.timers.register(tick, first_interval=0.5)


def main():
    args = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    mode = args[0] if args else ""
    if mode == "install":
        install(args[1:])
    elif mode == "install-control":
        install_control()
    elif mode == "template":
        template_checks()
    elif mode == "enable-nw":
        enable_nw()
    elif mode == "template-save":
        template_save()
    elif mode == "check-nw":
        check_nw()
    elif mode == "check-nw-control":
        check_nw_control()
    elif mode == "stub":
        stub_checks()
    elif mode == "check-stub-off":
        check_stub_off()
    elif mode == "gui":
        gui(args[1])
        return
    else:
        raise SystemExit(f"unknown mode {mode!r}")
    sys.stdout.flush()


main()
