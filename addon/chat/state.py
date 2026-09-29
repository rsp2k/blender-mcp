"""Chat state shared by the client loop thread (writes) and the panel (reads).

No bpy here, so progress parsing, history bounding and wrapping can be
tested outside Blender. Every mutation takes the lock; the panel draws
from snapshot() copies.
"""

from __future__ import annotations

import collections
import json
import textwrap
import threading
import time
from typing import Any

APPROVAL_PREFIX = "BlenderMCP approval:"
APPROVAL_TIMEOUT_S = 120.0
HISTORY_LIMIT = 20
MESSAGE_LIMIT = 200
EVENT_TYPES = ("status", "tool", "text")

_STATUS_TEXT = {
    "disabled": "Chat is turned off on this server.",
    "no_backend": (
        "No chat backend is available for your account. "
        "Set one in Preferences > Add-ons > Blender MCP > Chat backend."
    ),
    "busy": "A chat turn is already running for your account. Try again when it finishes.",
    "backend_error": "The model call failed.",
    "timeout": "The turn took too long and was stopped.",
}


def parse_progress_event(message: Any) -> dict | None:
    """Decode one progress message into an event dict, or None if it isn't one."""
    if not isinstance(message, str) or not message.strip():
        return None
    try:
        event = json.loads(message)
    except ValueError:
        return None
    if not isinstance(event, dict) or event.get("t") not in EVENT_TYPES:
        return None
    if event["t"] == "tool" and not isinstance(event.get("name"), str):
        return None
    return event


def is_approval_request(message: Any) -> bool:
    return isinstance(message, str) and message.startswith(APPROVAL_PREFIX)


def build_history(messages: list, limit: int = HISTORY_LIMIT) -> list[dict]:
    """Last `limit` user/assistant turns, text only, in the contract's shape."""
    turns = [
        {"role": m["role"], "content": m["text"]}
        for m in messages
        if m.get("role") in ("user", "assistant") and (m.get("text") or "").strip()
    ]
    return turns[-limit:] if limit > 0 else []


def wrap_text(text: str, width_px: float, ui_scale: float = 1.0,
              char_px: float = 7.0, reserve_px: float = 40.0,
              min_chars: int = 10) -> list[str]:
    """Wrap text to a sidebar region `width_px` wide, keeping blank lines.

    Blender's UI font averages about 7 px per character at scale 1.0;
    `reserve_px` covers the icon and panel margins.
    """
    scale = ui_scale if ui_scale and ui_scale > 0 else 1.0
    chars = int((width_px - reserve_px * scale) / (char_px * scale))
    chars = max(min_chars, chars)
    lines: list[str] = []
    for para in (text or "").splitlines() or [""]:
        if not para.strip():
            lines.append("")
            continue
        lines.extend(textwrap.wrap(para, width=chars, break_long_words=True,
                                   break_on_hyphens=False) or [""])
    return lines


def decode_tool_result(result: Any) -> dict | None:
    """JSON dict carried by a CallToolResult (text content or data), else None."""
    text: Any = None
    content = getattr(result, "content", None)
    if content:
        text = getattr(content[0], "text", None)
    if not text:
        text = getattr(result, "data", None)
    if isinstance(text, str):
        try:
            text = json.loads(text)
        except ValueError:
            return None
    return text if isinstance(text, dict) else None


def tool_error_text(result: Any) -> str:
    content = getattr(result, "content", None) or []
    text = getattr(content[0], "text", "") if content else ""
    return str(text or "the server reported an error")


def result_message(payload: dict | None) -> tuple[str, str]:
    """Map a blender_chat result to (role, text) for the transcript."""
    if not isinstance(payload, dict):
        return "error", "The server sent a reply the add-on couldn't read."
    status = payload.get("status")
    if status == "ok":
        return "assistant", str(payload.get("reply") or "")
    base = _STATUS_TEXT.get(status)
    if base is None:
        return "error", f"Unexpected reply from the server (status {status!r})."
    extra = payload.get("hint") if status == "no_backend" else payload.get("detail")
    if status == "no_backend" and extra:
        return "error", str(extra)
    if extra:
        return "error", f"{base} {extra}"
    return "error", base


def format_duration(ms: Any) -> str:
    """Tool timing in seconds: "0.39 s", "4.3 s", "1m 25s"."""
    if not isinstance(ms, (int, float)):
        return ""
    s = max(0.0, float(ms) / 1000.0)
    if s < 10:
        return f"{s:.2f} s"
    if s < 60:
        return f"{s:.1f} s"
    return f"{int(s // 60)}m {int(s % 60):02d}s"


def tool_line(entry: dict) -> str:
    """One transcript line for a tool step, with any approval wait.

    Tools from the user's own tool servers carry "server" and show as
    "pdf · read_text" rather than the chat name "pdf__read_text".
    """
    text = str(entry.get("name") or "?")
    server = entry.get("server")
    if isinstance(server, str) and server:
        prefix = f"{server}__"
        text = f"{server} · {text.removeprefix(prefix)}"
    took = format_duration(entry.get("ms"))
    if took:
        text += f"  {took}"
    waited = entry.get("wait_ms")
    if isinstance(waited, (int, float)) and waited >= 1000:
        text += f" · waited {format_duration(waited)} for you"
    return text


STEP_DETAIL_CHARS = 300


def step_detail(event: dict) -> dict:
    """The optional ``error`` (failed step) and ``head`` (start of a successful
    result) of a tool end event, as strings of bounded length."""
    out = {}
    for key in ("error", "head"):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            out[key] = value[:STEP_DETAIL_CHARS]
    return out


ROLE_ICONS = {"user": "USER", "assistant": "MONKEY", "error": "ERROR", "status": "INFO"}


def transcript_rows(messages: list, wrap: Any) -> list[dict]:
    """Messages as list rows for the Chat tab: wrapped lines, the speaker's icon
    on each message's first line, and one empty row at the end that the list
    keeps active so it scrolls to the newest message."""
    rows: list[dict] = []
    for m in messages:
        role = m.get("role")
        if role == "tool":
            ok = m.get("ok")
            icon = "TIME" if ok is None else ("CHECKMARK" if ok else "ERROR")
            rows.append({"role": "tool", "text": tool_line(m), "icon": icon})
            continue
        lines = wrap(m.get("text") or "") or [""]
        for i, line in enumerate(lines):
            rows.append({"role": role or "status", "text": line,
                         "icon": ROLE_ICONS.get(role, "INFO") if i == 0 else "BLANK1"})
    rows.append({"role": "spacer", "text": "", "icon": "NONE"})
    return rows


def format_log_line(entry: dict, when: float | None = None) -> str:
    stamp = time.strftime("%H:%M:%S", time.localtime(when if when is not None else time.time()))
    role = entry.get("role")
    text = entry.get("text") or ""
    if role == "tool":
        mark = "ok" if entry.get("ok") else "failed"
        err = entry.get("error") if not entry.get("ok") else None
        return f"[{stamp}]   tool {tool_line(entry)} {mark}" + (f": {err}" if err else "")
    label = {"user": "You", "assistant": "Assistant", "error": "Error"}.get(role, "Note")
    body = text.replace("\n", "\n    ")
    return f"[{stamp}] {label}: {body}"


class ChatState:
    """Everything the Chat tab shows. One instance per Blender (chat_state)."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.messages: list[dict] = []
        self.busy = False
        self.status = ""
        self.last_error: str | None = None
        # None until a registration answered; then whether "chat" was in features.
        self.available: bool | None = None
        self.features: list[str] = []
        # {"prompt", "future", "loop", "since"} while an approval waits.
        self.pending_approval: dict | None = None
        # Last blender_get/set_chat_backend answer, and its error.
        self.backend: dict | None = None
        self.backend_error: str | None = None
        self.backend_used: dict | None = None
        self.turn = 0
        self._open_turn: int | None = None
        self.turn_started_at: float | None = None
        # In-flight call: concurrent future, progress handler, bus client.
        self.inflight: tuple | None = None
        self._log = collections.deque()
        # The saved conversation these messages belong to (history.py).
        self.conversation_id: str | None = None
        # Bumped on every transcript change, so the panel knows to rebuild rows.
        self.revision = 0

    # --- transcript -------------------------------------------------------

    def add_message(self, role: str, text: str, log: bool = True, **extra: Any) -> dict:
        entry = {"role": role, "text": text, "turn": self.turn, "at": time.time(), **extra}
        with self.lock:
            self.revision += 1
            self.messages.append(entry)
            del self.messages[:-MESSAGE_LIMIT]
            if log:
                self._log.append(format_log_line(entry))
        return entry

    def log_raw(self, text: str) -> None:
        with self.lock:
            self._log.append(text)

    def drain_log(self) -> list[str]:
        with self.lock:
            lines = list(self._log)
            self._log.clear()
        return lines

    def has_messages(self) -> bool:
        return bool(self.messages)

    def clear(self) -> None:
        with self.lock:
            self.revision += 1
            self.messages.clear()
            self.last_error = None
            if not self.busy:
                self.status = ""

    def load_conversation(self, conversation_id: str | None, messages: list) -> bool:
        """Show a saved conversation. Refused while a turn is running."""
        with self.lock:
            if self.busy:
                return False
            restored = [dict(m) for m in messages if isinstance(m, dict)][-MESSAGE_LIMIT:]
            for m in restored:
                if m.get("role") == "tool" and m.get("ok") is None:
                    m["ok"] = False
            self.messages = restored
            self.conversation_id = conversation_id
            self.turn = max([m.get("turn", 0) for m in restored if isinstance(m.get("turn"), int)] or [0])
            self.last_error = None
            self.status = ""
            self.revision += 1
            return True

    def export(self) -> tuple[str | None, list[dict]]:
        with self.lock:
            return self.conversation_id, [dict(m) for m in self.messages]

    # --- turns ------------------------------------------------------------

    def begin_turn(self, text: str, clipped: list[str] | None = None) -> list[dict]:
        """Record the user's message and open a turn; returns the history to send.
        ``clipped`` labels what the user attached ("3 selected", "viewport")."""
        with self.lock:
            history = build_history(self.messages)
            self.turn += 1
            self._open_turn = self.turn
            self.busy = True
            self.status = "Sending…"
            self.last_error = None
            self.turn_started_at = time.monotonic()
            extra = {"clipped": list(clipped)} if clipped else {}
            self.add_message("user", text, **extra)
            return history

    def turn_open(self, turn: int) -> bool:
        return self._open_turn == turn

    def apply_event(self, event: dict) -> None:
        kind = event.get("t")
        with self.lock:
            if kind == "status":
                self.status = str(event.get("text") or "")
            elif kind == "text":
                text = str(event.get("text") or "")
                if text.strip():
                    self.add_message("assistant", text)
            elif kind == "tool":
                name = event["name"]
                server = event.get("server") if isinstance(event.get("server"), str) else None
                extra = {"server": server} if server else {}
                if event.get("phase") == "start":
                    self.status = f"Running {tool_line({'name': name, **extra})}…"
                    self.add_message("tool", "", log=False, name=name, ok=None, ms=None, **extra)
                    return
                ok, ms, wait = bool(event.get("ok")), event.get("ms"), event.get("wait_ms")
                detail = step_detail(event)
                for entry in reversed(self.messages):
                    if (entry.get("role") == "tool" and entry.get("name") == name
                            and entry.get("ok") is None and entry.get("turn") == self.turn):
                        entry["ok"], entry["ms"], entry["wait_ms"] = ok, ms, wait
                        entry.update(detail)
                        self.revision += 1
                        self._log.append(format_log_line(entry))
                        break
                else:
                    self.add_message("tool", "", name=name, ok=ok, ms=ms, wait_ms=wait,
                                     **detail, **extra)

    def finish_turn(self, turn: int, role: str, text: str,
                    payload: dict | None = None) -> bool:
        """Close `turn` with its final reply or error. False if it was already closed."""
        with self.lock:
            if self._open_turn != turn:
                return False
            self._open_turn = None
            self.busy = False
            self.status = ""
            self.inflight = None
            self.revision += 1
            # Tools that never reported an end are shown as failed.
            for entry in self.messages:
                if entry.get("role") == "tool" and entry.get("turn") == turn and entry.get("ok") is None:
                    entry["ok"] = False
            if isinstance(payload, dict) and isinstance(payload.get("backend"), dict):
                self.backend_used = payload["backend"]
            if role == "assistant":
                shown = {
                    (m.get("text") or "").strip() for m in self.messages
                    if m.get("role") == "assistant" and m.get("turn") == turn
                }
                if text.strip() and text.strip() not in shown:
                    self.add_message("assistant", text)
            elif role == "error":
                self.last_error = text
                self.add_message("error", text)
            elif text:
                self.add_message("status", text)
            return True

    # --- registration / approval -------------------------------------------

    def set_features(self, features: Any) -> None:
        feats = [str(f) for f in features] if isinstance(features, (list, tuple)) else []
        with self.lock:
            self.features = feats
            self.available = "chat" in feats

    def set_pending_approval(self, prompt: str, future: Any, loop: Any) -> dict | None:
        """Store a new approval; returns the one it replaced, if any."""
        with self.lock:
            previous = self.pending_approval
            self.pending_approval = {
                "prompt": prompt, "future": future, "loop": loop, "since": time.monotonic(),
            }
            return previous

    def clear_pending_approval(self, future: Any) -> None:
        with self.lock:
            if self.pending_approval and self.pending_approval.get("future") is future:
                self.pending_approval = None

    def snapshot(self) -> dict:
        with self.lock:
            pending = self.pending_approval
            return {
                "messages": [dict(m) for m in self.messages],
                "busy": self.busy,
                "status": self.status,
                "last_error": self.last_error,
                "available": self.available,
                "approval": pending["prompt"] if pending else None,
                "approval_since": pending["since"] if pending else None,
                "backend": dict(self.backend) if self.backend else None,
                "backend_error": self.backend_error,
                "backend_used": dict(self.backend_used) if self.backend_used else None,
                "turn_started_at": self.turn_started_at,
                "revision": self.revision,
                "conversation_id": self.conversation_id,
            }


def approval_preview(prompt: str, max_lines: int = 8) -> tuple[list[str], int]:
    """Lines of an approval prompt worth showing in the panel, and how many were cut."""
    body = prompt.removeprefix(APPROVAL_PREFIX)
    lines = [ln.rstrip() for ln in body.lstrip(" ").strip("\n").splitlines()]
    if not lines:
        return [], 0
    return lines[:max_lines], max(0, len(lines) - max_lines)


chat_state = ChatState()
