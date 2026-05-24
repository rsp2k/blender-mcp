# Code created by Siddharth Ahuja: www.github.com/ahujasid © 2025
# Transformed for FastMCP OAuth Message Bus Integration

import bpy
import bmesh
import mathutils
import json
import threading
import time
import requests
import tempfile
import traceback
import os
import shutil
import zipfile
import uuid
import hashlib
import base64
import logging
from datetime import datetime, timedelta
from bpy.props import StringProperty, IntProperty, BoolProperty, EnumProperty
import io
from contextlib import redirect_stdout, suppress
from urllib.parse import urlparse, parse_qs
from queue import PriorityQueue, Queue
import asyncio
from typing import Optional, Dict, Any, List
import weakref

# FastMCP imports - install in Blender's Python:
#   <blender_python> -m pip install fastmcp
try:
    from fastmcp import Client as FastMCPClient
    from fastmcp.client.transports import StreamableHttpTransport
    FASTMCP_AVAILABLE = True
except ImportError:
    FASTMCP_AVAILABLE = False
    print("[BlenderMCP] fastmcp not installed - run: <blender_python> -m pip install fastmcp")

# heapq for priority queue
import heapq

# Cryptography imports for secure token storage
try:
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    CRYPTOGRAPHY_AVAILABLE = True
except ImportError:
    CRYPTOGRAPHY_AVAILABLE = False
    print("Cryptography library not available - using fallback token encryption")

import platform

bl_info = {
    "name": "Blender MCP",
    "author": "BlenderMCP",
    "version": (2, 0),
    "blender": (3, 0, 0),
    "location": "View3D > Sidebar > BlenderMCP",
    "description": (
        "Connect Blender to the BlenderMCP server as a bus client. "
        "Requires fastmcp: <blender_python> -m pip install fastmcp"
    ),
    "category": "Interface",
}

# Constants + StickyUUIDManager moved to the addon/ package in phase 2 of
# the modularization refactor. Re-imported here so the rest of addon.py
# still sees the same names; will go away when addon.py becomes a thin
# shim in phase 9.
from addon.constants import RODIN_FREE_TRIAL_KEY, REQ_HEADERS  # noqa: F401  re-exported
from addon.identity import StickyUUIDManager  # noqa: F401  re-exported


# OAuthTokenManager deleted in phase 2 of the modularization refactor —
# it was never instantiated. The real OAuth login flow lands in phase 7
# (addon/auth/login.py + addon/auth/storage.py + BLENDERMCP_OT_Login
# operator + AddonPreferences-backed token storage).


# ============================================================================
# BlenderMCP Client moved to addon/client/ in phase 3 of the modularization
# refactor. The class is split across 4 sibling modules:
#   bus_client.py    — lifecycle + asyncio loop (the BlenderMCPClient class)
#   message_pump.py  — notifications/message filter + enqueue
#   drainer.py       — Blender main-thread timer; pops jobs, runs scripts
#   job_reporter.py  — sends blender_job_update back to the bus
# ============================================================================

from addon.client import BlenderMCPClient  # noqa: F401  re-exported


# (Singletons _client and _executor moved to addon/state.py in phase 6
# so the extracted UI operators in addon/ui/operators.py can mutate the
# same instances the panel reads.)


# BlenderCommandExecutor moved to addon/executor/ in phase 4 of the
# modularization refactor. Now split across 9 modules (8 handler domains +
# shared helpers + dispatch facade) using mixin composition. See:
#   addon/executor/__init__.py    — BlenderCommandExecutor facade + dispatch
#   addon/executor/_shared.py     — _get_aabb, _clean_imported_glb, _get_data_item_info
#   addon/executor/handlers/*.py  — per-domain handler mixins
from addon.executor import BlenderCommandExecutor  # noqa: F401  re-exported

# UI (panel + 3 operators) extracted in phase 6 to addon/ui/.
# `state` holds the module-level _client / _executor singletons; both
# the operators in addon/ui/ and the unregister() below need to mutate
# them via the shared module reference (not via `from ... import _x`,
# which would shadow on rebinding).
from addon import state
from addon.preferences import BlenderMCPPreferences, migrate_from_scene
from addon.ui import CLASSES as _CLASSES

# Transient per-session Scene properties — these never persist to disk
# and stay on Scene (vs migrating to AddonPreferences) because they're
# runtime state, not user config:
#   blendermcp_server_running — is the bus client connected right now?
#   blendermcp_client_id      — display the sticky UUID in the panel
#   blendermcp_password_tmp   — the in-flight password before Login runs;
#                              cleared by the operator on success
_TRANSIENT_SCENE_PROPS = (
    "blendermcp_server_running",
    "blendermcp_client_id",
    "blendermcp_password_tmp",
)


def register():
    # AddonPreferences is the home for all user config since Phase 8.
    bpy.utils.register_class(BlenderMCPPreferences)

    # Transient Scene props (per-session state that never leaves Blender).
    bpy.types.Scene.blendermcp_server_running = bpy.props.BoolProperty(
        name="Connected", default=False,
    )
    bpy.types.Scene.blendermcp_client_id = bpy.props.StringProperty(
        name="Client ID", default="",
    )
    bpy.types.Scene.blendermcp_password_tmp = bpy.props.StringProperty(
        name="Password",
        description="In-flight password; cleared on successful login. Never persisted.",
        subtype="PASSWORD",
        default="",
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
            for prop in (
                "blendermcp_server_url", "blendermcp_jwt_token",
                "blendermcp_username",
                "blendermcp_use_polyhaven", "blendermcp_use_hyper3d",
                "blendermcp_hyper3d_mode", "blendermcp_hyper3d_api_key",
                "blendermcp_use_sketchfab", "blendermcp_sketchfab_api_key",
            ):
                if hasattr(bpy.types.Scene, prop):
                    delattr(bpy.types.Scene, prop)
    except Exception as e:
        print(f"[BlenderMCP] Migration warning (non-fatal): {e}")

    print("[BlenderMCP] Addon registered")
    if not FASTMCP_AVAILABLE:
        print("[BlenderMCP] WARNING: fastmcp not installed.")
        print("[BlenderMCP]   Install with: <blender_python> -m pip install fastmcp")


def unregister():
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


if __name__ == "__main__":
    register()
