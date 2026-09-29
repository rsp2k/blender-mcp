"""BLENDERMCP_PT_Chat: View3D > Sidebar > Chat.

The sidebar is the chat's command line: what model answers, what's
clipped to the next message, the latest turn at a glance, undo, and the
input. The full conversation reads in the Reader (reader.py), a Text
Editor split beside the viewport. Reads chat_state snapshots only; every
action goes through the chat_operators.
"""

from __future__ import annotations

import time as _time

import bpy

from .. import connection, state
from ..chat import clip, reader
from ..chat.state import approval_preview, chat_state, wrap_text
from .icons import icon_id

PREVIEW_LINES = 8
REPLY_LINES = 6
APPROVAL_NOTES = ("Allowed.", "Declined.")


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
        col.label(text=f"…{cut} more line(s) in the Reader", icon='TEXT')
    if snap["approval_since"]:
        left = max(0, int(120 - (_time.monotonic() - snap["approval_since"])))
        col.label(text=f"Declines itself in about {left}s")
    row = col.row(align=True)
    row.scale_y = 1.3
    row.operator("blendermcp.chat_allow", text="Allow", icon='CHECKMARK')
    row.operator("blendermcp.chat_deny", text="Deny", icon='CANCEL')


def _draw_model_chip(layout, snap) -> None:
    backend = snap["backend_used"] or snap["backend"]
    label = reader.model_label(backend)
    row = layout.row(align=True)
    row.operator("blendermcp.chat_backend_settings",
                 text=label or "Choose a model", icon='MONKEY')


def _draw_clip(layout, context) -> None:
    ico = icon_id("binder_clip")
    kwargs = {"icon_value": ico} if ico else {"icon": 'LINKED'}
    layout.popover("BLENDERMCP_PT_ChatClip", text=clip.summary(context), **kwargs)


def _latest_turn(messages: list) -> dict | None:
    turns = reader.turns(messages)
    return turns[-1] if turns else None


def _draw_turn(layout, context, snap, wrap) -> None:
    turn = _latest_turn(snap["messages"])
    if turn is None:
        col = layout.column(align=True)
        col.enabled = False
        _label_lines(col, wrap("Ask for something in this scene. Each change is one "
                               "Ctrl+Z step, and replies open in the Reader."), 'INFO')
        return

    box = layout.box()
    col = box.column(align=True)
    user = turn["user"]
    if user:
        you = col.column(align=True)
        you.enabled = False  # the question is context; the answer is the point
        lines = wrap(reader.plain(user.get("text") or ""))
        _label_lines(you, lines[:2] + (["…"] if len(lines) > 2 else []), 'USER')
        col.separator(factor=0.6)

    if snap["busy"]:
        elapsed = ""
        if snap["turn_started_at"]:
            elapsed = f"  {int(_time.monotonic() - snap['turn_started_at'])}s"
        n = len(turn["steps"])
        steps = f" · step {n}" if n else ""
        col.label(text=f"{snap['status'] or 'Working…'}{steps}{elapsed}", icon='SORTTIME')
        return

    backend = snap["backend_used"] or snap["backend"]
    who = (reader.model_label(backend) or "Assistant").split(" ")[0]
    failed = sum(1 for s in turn["steps"] if s.get("ok") is False)
    header = reader.reply_header(turn, who)
    if failed:
        header += f" · {failed} failed"
    col.label(text=header, icon='MONKEY')

    # The last reply is the answer; earlier ones are narration between steps.
    reply = (turn["replies"][-1].get("text") or "") if turn["replies"] else ""
    cut = False
    if reply.strip():
        lines, cut = reader.preview(reply, lambda t: wrap(t, reserve=30.0), REPLY_LINES)
        for line in lines:
            col.label(text=line)
    for note in turn["notes"]:
        if note.get("role") == "status" and (note.get("text") or "").strip() in APPROVAL_NOTES:
            continue  # the step list already says what was allowed or declined
        _label_lines(col.column(align=True), wrap(reader.plain(note.get("text") or "")),
                     'ERROR' if note.get("role") == "error" else 'INFO',
                     alert=note.get("role") == "error")

    if (cut or failed or len(turn["steps"]) > 0) and not reader.is_open(context):
        row = col.row()
        row.operator("blendermcp.chat_reader",
                     text="Read the rest in the Reader" if cut else "Details in the Reader",
                     icon='TEXT')


def _draw_undo(layout, snap) -> None:
    started = snap["turn_started_at"]
    if snap["busy"] or not started:
        return
    try:
        from .. import undo_steps
        info = undo_steps.summary_since(started)
    except Exception:  # noqa: BLE001 - never break the panel over undo bookkeeping
        return
    n = info.get("steps") or 0
    if not n:
        return
    col = layout.column(align=True)
    row = col.row(align=True)
    row.enabled = bool(info.get("can_undo"))
    row.operator("blendermcp.chat_undo_turn",
                 text=f"Undo last chat change ({n} step{'s' if n != 1 else ''})",
                 icon='LOOP_BACK')
    if not info.get("can_undo") and info.get("reason"):
        sub = col.column(align=True)
        sub.enabled = False
        sub.scale_y = 0.8
        sub.label(text="Use Ctrl+Z: the scene changed since", icon='INFO')


class BLENDERMCP_PT_ChatClip(bpy.types.Panel):
    """The binder clip: what goes with the next message."""

    bl_label = "Clip to message"
    bl_idname = "BLENDERMCP_PT_ChatClip"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'HEADER'
    bl_ui_units_x = 13

    def draw(self, context):
        layout = self.layout
        wm = context.window_manager
        layout.label(text="Clip to your message")
        col = layout.column(align=True)
        n = len(context.selected_objects)
        col.prop(wm, "blendermcp_clip_selection",
                 text=f"Selection ({n} object{'s' if n != 1 else ''})" if n else "Selection (none)")
        col.prop(wm, "blendermcp_clip_viewport", text="What I see in the viewport")
        layout.prop_search(wm, "blendermcp_clip_text", bpy.data, "texts",
                           text="", icon='TEXT')
        sub = layout.column(align=True)
        sub.enabled = False
        sub.scale_y = 0.8
        sub.label(text="Viewport and text go with the next")
        sub.label(text="message only; selection stays on.")


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
        row.prop(context.window_manager, "blendermcp_chat_conversation", text="",
                 icon='OUTLINER_DATA_GP_LAYER')
        row.operator("blendermcp.chat_new", text="", icon='ADD')

        notice = _notice(context, snap)
        if notice:
            _label_lines(layout.column(align=True), wrap(notice[0]), notice[1])
        else:
            _draw_model_chip(layout, snap)

        _draw_approval(layout, snap, wrap)
        _draw_turn(layout, context, snap, wrap)
        _draw_undo(layout, snap)

        if snap["last_error"] and not snap["busy"] and not any(
                m.get("role") == "error" and m.get("text") == snap["last_error"]
                for m in snap["messages"]):
            # Local problems (not connected, etc.) that never became a message.
            _label_lines(layout.column(align=True), wrap(snap["last_error"]), 'ERROR', alert=True)

        layout.separator(factor=0.5)
        if not notice:
            _draw_clip(layout, context)
        can_send = notice is None and not snap["busy"]
        row = layout.row(align=True)
        row.scale_y = 1.25
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
        open_ = reader.is_open(context)
        row.operator("blendermcp.chat_reader", text="Close Reader" if open_ else "Reader",
                     icon='TEXT', depress=open_)
        row.operator("blendermcp.chat_clear", text="", icon='TRASH')
