"""Clear the selection for a moment and put it back exactly.

Used by get_viewport_screenshot(deselect=True): a vision model reading a
screenshot took the orange selection outline for the object's colour. No bpy
import, so it can be tested with stand-in objects; ``view_layer`` is anything
with ``objects`` (iterable of objects with select_get/select_set) and
``objects.active``.
"""

from __future__ import annotations

from contextlib import contextmanager


def capture(view_layer) -> dict:
    objs = view_layer.objects
    return {"selected": [o for o in objs if o.select_get()], "active": objs.active}


def clear(view_layer) -> int:
    """Deselect everything and drop the active object. Returns how many were selected."""
    n = 0
    for o in view_layer.objects:
        if o.select_get():
            n += 1
            try:
                o.select_set(False)
            except (RuntimeError, ReferenceError):
                pass
    view_layer.objects.active = None
    return n


def restore(view_layer, saved: dict) -> None:
    """Back to exactly ``saved``: same selected set, same active object. Objects
    deleted in between are skipped."""
    keep = set(map(id, saved["selected"]))
    for o in view_layer.objects:
        want = id(o) in keep
        try:
            if bool(o.select_get()) != want:
                o.select_set(want)
        except (RuntimeError, ReferenceError):
            pass
    try:
        view_layer.objects.active = saved["active"]
    except (RuntimeError, ReferenceError, TypeError, ValueError):
        view_layer.objects.active = None


@contextmanager
def deselected(view_layer, enabled: bool = True):
    """Nothing selected or active inside the block; restored on the way out,
    even when the block raises."""
    if not enabled:
        yield None
        return
    saved = capture(view_layer)
    clear(view_layer)
    try:
        yield saved
    finally:
        restore(view_layer, saved)
