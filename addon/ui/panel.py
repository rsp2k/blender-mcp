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
from ..preferences import draw_login_section, get_client_label, get_prefs


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

        # --- Fatal-error banner — surface auth-fatal failures prominently.
        # The bus_client sets `fatal_error` when it gives up (e.g. 401 from
        # the bus server, meaning the JWT is unrecoverable). It also clears
        # prefs.jwt_token in that case, so the Login section below will show
        # the un-authed state. The banner here gives the user the WHY and
        # an obvious next action.
        client = state._client
        if client is not None and client.fatal_error:
            box = layout.box()
            box.alert = True  # Blender's native red-tinted alert state
            box.label(text=client.fatal_error, icon='ERROR')
            row = box.row(align=True)
            row.operator("blendermcp.re_login", text="Re-login", icon='URL')
            # Dismiss clears the banner without taking action — useful when
            # the user has already moved on (e.g. tested a different server).
            row.operator(
                "blendermcp.dismiss_fatal_error", text="Dismiss", icon='X',
            )

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

        # Identity row — "You are: <label>" + Copy UUID button.
        # The label is always derivable (auto-fills from hostname + version
        # if blank), so we can preview pre-Connect. The UUID is sticky on
        # disk so once it's been minted (first Connect ever), it stays
        # the same across restarts — safe to copy at any time.
        client = state._client
        live_uuid = client.client_uuid if client else (scene.blendermcp_client_id or "")
        ident_row = bus_col.row(align=True)
        ident_row.label(
            text=f"You: {get_client_label(prefs)}",
            icon='POSE_HLT',
        )
        # Disable Copy if no uuid has ever been minted (first run, never
        # Connected). Otherwise enabled — sticky uuid is always copy-safe.
        copy_row = ident_row.row(align=True)
        copy_row.enabled = bool(live_uuid)
        copy_row.operator(
            "blendermcp.copy_client_uuid",
            text="",
            icon='COPYDOWN',
        )
        if live_uuid:
            bus_col.label(text=f"  uuid: {live_uuid[:24]}…")

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

        # --- Status (live) — identity row above already shows label + uuid.
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

        # --- Asset integrations (toggles only — API keys live in prefs) ---
        layout.separator()
        col = layout.column(align=True)
        col.label(text="Asset Integrations", icon='ASSET_MANAGER')
        col.prop(prefs, "use_polyhaven", text="Poly Haven")
        col.prop(prefs, "use_hyper3d", text="Hyper3D Rodin")
        col.prop(prefs, "use_sketchfab", text="Sketchfab")
