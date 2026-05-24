"""BlenderMCP addon package.

This package is being grown alongside the existing monolithic
`addon.py` during the modularization refactor. While work is in
progress, the top-level `addon.py` is still authoritative — Blender
loads that file directly. Submodules added here are pure-Python (or
gated to be importable outside Blender) so they can be exercised by
the pre-Blender verification gates.

The final phase will reduce `addon.py` to a thin shim that imports
`bl_info`, `register`, `unregister` from here.
"""

from __future__ import annotations

from ._version import __version__, tuple_version

__all__ = ["__version__", "tuple_version"]
