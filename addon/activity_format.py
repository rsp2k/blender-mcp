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


def wrap_message(text: str, width_px: float, ui_scale: float = 1.0,
                 reserve_px: float = 60.0, min_chars: int = 16) -> list[str]:
    """Split a sentence into label lines that fit a panel `width_px` wide.

    Blender labels truncate instead of wrapping. Its UI font averages
    about 7 px per character at scale 1.0; `reserve_px` covers the icon,
    the box border and the panel margins.
    """
    import textwrap

    scale = ui_scale if ui_scale and ui_scale > 0 else 1.0
    chars = max(min_chars, int((width_px - reserve_px * scale) / (7.0 * scale)))
    return textwrap.wrap(text or "", width=chars, break_long_words=True,
                         break_on_hyphens=False) or [""]
