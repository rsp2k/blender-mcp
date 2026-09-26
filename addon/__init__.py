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

    scripts/bump_addon_version.py              # CalVer YYYY.MDD.N for today
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
    "version": (2026, 926, 10),  # MUST match addon/_version.py:tuple_version
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


def _on_blend_load_post(_dummy):
    """Persistent handler: fires on every .blend file open.

    Two responsibilities:
    1. If the client is running, re-send registration metadata over the
       live connection so the server-side ClientInfo.blend_file follows
       the file now open. Deliberately NOT a stop/start: load_post fires
       synchronously inside wm.open_mainfile, so when a bus job opens a
       file, tearing the client down here dropped that job's reply and
       the caller timed out after 60 s even though the load took ~2 s.
    2. Otherwise let the connection supervisor decide now rather than on
       its next tick (connects if Stay connected is on and logged in).
    """
    from . import connection, state
    from .worker import is_worker_mode

    if is_worker_mode():
        # A worker's connection is driven by run_worker_loop, never by the
        # supervisor (which would log in with the user's stored token).
        return

    try:
        client = state._client
        if connection.client_alive(client):
            # Timer is persistent now; this only matters for a client
            # started by an older addon build still in memory.
            client.ensure_drain_timer()
            if not client.refresh_registration():
                print("[BlenderMCP] .blend loaded while not yet registered; "
                      "metadata goes out with the pending register")
        else:
            connection.supervisor().check_now()
    except Exception as e:
        print(f"[BlenderMCP] load_post handler failed (non-fatal): {e}")


# Marks the handler persistent so Blender doesn't unregister it after
# the first .blend load (default handlers are dropped on load).
try:
    import bpy as _bpy_for_handler_decoration
    _on_blend_load_post = _bpy_for_handler_decoration.app.handlers.persistent(_on_blend_load_post)
except Exception:
    pass  # non-Blender import context (tests/CI)


def _install_lifecycle_handlers() -> None:
    """Hook _on_blend_load_post into bpy.app.handlers.load_post,
    idempotently (safe to call from register() even if the handler
    was already installed by a prior register())."""
    import bpy

    try:
        handlers = bpy.app.handlers.load_post
        # Idempotent add: only append if not already present.
        if _on_blend_load_post not in handlers:
            handlers.append(_on_blend_load_post)
    except Exception as e:
        print(f"[BlenderMCP] Could not install load_post handler: {e}")


def _uninstall_lifecycle_handlers() -> None:
    """Remove _on_blend_load_post from bpy.app.handlers.load_post,
    tolerating the case where it isn't there."""
    import bpy

    try:
        handlers = bpy.app.handlers.load_post
        while _on_blend_load_post in handlers:
            handlers.remove(_on_blend_load_post)
    except Exception as e:
        print(f"[BlenderMCP] Could not remove load_post handler: {e}")


def _line_buffer_stdout() -> None:
    """Flush Python stdout per line so [BlenderMCP] logs appear when printed.

    Blender runs its embedded Python with ignore_environment set, so
    PYTHONUNBUFFERED has no effect, and when stdout is a pipe (docker
    logs, a service manager, `blender > file`) it's block-buffered: log
    lines surfaced minutes late or not at all, which made connection
    problems look silent. Line buffering only changes flush timing.
    """
    import sys

    for stream in (sys.__stdout__, sys.stdout):
        try:
            if stream is not None and not stream.line_buffering:
                stream.reconfigure(line_buffering=True)
        except (AttributeError, ValueError, OSError):
            pass


def register():
    """Blender entry point — register all classes + properties.

    Imports bpy + UI lazily so the addon package can be imported in
    non-Blender contexts (tests, gate scripts) without dragging in
    bpy.types.Operator / Panel references at module load time.
    """
    import bpy

    _line_buffer_stdout()

    from . import state  # noqa: F401  (re-imported here to make the symbol exist on `addon`)
    from .client.bus_client import ensure_fastmcp, fastmcp_problem_lines
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

    # Stored auth is preserved across addon loads now. Previously we
    # wiped tokens on every register() because early server-side state
    # wasn't restart-safe (JTI mapping in memory). Since then we've
    # added PostgreSQL kv_store persistence, a JWT-exp-claim fallback,
    # and the token middleware that guarantees expires_in — so refresh
    # tokens survive server restarts, and the auth-fatal path already
    # clears creds if they DO turn out to be truly dead. Keeping the
    # tokens means auto-reconnect (below) can actually reconnect.

    # Register load_post handler: when Blender opens a .blend file
    # (either at startup or via File > Open), soft-reconnect so the
    # bus sees the current blend_file in ClientInfo. Handler is marked
    # persistent so Blender doesn't drop it after the first .blend load.
    _install_lifecycle_handlers()

    # Stay connected: a persistent timer that connects ~2s after startup
    # (once the initial scene has loaded) and keeps a client alive while
    # prefs.auto_connect is on and a token is stored.
    from . import connection
    from .worker import is_worker_mode
    if is_worker_mode():
        # Headless worker: worker_main.py connects with the parent's token
        # and an ephemeral uuid. No supervisor, no stored token, no slot
        # lease, no preference writes.
        print(f"[BlenderMCP] Addon v{__version__} registered (background worker mode)")
    else:
        try:
            connection.supervisor().install()
        except Exception as e:
            print(f"[BlenderMCP] Could not start connection supervisor: {e}")
        print(f"[BlenderMCP] Addon v{__version__} registered")
    if not ensure_fastmcp(force=True):
        for line in fastmcp_problem_lines():
            print(f"[BlenderMCP] {line}")


def unregister():
    """Blender entry point — tear down."""
    import bpy

    from . import state
    from .preferences import BlenderMCPPreferences
    from .ui import CLASSES as _CLASSES

    # Remove our load_post handler and the supervisor timer first so
    # nothing fires against torn-down state during unregister.
    _uninstall_lifecycle_handlers()
    if state._supervisor is not None:
        state._supervisor.uninstall()
        state._supervisor = None

    # Kill background workers this Blender spawned; they would otherwise
    # outlive the addon until their parent-pid check noticed.
    try:
        from .executor.handlers.workers import stop_all_workers
        stop_all_workers()
    except Exception as e:
        print(f"[BlenderMCP] Error stopping background workers: {e}")

    # Stop any running client; state owns the singletons since phase 6.
    if state._client is not None:
        try:
            state._client.stop()
        except Exception as e:
            print(f"[BlenderMCP] Error stopping client during unregister: {e}")
        state._client = None
    state._executor = None
    state._identity = None

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
