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
