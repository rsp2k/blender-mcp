"""Chat tab and Chat backend operators, plus the input field's update hook."""

from __future__ import annotations

import bpy

from ..chat import client as chat_client
from ..chat import clip, compose, reader
from ..chat.elicitation import resolve_approval
from ..chat.state import chat_state


def _send(context, text: str) -> tuple[bool, str | None]:
    """Send with whatever the binder clip holds; reset the one-message clips."""
    attachments, clipped = clip.gather(context)
    started, problem = chat_client.send(text, attachments or None, clipped)
    if started:
        clip.after_send(context)
    return started, problem


def on_chat_input_update(wm, context):
    """Enter in the message field sends it; text a starter filled in waits
    for the user (compose.py)."""
    compose.on_input_changed(wm, lambda text: _send(context or bpy.context, text))


def _take_key(wm) -> str:
    """The pasted key, with the field cleared at once so it never lingers."""
    key = (wm.blendermcp_chat_key or "").strip()
    if wm.blendermcp_chat_key:
        wm.blendermcp_chat_key = ""
    return key


def on_chat_key_update(wm, context):
    """Enter in the key field saves it, like the Save key button. Clearing
    the field re-fires this with an empty value, which is a no-op."""
    if not (wm.blendermcp_chat_key or "").strip() or chat_state.key_checking:
        return
    chat_client.save_key(_take_key(wm))


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
        started, problem = _send(context, text)
        if not started:
            if problem:
                self.report({'WARNING'}, problem)
            return {'CANCELLED'}
        wm.blendermcp_chat_input = ""
        return {'FINISHED'}


class BLENDERMCP_OT_ChatStarter(bpy.types.Operator):
    """Put this starter in the message box"""

    bl_idname = "blendermcp.chat_starter"
    bl_label = "Starter prompt"
    bl_options = {'INTERNAL'}

    name: bpy.props.StringProperty(options={'SKIP_SAVE'})

    @classmethod
    def description(cls, context, properties):
        about = next((s.get("description") for s in chat_state.starters
                      if s.get("name") == properties.name), "")
        tail = "Fills the message box so you can edit it, then press Enter to send"
        return f"{about.rstrip('.')}.\n{tail}" if about else tail

    def execute(self, context):
        problem = chat_client.use_starter(self.name)
        if problem:
            self.report({'WARNING'}, problem)
            return {'CANCELLED'}
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


class BLENDERMCP_OT_ChatReader(bpy.types.Operator):
    """Open or close the Chat Reader"""

    bl_idname = "blendermcp.chat_reader"
    bl_label = "Reader"
    bl_description = (
        "Read the whole conversation in a Text Editor beside the viewport "
        "(Ctrl+wheel to zoom, Ctrl+F to search). Click again to close it"
    )

    def execute(self, context):
        try:
            where = reader.toggle(context)
        except Exception as e:  # noqa: BLE001
            self.report({'WARNING'}, f"Couldn't open the Reader: {e}")
            return {'CANCELLED'}
        self.report({'INFO'}, where)
        return {'FINISHED'}


class BLENDERMCP_OT_ChatUndoTurn(bpy.types.Operator):
    """Undo what the assistant changed in its last reply"""

    bl_idname = "blendermcp.chat_undo_turn"
    bl_label = "Undo last chat change"
    bl_description = (
        "Undo every scene change from the assistant's last reply, as if you "
        "pressed Ctrl+Z once per step"
    )

    def execute(self, context):
        from .. import undo_steps
        started = chat_state.turn_started_at
        if not started:
            return {'CANCELLED'}
        undone, message = undo_steps.undo_since(started)
        self.report({'INFO'} if undone else {'WARNING'}, message)
        chat_client.request_redraw()
        return {'FINISHED'} if undone else {'CANCELLED'}


class BLENDERMCP_OT_SetChatAdvisor(bpy.types.Operator):
    """Choose the model your chat may escalate to"""

    bl_idname = "blendermcp.set_chat_advisor"
    bl_label = "Escalate to"
    bl_description = (
        "When a request needs more thought, your chat model can consult this "
        "stronger model mid-reply. It costs that model's rates for the advice only"
    )

    advisor: bpy.props.StringProperty(default="")

    def execute(self, context):
        problem = chat_client.set_advisor(self.advisor)
        if problem:
            self.report({'WARNING'}, problem)
            return {'CANCELLED'}
        return {'FINISHED'}


class BLENDERMCP_MT_ChatAdvisor(bpy.types.Menu):
    bl_idname = "BLENDERMCP_MT_ChatAdvisor"
    bl_label = "Escalate to"

    def draw(self, context):
        layout = self.layout
        backend = chat_state.backend or {}
        default = backend.get("advisor_default") or ""
        op = layout.operator("blendermcp.set_chat_advisor",
                             text=f"Server default ({reader.model_label({'model': default}) or 'off'})",
                             icon='WORLD')
        op.advisor = ""
        layout.operator("blendermcp.set_chat_advisor", text="Off", icon='CANCEL').advisor = "off"
        layout.separator()
        for model in backend.get("advisors") or []:
            layout.operator("blendermcp.set_chat_advisor",
                            text=reader.model_label({"model": model}), icon='TRIA_UP').advisor = model


class BLENDERMCP_OT_ChatBackendSettings(bpy.types.Operator):
    """Choose which model answers in the chat"""

    bl_idname = "blendermcp.chat_backend_settings"
    bl_label = "Chat model"
    bl_description = "The model answering in this chat. Click to change it in Preferences"

    def execute(self, context):
        from ..preferences import ADDON_PACKAGE_NAME
        try:
            bpy.ops.preferences.addon_show(module=ADDON_PACKAGE_NAME)
        except Exception as e:  # noqa: BLE001
            self.report({'WARNING'}, f"Open Preferences > Add-ons > Blender MCP ({e})")
            return {'CANCELLED'}
        return {'FINISHED'}


class BLENDERMCP_OT_ChatSaveKey(bpy.types.Operator):
    """Check your Claude API key and store it on the server"""

    bl_idname = "blendermcp.chat_save_key"
    bl_label = "Save key"
    bl_description = (
        "Send the key to the server, which checks it and stores it encrypted. "
        "The field is cleared here right away"
    )

    def execute(self, context):
        key = _take_key(context.window_manager)
        if not key:
            self.report({'WARNING'}, "Paste your Claude API key first")
            return {'CANCELLED'}
        problem = chat_client.save_key(key)
        if problem:
            self.report({'WARNING'}, problem)
            return {'CANCELLED'}
        return {'FINISHED'}


class BLENDERMCP_OT_ChatKeyPrompt(bpy.types.Operator):
    """Show or hide the box for your own Claude API key"""

    bl_idname = "blendermcp.chat_key_prompt"
    bl_label = "Use my own key"
    bl_description = "Paste your own Claude API key so chat keeps working after the free trial"

    def execute(self, context):
        chat_state.toggle_key_prompt()
        chat_client.request_redraw()
        return {'FINISHED'}


class BLENDERMCP_OT_ChatClear(bpy.types.Operator):
    """Delete the conversation on screen"""

    bl_idname = "blendermcp.chat_clear"
    bl_label = "Delete chat"
    bl_description = (
        "Delete this conversation, on screen and from your saved chats. "
        "The text-block log is kept"
    )

    def execute(self, context):
        from ..chat import history
        if chat_state.busy:
            self.report({'WARNING'}, "Wait for the reply, or press Stop first")
            return {'CANCELLED'}
        folder = chat_client.chats_dir()
        if folder and chat_state.conversation_id:
            history.delete(folder, chat_state.conversation_id)
        chat_state.load_conversation(None, [])
        chat_client.request_redraw()
        return {'FINISHED'}


class BLENDERMCP_OT_ChatNew(bpy.types.Operator):
    """Start a new conversation; the current one stays in your saved chats"""

    bl_idname = "blendermcp.chat_new"
    bl_label = "New chat"
    bl_description = "Start a new conversation. The current one is kept in the chat picker"

    def execute(self, context):
        if not chat_client.new_conversation():
            self.report({'WARNING'}, "Wait for the reply, or press Stop first")
            return {'CANCELLED'}
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
    bl_description = "Forget the stored backend and key; chat goes back to the server default"

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
    BLENDERMCP_OT_ChatNew,
    BLENDERMCP_OT_ChatSend,
    BLENDERMCP_OT_ChatStarter,
    BLENDERMCP_OT_ChatStop,
    BLENDERMCP_OT_ChatReader,
    BLENDERMCP_OT_ChatUndoTurn,
    BLENDERMCP_OT_ChatBackendSettings,
    BLENDERMCP_OT_SetChatAdvisor,
    BLENDERMCP_MT_ChatAdvisor,
    BLENDERMCP_OT_ChatSaveKey,
    BLENDERMCP_OT_ChatKeyPrompt,
    BLENDERMCP_OT_ChatClear,
    BLENDERMCP_OT_ChatAllow,
    BLENDERMCP_OT_ChatDeny,
    BLENDERMCP_OT_SaveChatBackend,
    BLENDERMCP_OT_ClearChatBackend,
    BLENDERMCP_OT_RefreshChatBackend,
)
