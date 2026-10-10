"""Are the add-on's bundled packages still on disk?

Blender installs an extension's wheels into one folder shared by every
extension (``extensions/.local/lib/pythonX.Y/site-packages``). If another
program or another Blender process clears that folder while this Blender
runs, modules that are already imported keep working, but anything that
reads a package file later fails. certifi is the first casualty: every new
TLS context opens ``cacert.pem`` from disk, so reconnects and Login fail
with ``[Errno 2] No such file or directory``.

Nothing here tries to repair that. Other packages are gone too, so a
workaround for one file would only move the failure; a Blender restart
reinstalls the wheels. This module only detects the state cheaply (a few
``os.path`` checks, no network) so the UI can say so plainly.

No bpy import, so it is unit-testable and safe to call from any thread.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass

from . import state

RESTART_MESSAGE = (
    "BlenderMCP's packages were removed from disk while Blender was running. "
    "Save your work and restart Blender to restore them."
)
# Short form for one-line spots (connection status, last_error).
SHORT_MESSAGE = "Packages removed from disk; restart Blender"

# Bundled packages whose files we can check once imported. certifi is
# checked separately through certifi.where(), the file every TLS context
# opens. Only modules already in sys.modules are checked: a package that
# was never imported says nothing about the disk.
_IMPORTED_CANARIES = ("fastmcp", "mcp", "httpx", "httpcore")


@dataclass(frozen=True)
class PackageHealth:
    ok: bool
    missing_path: str | None = None
    detail: str = ""


def check() -> PackageHealth:
    """Check that bundled package files still exist. Cheap, no network."""
    try:
        import certifi
    except Exception as e:  # noqa: BLE001 - any import failure means missing
        return PackageHealth(False, None, f"certifi import failed: {e}")
    try:
        bundle = certifi.where()
    except Exception as e:  # noqa: BLE001
        return PackageHealth(False, None, f"certifi.where() failed: {e}")
    if not bundle or not os.path.isfile(bundle):
        return PackageHealth(False, bundle or None, "CA bundle missing")

    for name in _IMPORTED_CANARIES:
        module = sys.modules.get(name)
        path = getattr(module, "__file__", None) if module is not None else None
        if not path or not os.path.isabs(path) or ".zip" in path.lower():
            continue
        if not os.path.exists(path):
            return PackageHealth(False, path, f"{name} files missing")
    return PackageHealth(True)


def update_state() -> PackageHealth:
    """Run check() and record the result on state._package_problem.

    Prints one console line when the problem first appears (with the
    missing path, for diagnosis) and one when it clears, never per call.
    """
    health = check()
    previous = getattr(state, "_package_problem", None)
    if health.ok:
        if previous is not None:
            print("[BlenderMCP] Bundled package files are back on disk")
        state._package_problem = None
    else:
        problem = health.missing_path or health.detail or "unknown"
        if previous is None:
            print(f"[BlenderMCP] Bundled package files missing ({health.detail}): {problem}")
        state._package_problem = problem
    return health


def packages_missing() -> bool:
    """Last recorded result, without touching the disk (safe in draw code)."""
    return getattr(state, "_package_problem", None) is not None


_FILE_ERROR_MARKERS = (
    "ca certificate bundle",
    "cacert.pem",
    "[errno 2] no such file or directory",
)


def is_missing_file_error(exc: BaseException) -> bool:
    """True if exc (or anything it wraps) looks like a vanished package file.

    Walks __cause__, __context__ and exception groups, since transports
    re-raise the original OSError inside their own exception types. A True
    here is only a hint; callers confirm with check() before telling the
    user to restart.
    """
    seen: set[int] = set()
    stack: list[BaseException] = [exc]
    while stack:
        e = stack.pop()
        if e is None or id(e) in seen:
            continue
        seen.add(id(e))
        if isinstance(e, FileNotFoundError):
            return True
        if any(m in str(e).lower() for m in _FILE_ERROR_MARKERS):
            return True
        stack.extend(getattr(e, "exceptions", None) or ())
        stack.extend(x for x in (e.__cause__, e.__context__) if x is not None)
    return False
