"""B. Clip, a starter template for people new to Blender.

Opens on B, the BlenderMCP mascot, with the 3D Viewport sidebar on the
BlenderMCP tab so the Chat panel is the first thing you see. This module
adds one small panel to that tab: starter prompts when BlenderMCP is
ready, or how to get it when it isn't. If BlenderMCP is installed but
turned off, it is turned on shortly after startup.

register() and unregister() are symmetric: File > New > General runs
unregister() in the same session.
"""

import textwrap

import addon_utils
import bpy

PACKAGE = "blender_mcp"  # the extension's module, under bl_ext.<repo>.
INSTALL_URL = "https://mcp.blender.bet/install"
GUIDE_URL = "https://docs.blender.bet/tutorials/quickstart/"
STARTERS = ("Make B wave", "Give B a top hat", "Put B on a desk")


def _is_blender_mcp(name: str) -> bool:
    return name == PACKAGE or name.endswith("." + PACKAGE)


def blender_mcp_status() -> tuple:
    """("ready" | "off" | "missing", module name or None)."""
    for name in bpy.context.preferences.addons.keys():
        if _is_blender_mcp(name):
            return "ready", name
    try:
        modules = addon_utils.modules(refresh=False)
    except Exception:
        modules = []
    for mod in modules:
        if _is_blender_mcp(mod.__name__):
            return "off", mod.__name__
    return "missing", None


def enable_blender_mcp() -> str:
    """Turn BlenderMCP on if it's installed but off; returns the status."""
    status, name = blender_mcp_status()
    if status == "off":
        try:
            addon_utils.enable(name, default_set=True)
        except Exception as exc:
            print(f"B. Clip: could not turn on {name}: {exc}")
        status, _ = blender_mcp_status()
    return status


def _after_startup():
    enable_blender_mcp()
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()
    return None  # run once


def _wrapped(layout, context, text, icon='NONE'):
    region = context.region
    scale = context.preferences.view.ui_scale
    width = max(18, int((region.width if region else 300) / (7 * scale)))
    col = layout.column(align=True)
    indent = 'NONE' if icon == 'NONE' else 'BLANK1'
    for i, line in enumerate(textwrap.wrap(text, width)):
        col.label(text=line, icon=icon if i == 0 else indent)


class BCLIP_OT_starter(bpy.types.Operator):
    """Send this prompt to Claude in the Chat panel"""
    bl_idname = "bclip.starter"
    bl_label = "Starter prompt"
    bl_options = {'INTERNAL'}

    prompt: bpy.props.StringProperty()

    def execute(self, context):
        wm = context.window_manager
        if hasattr(wm, "blendermcp_chat_input"):
            # The chat field sends on change; if it can't send, the text stays
            # in the field for the Send button.
            wm.blendermcp_chat_input = self.prompt
        else:
            wm.clipboard = self.prompt
            self.report({'INFO'}, "Copied. Paste it into the BlenderMCP chat.")
        return {'FINISHED'}


class BCLIP_OT_enable(bpy.types.Operator):
    """Turn on the BlenderMCP add-on"""
    bl_idname = "bclip.enable_blender_mcp"
    bl_label = "Turn on BlenderMCP"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        if enable_blender_mcp() != "ready":
            self.report({'WARNING'}, "BlenderMCP didn't start. Try restarting Blender.")
        return {'FINISHED'}


class BCLIP_PT_start(bpy.types.Panel):
    bl_label = "B. Clip"
    bl_idname = "BCLIP_PT_start"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'BlenderMCP'  # same tab as the Chat, so it exists without it
    bl_order = 2  # below the Chat panel

    def draw(self, context):
        layout = self.layout
        status, _ = blender_mcp_status()
        if status == "ready":
            layout.label(text="Try one of these:")
            col = layout.column(align=True)
            for text in STARTERS:
                col.operator(BCLIP_OT_starter.bl_idname, text=text,
                             icon='PLAY').prompt = text
            return
        if status == "off":
            _wrapped(layout, context, "BlenderMCP is installed but turned off.", 'INFO')
            layout.operator(BCLIP_OT_enable.bl_idname, icon='CHECKMARK')
            return
        _wrapped(layout, context,
                 "Hi, I'm B. To ask Claude for changes right here, "
                 "install the free BlenderMCP add-on.", 'INFO')
        col = layout.column(align=True)
        col.scale_y = 1.3
        col.operator("wm.url_open", text="Install BlenderMCP", icon='URL').url = INSTALL_URL
        layout.operator("wm.url_open", text="Quickstart guide", icon='HELP').url = GUIDE_URL
        _wrapped(layout.box(), context, "Then ask things like: " + ", ".join(
            f'"{s}"' for s in STARTERS) + ".")


CLASSES = (BCLIP_OT_starter, BCLIP_OT_enable, BCLIP_PT_start)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    if not bpy.app.timers.is_registered(_after_startup):
        bpy.app.timers.register(_after_startup, first_interval=0.5)


def unregister():
    if bpy.app.timers.is_registered(_after_startup):
        bpy.app.timers.unregister(_after_startup)
    for cls in reversed(CLASSES):
        if cls.is_registered:
            bpy.utils.unregister_class(cls)
