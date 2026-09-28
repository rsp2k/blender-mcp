"""The Chat tab's scrolling transcript and conversation picker.

The transcript is a UIList over wrapped message rows kept on the
WindowManager, so Blender gives it scrolling and a drag handle to resize.
Draw callbacks may not write ID data, so the panel only notices that the
transcript changed (chat_state.revision, or the panel width) and a one-shot
timer rebuilds the rows. The last row is an empty spacer kept active, which
is what scrolls the list to the newest message.
"""

from __future__ import annotations

import bpy

from ..chat.state import chat_state, transcript_rows, wrap_text

# (revision, width bucket) the rows were last built for.
_built_for: list = [None]
_pending_width: list = [0]
# EnumProperty item strings must stay referenced while Blender shows them.
_conversation_items: list = []


class BLENDERMCP_PG_ChatRow(bpy.types.PropertyGroup):
    text: bpy.props.StringProperty()
    role: bpy.props.StringProperty()
    icon: bpy.props.StringProperty(default="NONE")


class BLENDERMCP_UL_ChatTranscript(bpy.types.UIList):
    bl_idname = "BLENDERMCP_UL_ChatTranscript"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        role, ico = item.role, (item.icon or "NONE")
        if role == "spacer":
            # Always the active (highlighted) row, so make it the status line.
            layout.label(text=_status_text(), icon='SORTTIME' if chat_state.busy else 'NONE')
            return
        if role == "user":
            # Your messages sit to the right, like any chat app.
            split = layout.split(factor=0.18)
            split.label(text="")
            split.label(text=item.text, icon=ico)
            return
        row = layout.row()
        if role == "error":
            row.alert = True
        if role in ("tool", "status"):
            row.enabled = False  # dimmed: steps and notes, not conversation
        row.label(text=item.text, icon=ico)

    def filter_items(self, context, data, propname):
        # Keep the list's own filter/sort UI out of the way.
        return [], []


def _status_text() -> str:
    snap = chat_state.snapshot()
    if snap["busy"]:
        elapsed = ""
        if snap["turn_started_at"]:
            import time
            elapsed = f"  {int(time.monotonic() - snap['turn_started_at'])}s"
        return f"{snap['status'] or 'Working…'}{elapsed}"
    return "Ask a follow-up below"


def _ui_scale(context) -> float:
    prefs = context.preferences
    scale = getattr(prefs.system, "ui_scale", 0) or getattr(prefs.view, "ui_scale", 1.0)
    return float(scale or 1.0)


def _rebuild() -> None:
    wm = bpy.context.window_manager
    width = _pending_width[0]
    scale = _ui_scale(bpy.context)
    snap = chat_state.snapshot()

    def wrap(text):
        # List rows lose ~70 px to margins, the icon and the scrollbar.
        return wrap_text(text, width, scale, reserve_px=70.0)

    rows = wm.blendermcp_chat_rows
    rows.clear()
    for r in transcript_rows(snap["messages"], wrap):
        item = rows.add()
        item.text, item.role, item.icon = r["text"], r["role"], r["icon"]
    wm.blendermcp_chat_row_index = len(rows) - 1
    _built_for[0] = (snap["revision"], width // 20)
    for window in wm.windows:
        for area in window.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()


def ensure_rows(context) -> None:
    """Called from draw: schedule a rebuild when the transcript or width changed."""
    width = context.region.width if context.region else 300
    key = (chat_state.revision, width // 20)
    if key != _built_for[0] and not bpy.app.timers.is_registered(_rebuild):
        _pending_width[0] = width
        bpy.app.timers.register(_rebuild, first_interval=0.0)


def draw_transcript(layout, context, rows: int = 14) -> None:
    ensure_rows(context)
    wm = context.window_manager
    layout.template_list(
        "BLENDERMCP_UL_ChatTranscript", "", wm, "blendermcp_chat_rows",
        wm, "blendermcp_chat_row_index", rows=rows,
    )


def _conversations(self, context):
    from ..chat import client, history

    folder = client.chats_dir()
    items = [("__current__", "This chat", "The conversation on screen")]
    current = chat_state.conversation_id
    for c in history.listing(folder) if folder else []:
        if c["id"] == current:
            items[0] = ("__current__", c["title"], "The conversation on screen")
            continue
        items.append((c["id"], c["title"], "Open this saved chat"))
    _conversation_items[:] = items
    return _conversation_items


def _on_pick(self, context):
    choice = self.blendermcp_chat_conversation
    if choice and choice != "__current__":
        from ..chat import client
        client.open_conversation(choice)
    # Snap back so the picker always shows the chat on screen.
    if choice != "__current__":
        bpy.app.timers.register(_reset_pick, first_interval=0.0)


def _reset_pick() -> None:
    try:
        bpy.context.window_manager.blendermcp_chat_conversation = "__current__"
    except (TypeError, AttributeError):
        pass


def register_props() -> None:
    wm = bpy.types.WindowManager
    wm.blendermcp_chat_rows = bpy.props.CollectionProperty(type=BLENDERMCP_PG_ChatRow)
    wm.blendermcp_chat_row_index = bpy.props.IntProperty(default=0)
    wm.blendermcp_chat_conversation = bpy.props.EnumProperty(
        name="Conversation", description="Switch to a saved chat",
        items=_conversations, update=_on_pick,
    )
    _built_for[0] = None
    # Bring back the last conversation once Blender has finished starting.
    from ..chat import client
    bpy.app.timers.register(lambda: client.restore_latest(), first_interval=1.0)


def unregister_props() -> None:
    wm = bpy.types.WindowManager
    for name in ("blendermcp_chat_conversation", "blendermcp_chat_row_index", "blendermcp_chat_rows"):
        if hasattr(wm, name):
            delattr(wm, name)
