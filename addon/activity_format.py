"""Short labels for the sidebar's Activity list. No bpy, so it's testable."""

from __future__ import annotations


def format_ms(ms: float) -> str:
    if ms < 1000:
        return f"{ms:.0f} ms"
    if ms < 60_000:
        return f"{ms / 1000:.1f} s"
    return f"{ms / 60_000:.0f} min"


def format_ago(seconds: float) -> str:
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60}m ago"
    return f"{s // 3600}h ago"


def pending_prompt(control, extension, reload, merge, chat_approval=None) -> str | None:
    """What the status-bar indicator should shout about, most urgent first."""
    if chat_approval:
        return "approval waiting"
    if control:
        return "control request"
    if extension:
        return "install request"
    if merge:
        return "merge offer"
    if reload:
        return "worker result"
    return None
