"""Change the 3D view for a moment and put it back exactly.

Used by get_viewport_screenshot: looking at the scene from another angle
(or with another shading) to answer a question must not leave the user's
view moved. No bpy import, so it can be tested with stand-in objects; a
``space`` is anything with ``region_3d`` (view_location, view_rotation,
view_distance, view_perspective) and ``shading`` (type).
"""

from __future__ import annotations

from contextlib import contextmanager

_SHADING_ATTRS = ("type", "color_type", "light")


def _copy(value):
    return value.copy() if hasattr(value, "copy") else value


def capture(spaces) -> list[tuple]:
    saved = []
    for space in spaces:
        rv3d = space.region_3d
        view = {
            "view_location": _copy(rv3d.view_location),
            "view_rotation": _copy(rv3d.view_rotation),
            "view_distance": rv3d.view_distance,
            "view_perspective": rv3d.view_perspective,
        }
        shading = {a: getattr(space.shading, a) for a in _SHADING_ATTRS
                   if hasattr(space.shading, a)}
        saved.append((space, view, shading))
    return saved


def restore(saved) -> None:
    """Back to exactly what ``capture`` saw. A space that went away in between
    is skipped, and so is a value Blender refuses (for example a light mode
    that isn't valid for the restored shading type)."""
    for space, view, shading in saved:
        try:
            rv3d = space.region_3d
            shade = space.shading
        except (ReferenceError, AttributeError):
            continue
        # Shading type first: color_type and light choices depend on it.
        for attr in _SHADING_ATTRS:
            if attr in shading:
                try:
                    setattr(shade, attr, shading[attr])
                except (TypeError, ValueError, AttributeError):
                    pass
        # Perspective before the matrix parts: leaving CAMERA view resets them.
        for attr in ("view_perspective", "view_rotation", "view_location", "view_distance"):
            try:
                setattr(rv3d, attr, view[attr])
            except (TypeError, ValueError, AttributeError):
                pass


@contextmanager
def preserved(spaces, enabled: bool = True):
    """The views and shading of ``spaces`` are restored on the way out, even
    when the block raises."""
    if not enabled:
        yield None
        return
    saved = capture(spaces)
    try:
        yield saved
    finally:
        restore(saved)
