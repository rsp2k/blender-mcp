"""Starter prompts for a new chat: parsing prompts/list, picking a group,
and reading the text out of prompts/get. No bpy.

The server marks starters with ``_meta.blender_mcp`` (see
src/blender_mcp/starter_prompts.py):

    {"starter": true, "group": "b_clip" | "general", "order": 1}

A server without starters (or without prompts at all) yields an empty
list, and the panel then shows none.
"""

from __future__ import annotations

from typing import Any

META_KEY = "blender_mcp"
B_CLIP_TEMPLATE = "B_Clip"
# B's rig is "B. Clip" in the template and "Clip_Rig" in the mascot file.
B_CLIP_OBJECTS = ("B. Clip", "Clip_Rig")
GROUP_B_CLIP = "b_clip"
GROUP_GENERAL = "general"


def _field(item: Any, name: str, alias: str | None = None) -> Any:
    """A field of an mcp type or a plain dict. Older mcp releases had no
    ``title``/``meta`` on Prompt; pydantic keeps them as extras then."""
    if isinstance(item, dict):
        value = item.get(name)
        return item.get(alias) if value is None and alias else value
    value = getattr(item, name, None)
    if value is None:
        extra = getattr(item, "model_extra", None) or {}
        value = extra.get(alias or name)
    return value


def _starter_meta(prompt: Any) -> dict | None:
    meta = _field(prompt, "meta", "_meta")
    if not isinstance(meta, dict):
        return None
    ours = meta.get(META_KEY)
    if isinstance(ours, dict) and ours.get("starter") is True:
        return ours
    return None


def _fallback_title(name: str) -> str:
    stem = name.split("starter_", 1)[-1] if "starter_" in name else name
    return stem.replace("_", " ").strip().capitalize() or name


def parse_starters(prompts: Any) -> list[dict]:
    """prompts/list result -> starters, in the server's order.

    Each is {"name", "title", "description", "group", "order"}. Prompts
    without the starter flag, or that need arguments, are skipped."""
    if not isinstance(prompts, (list, tuple)):
        prompts = getattr(prompts, "prompts", None) or []
    found = []
    for prompt in prompts:
        meta = _starter_meta(prompt)
        name = _field(prompt, "name")
        if meta is None or not isinstance(name, str) or not name:
            continue
        if any(_field(a, "required") for a in (_field(prompt, "arguments") or [])):
            continue  # a starter fills the field in one click; it can't ask for input
        order = meta.get("order")
        if isinstance(order, bool) or not isinstance(order, (int, float)):
            order = None
        found.append({
            "name": name,
            "title": str(_field(prompt, "title") or _fallback_title(name)),
            "description": str(_field(prompt, "description") or ""),
            "group": str(meta.get("group") or GROUP_GENERAL),
            "order": order,
        })
    # Stable: unordered starters keep the server's order, after ordered ones.
    found.sort(key=lambda s: (s["order"] is None, s["order"] or 0))
    return found


def wants_b_clip(app_template: str | None, has_b: bool) -> bool:
    return app_template == B_CLIP_TEMPLATE or bool(has_b)


def pick_starters(starters: list[dict] | None, app_template: str | None = None,
                  has_b: bool = False) -> list[dict]:
    """The starters to show: B. Clip's when that template is active or B is
    in the scene, otherwise the general ones. Falls back to general when
    the server has no B. Clip starters."""
    if not starters:
        return []
    by_group: dict[str, list[dict]] = {}
    for s in starters:
        by_group.setdefault(s.get("group") or GROUP_GENERAL, []).append(s)
    if wants_b_clip(app_template, has_b) and by_group.get(GROUP_B_CLIP):
        return by_group[GROUP_B_CLIP]
    return by_group.get(GROUP_GENERAL, [])


def first_user_text(result: Any) -> str | None:
    """The first user message's text in a prompts/get result, or None."""
    for message in _field(result, "messages") or []:
        if _field(message, "role") != "user":
            continue
        content = _field(message, "content")
        if isinstance(content, (list, tuple)):  # tolerate a list of blocks
            content = next((c for c in content if _field(c, "type") == "text"), None)
        text = _field(content, "text") if content is not None else None
        if isinstance(text, str) and text.strip():
            return text.strip()
    return None
