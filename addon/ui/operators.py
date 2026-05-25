"""BlenderMCP operators — Login, Start/Stop server, Set Hyper3D free-trial key.

Phase 7 added BLENDERMCP_OT_Login (replacing manual JWT paste with a
real /auth/login round-trip). Phase 8 swaps Scene-property reads for
AddonPreferences accessors.
"""

from __future__ import annotations

import traceback

import bpy
import requests  # for catching requests.exceptions.RequestException

from .. import state
from ..auth import LoginError, login
from ..client import BlenderMCPClient
from ..client.bus_client import FASTMCP_AVAILABLE
from ..constants import RODIN_FREE_TRIAL_KEY
from ..executor import BlenderCommandExecutor
from ..identity import StickyUUIDManager
from ..preferences import get_prefs


class BLENDERMCP_OT_Login(bpy.types.Operator):
    """Exchange username/password for a JWT against the BlenderMCP server.

    Synchronous (blocks Blender's UI for the round-trip) — keeps the
    operator simple; the request usually completes in well under a
    second. On success: writes scene.blendermcp_jwt_token and clears the
    transient password field. On failure: surfaces a structured error
    via self.report({'ERROR'}, ...).
    """

    bl_idname = "blendermcp.login"
    bl_label = "Login to BlenderMCP Server"
    bl_description = "POST your username/password to /auth/login and store the returned JWT"

    def execute(self, context):
        scene = context.scene
        prefs = get_prefs(context)
        server_url = prefs.server_url
        username = prefs.username
        # Password is the only credential field still on Scene — transient,
        # never persisted, never shipped in .blend files.
        password = getattr(scene, "blendermcp_password_tmp", "")

        if not username or not password:
            self.report({'ERROR'}, "Username and password required")
            return {'CANCELLED'}

        try:
            payload = login(server_url, username, password)
        except LoginError as e:
            code = f" ({e.status_code})" if e.status_code else ""
            self.report({'ERROR'}, f"Login failed{code}: {e}")
            return {'CANCELLED'}
        except requests.exceptions.RequestException as e:
            self.report({'ERROR'}, f"Network error contacting {server_url}: {e}")
            return {'CANCELLED'}
        except Exception as e:
            self.report({'ERROR'}, f"Unexpected error: {e}")
            traceback.print_exc()
            return {'CANCELLED'}

        prefs.jwt_token = payload.get("access_token", "")
        # Clear the password field so it's not lingering in the UI.
        scene.blendermcp_password_tmp = ""

        user = payload.get("user", {})
        who = user.get("username", username)
        self.report({'INFO'}, f"Logged in as {who}")
        return {'FINISHED'}


class BLENDERMCP_OT_SetFreeTrialHyper3DAPIKey(bpy.types.Operator):
    bl_idname = "blendermcp.set_hyper3d_free_trial_api_key"
    bl_label = "Set Free Trial API Key"

    def execute(self, context):
        prefs = get_prefs(context)
        prefs.hyper3d_api_key = RODIN_FREE_TRIAL_KEY
        prefs.hyper3d_mode = 'MAIN_SITE'
        self.report({'INFO'}, "API Key set successfully!")
        return {'FINISHED'}


class BLENDERMCP_OT_StartServer(bpy.types.Operator):
    """Connect to the BlenderMCP server's _message_bus channel."""

    bl_idname = "blendermcp.start_server"
    bl_label = "Connect to BlenderMCP Server"
    bl_description = "Connect to the BlenderMCP server's _message_bus channel"

    def execute(self, context):
        scene = context.scene
        prefs = get_prefs(context)

        if not FASTMCP_AVAILABLE:
            self.report(
                {'ERROR'},
                "fastmcp not installed. Run: <blender_python> -m pip install fastmcp",
            )
            return {'CANCELLED'}

        if not prefs.jwt_token:
            self.report({'ERROR'}, "Not logged in. Click Login first to obtain a JWT.")
            return {'CANCELLED'}

        try:
            if state._executor is None:
                state._executor = BlenderCommandExecutor()

            if state._client is None:
                uuid_mgr = StickyUUIDManager()
                state._client = BlenderMCPClient(
                    server_url=prefs.server_url,
                    jwt_token=prefs.jwt_token,
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
