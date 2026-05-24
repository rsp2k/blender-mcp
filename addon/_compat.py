"""Blender-version compatibility probes.

The addon currently uses `bpy.context.temp_override()` which is 3.2+.
The `bl_info["blender"]` floor will be bumped to (3, 2, 0) in the final
phase of the refactor; until then, callers that want to be defensive
can branch on `HAS_TEMP_OVERRIDE`.
"""

from __future__ import annotations

try:
    import bpy
except ImportError:
    # Importing the addon outside Blender (tests, linting) should not crash.
    bpy = None  # type: ignore[assignment]


HAS_TEMP_OVERRIDE: bool = bool(
    bpy is not None and hasattr(getattr(bpy, "context", None), "temp_override")
)


def bpy_version() -> tuple[int, int, int]:
    """Return Blender's (major, minor, patch). Returns (0, 0, 0) outside Blender."""
    if bpy is None:
        return (0, 0, 0)
    try:
        return tuple(int(x) for x in bpy.app.version[:3])  # type: ignore[return-value]
    except Exception:
        return (0, 0, 0)
