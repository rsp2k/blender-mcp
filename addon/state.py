"""Module-level singletons for the running client + executor.

addon.py and addon/ui/operators.py both need read/write access to the
same `_client` and `_executor` instances — the operators construct them
and the panel reads them to render status. Living here means both
modules import the *same* module attribute (instead of each having
their own private copy via `from addon.state import _client`, which
would shadow on rebinding).

Convention: callers do ``from addon import state`` and then
``state._client`` / ``state._executor``. The bare names are written
back via ``state._client = ...`` so mutations are visible everywhere.

Phase 8 will move these to WindowManager properties so they're truly
per-session and don't dirty the .blend file.
"""

from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .client import BlenderMCPClient
    from .executor import BlenderCommandExecutor

_client: Optional["BlenderMCPClient"] = None
_executor: Optional["BlenderCommandExecutor"] = None
# addon.connection.ConnectionSupervisor, created on first use.
_supervisor = None
# addon.identity.StickyUUIDManager holding this process's identity lease.
_identity = None
# addon.update_prefetch.Prefetch while Update now downloads the archive itself.
_update_prefetch = None
# {"version", "previous", "until"} for a few seconds after an update installed.
_just_updated = None

# Background workers this GUI Blender spawned: worker uuid -> dict with
# popen, pid, dir, snapshot, log, deadline, label (see executor handler
# spawn_worker).
_workers: dict = {}
# Set inside a worker process when a stop arrives; run_worker_loop exits.
_worker_stop_requested: bool = False
# Pending "background result ready" offer: {path, message, offered_at}.
_pending_reload: Optional[dict] = None
# Pending per-collection merge offer: {path, collections, mode, message,
# offered_at}; and the report of the last merge (see addon.worker_merge).
_pending_merge: Optional[dict] = None
_last_merge_result: Optional[dict] = None
# report_progress callable for the job currently executing on the main
# thread (set by the drainer around each job), or None.
_current_progress = None
# addon.tool_servers.bridge.ToolServerBridge: the user's own MCP tool
# servers, running on their own loop thread. Created at first registration.
_tool_servers = None

# Phase I7: cached list of buses the user is a member of (populated by
# BLENDERMCP_OT_RefreshBuses, read by the sidebar panel to render the bus
# picker dropdown). Each entry is the dict shape returned by the
# bus_list_buses MCP tool: {bus_id, name, role, is_personal, owner_user_id,
# is_owned_by_me, created_at, description}.
_buses: list = []
# Set once the sidebar has started its background fetch of _buses.
_buses_fetch_started: bool = False

# Last commands run in this Blender, newest last, for the sidebar's Activity
# subpanel: {command, ok, ms, at (epoch s), error}. Written by the drainer
# on the main thread.
_activity: deque = deque(maxlen=20)

# True between register and unregister: addon.stage_snapshot only writes
# bpy.app.driver_namespace["blender_mcp.stage"] while this is set.
_stage_live: bool = False

# 1.5.8: OAuth in-flight indicator. Set True when the Login worker starts;
# flipped False by the poll() timer on completion (success OR error). The
# panel reads it to render an "Authenticating..." spinner widget and to
# disable the Login button (no double-click races). A simple monotonic
# counter drives the animated dots — increments on every panel redraw
# while in flight, modulo 3 picks 1/2/3 dots.
_auth_in_progress: bool = False
_auth_dots: int = 0
# Why the last Login click failed, in plain words, for the panel near the
# Login button. Set by addon.auth.login_feedback; cleared by a new Login
# click, the Dismiss button, or a successful login.
_login_error: str | None = None
# Set by addon.package_health when bundled package files vanished from disk
# while Blender runs (the missing path, or a short reason). The panel and
# status bar then ask for a restart, and the connection supervisor stops
# starting clients until the files are back.
_package_problem: str | None = None

# Server-advertised latest addon version, populated from the
# register_client response envelope. `_update_available` is set True
# only when the server sent a hint AND the semver tuple is strictly
# greater than addon._version.tuple_version. Both are None until the
# first successful registration; the sidebar/preferences panels read
# them to render an "Update available" banner. Cleared on Logout so a
# fresh login re-derives them.
_latest_addon_version: Optional[str] = None
_addon_download_url: Optional[str] = None
_update_available: bool = False

# Pending extension-install request from an LLM via
# blender_install_extension. Set by the drainer's extension_install_request
# handler when consent is needed (repo not pre-authorized OR requester
# not pre-authorized). Cleared by the Grant/Deny/AlwaysAllow operators,
# which also submit the reply. Dict shape:
#   {job_id, requester_uuid, requester_label, repo_url, repo_name,
#    package_id, reason, new_repo: bool}
_pending_extension_request: Optional[dict] = None

# Cooperative control-lock state. Two orthogonal things live here:
#
#   1. Pending REQUEST — an LLM asked for control, user hasn't clicked
#      yet. The banner in the sidebar draws Allow/Deny/Always-allow
#      buttons wired to operators that resolve _pending_control_request
#      by posting a submit_job_update reply and clearing this dict.
#
#   2. Active LOCK — an LLM's request was granted (either by pre-auth
#      or by the user). Header text shows the countdown; auto-release
#      timer (bpy.app.timers) fires at _lock_expires_at to submit a
#      release_control call and clear these fields.
#
# Both are None/False when nothing's happening; that's the vast common
# case, and the draw path early-returns on it so the banner has zero
# cost when idle.
_pending_control_request: Optional[dict] = None  # {job_id, requester_uuid, requester_label, reason, duration_s}
_lock_holder_uuid: Optional[str] = None
_lock_holder_label: Optional[str] = None
_lock_expires_at: Optional[float] = None  # unix epoch
_lock_reason: Optional[str] = None
