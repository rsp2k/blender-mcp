"""``look_at_viewport``: a screenshot of the calling Blender, described by a vision model.

The screenshot goes through object storage (``store=True``) and is read back
over the internal endpoint, so the image never travels through MCP. Offered
only when storage is configured and the user has a vision-capable backend:
the gateway's CHAT_VISION_MODEL, or their own Claude backend.
"""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

from mcp.types import ImageContent, SamplingMessage, TextContent

from ..object_storage import StorageError, check_key, get_store
from .config import ChatConfig
from .providers import Backend, ProviderError

SCREENSHOT_TOOL = "blender_get_viewport_screenshot"
MAX_SIZE = 1024
MAX_IMAGE_BYTES = 8 * 1024 * 1024
FETCH_TRIES = 10
FETCH_DELAY_S = 0.5
VISION_MAX_TOKENS = 700
VISION_SYSTEM = (
    "You describe screenshots of a Blender 3D viewport for another assistant that "
    "cannot see them. Answer the question directly and concretely: objects, their "
    "rough positions and sizes relative to each other, colours, annotations (drawn "
    "boxes and labels), and anything that looks wrong. Be brief."
)
_MIME = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp"}


def vision_backend(cfg: ChatConfig, user_sub: str | None, turn_backend: Backend | None) -> Backend | None:
    if cfg.gateway_allowed(user_sub):
        return Backend("gateway", cfg.vision_model, cfg.gpu_base_url, cfg.gpu_api_key)
    if turn_backend is not None and turn_backend.provider == "anthropic":
        return turn_backend
    return None


def available(cfg: ChatConfig, user_sub: str | None, turn_backend: Backend | None) -> bool:
    return get_store() is not None and vision_backend(cfg, user_sub, turn_backend) is not None


def find_object_key(value: Any, depth: int = 0) -> str | None:
    """The ``stored.object_key`` somewhere in a dispatch reply (inner results
    are often JSON strings)."""
    if depth > 4:
        return None
    if isinstance(value, str):
        s = value.strip()
        if s[:1] in "{[":
            try:
                return find_object_key(json.loads(s), depth + 1)
            except ValueError:
                return None
        return None
    if isinstance(value, dict):
        stored = value.get("stored")
        if isinstance(stored, dict) and isinstance(stored.get("object_key"), str):
            return stored["object_key"]
        for v in value.values():
            key = find_object_key(v, depth + 1)
            if key:
                return key
    if isinstance(value, list):
        for v in value:
            key = find_object_key(v, depth + 1)
            if key:
                return key
    return None


async def _fetch(store, key: str) -> bytes:
    for attempt in range(FETCH_TRIES):
        try:
            return await store.get_bytes(key, MAX_IMAGE_BYTES)
        except StorageError as e:
            # The add-on may still be uploading.
            if e.code != "object_not_found" or attempt == FETCH_TRIES - 1:
                raise
            await asyncio.sleep(FETCH_DELAY_S)
    raise StorageError("object_not_found", "screenshot never arrived in storage")


def rejected_deselect(reply: str) -> bool:
    """An add-on from before ``deselect`` existed refusing the parameter."""
    text = str(reply or "")
    return "deselect" in text or "unexpected keyword" in text


async def capture(executor) -> tuple[bool, str]:
    """Screenshot with the selection cleared, so a selected object's outline
    isn't read as its colour. Add-ons without ``deselect`` normally drop the
    unknown key (registry.filter_kwargs); one that refuses it gets a plain
    capture instead."""
    base = {"store": True, "max_size": MAX_SIZE}
    ok, reply = await executor.call(SCREENSHOT_TOOL, {}, extra={**base, "deselect": True})
    if not ok and rejected_deselect(reply):
        ok, reply = await executor.call(SCREENSHOT_TOOL, {}, extra=base)
    return ok, reply


async def look(executor, handler, backend: Backend, question: str) -> tuple[bool, str]:
    """(ok, description) for the calling Blender's viewport."""
    store = get_store()
    if store is None:
        return False, "Viewport screenshots are not available on this server."
    ok, reply = await capture(executor)
    if not ok:
        return False, reply
    key = find_object_key(reply)
    if not key:
        # Say what came back: "not stored" alone hid a dropped session once.
        return False, ("The screenshot came back without a stored copy, so it can't be "
                       f"looked at. Reply: {str(reply)[:300]}")
    try:
        if executor.bus_id:
            check_key(executor.bus_id, key)
        data = await _fetch(store, key)
    except (ValueError, PermissionError) as e:
        return False, f"Screenshot reference rejected: {e}"
    except StorageError as e:
        return False, f"Could not read the screenshot: {e.detail}"
    mime = _MIME.get(key.rsplit(".", 1)[-1].lower(), "image/png")
    msg = SamplingMessage(role="user", content=[
        TextContent(type="text", text=question or "Describe the viewport."),
        ImageContent(type="image", data=base64.b64encode(data).decode(), mimeType=mime),
    ])
    try:
        c = await handler.complete(backend, VISION_SYSTEM, [msg], None,
                                   max_tokens=VISION_MAX_TOKENS)
    except ProviderError as e:
        return False, f"The vision model failed: {e}"
    return True, c.text or "(the vision model gave no description)"
