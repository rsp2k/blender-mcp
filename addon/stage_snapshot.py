"""Read-only snapshot of the add-on's state for other code in this Blender.

Published at bpy.app.driver_namespace["blender_mcp.stage"] so an app
template (agent-stage) can draw a viewport overlay without importing the
add-on. Contract v1, documented in docs-site reference/stage-snapshot:

- every publish assigns a freshly built dict; the published one is never
  mutated, so a reader can hold a reference safely
- a missing key means the add-on isn't running (clear() on unregister)
- an expires_at in the past means no lock

Main thread only: driver_namespace is a plain dict that drivers read on
the main thread. Off-thread changes reach publish() through
connection.request_ui_redraw, which already hops to the main thread.
Blender replaces driver_namespace with a new dict on every file load, so
the load_post handler republishes and readers must look it up each time.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from typing import Any

from . import state
from ._version import __version__

KEY = "blender_mcp.stage"
VERSION = 1
SOURCE = "blender_mcp"
ACTIVITY_MAX = 20


def _label(label: Any, uuid: Any) -> str:
    return str(label or (uuid or "?")[:12])


def _activity_entry(entry: dict) -> dict:
    return {
        "command": str(entry.get("command") or ""),
        "ok": bool(entry.get("ok")),
        "ms": float(entry.get("ms") or 0.0),
        "at": float(entry.get("at") or 0.0),
        "error": str(entry.get("error") or ""),
    }


def build_snapshot(
    *,
    connected: bool,
    lock_holder: str | None,
    lock_expires_at: float | None,
    lock_reason: str | None,
    pending: dict | None,
    chat_busy: bool,
    awaiting_approval: str | None,
    activity: Iterable[dict],
    now: float,
    addon_version: str = __version__,
) -> dict:
    """The v1 dict from plain values. No bpy, so it's testable."""
    lock = None
    if lock_holder and lock_expires_at is not None:
        lock = {
            "holder": str(lock_holder),
            "expires_at": float(lock_expires_at),
            "reason": str(lock_reason or ""),
        }
    pending_request = None
    if pending:
        duration = pending.get("duration_s")
        pending_request = {
            "requester": _label(pending.get("requester_label"), pending.get("requester_uuid")),
            "reason": str(pending.get("reason") or ""),
            "duration_s": duration if isinstance(duration, (int, float)) else 0,
        }
    entries = [_activity_entry(e) for e in activity if isinstance(e, dict)]
    return {
        "v": VERSION,
        "source": SOURCE,
        "addon_version": addon_version,
        "connected": bool(connected),
        "lock": lock,
        "pending_request": pending_request,
        "chat": {"busy": bool(chat_busy), "awaiting_approval": awaiting_approval},
        "activity": entries[-ACTIVITY_MAX:],
        "updated_at": float(now),
    }


def _snapshot_from_state() -> dict:
    from .chat.state import approval_action, chat_state

    client = state._client
    with chat_state.lock:
        busy = chat_state.busy
        approval = chat_state.pending_approval
    holder = None
    if state._lock_holder_label or state._lock_holder_uuid:
        holder = _label(state._lock_holder_label, state._lock_holder_uuid)
    return build_snapshot(
        connected=bool(client is not None and client.running and client.connected),
        lock_holder=holder,
        lock_expires_at=state._lock_expires_at,
        lock_reason=state._lock_reason,
        pending=state._pending_control_request,
        chat_busy=busy,
        awaiting_approval=approval_action(approval.get("prompt")) if approval else None,
        activity=list(state._activity),
        now=time.time(),
    )


def start() -> None:
    """Begin publishing (add-on register)."""
    state._stage_live = True
    publish()


def publish() -> None:
    """Rebuild and assign the snapshot. Main thread; never raises."""
    if not state._stage_live:
        # Unregistered: a redraw timer queued before unregister must not
        # bring back a snapshot nobody will update.
        return
    try:
        import bpy
        bpy.app.driver_namespace[KEY] = _snapshot_from_state()
    except Exception as e:  # noqa: BLE001 - a reader's overlay is never worth a crash
        print(f"[BlenderMCP] Stage snapshot not published: {e!r}")


def clear() -> None:
    """Stop publishing and remove the key (add-on unregister)."""
    state._stage_live = False
    try:
        import bpy
        bpy.app.driver_namespace.pop(KEY, None)
    except Exception:  # noqa: BLE001, S110
        pass
