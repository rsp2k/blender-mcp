"""Whether a failed import of the bundled libraries is just the first run after
installing. No bpy, so it's testable.

Right after installing the extension, Blender has already imported its own
older typing_extensions, so the bundled fastmcp fails on it; a restart puts
the extension's site-packages first and it loads.
"""

from __future__ import annotations


def needs_restart(package: str | None, import_error: str | None) -> bool:
    return (package or "").startswith("bl_ext.") and "typing_extensions" in (import_error or "")


RESTART_LINES = [
    "One more step to finish installing:",
    "restart Blender (File > Quit, then open it again).",
]
