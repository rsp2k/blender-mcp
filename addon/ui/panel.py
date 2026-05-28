"""BLENDERMCP_PT_Panel — View3D > Sidebar > BlenderMCP panel.

Setup config (server URL, asset API keys) lives in Edit > Preferences >
Add-ons > BlenderMCP. The sidebar carries the actions you take *while
working*: Login/Logout, Connect/Disconnect, asset-integration toggles,
and live connection status.

Login UI is shared with the prefs panel via
:func:`preferences.draw_login_section` so both call sites stay
identical without copy-paste drift.
"""

from __future__ import annotations

import bpy

from .. import state
from ..client.bus_client import FASTMCP_AVAILABLE
from ..preferences import draw_login_section, get_prefs


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

        # --- fastmcp install hint (only hard dep that can't be auto-fixed) ---
        if not FASTMCP_AVAILABLE:
            box = layout.box()
            box.label(text="fastmcp not installed", icon='ERROR')
            box.label(text="In Blender's Python console:")
            box.label(text="  python -m pip install fastmcp")
            return  # Everything below depends on fastmcp.

        # --- Login / Logout (shared widget with prefs panel) ---
        draw_login_section(layout, prefs)

        # If not logged in, stop here — Connect needs a JWT, asset toggles
        # are pointless without a session.
        if not prefs.jwt_token:
            return

        # --- Bus selection (Phase I7) ---
        layout.separator()
        bus_col = layout.column(align=True)
        bus_col.label(text="Bus", icon='OUTLINER_COLLECTION')

        # Surface the chosen bus + the refresh affordance.
        chosen_name = "Personal (default)"
        for b in state._buses:
            if b.get("bus_id") == prefs.default_bus_id:
                tag = " (owner)" if b.get("is_owned_by_me") else f" ({b.get('role')})"
                chosen_name = f"{b.get('name', '?')}{tag}"
                break
        row = bus_col.row(align=True)
        row.label(text=f"Current: {chosen_name}")
        row.operator("blendermcp.refresh_buses", text="", icon='FILE_REFRESH')

        # Picker — populated from state._buses. Each button writes
        # prefs.default_bus_id via wm.context_set_string (Personal = "").
        if state._buses:
            from ..preferences import ADDON_PACKAGE_NAME
            data_path = (
                f"preferences.addons[\"{ADDON_PACKAGE_NAME}\"]"
                ".preferences.default_bus_id"
            )
            picker = bus_col.column(align=True)
            picker.scale_y = 0.9
            for b in state._buses:
                if b.get("is_personal"):
                    text = "Personal"
                    icon = 'USER'
                    value = ""
                else:
                    text = b.get("name", "?")
                    icon = 'CHECKMARK' if b.get("is_owned_by_me") else 'COMMUNITY'
                    value = b["bus_id"]
                op = picker.operator(
                    "wm.context_set_string",
                    text=text,
                    icon=icon,
                    depress=(prefs.default_bus_id == value),
                )
                op.data_path = data_path
                op.value = value
        else:
            bus_col.label(text="(click refresh to populate)", icon='INFO')

        # Bus management buttons — only shown when the user has fetched buses
        if state._buses:
            mgmt = bus_col.row(align=True)
            mgmt.operator("blendermcp.create_bus", text="Create", icon='ADD')
            mgmt.operator("blendermcp.join_bus", text="Join", icon='LINKED')
            if prefs.default_bus_id:
                # leave/invite only meaningful on a non-personal bus
                mgmt2 = bus_col.row(align=True)
                mgmt2.operator("blendermcp.invite_to_bus", text="Invite", icon='COPYDOWN')
                mgmt2.operator("blendermcp.leave_bus", text="Leave", icon='X')

        # --- Connection ---
        layout.separator()
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
