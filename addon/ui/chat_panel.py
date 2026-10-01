"""BLENDERMCP_PT_Chat: View3D > Sidebar > BlenderMCP, below the status panel.

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
from ..chat import reader
from ..chat.state import (
    KEY_URL,
    TRIAL_LOW,
    approval_preview,
    chat_state,
    key_prompt_heading,
    trial_row_text,
    wrap_text,
)
from .icons import ensure_thinking_timer, icon_id, think_frame

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
    advisor = reader.model_label({"model": (snap["backend"] or {}).get("advisor")})
    if label and advisor:
        label += f" › {advisor.removeprefix('Claude ')}"
    row = layout.row(align=True)
    ico = icon_id("clip_mascot")
    kwargs = {"icon_value": ico} if ico else {"icon": 'MONKEY'}
    row.operator("blendermcp.chat_backend_settings",
                 text=label or "Choose a model", **kwargs)


def _draw_trial(layout, context, snap) -> None:
    """One quiet row under the model chip while free messages remain."""
    trial = snap["trial"]
    if not trial or trial["remaining"] <= 0 or snap["needs_key"]:
        return
    width = context.region.width if context.region else 300
    scale = _ui_scale(context)
    chars = int((width - 20 * scale) / (7 * scale))
    label, button = trial_row_text(trial["remaining"], chars)
    # Split by text length: a plain row halves the width and cuts the label.
    left, right = len(label) + 5, len(button) + 3
    split = layout.split(factor=left / (left + right), align=True)
    low = trial["remaining"] <= TRIAL_LOW
    text = split.row(align=True)
    text.alert = low
    text.label(text=label, icon='ERROR' if low else 'INFO')
    split.operator("blendermcp.chat_key_prompt", text=button,
                   depress=bool(snap["key_prompt_requested"]))


def _draw_key_prompt(layout, context, snap, wrap) -> None:
    """The box for the user's own Claude API key, above the input row.
    Drawn only while connected (no notice), so Save key can reach the server."""
    heading = key_prompt_heading(snap)
    if heading is None:
        return
    box = layout.box()
    col = box.column(align=True)
    _label_lines(col, wrap(heading, reserve=40.0), 'KEYINGSET')
    col.separator(factor=0.5)
    checking = bool(snap["key_checking"])
    field = col.row(align=True)
    field.enabled = not checking
    field.prop(context.window_manager, "blendermcp_chat_key", text="")
    row = col.row(align=True)
    row.operator("wm.url_open", text="Get a key", icon='URL').url = KEY_URL
    save = row.row(align=True)
    save.enabled = not checking
    save.operator("blendermcp.chat_save_key", text="Save key", icon='CHECKMARK')
    if checking:
        col.separator(factor=0.5)
        col.label(text="Checking key…", icon='TIME')
    elif snap["key_error"]:
        col.separator(factor=0.5)
        _label_lines(col.column(align=True), wrap(snap["key_error"], reserve=40.0),
                     'ERROR', alert=True)
    col.separator(factor=0.5)
    note = col.column(align=True)
    note.enabled = False
    note.scale_y = 0.8
    for line in wrap("Sent once to the server and stored encrypted; "
                     "never saved in Blender.", reserve=30.0):
        note.label(text=line)


def _draw_clip(layout, context) -> None:
    """The binder clip: an icon button at the start of the input row. When
    something is clipped it grips paper and shows a count; the popover says what."""
    wm = context.window_manager
    n = (len(context.selected_objects) if wm.blendermcp_clip_selection else 0) \
        + int(bool(wm.blendermcp_clip_viewport)) + int(bool(wm.blendermcp_clip_text))
    ico = (n and icon_id("binder_clip_full")) or icon_id("binder_clip")
    kwargs = {"icon_value": ico} if ico else {"icon": 'LINKED'}
    layout.popover("BLENDERMCP_PT_ChatClip", text=str(n) if n else "", **kwargs)


def _latest_turn(messages: list) -> dict | None:
    turns = reader.turns(messages)
    return turns[-1] if turns else None


def _mascot_label(layout, text: str) -> None:
    """A label with Clip, the assistant's avatar (MONKEY if the icon didn't load)."""
    ico = icon_id("clip_mascot")
    if ico:
        layout.label(text=text, icon_value=ico)
    else:
        layout.label(text=text, icon='MONKEY')


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
        status = f"{snap['status'] or 'Working…'}{steps}{elapsed}"
        # Redraw on a timer while the turn runs: it plays Clip's thinking loop
        # and ticks the seconds. Started even before a frame is ready, since
        # previews load lazily and report icon 0 until a later redraw.
        ensure_thinking_timer()
        frame = think_frame(_time.monotonic())
        if frame:
            row = col.row(align=True)
            row.template_icon(icon_value=frame, scale=2.2)
            text = row.column(align=True)
            text.separator(factor=1.4)
            for line in wrap(status, reserve=90.0)[:2]:
                text.label(text=line)
        else:
            col.label(text=status, icon='SORTTIME')
        live = next((r for r in reversed(turn["replies"]) if r.get("streaming")), None)
        if live and (live.get("text") or "").strip():
            # The newest lines of the reply as it streams (it grows downward).
            lines = []
            for para in reader.plain(live["text"]).split("\n"):
                if para.strip():
                    lines.extend(wrap(para, reserve=30.0) or [""])
            col.separator(factor=0.4)
            for line in lines[-REPLY_LINES:]:
                col.label(text=line)
        return

    backend = snap["backend_used"] or snap["backend"]
    who = (reader.model_label(backend) or "Assistant").split(" ")[0]
    failed = sum(1 for s in turn["steps"] if s.get("ok") is False)
    header = reader.reply_header(turn, who)
    if failed:
        header += f" · {failed} failed"
    _mascot_label(col, header)

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
        sub.label(text="Not undoable in one step; use Ctrl+Z", icon='INFO')


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
    bl_category = 'BlenderMCP'
    bl_order = 1  # after the status panel and its subpanels

    @classmethod
    def poll(cls, context):
        return chat_state.available is not False or chat_state.has_messages()

    def draw_header(self, context):
        ico = icon_id("clip_mascot")
        if ico:
            self.layout.label(text="", icon_value=ico)

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
            _draw_trial(layout, context, snap)

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
            _draw_key_prompt(layout, context, snap, wrap)
        # The server refuses every turn until a key is saved, so don't offer one.
        can_send = notice is None and not snap["busy"] and not snap["needs_key"]
        row = layout.row(align=True)
        row.scale_y = 1.25
        if not notice:
            _draw_clip(row, context)
        field = row.row(align=True)
        field.enabled = can_send
        field.prop(context.window_manager, "blendermcp_chat_input", text="")
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
