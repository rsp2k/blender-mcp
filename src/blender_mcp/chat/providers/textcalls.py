"""Recover tool calls a model wrote as text instead of structured calls.

qwen through Ollama sometimes answers with ``<tool_call>{"name": ...,
"arguments": {...}}</tool_call>``, a fenced JSON block, or bare JSON in
``content`` and an empty ``tool_calls``. Only names in the offered tool list
count (bare or ``blender_``-prefixed); anything else stays plain text.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Iterable
from typing import Any

from . import ToolCall

_PREFIXES = ("blender_", "functions.", "mcp__blender__")
_TAG_RE = re.compile(r"</?tool_call>|```(?:json)?|```", re.IGNORECASE)
_decoder = json.JSONDecoder()


def normalize_name(name: Any, allowed: Iterable[str]) -> str | None:
    """The offered tool name ``name`` refers to, or None."""
    if not isinstance(name, str):
        return None
    allowed = set(allowed)
    name = name.strip()
    if name in allowed:
        return name
    for p in _PREFIXES:
        if name.startswith(p) and name[len(p):] in allowed:
            return name[len(p):]
    return None


def parse_arguments(raw: Any) -> dict | None:
    """Tool arguments as a dict: accepts a dict, a JSON string, or empty."""
    if raw is None or raw == "":
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            v = json.loads(raw)
        except ValueError:
            return None
        return v if isinstance(v, dict) else None
    return None


def _as_call(obj: Any, allowed: set[str]) -> ToolCall | None:
    if not isinstance(obj, dict):
        return None
    fn = obj.get("function") if isinstance(obj.get("function"), dict) else obj
    name = normalize_name(fn.get("name"), allowed)
    if name is None:
        return None
    args = parse_arguments(fn.get("arguments", fn.get("parameters")))
    if args is None:
        return None
    return ToolCall(id=f"call_{uuid.uuid4().hex[:12]}", name=name, arguments=args)


def recover(text: str, allowed: Iterable[str]) -> tuple[list[ToolCall], str]:
    """(calls found in ``text``, the text with them removed)."""
    allowed = set(allowed)
    if not text or not allowed or ("{" not in text):
        return [], text
    calls: list[ToolCall] = []
    spans: list[tuple[int, int]] = []
    i = 0
    while i < len(text):
        if text[i] not in "{[":
            i += 1
            continue
        try:
            value, end = _decoder.raw_decode(text, i)
        except ValueError:
            i += 1
            continue
        found = [c for c in (_as_call(v, allowed) for v in
                             (value if isinstance(value, list) else [value])) if c]
        if found:
            calls.extend(found)
            spans.append((i, end))
            i = end
        else:
            i += 1
    if not calls:
        return [], text
    rest, last = [], 0
    for a, b in spans:
        rest.append(text[last:a])
        last = b
    rest.append(text[last:])
    remainder = _TAG_RE.sub("", "".join(rest)).strip()
    return calls, remainder
