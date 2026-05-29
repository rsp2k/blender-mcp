"""BlenderMCP addon — connects Blender to a BlenderMCP OAuth bus server.

Phase 9 (final phase of the modularization refactor): this module is now
the authoritative entry point. The top-level `addon.py` survives as a
~30-line shim so Blender's "Install Add-on" -> single-file path still
works; users who prefer the directory layout install this package
directly (zip the addon/ folder).

The bl_info dict below MUST be a literal — Blender's addon enumeration
uses ``ast.literal_eval`` on the right-hand side to populate the
Preferences > Add-ons list WITHOUT importing the module (safety: don't
run arbitrary code to list addons). ``literal_eval`` rejects imported
names like ``tuple_version`` with ValueError, so the version tuple is
duplicated across ``addon.py``, ``addon/__init__.py``, and
``addon/_version.py``. Verified by experiment (5-line ast.literal_eval
probe — see commit history). No DRY workaround exists short of code
generation; the bump script below is the chosen mitigation.

(Note: any line starting with "bl_info" at column zero confuses
Blender's _fake_module speedy line-extractor — it will think the line
is the start of the manifest and try to ast.parse subsequent prose
lines as Python. Keep that token off the left margin in this docstring.)

To bump the version across all three files atomically, use::

    scripts/bump_addon_version.py patch    # 1.5.6 → 1.5.7
    scripts/bump_addon_version.py minor    # 1.5.6 → 1.6.0
    scripts/bump_addon_version.py 1.7.2    # exact

The script verifies the three files agree before writing, so drift
gets caught immediately. To audit by hand::

    grep -hE '"version": \\(' addon.py addon/__init__.py
    grep -E '__version__' addon/_version.py

**Why register/unregister do their bpy imports lazily:** the addon
package is imported in non-Blender contexts (tests, Gate E, Gate G
scripts) where `import bpy` and the UI/operators' top-level bpy
references would fail. Keeping the module surface cheap to import
also means `from addon.auth import login` and `from addon.client import
BlenderMCPClient` don't drag the panel + operators along.
"""

from __future__ import annotations

from ._version import __version__, tuple_version

bl_info = {
    "name": "Blender MCP",
    "author": "BlenderMCP",
    "version": (1, 5, 7),  # MUST match addon/_version.py:tuple_version
    "blender": (3, 2, 0),  # uses bpy.context.temp_override (3.2+)
    "location": "View3D > Sidebar > BlenderMCP",
    "description": (
        "Connect Blender to the BlenderMCP server as a bus client. "
        "Requires fastmcp: <blender_python> -m pip install fastmcp"
    ),
    "category": "Interface",
}

# Transient per-session Scene properties — these never persist to disk
# and stay on Scene (vs migrating to AddonPreferences) because they're
# runtime state, not user config:
#   blendermcp_server_running — is the bus client connected right now?
#   blendermcp_client_id      — display the sticky UUID in the panel
_TRANSIENT_SCENE_PROPS = (
    "blendermcp_server_running",
    "blendermcp_client_id",
)

# Legacy Scene props from pre-Phase-8 installs. Removed in register()
# after migration so they stop riding along in saved .blend files.
# blendermcp_username + blendermcp_password_tmp are listed here so any
# old .blend files that still carry them get them stripped on load.
_LEGACY_SCENE_PROPS = (
    "blendermcp_server_url",
    "blendermcp_jwt_token",
    "blendermcp_username",
    "blendermcp_password_tmp",
    "blendermcp_use_polyhaven",
    "blendermcp_use_hyper3d",
    "blendermcp_hyper3d_mode",
    "blendermcp_hyper3d_api_key",
    "blendermcp_use_sketchfab",
    "blendermcp_sketchfab_api_key",
)


def register():
    """Blender entry point — register all classes + properties.

    Imports bpy + UI lazily so the addon package can be imported in
    non-Blender contexts (tests, gate scripts) without dragging in
    bpy.types.Operator / Panel references at module load time.
    """
    import bpy

    from . import state  # noqa: F401  (re-imported here to make the symbol exist on `addon`)
    from .client.bus_client import FASTMCP_AVAILABLE
    from .preferences import BlenderMCPPreferences, migrate_from_scene
    from .ui import CLASSES as _CLASSES

    # AddonPreferences is the home for all user config since Phase 8.
    bpy.utils.register_class(BlenderMCPPreferences)

    # Transient Scene props (per-session state that never leaves Blender).
    bpy.types.Scene.blendermcp_server_running = bpy.props.BoolProperty(
        name="Connected", default=False,
    )
    bpy.types.Scene.blendermcp_client_id = bpy.props.StringProperty(
        name="Client ID", default="",
    )

    # Register the UI classes (panel + operators).
    for cls in _CLASSES:
        bpy.utils.register_class(cls)

    # One-shot migration from legacy Scene properties (pre-Phase-8 installs).
    # Runs against bpy.context.scene if available; safe to no-op otherwise.
    try:
        scene = getattr(bpy.context, "scene", None)
        if scene is not None:
            migrated = migrate_from_scene(scene)
            if migrated:
                print(f"[BlenderMCP] Migrated {len(migrated)} setting(s) from Scene to AddonPreferences: "
                      f"{', '.join(migrated)}")
            # Whether or not values were migrated, remove the legacy props
            # so they don't ride along in .blend files saved after this run.
            for prop in _LEGACY_SCENE_PROPS:
                if hasattr(bpy.types.Scene, prop):
                    delattr(bpy.types.Scene, prop)
    except Exception as e:
        print(f"[BlenderMCP] Migration warning (non-fatal): {e}")

    # Clear stored auth on every addon register. Trade-off: user clicks
    # Login once per Blender session (browser consent + one Allow click)
    # in exchange for never seeing the stale-JWT-after-restart confusion
    # that the in-server JTI mapping can't survive a server restart of.
    # The addon's reconnect/refresh logic can usually recover (since
    # 1.5.4), but "Connect button does nothing visible" is a worse UX
    # than "Click Login first." Predictable beats clever.
    #
    # Edge case: this also wipes auth when the user disables + re-enables
    # the addon mid-session. Rare; acceptable trade.
    try:
        from .preferences import get_prefs
        prefs_now = get_prefs()
        if prefs_now.jwt_token:
            print("[BlenderMCP] Clearing stored auth on addon load — click Login to re-authenticate")
            prefs_now.jwt_token = ""
            prefs_now.refresh_token = ""
            prefs_now.jwt_expires_at = ""
            prefs_now.oauth_client_id = ""
    except Exception as e:
        # Non-fatal: register() shouldn't fail because we couldn't clear
        # prefs. Worst case, the user sees stale auth and falls back to
        # the (now-working) Re-login flow.
        print(f"[BlenderMCP] Auth clear at register failed (non-fatal): {e}")

    print(f"[BlenderMCP] Addon v{__version__} registered")
    if not FASTMCP_AVAILABLE:
        print("[BlenderMCP] WARNING: fastmcp not installed.")
        print("[BlenderMCP]   Install with: <blender_python> -m pip install fastmcp")


def unregister():
    """Blender entry point — tear down."""
    import bpy

    from . import state
    from .preferences import BlenderMCPPreferences
    from .ui import CLASSES as _CLASSES

    # Stop any running client; state owns the singletons since phase 6.
    if state._client is not None:
        try:
            state._client.stop()
        except Exception as e:
            print(f"[BlenderMCP] Error stopping client during unregister: {e}")
        state._client = None
    state._executor = None

    for cls in reversed(_CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            pass

    # Remove transient Scene props.
    for prop in _TRANSIENT_SCENE_PROPS:
        if hasattr(bpy.types.Scene, prop):
            delattr(bpy.types.Scene, prop)

    # Unregister AddonPreferences last (panel/operators reference it).
    try:
        bpy.utils.unregister_class(BlenderMCPPreferences)
    except Exception:
        pass

    print("[BlenderMCP] Addon unregistered")


__all__ = ["bl_info", "register", "unregister", "__version__", "tuple_version"]
