"""BlenderMCP operators — Start/Stop server, Set Hyper3D free-trial key.

Phase 7 adds BLENDERMCP_OT_Login here. Phase 8 swaps Scene-property
reads for AddonPreferences accessors. For now the operators preserve
the exact pre-Phase-6 behavior.
"""

from __future__ import annotations

import traceback

import bpy

from .. import state
from ..client import BlenderMCPClient
from ..client.bus_client import FASTMCP_AVAILABLE
from ..constants import RODIN_FREE_TRIAL_KEY
from ..executor import BlenderCommandExecutor
from ..identity import StickyUUIDManager


class BLENDERMCP_OT_SetFreeTrialHyper3DAPIKey(bpy.types.Operator):
    bl_idname = "blendermcp.set_hyper3d_free_trial_api_key"
    bl_label = "Set Free Trial API Key"

    def execute(self, context):
        context.scene.blendermcp_hyper3d_api_key = RODIN_FREE_TRIAL_KEY
        context.scene.blendermcp_hyper3d_mode = 'MAIN_SITE'
        self.report({'INFO'}, "API Key set successfully!")
        return {'FINISHED'}


class BLENDERMCP_OT_StartServer(bpy.types.Operator):
    """Connect to the BlenderMCP server's _message_bus channel."""

    bl_idname = "blendermcp.start_server"
    bl_label = "Connect to BlenderMCP Server"
    bl_description = "Connect to the BlenderMCP server's _message_bus channel"

    def execute(self, context):
        scene = context.scene

        if not FASTMCP_AVAILABLE:
            self.report(
                {'ERROR'},
                "fastmcp not installed. Run: <blender_python> -m pip install fastmcp",
            )
            return {'CANCELLED'}

        if not scene.blendermcp_jwt_token:
            self.report({'ERROR'}, "JWT token required (paste from OAuth login)")
            return {'CANCELLED'}

        try:
            if state._executor is None:
                state._executor = BlenderCommandExecutor()

            if state._client is None:
                uuid_mgr = StickyUUIDManager()
                state._client = BlenderMCPClient(
                    server_url=scene.blendermcp_server_url,
                    jwt_token=scene.blendermcp_jwt_token,
                    client_uuid=uuid_mgr.get_client_id(),
                    executor=state._executor,  # injected so drainer exposes to scripts
                )
                scene.blendermcp_client_id = state._client.client_uuid

            state._client.start()
            scene.blendermcp_server_running = True
            self.report({'INFO'}, f"Connecting as {scene.blendermcp_client_id}")
        except Exception as e:
            self.report({'ERROR'}, f"Failed to start client: {e}")
            traceback.print_exc()
            return {'CANCELLED'}

        return {'FINISHED'}


class BLENDERMCP_OT_StopServer(bpy.types.Operator):
    """Disconnect from the MCP message bus."""

    bl_idname = "blendermcp.stop_server"
    bl_label = "Disconnect from BlenderMCP Server"
    bl_description = "Disconnect from the MCP message bus"

    def execute(self, context):
        scene = context.scene
        try:
            if state._client is not None:
                state._client.stop()
                state._client = None
            scene.blendermcp_server_running = False
            self.report({'INFO'}, "Disconnected")
        except Exception as e:
            self.report({'ERROR'}, f"Error during disconnect: {e}")
            traceback.print_exc()
        return {'FINISHED'}
