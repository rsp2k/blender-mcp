"""Custom icons (bpy.utils.previews), loaded once at register."""

from __future__ import annotations

import os

_collection = None
ICON_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "icons")


def register() -> None:
    global _collection
    import bpy.utils.previews
    _collection = bpy.utils.previews.new()
    for key, filename in (("binder_clip", "binder-clip.png"), ("clip_mascot", "clip-mascot.png")):
        path = os.path.join(ICON_DIR, filename)
        if os.path.exists(path):
            _collection.load(key, path, 'IMAGE')
    # Clip's thinking loop, one icon per frame (think/00.png ...).
    think = os.path.join(ICON_DIR, "think")
    if os.path.isdir(think):
        for name in sorted(os.listdir(think)):
            if name.endswith(".png"):
                prev = _collection.load(f"think_{name[:-4]}", os.path.join(think, name), 'IMAGE')
                _ = prev.icon_id  # asking for the id starts loading it now, not mid-turn


def unregister() -> None:
    global _collection
    if _collection is not None:
        import bpy.utils.previews
        bpy.utils.previews.remove(_collection)
        _collection = None


def icon_id(name: str) -> int:
    """icon_value for layout calls; 0 (no icon) when it didn't load."""
    if _collection is None or name not in _collection:
        return 0
    return _collection[name].icon_id


THINK_FPS = 8


def think_frame(now: float) -> int:
    """icon_value of the thinking loop's frame for time ``now`` (0 if absent)."""
    if _collection is None:
        return 0
    frames = sorted(k for k in _collection if k.startswith("think_"))
    if not frames:
        return 0
    # 0 until that frame has loaded; the caller falls back to a plain icon.
    return _collection[frames[int(now * THINK_FPS) % len(frames)]].icon_id


def _tick_thinking():
    """Redraw 3D views while a chat turn runs, so the thinking loop plays."""
    import bpy

    from ..chat.state import chat_state
    if not chat_state.busy:
        return None
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()
    return 1.0 / THINK_FPS


def ensure_thinking_timer() -> None:
    import bpy
    if not bpy.app.timers.is_registered(_tick_thinking):
        bpy.app.timers.register(_tick_thinking, first_interval=1.0 / THINK_FPS)
