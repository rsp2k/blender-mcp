"""The Chat Reader: the conversation as a document in a Text Editor.

The sidebar is too narrow to read a reply in, so the full conversation is
kept in the "BlenderMCP Chat" text block, rebuilt whenever the transcript
changes, and shown in a Text Editor split beside the 3D viewport (word
wrap on, Ctrl+wheel zooms, Ctrl+F searches, text can be copied).

render_document and friends have no bpy dependency so they can be tested.
"""

from __future__ import annotations

import re
import time

from .state import chat_state, format_duration, tool_line

TEXT_NAME = "BlenderMCP Chat"
RULE_WIDTH = 44
READER_FONT_SIZE = 13
SPLIT_FACTOR = 0.62  # share of the width the 3D viewport keeps

_MD_PATTERNS = (
    (re.compile(r"\*\*(.+?)\*\*"), r"\1"),
    (re.compile(r"__(.+?)__"), r"\1"),
    (re.compile(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])"), r"\1"),
    (re.compile(r"`([^`]+)`"), r"\1"),
    (re.compile(r"^#{1,6}\s+", re.MULTILINE), ""),
)


def plain(text: str) -> str:
    """Model replies often carry markdown; the Text Editor shows it raw."""
    out = text or ""
    for pattern, repl in _MD_PATTERNS:
        out = pattern.sub(repl, out)
    return out.strip()


def model_label(backend: dict | None) -> str:
    """"claude-opus-5" -> "Claude Opus 5"; other models keep their id."""
    if not isinstance(backend, dict) or not backend.get("model"):
        return ""
    model = str(backend["model"])
    if model.startswith("claude-"):
        words = model.removeprefix("claude-").split("-")
        name, nums = [w for w in words if not w.isdigit()], [w for w in words if w.isdigit()]
        return " ".join(["Claude", *(w.capitalize() for w in name)]
                        + ([".".join(nums)] if nums else []))
    return model


def _clock(ts) -> str:
    if not isinstance(ts, (int, float)):
        return ""
    return time.strftime("%H:%M", time.localtime(ts))


def _rule(label: str) -> str:
    head = f"── {label} "
    return head + "─" * max(3, RULE_WIDTH - len(head))


def turns(messages: list) -> list[dict]:
    """Group messages by turn: the user's message, the steps, the replies."""
    out: list[dict] = []
    for m in messages:
        role = m.get("role")
        if role == "user" or not out:
            out.append({"user": None, "steps": [], "replies": [], "notes": []})
        cur = out[-1]
        if role == "user":
            cur["user"] = m
        elif role == "tool":
            cur["steps"].append(m)
        elif role == "assistant":
            cur["replies"].append(m)
        else:
            cur["notes"].append(m)
    return out


def turn_seconds(turn: dict) -> float | None:
    total = sum(s.get("ms") or 0 for s in turn["steps"] if isinstance(s.get("ms"), (int, float)))
    return total / 1000.0 if total else None


def reply_header(turn: dict, who: str = "Claude") -> str:
    n = len(turn["steps"])
    bits = [who]
    if n:
        bits.append(f"{n} step{'s' if n != 1 else ''}")
    secs = turn_seconds(turn)
    if secs:
        bits.append(format_duration(secs * 1000))
    return " · ".join(bits)


def render_document(messages: list, backend: dict | None = None) -> str:
    who = model_label(backend) or "Assistant"
    lines = ["BlenderMCP Chat", f"{who}. Newest at the bottom.", ""]
    for t in turns(messages):
        user = t["user"]
        if user:
            when = _clock(user.get("at"))
            lines.append(_rule("You" + (f" · {when}" if when else "")))
            lines.append(plain(user.get("text") or ""))
            clipped = user.get("clipped")
            if clipped:
                lines.append("(clipped: " + ", ".join(clipped) + ")")
            lines.append("")
        if t["steps"] or t["replies"]:
            lines.append(_rule(reply_header(t, who.split(" ")[0] if who != "Assistant" else who)))
            for s in t["steps"]:
                ok = s.get("ok")
                mark = "…" if ok is None else ("✓" if ok else "✗")
                row = f"  {mark} {tool_line(s)}"
                if ok is False and s.get("error"):
                    row += f": {s['error']}"
                lines.append(row)
            if t["steps"]:
                lines.append("")
            for r in t["replies"]:
                lines.append(plain(r.get("text") or ""))
                lines.append("")
        for n in t["notes"]:
            prefix = "Error: " if n.get("role") == "error" else ""
            lines.append(prefix + plain(n.get("text") or ""))
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def latest_turn_line(doc: str) -> int:
    """Line index of the newest "── You" header, where reading should start."""
    lines = doc.split("\n")
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].startswith("── You"):
            return i
    return 0


def preview(text: str, wrap, max_lines: int = 6) -> tuple[list[str], bool]:
    """The first lines of a reply for the sidebar, and whether it was cut."""
    lines: list[str] = []
    for para in plain(text).split("\n"):
        if not para.strip():
            continue
        lines.extend(wrap(para) or [""])
        if len(lines) > max_lines:
            return lines[:max_lines], True
    return lines, False


# --- bpy side ------------------------------------------------------------

_synced: list = [None]


def _text():
    import bpy
    text = bpy.data.texts.get(TEXT_NAME)
    if text is None:
        text = bpy.data.texts.new(TEXT_NAME)
    return text


def _reader_spaces():
    import bpy
    wm = bpy.context.window_manager
    for window in wm.windows:
        for area in window.screen.areas:
            if area.type == 'TEXT_EDITOR' and getattr(area.spaces.active, "text", None) \
                    and area.spaces.active.text.name == TEXT_NAME:
                yield area


def sync() -> None:
    """Rebuild the text block when the transcript changed (main thread)."""
    snap = chat_state.snapshot()
    key = (snap["revision"], snap["conversation_id"])
    if key == _synced[0]:
        return
    text = _text()
    text.from_string(render_document(snap["messages"], snap["backend_used"] or snap["backend"]))
    _synced[0] = key
    start = latest_turn_line(text.as_string())
    for area in _reader_spaces():
        area.spaces.active.top = start
        area.tag_redraw()


def is_open(context) -> bool:
    screen = context.window.screen if context.window else None
    return any(a.type == 'TEXT_EDITOR' and getattr(a.spaces.active, "text", None)
               and a.spaces.active.text.name == TEXT_NAME
               for a in (screen.areas if screen else []))


def toggle(context) -> str:
    """Open the Reader beside the 3D viewport, or close it if it's open."""
    import bpy

    screen = context.window.screen
    for area in screen.areas:
        space = area.spaces.active
        if area.type == 'TEXT_EDITOR' and getattr(space, "text", None) \
                and space.text.name == TEXT_NAME:
            with context.temp_override(area=area):
                bpy.ops.screen.area_close()
            return "Reader closed"

    _synced[0] = None
    sync()
    view = context.area if context.area and context.area.type == 'VIEW_3D' else next(
        (a for a in screen.areas if a.type == 'VIEW_3D'), None)
    if view is None:
        return "No 3D viewport to open the Reader beside"
    before = {a.as_pointer() for a in screen.areas}
    with context.temp_override(area=view):
        bpy.ops.screen.area_split(direction='VERTICAL', factor=SPLIT_FACTOR)
    new = [a for a in screen.areas if a.as_pointer() not in before]
    if not new:
        return "Couldn't split the viewport"
    # A vertical split leaves the original area on the left with SPLIT_FACTOR
    # of the width, and the new area on the right. The new one becomes the
    # Reader, so the viewport (and the Chat tab in its sidebar) stay put.
    # Area x/width aren't updated until the next redraw, so don't compare them.
    area = new[0]
    area.type = 'TEXT_EDITOR'
    space = area.spaces.active
    space.text = _text()
    space.show_word_wrap = True
    space.show_line_numbers = False
    space.show_syntax_highlight = False
    space.show_margin = False
    try:
        space.font_size = READER_FONT_SIZE
    except (AttributeError, TypeError):
        pass
    space.top = latest_turn_line(space.text.as_string())
    return "Reader opened"
