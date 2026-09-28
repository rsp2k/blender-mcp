"""Blender MCP indicator in the status bar.

The sidebar is usually closed while you work, which is exactly when a
consent prompt can sit unseen. This puts a small "MCP" entry in the status
bar with the connection icon, turning into a red button naming the prompt
when one is waiting. Clicking it opens the sidebar on the BlenderMCP tab.
"""

from __future__ import annotations

import bpy

from .. import state
from ..activity_format import pending_prompt
from ..preferences import get_prefs


def _chat_approval_pending() -> bool:
    # The chat module ships separately; until it's installed this is a no-op.
    try:
        from ..chat.state import chat_state
    except ImportError:
        return False
    return bool(getattr(chat_state, "pending_approval", None))


def draw_statusbar(self, context):
    try:
        prefs = get_prefs(context)
    except Exception:  # noqa: BLE001 - never break Blender's status bar
        return
    if prefs is None:
        return
    from .panel import _connection_status

    prompt = pending_prompt(
        state._pending_control_request,
        state._pending_extension_request,
        state._pending_reload,
        state._pending_merge,
        _chat_approval_pending(),
    )
    row = self.layout.row(align=True)
    if prompt:
        row.alert = True
        row.operator("blendermcp.show_panel", text=f"MCP: {prompt}", icon='ERROR')
    else:
        icon, _, _ = _connection_status(prefs, state._client)
        row.operator("blendermcp.show_panel", text="MCP", icon=icon, emboss=False)


def register():
    bpy.types.STATUSBAR_HT_header.append(draw_statusbar)


def unregister():
    try:
        bpy.types.STATUSBAR_HT_header.remove(draw_statusbar)
    except (ValueError, AttributeError):
        pass
