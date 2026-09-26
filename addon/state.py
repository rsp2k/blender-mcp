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

# Phase I7: cached list of buses the user is a member of (populated by
# BLENDERMCP_OT_RefreshBuses, read by the sidebar panel to render the bus
# picker dropdown). Each entry is the dict shape returned by the
# bus_list_buses MCP tool: {bus_id, name, role, is_personal, owner_user_id,
# is_owned_by_me, created_at, description}.
_buses: list = []

# 1.5.8: OAuth in-flight indicator. Set True when the Login worker starts;
# flipped False by the poll() timer on completion (success OR error). The
# panel reads it to render an "Authenticating..." spinner widget and to
# disable the Login button (no double-click races). A simple monotonic
# counter drives the animated dots — increments on every panel redraw
# while in flight, modulo 3 picks 1/2/3 dots.
_auth_in_progress: bool = False
_auth_dots: int = 0

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
