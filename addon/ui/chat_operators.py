"""Chat tab and Chat backend operators, plus the input field's update hook."""

from __future__ import annotations

import bpy

from ..chat import client as chat_client
from ..chat.elicitation import resolve_approval
from ..chat.state import chat_state


def on_chat_input_update(wm, _context):
    """Enter in the message field sends it. Clearing the field re-fires this
    with an empty value, which is a no-op. A message that couldn't be sent
    stays in the field for the Send button."""
    text = (wm.blendermcp_chat_input or "").strip()
    if not text:
        return
    started, _problem = chat_client.send(text)
    if started:
        wm.blendermcp_chat_input = ""


class BLENDERMCP_OT_ChatSend(bpy.types.Operator):
    """Send the message to the chat"""

    bl_idname = "blendermcp.chat_send"
    bl_label = "Send"
    bl_description = "Send the message; the model carries it out in this Blender"

    def execute(self, context):
        wm = context.window_manager
        text = (wm.blendermcp_chat_input or "").strip()
        if not text:
            return {'CANCELLED'}
        started, problem = chat_client.send(text)
        if not started:
            if problem:
                self.report({'WARNING'}, problem)
            return {'CANCELLED'}
        wm.blendermcp_chat_input = ""
        return {'FINISHED'}


class BLENDERMCP_OT_ChatStop(bpy.types.Operator):
    """Stop the reply in progress"""

    bl_idname = "blendermcp.chat_stop"
    bl_label = "Stop"
    bl_description = "Cancel the running chat turn on the server"

    def execute(self, context):
        if not chat_client.stop():
            return {'CANCELLED'}
        return {'FINISHED'}


class BLENDERMCP_OT_ChatOpenLog(bpy.types.Operator):
    """Show the full chat transcript"""

    bl_idname = "blendermcp.chat_open_log"
    bl_label = "Open log"
    bl_description = "Show the 'BlenderMCP Chat' text block in a Text Editor"

    def execute(self, context):
        from ..chat import log as chat_log
        chat_log.flush_pending()
        try:
            where = chat_log.open_log(context)
        except Exception as e:  # noqa: BLE001
            self.report({'WARNING'}, f"Couldn't open the log: {e}")
            return {'CANCELLED'}
        self.report({'INFO'}, where)
        return {'FINISHED'}


class BLENDERMCP_OT_ChatClear(bpy.types.Operator):
    """Clear the messages shown in the Chat tab"""

    bl_idname = "blendermcp.chat_clear"
    bl_label = "Clear"
    bl_description = (
        "Clear the panel and start a fresh conversation. "
        "The text-block log is kept"
    )

    def execute(self, context):
        chat_state.clear()
        chat_client.request_redraw()
        return {'FINISHED'}


class BLENDERMCP_OT_ChatAllow(bpy.types.Operator):
    """Allow the step the model asked to run"""

    bl_idname = "blendermcp.chat_allow"
    bl_label = "Allow"
    bl_description = "Let the model run this step in your Blender"

    def execute(self, context):
        if not resolve_approval(True):
            self.report({'WARNING'}, "Nothing is waiting for approval")
            return {'CANCELLED'}
        return {'FINISHED'}


class BLENDERMCP_OT_ChatDeny(bpy.types.Operator):
    """Deny the step the model asked to run"""

    bl_idname = "blendermcp.chat_deny"
    bl_label = "Deny"
    bl_description = "Refuse; the model is told you declined and carries on without it"

    def execute(self, context):
        if not resolve_approval(False):
            self.report({'WARNING'}, "Nothing is waiting for approval")
            return {'CANCELLED'}
        return {'FINISHED'}


class BLENDERMCP_OT_SaveChatBackend(bpy.types.Operator):
    """Send the chat backend settings to the server"""

    bl_idname = "blendermcp.save_chat_backend"
    bl_label = "Save backend"
    bl_description = (
        "Store this backend on the server. The API key is sent once, "
        "then cleared here so it never sits in your preferences file"
    )

    def execute(self, context):
        from ..preferences import get_prefs, persist_prefs
        prefs = get_prefs(context)
        error = chat_client.set_backend(
            prefs.chat_provider,
            model=prefs.chat_model,
            base_url=prefs.chat_base_url if prefs.chat_provider == 'openai' else "",
            api_key=prefs.chat_api_key if prefs.chat_provider != 'gateway' else "",
        )
        # Clear and save whether or not the send worked: a key typed into
        # the field may already have been auto-saved into userpref.blend.
        prefs.chat_api_key = ""
        persist_prefs()
        if error:
            self.report({'WARNING'}, f"Couldn't save the backend: {error}")
            return {'CANCELLED'}
        self.report({'INFO'}, "Chat backend sent to the server")
        return {'FINISHED'}


class BLENDERMCP_OT_ClearChatBackend(bpy.types.Operator):
    """Remove your chat backend from the server"""

    bl_idname = "blendermcp.clear_chat_backend"
    bl_label = "Clear backend"
    bl_description = "Forget the stored backend and key; chat falls back to the shared gateway"

    def execute(self, context):
        from ..preferences import get_prefs, persist_prefs
        prefs = get_prefs(context)
        error = chat_client.clear_backend(prefs.chat_provider)
        prefs.chat_api_key = ""
        persist_prefs()
        if error:
            self.report({'WARNING'}, f"Couldn't clear the backend: {error}")
            return {'CANCELLED'}
        return {'FINISHED'}


class BLENDERMCP_OT_RefreshChatBackend(bpy.types.Operator):
    """Ask the server which chat backend is set"""

    bl_idname = "blendermcp.refresh_chat_backend"
    bl_label = "Refresh"
    bl_description = "Show the backend the server has for your account"

    def execute(self, context):
        error = chat_client.refresh_backend()
        if error:
            self.report({'WARNING'}, error)
            return {'CANCELLED'}
        return {'FINISHED'}


CHAT_OPERATORS = (
    BLENDERMCP_OT_ChatSend,
    BLENDERMCP_OT_ChatStop,
    BLENDERMCP_OT_ChatOpenLog,
    BLENDERMCP_OT_ChatClear,
    BLENDERMCP_OT_ChatAllow,
    BLENDERMCP_OT_ChatDeny,
    BLENDERMCP_OT_SaveChatBackend,
    BLENDERMCP_OT_ClearChatBackend,
    BLENDERMCP_OT_RefreshChatBackend,
)
