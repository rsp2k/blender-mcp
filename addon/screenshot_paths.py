"""Default output paths for viewport screenshots.

Kept free of bpy so it can be unit-tested outside Blender.
"""

from __future__ import annotations

import datetime
import os
import tempfile
import uuid

SUBDIR = "blender_mcp_screenshots"

# File extensions for the formats get_viewport_screenshot accepts. Anything
# unmapped uses the format name lower-cased (png, bmp, webp, avif).
_EXTENSIONS = {
    "jpg": "jpg",
    "jpeg": "jpg",
    "tif": "tif",
    "tiff": "tif",
    "tga": "tga",
    "targa": "tga",
    "exr": "exr",
    "open_exr": "exr",
}


def extension_for(fmt: str) -> str:
    fmt = (fmt or "png").lower()
    return _EXTENSIONS.get(fmt, fmt)


def default_screenshot_path(fmt: str = "png", base_dir: str | None = None,
                            now: datetime.datetime | None = None) -> str:
    """A unique path under ``base_dir``/blender_mcp_screenshots, created if needed.

    ``base_dir`` is normally Blender's session temp dir (bpy.app.tempdir,
    removed when Blender exits); it falls back to the OS temp dir.
    """
    base = base_dir or tempfile.gettempdir()
    directory = os.path.join(base, SUBDIR)
    os.makedirs(directory, exist_ok=True)
    stamp = (now or datetime.datetime.now(datetime.timezone.utc)).strftime("%Y%m%d-%H%M%S")
    name = f"viewport-{stamp}-{uuid.uuid4().hex[:8]}.{extension_for(fmt)}"
    return os.path.join(directory, name)
