"""BLENDERMCP_PT_Panel — View3D > Sidebar > BlenderMCP panel.

Setup config (server URL, asset integrations and their API keys) lives in
Edit > Preferences > Add-ons > BlenderMCP. The sidebar carries what you
use *while working*: consent prompts, the connection switch and status
(also shown as the header icon), the account row, and subpanels for the
bus and for marking the selection.

Login UI is shared with the prefs panel via
:func:`preferences.draw_login_section` so both call sites stay
identical without copy-paste drift.
"""

from __future__ import annotations

import time as _time

import bpy

from .. import state
from .._version import __version__
from ..activity_format import format_ago, format_ms
from ..client import bus_client as _bus_client
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


def _draw_pending_reload(layout) -> None:
    """Consent banner for a background worker's result (blender_offer_reload)."""
    from ..worker import reload_banner_lines
    lines = reload_banner_lines(state._pending_reload, bool(bpy.data.is_dirty))
    if not lines:
        return
    box = layout.box()
    col = box.column(align=True)
    col.label(text=lines[0][:80], icon='FILE_REFRESH')
    for line in lines[1:]:
        icon = 'ERROR' if line.startswith("Reloading discards") else 'NONE'
        col.label(text=line[:80], icon=icon)
    row = col.row(align=True)
    row.operator("blendermcp.reload_worker_result", text="Reload", icon='FILE_REFRESH')
    row.operator("blendermcp.dismiss_worker_result", text="Dismiss", icon='X')


def _draw_pending_merge(layout) -> None:
    """Consent banner for merging collections from a background result."""
    from ..worker_merge import merge_banner_lines
    lines = merge_banner_lines(state._pending_merge)
    if not lines:
        return
    box = layout.box()
    col = box.column(align=True)
    col.label(text=lines[0][:80], icon='OUTLINER_COLLECTION')
    for line in lines[1:]:
        icon = 'INFO' if line.startswith("These collections") else 'NONE'
        col.label(text=line[:80], icon=icon)
    row = col.row(align=True)
    row.operator("blendermcp.merge_worker_result", text="Merge", icon='CHECKMARK')
    row.operator("blendermcp.dismiss_worker_merge", text="Dismiss", icon='X')


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


def _bus_name(prefs) -> str:
    for b in state._buses:
        if b.get("bus_id") == prefs.default_bus_id and not b.get("is_personal"):
            return b.get("name", "?")
    return "Personal"


def _connection_status(prefs, client):
    """(icon, text, offer_retry_now) for the header and the status row."""
    from .. import connection

    if not prefs.auto_connect:
        return 'UNLINKED', "Off", False
    if not prefs.jwt_token:
        return 'INFO', "Log in to connect", False
    if not connection.client_alive(client):
        wait = connection.supervisor().seconds_until_next_attempt()
        return 'TIME', (f"Retry in {int(wait)}s" if wait else "Starting..."), True
    if client.connected:
        return 'CHECKMARK', f"Connected · {_bus_name(prefs)}", False
    attempt = getattr(client, "reconnect_attempt", 0)
    next_at = getattr(client, "next_retry_at", None)
    if next_at is not None:
        return 'TIME', f"Retry in {int(max(0, next_at - _time.time()))}s", True
    return 'TIME', (f"Reconnecting ({attempt})" if attempt else "Connecting..."), True


def _maybe_fetch_buses(prefs) -> None:
    """Fetch the bus list once per session in the background, so the Bus
    subpanel is filled without a manual refresh. The request runs off the
    main thread (it's a blocking HTTP call) with a snapshot of the two prefs
    it needs; the result lands via a timer."""
    if state._buses or state._buses_fetch_started:
        return
    state._buses_fetch_started = True
    import threading
    from types import SimpleNamespace

    snapshot = SimpleNamespace(jwt_token=prefs.jwt_token, server_url=prefs.server_url)

    def work():
        from .operators import _api_call

        try:
            buses = _api_call("GET", "/api/buses", snapshot).get("buses", [])
        except Exception as e:  # noqa: BLE001 - the Refresh button is the fallback
            print(f"[BlenderMCP] bus list fetch failed: {e}")
            return

        def apply():
            state._buses = buses
            _bus_client._request_ui_redraw()

        bpy.app.timers.register(apply, first_interval=0.0)

    threading.Thread(target=work, name="blendermcp-buses", daemon=True).start()


class BLENDERMCP_PT_Panel(bpy.types.Panel):
    bl_label = "Blender MCP"
    bl_idname = "BLENDERMCP_PT_Panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'BlenderMCP'

    def draw_header(self, context):
        # Connection state stays visible with the panel collapsed.
        prefs = get_prefs(context)
        icon, _, _ = _connection_status(prefs, state._client)
        self.layout.label(icon=icon)

    def draw_header_preset(self, context):
        self.layout.label(text=__version__)

    def draw(self, context):
        layout = self.layout
        prefs = get_prefs(context)

        # --- fastmcp missing: extension installs need a restart, legacy
        # single-file installs need a pip install. ensure_fastmcp retries
        # at most every 30 s, so calling it per draw is cheap.
        if not _bus_client.ensure_fastmcp():
            box = layout.box()
            lines = _bus_client.fastmcp_problem_lines()
            box.label(text=lines[0], icon='ERROR')
            for line in lines[1:]:
                box.label(text=line)
            return  # Everything below depends on fastmcp.

        # --- Fatal-error banner: the bus_client gave up (e.g. 401, JWT
        # unrecoverable) and cleared the token; say why and offer re-login.
        client = state._client
        if client is not None and client.fatal_error:
            box = layout.box()
            box.alert = True
            box.label(text=client.fatal_error, icon='ERROR')
            row = box.row(align=True)
            row.operator("blendermcp.re_login", text="Re-login", icon='URL')
            row.operator("blendermcp.dismiss_fatal_error", text="Dismiss", icon='X')

        # Consent prompts first: they block an LLM until answered.
        _draw_pending_control_request(layout)
        _draw_active_lock_header(layout)
        _draw_pending_extension_install(layout)
        _draw_pending_reload(layout)
        _draw_pending_merge(layout)
        draw_update_banner(layout, compact=True)

        if not prefs.jwt_token:
            draw_login_section(layout, prefs)
            return

        # --- Connection: one switch for intent (pressed = stay connected,
        # with backoff, and connect on next start). Status comes from the
        # live client, never the Scene flag, which serializes into .blend.
        icon, text, retry_now = _connection_status(prefs, client)
        if not prefs.auto_connect:
            row = layout.row()
            row.scale_y = 1.3
            row.prop(prefs, "auto_connect", text="Connect", toggle=True, icon='UNLINKED')
        else:
            row = layout.row(align=True)
            row.prop(prefs, "auto_connect", text="", toggle=True, icon='LINKED')
            row.label(text=text, icon=icon)
            if retry_now:
                row.operator("blendermcp.reconnect_now", text="", icon='FILE_REFRESH')
        if client is not None and client.connected:
            _maybe_fetch_buses(prefs)
            with client.queue_lock:
                queued = len(client.job_queue)
            active = len(client.active_jobs)
            if queued or active:
                layout.label(text=f"Running {active} · {queued} queued", icon='SORTTIME')
        elif client is not None and client.last_error and prefs.auto_connect:
            row = layout.row()
            row.alert = True
            row.label(text=client.last_error[:60], icon='ERROR')

        draw_login_section(layout, prefs, compact=True)


class _SubPanel:
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'BlenderMCP'
    bl_parent_id = "BLENDERMCP_PT_Panel"


class BLENDERMCP_PT_Bus(_SubPanel, bpy.types.Panel):
    bl_label = "Bus"
    bl_idname = "BLENDERMCP_PT_Bus"
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        return bool(get_prefs(context).jwt_token) and _bus_client.ensure_fastmcp()

    def draw_header_preset(self, context):
        self.layout.label(text=_bus_name(get_prefs(context)))

    def draw(self, context):
        layout = self.layout
        prefs = get_prefs(context)
        client = state._client
        live_uuid = client.client_uuid if client else (context.scene.blendermcp_client_id or "")

        # Who this Blender is on the bus. The uuid is sticky on disk once
        # minted, so the copy button is safe any time it exists.
        row = layout.row(align=True)
        row.label(text=get_client_label(prefs), icon='POSE_HLT')
        sub = row.row(align=True)
        sub.enabled = bool(live_uuid)
        sub.operator("blendermcp.copy_client_uuid", text="", icon='COPYDOWN')
        row.operator("blendermcp.refresh_buses", text="", icon='FILE_REFRESH')

        if not state._buses:
            layout.label(text="Fetching buses..." if state._buses_fetch_started else "Not loaded yet")
            return

        from ..preferences import ADDON_PACKAGE_NAME

        data_path = f"preferences.addons[\"{ADDON_PACKAGE_NAME}\"].preferences.default_bus_id"
        col = layout.column(align=True)
        for b in state._buses:
            if b.get("is_personal"):
                text, icon, value = "Personal", 'USER', ""
            else:
                text = b.get("name", "?")
                icon = 'CHECKMARK' if b.get("is_owned_by_me") else 'COMMUNITY'
                value = b["bus_id"]
            op = col.operator("wm.context_set_string", text=text, icon=icon,
                              depress=(prefs.default_bus_id == value))
            op.data_path = data_path
            op.value = value

        row = layout.row(align=True)
        row.operator("blendermcp.create_bus", text="Create", icon='ADD')
        row.operator("blendermcp.join_bus", text="Join", icon='LINKED')
        if prefs.default_bus_id:
            # Invite/leave only mean something on a shared bus.
            row = layout.row(align=True)
            row.operator("blendermcp.invite_to_bus", text="Invite", icon='COPYDOWN')
            row.operator("blendermcp.leave_bus", text="Leave", icon='X')


class BLENDERMCP_PT_PointItOut(_SubPanel, bpy.types.Panel):
    """Marks the assistant reads back with list_annotations (layer "Marked")."""

    bl_label = "Point it out"
    bl_idname = "BLENDERMCP_PT_PointItOut"

    def draw(self, context):
        row = self.layout.row(align=True)
        row.operator("blendermcp.mark_selection", text="Mark selection", icon='SELECT_SET').style = 'box'
        row.operator("blendermcp.mark_selection", text="", icon='MESH_CIRCLE').style = 'circle'
        row.operator("blendermcp.mark_selection", text="", icon='SORT_ASC').style = 'arrow'
        row.operator("blendermcp.clear_marks", text="", icon='TRASH')


class BLENDERMCP_PT_Activity(_SubPanel, bpy.types.Panel):
    """The last commands run in this Blender, newest first."""

    bl_label = "Activity"
    bl_idname = "BLENDERMCP_PT_Activity"
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        return bool(get_prefs(context).jwt_token)

    def draw_header_preset(self, context):
        if state._activity:
            last = state._activity[-1]
            row = self.layout.row()
            row.alert = not last["ok"]
            row.label(text=f"{last['command']} · {format_ago(_time.time() - last['at'])}")

    def draw(self, context):
        layout = self.layout
        if not state._activity:
            layout.label(text="Nothing has run yet", icon='INFO')
            return
        now = _time.time()
        col = layout.column(align=True)
        for entry in reversed(state._activity):
            row = col.row(align=True)
            row.alert = not entry["ok"]
            row.label(text=entry["command"], icon='CHECKMARK' if entry["ok"] else 'ERROR')
            row.label(text=f"{format_ms(entry['ms'])} · {format_ago(now - entry['at'])}")
            if entry["error"]:
                err = col.row()
                err.alert = True
                err.label(text=f"    {entry['error'][:60]}")
