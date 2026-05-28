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

# Phase I7: cached list of buses the user is a member of (populated by
# BLENDERMCP_OT_RefreshBuses, read by the sidebar panel to render the bus
# picker dropdown). Each entry is the dict shape returned by the
# bus_list_buses MCP tool: {bus_id, name, role, is_personal, owner_user_id,
# is_owned_by_me, created_at, description}.
_buses: list = []
