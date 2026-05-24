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
from addon.ui import CLASSES as _CLASSES


def register():
    # Connection settings
    bpy.types.Scene.blendermcp_server_url = bpy.props.StringProperty(
        name="Server URL",
        description="BlenderMCP server's Streamable HTTP endpoint",
        default="http://localhost:8000/mcp",
    )
    bpy.types.Scene.blendermcp_jwt_token = bpy.props.StringProperty(
        name="JWT Token",
        description="Bearer token obtained via OAuth login. Populated by the Login operator; cleared via the panel's clear button.",
        subtype="PASSWORD",
        default="",
    )
    # Phase 7: real OAuth login. `_username` is persisted (handy for repeated
    # logins) while `_password_tmp` is cleared after a successful login —
    # name suffix is a hint that it shouldn't end up in the .blend file.
    bpy.types.Scene.blendermcp_username = bpy.props.StringProperty(
        name="Username",
        description="Account username for /auth/login",
        default="",
    )
    bpy.types.Scene.blendermcp_password_tmp = bpy.props.StringProperty(
        name="Password",
        description="Password (cleared after a successful login)",
        subtype="PASSWORD",
        default="",
    )
    bpy.types.Scene.blendermcp_server_running = bpy.props.BoolProperty(
        name="Connected", default=False,
    )
    bpy.types.Scene.blendermcp_client_id = bpy.props.StringProperty(
        name="Client ID", default="",
    )

    # Asset integrations
    bpy.types.Scene.blendermcp_use_polyhaven = bpy.props.BoolProperty(
        name="Use Poly Haven",
        description="Enable Poly Haven asset integration", default=False,
    )
    bpy.types.Scene.blendermcp_use_hyper3d = bpy.props.BoolProperty(
        name="Use Hyper3D Rodin",
        description="Enable Hyper3D Rodin generation integration", default=False,
    )
    bpy.types.Scene.blendermcp_hyper3d_mode = bpy.props.EnumProperty(
        name="Rodin Mode",
        description="Choose the platform used to call Rodin APIs",
        items=[
            ("MAIN_SITE", "hyper3d.ai", "hyper3d.ai"),
            ("FAL_AI", "fal.ai", "fal.ai"),
        ],
        default="MAIN_SITE",
    )
    bpy.types.Scene.blendermcp_hyper3d_api_key = bpy.props.StringProperty(
        name="Hyper3D API Key", subtype="PASSWORD",
        description="API Key provided by Hyper3D", default="",
    )
    bpy.types.Scene.blendermcp_use_sketchfab = bpy.props.BoolProperty(
        name="Use Sketchfab",
        description="Enable Sketchfab asset integration", default=False,
    )
    bpy.types.Scene.blendermcp_sketchfab_api_key = bpy.props.StringProperty(
        name="Sketchfab API Key", subtype="PASSWORD",
        description="API Key provided by Sketchfab", default="",
    )

    for cls in _CLASSES:
        bpy.utils.register_class(cls)

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

    for prop in (
        "blendermcp_server_url", "blendermcp_jwt_token",
        "blendermcp_username", "blendermcp_password_tmp",
        "blendermcp_server_running", "blendermcp_client_id",
        "blendermcp_use_polyhaven", "blendermcp_use_hyper3d",
        "blendermcp_hyper3d_mode", "blendermcp_hyper3d_api_key",
        "blendermcp_use_sketchfab", "blendermcp_sketchfab_api_key",
    ):
        if hasattr(bpy.types.Scene, prop):
            delattr(bpy.types.Scene, prop)

    print("[BlenderMCP] Addon unregistered")


if __name__ == "__main__":
    register()
