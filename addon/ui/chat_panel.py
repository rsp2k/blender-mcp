"""BLENDERMCP_PT_Chat: View3D > Sidebar > Chat.

Reads chat_state snapshots only; every action goes through the
chat_operators. The tab hides itself once a registration says the
server has no chat, unless there's a conversation to show.
"""

from __future__ import annotations

import time as _time

import bpy

from .. import connection, state
from ..chat.state import approval_preview, chat_state, wrap_text
from .chat_transcript import draw_transcript

PREVIEW_LINES = 8


def _ui_scale(context) -> float:
    prefs = context.preferences
    # system.ui_scale folds in the HiDPI pixel size; view.ui_scale is the slider.
    scale = getattr(prefs.system, "ui_scale", 0) or getattr(prefs.view, "ui_scale", 1.0)
    return float(scale or 1.0)


def _wrapper(context):
    width = context.region.width if context.region else 300
    scale = _ui_scale(context)
    return lambda text, reserve=40.0: wrap_text(text, width, scale, reserve_px=reserve)


def _label_lines(col, lines, icon, alert=False):
    col.alert = alert
    for i, line in enumerate(lines):
        col.label(text=line, icon=icon if i == 0 else 'BLANK1')


def _notice(context, snap) -> tuple[str, str] | None:
    from ..preferences import get_prefs
    if not get_prefs(context).jwt_token:
        return "Log in from the BlenderMCP tab to chat.", 'LOCKED'
    client = state._client
    if not (connection.client_alive(client) and client.connected):
        return "Not connected. Turn on Connect in the BlenderMCP tab.", 'UNLINKED'
    if snap["available"] is None:
        return "Checking whether this server offers chat…", 'TIME'
    if snap["available"] is False:
        return "This server doesn't offer chat.", 'INFO'
    return None


def _draw_approval(layout, snap, wrap) -> None:
    prompt = snap["approval"]
    if not prompt:
        return
    box = layout.box()
    col = box.column(align=True)
    lines, cut = approval_preview(prompt, PREVIEW_LINES)
    first = True
    for line in lines:
        for piece in wrap(line, reserve=30.0) or [""]:
            # The server's first line says what the model wants to do.
            col.label(text=piece[:1].upper() + piece[1:] if first else piece,
                      icon='HAND' if first else 'NONE')
            first = False
    if cut:
        col.label(text=f"…{cut} more line(s) in the log", icon='TEXT')
    if snap["approval_since"]:
        left = max(0, int(120 - (_time.monotonic() - snap["approval_since"])))
        col.label(text=f"Declines itself in about {left}s")
    row = col.row(align=True)
    row.operator("blendermcp.chat_allow", text="Allow", icon='CHECKMARK')
    row.operator("blendermcp.chat_deny", text="Deny", icon='CANCEL')


class BLENDERMCP_PT_Chat(bpy.types.Panel):
    bl_label = "Chat"
    bl_idname = "BLENDERMCP_PT_Chat"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Chat'

    @classmethod
    def poll(cls, context):
        return chat_state.available is not False or chat_state.has_messages()

    def draw(self, context):
        layout = self.layout
        snap = chat_state.snapshot()
        wrap = _wrapper(context)

        row = layout.row(align=True)
        row.enabled = not snap["busy"]
        row.prop(context.window_manager, "blendermcp_chat_conversation", text="", icon='OUTLINER_DATA_GP_LAYER')
        row.operator("blendermcp.chat_new", text="", icon='ADD')

        notice = _notice(context, snap)
        if notice:
            _label_lines(layout.column(align=True), wrap(notice[0]), notice[1])

        _draw_approval(layout, snap, wrap)

        messages = snap["messages"]
        if messages:
            draw_transcript(layout, context)
        elif not notice:
            layout.label(text="Ask for something in this scene.", icon='MONKEY')

        if snap["busy"] and not messages:
            elapsed = ""
            if snap["turn_started_at"]:
                elapsed = f"  {int(_time.monotonic() - snap['turn_started_at'])}s"
            layout.label(text=f"{snap['status'] or 'Working…'}{elapsed}", icon='SORTTIME')
        elif snap["last_error"] and not any(
                m.get("role") == "error" and m.get("text") == snap["last_error"] for m in messages):
            # Local problems (not connected, etc.) that never became a message.
            _label_lines(layout.column(align=True), wrap(snap["last_error"]), 'ERROR', alert=True)

        can_send = notice is None and not snap["busy"]
        row = layout.row(align=True)
        field = row.row(align=True)
        field.enabled = can_send
        field.prop(context.window_manager, "blendermcp_chat_input", text="", icon='GREASEPENCIL')
        if snap["busy"]:
            row.operator("blendermcp.chat_stop", text="", icon='CANCEL')
        else:
            send = row.row(align=True)
            send.enabled = can_send
            send.operator("blendermcp.chat_send", text="", icon='PLAY')

        row = layout.row(align=True)
        row.operator("blendermcp.chat_open_log", text="Open log", icon='TEXT')
        row.operator("blendermcp.chat_clear", text="Delete chat", icon='TRASH')
        used = snap["backend_used"]
        if used and used.get("model"):
            sub = layout.row()
            sub.scale_y = 0.7
            sub.label(text=f"{used.get('model')} via {used.get('provider', '?')}")
