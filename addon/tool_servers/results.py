"""Shape a tool server's CallToolResult into the user_tool_call reply.

Reply: {"ok": bool, "text": str, "truncated": bool}. Text blocks are
joined, images and audio become a short note, and the text is cut to
TEXT_LIMIT characters. No bpy, no fastmcp imports: blocks are read by
attribute so tests can pass plain objects.
"""

from __future__ import annotations

import json
from typing import Any

TEXT_LIMIT = 20_000


def failure(reason: str) -> dict:
    return {"ok": False, "text": str(reason or "the tool call failed"), "truncated": False}


def _b64_size_note(data: Any) -> str:
    if isinstance(data, str) and data:
        kb = max(1, round(len(data) * 3 / 4 / 1024))
        return f", {kb} KB"
    return ""


def _block_text(block: Any) -> str:
    kind = getattr(block, "type", None)
    if kind == "text" or (kind is None and isinstance(getattr(block, "text", None), str)):
        return str(getattr(block, "text", "") or "")
    if kind in ("image", "audio"):
        mime = getattr(block, "mimeType", None) or getattr(block, "mime_type", None) or kind
        return f"[{kind} omitted ({mime}{_b64_size_note(getattr(block, 'data', None))}); only text is passed on]"
    if kind == "resource":
        res = getattr(block, "resource", None)
        text = getattr(res, "text", None)
        if isinstance(text, str):
            return text
        uri = getattr(res, "uri", "") or ""
        return f"[binary resource omitted: {uri}]"
    if kind == "resource_link":
        return f"[resource: {getattr(block, 'uri', '') or getattr(block, 'name', '')}]"
    return f"[{kind or 'unknown'} content omitted]"


def shape_result(result: Any, limit: int = TEXT_LIMIT) -> dict:
    content = getattr(result, "content", None) or []
    parts = [t for t in (_block_text(b) for b in content) if t]
    text = "\n".join(parts)
    if not text:
        structured = getattr(result, "structured_content", None)
        if structured is None:
            structured = getattr(result, "structuredContent", None)
        if structured is not None:
            try:
                text = json.dumps(structured, ensure_ascii=False)
            except (TypeError, ValueError):
                text = repr(structured)
    is_error = bool(getattr(result, "is_error", False) or getattr(result, "isError", False))
    if is_error and not text:
        text = "the tool reported an error"
    truncated = len(text) > limit
    if truncated:
        text = text[:limit]
    return {"ok": not is_error, "text": text, "truncated": truncated}
