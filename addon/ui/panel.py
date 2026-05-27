"""BLENDERMCP_PT_Panel — View3D > Sidebar > BlenderMCP panel (minimal).

Setup config (server URL, OAuth login) lives in Edit > Preferences >
Add-ons > BlenderMCP. This sidebar carries the **runtime** view only:
connection status while you work, a one-click Connect/Disconnect, and
the asset-integration toggles you might want to flip mid-session.

If you've never logged in, the sidebar surfaces a hint to open prefs
rather than showing a login form here (login is a once-per-machine
event, asset toggles are per-session).
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

        # --- fastmcp install hint (this is the only hard dependency that
        # can't be auto-fixed from the addon UI) ---
        if not FASTMCP_AVAILABLE:
            box = layout.box()
            box.label(text="fastmcp not installed", icon='ERROR')
            box.label(text="In Blender's Python console:")
            box.label(text="  python -m pip install fastmcp")
            return  # Everything below depends on fastmcp.

        has_jwt = bool(prefs.jwt_token)

        # --- Not logged in: point users to prefs ---
        if not has_jwt:
            box = layout.box()
            box.label(text="Not logged in", icon='LOCKED')
            box.label(text="Open Edit > Preferences >")
            box.label(text="Add-ons > BlenderMCP")
            box.label(text="and click 'Login with OAuth'.")
            return

        # --- Connection ---
        col = layout.column(align=True)
        col.label(text="Connection", icon='NETWORK_DRIVE')

        if not scene.blendermcp_server_running:
            col.operator("blendermcp.start_server", text="Connect", icon='PLAY')
        else:
            col.operator("blendermcp.stop_server", text="Disconnect", icon='PAUSE')

        # --- Status (live) ---
        client = state._client
        if client:
            if client.connected:
                col.label(text="Status: Connected", icon='CHECKMARK')
                with client.queue_lock:
                    qlen = len(client.job_queue)
                col.label(
                    text=f"Queue: {qlen} pending  Active: {len(client.active_jobs)}",
                )
            elif client.running:
                col.label(text="Status: Connecting...", icon='TIME')
            if client.last_error:
                col.label(text=f"Last error: {client.last_error[:40]}", icon='ERROR')

        if getattr(scene, "blendermcp_client_id", ""):
            col.label(text=f"Client: {scene.blendermcp_client_id[:24]}")

        # --- Asset integrations (toggles only — API keys live in prefs) ---
        layout.separator()
        col = layout.column(align=True)
        col.label(text="Asset Integrations", icon='ASSET_MANAGER')
        col.prop(prefs, "use_polyhaven", text="Poly Haven")
        col.prop(prefs, "use_hyper3d", text="Hyper3D Rodin")
        col.prop(prefs, "use_sketchfab", text="Sketchfab")
