"""BLENDERMCP_PT_Panel — the View3D > Sidebar > BlenderMCP panel.

Reads `addon.state._client` to render status (queue depth, active jobs,
last error). Phase 8 will swap the Scene-property reads for
AddonPreferences accessors.
"""

from __future__ import annotations

import bpy

from .. import state
from ..client.bus_client import FASTMCP_AVAILABLE


class BLENDERMCP_PT_Panel(bpy.types.Panel):
    bl_label = "Blender MCP"
    bl_idname = "BLENDERMCP_PT_Panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'BlenderMCP'

    def draw(self, context):
        layout = self.layout
        scene = context.scene

        # --- Connection section ---
        col = layout.column(align=True)
        col.label(text="MCP Server Connection:", icon='NETWORK_DRIVE')

        # Server URL + JWT input
        col.prop(scene, "blendermcp_server_url", text="URL")
        col.prop(scene, "blendermcp_jwt_token", text="JWT")

        if getattr(scene, "blendermcp_client_id", ""):
            col.label(text=f"Client: {scene.blendermcp_client_id[:24]}")

        # Buttons
        row = col.row(align=True)
        if not scene.blendermcp_server_running:
            row.operator("blendermcp.start_server", text="Connect", icon='PLAY')
        else:
            row.operator("blendermcp.stop_server", text="Disconnect", icon='PAUSE')

        # Status indicator — reads the singleton owned by addon.state
        client = state._client
        if client:
            if client.connected:
                col.label(text="Status: Connected", icon='CHECKMARK')
                with client.queue_lock:
                    qlen = len(client.job_queue)
                col.label(text=f"Queue: {qlen} pending  Active: {len(client.active_jobs)}")
            elif client.running:
                col.label(text="Status: Connecting...", icon='TIME')
            if client.last_error:
                col.label(text=f"Last error: {client.last_error[:40]}", icon='ERROR')

        col.separator()
        if not FASTMCP_AVAILABLE:
            col.label(text="fastmcp not installed", icon='ERROR')
            col.label(text="Install in Blender's Python:")
            col.label(text="python -m pip install fastmcp")
            col.separator()

        # --- Asset integrations ---
        col.label(text="Asset Integrations:", icon='ASSET_MANAGER')
        col.prop(scene, "blendermcp_use_polyhaven", text="Poly Haven Assets")
        col.prop(scene, "blendermcp_use_hyper3d", text="Hyper3D Rodin Generation")
        if scene.blendermcp_use_hyper3d:
            sub = col.column(align=True)
            sub.prop(scene, "blendermcp_hyper3d_mode", text="Mode")
            sub.prop(scene, "blendermcp_hyper3d_api_key", text="API Key")
            sub.operator("blendermcp.set_hyper3d_free_trial_api_key", text="Free Trial Key")

        col.prop(scene, "blendermcp_use_sketchfab", text="Sketchfab Models")
        if scene.blendermcp_use_sketchfab:
            sub = col.column(align=True)
            sub.prop(scene, "blendermcp_sketchfab_api_key", text="API Key")
