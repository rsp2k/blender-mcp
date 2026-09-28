"""Saved chat conversations: one JSON file each in Blender's config folder.

No bpy here; the caller passes the folder (client.chats_dir()), so this is
testable. Keeps the newest KEEP conversations and deletes older ones.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

KEEP = 20
TITLE_CHARS = 40


def new_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


def title_for(messages: list) -> str:
    for m in messages:
        if m.get("role") == "user" and (m.get("text") or "").strip():
            t = " ".join(m["text"].split())
            return t if len(t) <= TITLE_CHARS else t[:TITLE_CHARS - 1] + "…"
    return "New chat"


def _path(folder: Path, cid: str) -> Path:
    safe = "".join(c for c in cid if c.isalnum() or c in "-_")
    return Path(folder) / f"{safe}.json"


def save(folder, cid: str, messages: list) -> None:
    """Write atomically (temp file + rename) so a crash never leaves half a file."""
    if not messages:
        return
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    path = _path(folder, cid)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"id": cid, "title": title_for(messages),
                               "updated": time.time(), "messages": messages}, default=str),
                   encoding="utf-8")
    os.replace(tmp, path)
    prune(folder)


def load(folder, cid: str) -> list:
    try:
        data = json.loads(_path(folder, cid).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    msgs = data.get("messages") if isinstance(data, dict) else None
    return msgs if isinstance(msgs, list) else []


def listing(folder) -> list[dict]:
    """Newest first: [{id, title, updated}]. Unreadable files are skipped."""
    out = []
    for f in Path(folder).glob("*.json") if Path(folder).is_dir() else []:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            out.append({"id": str(data["id"]), "title": str(data.get("title") or "Chat"),
                        "updated": float(data.get("updated") or f.stat().st_mtime)})
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return sorted(out, key=lambda c: c["updated"], reverse=True)


def delete(folder, cid: str) -> None:
    try:
        _path(folder, cid).unlink()
    except OSError:
        pass


def prune(folder, keep: int = KEEP) -> None:
    for c in listing(folder)[keep:]:
        delete(folder, c["id"])
