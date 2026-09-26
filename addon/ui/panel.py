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

import time as _time

import bpy

from .. import state
from .._version import __version__
from ..client.bus_client import FASTMCP_AVAILABLE
from ..preferences import (
    draw_login_section,
    draw_update_banner,
    get_client_label,
    get_prefs,
)


def _draw_pending_control_request(layout) -> None:
    """Prominent Allow/Deny/Always-allow banner for an unanswered request."""
    pending = state._pending_control_request
    if not pending:
        return
    box = layout.box()
    col = box.column(align=True)
    who = pending.get("requester_label") or pending.get("requester_uuid", "?")[:12]
    col.label(text=f"'{who}' wants control", icon='HAND')
    col.label(text=f"reason: {pending.get('reason', '')[:60]}")
    col.label(text=f"for {int(pending.get('duration_s') or 0)}s")
    row = col.row(align=True)
    row.operator("blendermcp.grant_control", text="Allow", icon='CHECKMARK')
    row.operator("blendermcp.deny_control", text="Deny", icon='CANCEL')
    row2 = col.row(align=True)
    row2.operator(
        "blendermcp.always_allow_control",
        text="Always allow this LLM",
        icon='FUND',
    )


def _draw_pending_extension_install(layout) -> None:
    """Banner for an unanswered blender_install_extension request."""
    pending = state._pending_extension_request
    if not pending:
        return
    box = layout.box()
    col = box.column(align=True)
    who = pending.get("requester_label") or pending.get("requester_uuid", "?")[:12]
    col.label(text=f"'{who}' wants to install an extension", icon='IMPORT')
    col.label(text=f"package: {pending.get('package_id', '')[:60]}")
    # Repo URL is important trust context — show it, even if it wraps.
    col.label(text=f"from: {pending.get('repo_url', '')[:80]}")
    if pending.get("new_repo"):
        # A never-seen repo is a bigger trust decision than a new
        # package from an already-trusted repo; call it out.
        col.label(text="(NEW REPO — never installed from before)", icon='ERROR')
    col.label(text=f"reason: {(pending.get('reason') or '')[:60]}")
    row = col.row(align=True)
    row.operator("blendermcp.grant_extension_install", text="Install", icon='CHECKMARK')
    row.operator("blendermcp.deny_extension_install", text="Deny", icon='CANCEL')
    row2 = col.row(align=True)
    row2.operator(
        "blendermcp.always_allow_extension_install",
        text="Always allow this LLM + repo",
        icon='FUND',
    )


def _draw_active_lock_header(layout) -> None:
    """Header + Take-back for an active control lock. Countdown auto-updates."""
    if state._lock_expires_at is None:
        return
    remaining = state._lock_expires_at - _time.time()
    if remaining <= 0:
        # Lazy-clear stale local mirror when the natural expiry has
        # passed. The server's ClientInfo.lock_is_active() reports the
        # same thing via the same time check.
        state._lock_holder_uuid = None
        state._lock_holder_label = None
        state._lock_expires_at = None
        state._lock_reason = None
        return
    box = layout.box()
    col = box.column(align=True)
    who = state._lock_holder_label or (state._lock_holder_uuid or "?")[:12]
    col.label(text=f"{who} has control", icon='LOCKED')
    col.label(text=f"{int(remaining)}s left · {(state._lock_reason or '')[:50]}")
    col.operator(
        "blendermcp.take_back_control",
        text="Take back control",
        icon='UNLOCKED',
    )


class BLENDERMCP_PT_Panel(bpy.types.Panel):
    # Version in the header is set at class-definition time (which runs at
    # addon register), so it tracks every install of a fresh build.
    # Reads from ``__version__`` rather than re-derivation, so it stays
    # in sync with bl_info via the bump script.
    bl_label = f"Blender MCP {__version__}"
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

        # Cooperative control-lock banners. Pending request is drawn
        # first (time-sensitive, blocks an LLM until answered); active
        # lock header goes below it (informational + Take-back). Both
        # no-op when their state is empty.
        _draw_pending_control_request(layout)
        _draw_active_lock_header(layout)
        # Extension-install request lives at the same visual tier as
        # the control-lock request — both are consent prompts.
        _draw_pending_extension_install(layout)

        # Version-mismatch banner — no-op unless the server told us we're
        # behind on the last register_client. Drawn before login so users
        # see the hint whether or not they're signed in for the moment.
        draw_update_banner(layout)

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

        # One switch for intent: pressed = stay connected (connect now,
        # reconnect with backoff, connect on next start); released = off.
        # Status below is derived from the live client, never from the
        # Scene flag, which serializes into .blend files.
        from .. import connection

        armed = prefs.auto_connect
        col.prop(
            prefs, "auto_connect",
            text="Stay connected" if armed else "Connect",
            toggle=True,
            icon='LINKED' if armed else 'UNLINKED',
        )
        alive = connection.client_alive(client)

        if not armed:
            col.label(text="Status: Disconnected", icon='CANCEL')
        elif not prefs.jwt_token:
            col.label(text="Status: Log in to connect", icon='INFO')
        elif not alive:
            wait = connection.supervisor().seconds_until_next_attempt()
            row = col.row(align=True)
            if wait:
                row.label(text=f"Status: Retrying in {int(wait)}s", icon='TIME')
            else:
                row.label(text="Status: Starting...", icon='TIME')
            row.operator("blendermcp.reconnect_now", text="Now", icon='FILE_REFRESH')
            if client is not None and client.last_error:
                col.label(text=f"Last error: {client.last_error[:60]}", icon='ERROR')

        # --- Status (live) — identity row above already shows label + uuid.
        if client and alive:
            if client.connected:
                col.label(text="Status: Connected", icon='CHECKMARK')
                with client.queue_lock:
                    qlen = len(client.job_queue)
                col.label(
                    text=f"Queue: {qlen} pending  Active: {len(client.active_jobs)}",
                )
            elif client.running:
                # Enriched reconnect status: attempt counter + countdown to
                # the next retry so extended outages don't look like a
                # frozen "Connecting..." spinner. next_retry_at is None
                # during the actual connect attempt itself (versus the
                # sleep between attempts), so guard the countdown.
                attempt = getattr(client, "reconnect_attempt", 0)
                if attempt > 0:
                    header = f"Status: Reconnecting (attempt {attempt})"
                else:
                    header = "Status: Connecting..."
                col.label(text=header, icon='TIME')
                next_at = getattr(client, "next_retry_at", None)
                if next_at is None:
                    # Mid-attempt: connect is bounded (~20 s) but offer the
                    # escape hatch anyway rather than a bare spinner.
                    col.operator(
                        "blendermcp.reconnect_now",
                        text="Retry now",
                        icon='FILE_REFRESH',
                    )
                else:
                    remaining = int(max(0, next_at - _time.time()))
                    # Countdown + escape hatch on the same row: skips
                    # the current backoff sleep AND the heartbeat
                    # detect window by tearing down the client and
                    # standing up a fresh one with backoff reset.
                    retry_row = col.row(align=True)
                    retry_row.label(text=f"Next try in {remaining}s")
                    retry_row.operator(
                        "blendermcp.reconnect_now",
                        text="Now",
                        icon='FILE_REFRESH',
                    )
            if client.last_error:
                col.label(text=f"Last error: {client.last_error[:60]}", icon='ERROR')

        # --- Asset integrations (toggles only — API keys live in prefs) ---
        layout.separator()
        col = layout.column(align=True)
        col.label(text="Asset Integrations", icon='ASSET_MANAGER')
        col.prop(prefs, "use_polyhaven", text="Poly Haven")
        col.prop(prefs, "use_hyper3d", text="Hyper3D Rodin")
        col.prop(prefs, "use_sketchfab", text="Sketchfab")
