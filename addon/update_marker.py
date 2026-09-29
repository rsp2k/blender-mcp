"""Remember across the reload that an update was just installed.

Installing an update unloads this add-on, so nothing of ours runs while
Blender installs it. A small marker file written just before the install
lets the freshly loaded version say "Updated to X" in the status bar.
Only os/time here, so it can be tested without bpy.
"""

from __future__ import annotations

import json
import os
import time

MARKER = "updating_to.json"
SHOW_FOR_S = 20.0
MAX_AGE_S = 600.0


def write(folder: str, version: str, previous: str) -> None:
    try:
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, MARKER), "w", encoding="utf-8") as f:
            json.dump({"version": version, "previous": previous, "at": time.time()}, f)
    except OSError as e:
        print(f"[BlenderMCP] Couldn't write the update marker: {e}")


def consume(folder: str, running_version: str) -> dict | None:
    """The marker, if it names the version now running and is recent; it's
    removed either way so a failed install doesn't announce later."""
    path = os.path.join(folder, MARKER)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    try:
        os.remove(path)
    except OSError:
        pass
    if not isinstance(data, dict) or time.time() - float(data.get("at") or 0) > MAX_AGE_S:
        return None
    if str(data.get("version") or "") not in ("", running_version):
        return None
    return {"version": running_version, "previous": data.get("previous") or "",
            "until": time.monotonic() + SHOW_FOR_S}


def folder() -> str:
    """Per-user extension data folder (survives the package being replaced)."""
    import bpy
    from .preferences import ADDON_PACKAGE_NAME
    try:
        return bpy.utils.extension_path_user(ADDON_PACKAGE_NAME, path="", create=True)
    except (ValueError, AttributeError, TypeError):
        return os.path.join(bpy.utils.user_resource('CONFIG'), "blender_mcp")
