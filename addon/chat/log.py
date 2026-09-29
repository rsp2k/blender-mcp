"""The "BlenderMCP Chat" text block: the full transcript, saved with the .blend.

Main thread only. The loop thread queues lines in chat_state and
schedules flush_pending with a timer.
"""

from __future__ import annotations

from .state import chat_state

TEXT_NAME = "BlenderMCP Chat"


def ensure_text():
    import bpy
    text = bpy.data.texts.get(TEXT_NAME)
    if text is None:
        text = bpy.data.texts.new(TEXT_NAME)
    return text


def append_lines(lines: list[str]) -> None:
    if not lines:
        return
    text = ensure_text()
    chunk = "\n".join(lines) + "\n"
    try:
        last = len(text.lines) - 1
        text.current_line_index = last
        text.current_character = len(text.lines[last].body)
        text.select_end_line_index = last
        text.select_end_character = text.current_character
        text.write(chunk)
    except Exception:  # noqa: BLE001
        text.from_string(text.as_string() + chunk)


def flush_pending():
    """Timer callback: rebuild the Reader document. Returns None (one-shot).

    The text block used to be an append-only log; it is now the Reader's
    document (reader.py), regenerated from the transcript."""
    chat_state.drain_log()
    try:
        from .reader import sync
        sync()
    except Exception as e:  # noqa: BLE001
        print(f"[BlenderMCP] Chat reader update failed: {e}")


def open_log(context) -> str:
    """Show the log in a Text Editor: an open one if any, else a new window.

    Returns a short description of where it went, for the operator report.
    """
    import bpy

    text = ensure_text()
    screen = context.window.screen if context.window else None
    for area in (screen.areas if screen else []):
        if area.type == 'TEXT_EDITOR':
            area.spaces.active.text = text
            area.tag_redraw()
            return "Shown in the Text Editor"

    before = {w.as_pointer() for w in context.window_manager.windows}
    bpy.ops.wm.window_new()
    new = [w for w in context.window_manager.windows if w.as_pointer() not in before]
    if not new:
        return f"Open a Text Editor and pick '{TEXT_NAME}'"
    area = new[0].screen.areas[0]
    area.type = 'TEXT_EDITOR'
    space = area.spaces.active
    space.text = text
    space.show_word_wrap = True
    space.top = max(0, len(text.lines) - 20)
    return "Opened in a new window"
