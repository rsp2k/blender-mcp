"""BLENDERMCP_PT_Panel — the View3D > Sidebar > BlenderMCP panel.

Reads `addon.state._client` to render status (queue depth, active jobs,
last error). Phase 8 will swap the Scene-property reads for
AddonPreferences accessors.
"""

from __future__ import annotations

import bpy

from .. import state
from ..client.bus_client import FASTMCP_AVAILABLE
from ..preferences import get_prefs


class BLENDERMCP_PT_Panel(bpy.types.Panel):
    bl_label = "Blender MCP"
    bl_idname = "BLENDERMCP_PT_Panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'BlenderMCP'

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        prefs = get_prefs(context)

        # --- Connection section ---
        col = layout.column(align=True)
        col.label(text="MCP Server Connection:", icon='NETWORK_DRIVE')

        col.prop(prefs, "server_url", text="URL")

        # --- Login section ---
        has_jwt = bool(prefs.jwt_token)
        is_oauth = bool(prefs.oauth_client_id)
        if not has_jwt:
            col.separator()
            col.label(text="Login:", icon='UNLOCKED')
            # OAuth (production path): browser flow against Authentik.
            col.operator(
                "blendermcp.oauth_login",
                text="Login with OAuth (browser)",
                icon='URL',
            )
            # Legacy password flow (only for AUTH_BACKEND=inmemory dev servers).
            col.separator()
            col.label(text="(or password login — dev only)", icon='INFO')
            col.prop(prefs, "username", text="Username")
            col.prop(scene, "blendermcp_password_tmp", text="Password")
            col.operator("blendermcp.login", text="Password Login", icon='KEYINGSET')
        else:
            row = col.row(align=True)
            who = prefs.username or "OAuth user"
            method = "OAuth" if is_oauth else "password"
            row.label(text=f"Logged in ({method}): {who}", icon='LOCKED')
            # Logout: disconnects + notifies server + clears local JWT.
            row.operator("blendermcp.logout", text="", icon='UNLOCKED')

        if getattr(scene, "blendermcp_client_id", ""):
            col.label(text=f"Client: {scene.blendermcp_client_id[:24]}")

        # Connect/Disconnect button (gated on having a JWT)
        col.separator()
        row = col.row(align=True)
        if not scene.blendermcp_server_running:
            sub = row.row()
            sub.enabled = has_jwt
            sub.operator("blendermcp.start_server", text="Connect", icon='PLAY')
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

        # --- Asset integrations (all read/write via AddonPreferences) ---
        col.label(text="Asset Integrations:", icon='ASSET_MANAGER')
        col.prop(prefs, "use_polyhaven", text="Poly Haven Assets")
        col.prop(prefs, "use_hyper3d", text="Hyper3D Rodin Generation")
        if prefs.use_hyper3d:
            sub = col.column(align=True)
            sub.prop(prefs, "hyper3d_mode", text="Mode")
            sub.prop(prefs, "hyper3d_api_key", text="API Key")
            sub.operator("blendermcp.set_hyper3d_free_trial_api_key", text="Free Trial Key")

        col.prop(prefs, "use_sketchfab", text="Sketchfab Models")
        if prefs.use_sketchfab:
            sub = col.column(align=True)
            sub.prop(prefs, "sketchfab_api_key", text="API Key")
