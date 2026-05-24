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


# Module-level singleton for the BlenderCommandExecutor (helpers reachable
# from scripts via globals)
_executor = None
_client = None


# BlenderCommandExecutor moved to addon/executor/ in phase 4 of the
# modularization refactor. Now split across 9 modules (8 handler domains +
# shared helpers + dispatch facade) using mixin composition. See:
#   addon/executor/__init__.py    — BlenderCommandExecutor facade + dispatch
#   addon/executor/_shared.py     — _get_aabb, _clean_imported_glb, _get_data_item_info
#   addon/executor/handlers/*.py  — per-domain handler mixins
from addon.executor import BlenderCommandExecutor  # noqa: F401  re-exported


# ============================================================================
# Blender UI Panel
# ============================================================================
class BLENDERMCP_PT_Panel(bpy.types.Panel):
    bl_label = "Blender MCP"
    bl_idname = "BLENDERMCP_PT_Panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'BlenderMCP'

    def draw(self, context):
        layout = self.layout
        scene = context.scene

        # --- Connection section ---
        col = layout.column(align=True)
        col.label(text="MCP Server Connection:", icon='NETWORK_DRIVE')

        # Server URL + JWT input
        col.prop(scene, "blendermcp_server_url", text="URL")
        col.prop(scene, "blendermcp_jwt_token", text="JWT")

        if getattr(scene, "blendermcp_client_id", ""):
            col.label(text=f"Client: {scene.blendermcp_client_id[:24]}")

        # Buttons
        row = col.row(align=True)
        if not scene.blendermcp_server_running:
            row.operator("blendermcp.start_server", text="Connect", icon='PLAY')
        else:
            row.operator("blendermcp.stop_server", text="Disconnect", icon='PAUSE')

        # Status indicator
        client = _client
        if client:
            if client.connected:
                col.label(text="Status: Connected", icon='CHECKMARK')
                with client.queue_lock:
                    qlen = len(client.job_queue)
                col.label(text=f"Queue: {qlen} pending  Active: {len(client.active_jobs)}")
            elif client.running:
                col.label(text="Status: Connecting...", icon='TIME')
            if client.last_error:
                col.label(text=f"Last error: {client.last_error[:40]}", icon='ERROR')

        col.separator()
        if not FASTMCP_AVAILABLE:
            col.label(text="fastmcp not installed", icon='ERROR')
            col.label(text="Install in Blender's Python:")
            col.label(text="python -m pip install fastmcp")
            col.separator()

        # --- Asset integrations ---
        col.label(text="Asset Integrations:", icon='ASSET_MANAGER')
        col.prop(scene, "blendermcp_use_polyhaven", text="Poly Haven Assets")
        col.prop(scene, "blendermcp_use_hyper3d", text="Hyper3D Rodin Generation")
        if scene.blendermcp_use_hyper3d:
            sub = col.column(align=True)
            sub.prop(scene, "blendermcp_hyper3d_mode", text="Mode")
            sub.prop(scene, "blendermcp_hyper3d_api_key", text="API Key")
            sub.operator("blendermcp.set_hyper3d_free_trial_api_key", text="Free Trial Key")

        col.prop(scene, "blendermcp_use_sketchfab", text="Sketchfab Models")
        if scene.blendermcp_use_sketchfab:
            sub = col.column(align=True)
            sub.prop(scene, "blendermcp_sketchfab_api_key", text="API Key")


# Operator to set Hyper3D API Key
class BLENDERMCP_OT_SetFreeTrialHyper3DAPIKey(bpy.types.Operator):
    bl_idname = "blendermcp.set_hyper3d_free_trial_api_key"
    bl_label = "Set Free Trial API Key"

    def execute(self, context):
        context.scene.blendermcp_hyper3d_api_key = RODIN_FREE_TRIAL_KEY
        context.scene.blendermcp_hyper3d_mode = 'MAIN_SITE'
        self.report({'INFO'}, "API Key set successfully!")
        return {'FINISHED'}


# Connect to MCP server
class BLENDERMCP_OT_StartServer(bpy.types.Operator):
    bl_idname = "blendermcp.start_server"
    bl_label = "Connect to BlenderMCP Server"
    bl_description = "Connect to the BlenderMCP server's _message_bus channel"

    def execute(self, context):
        global _client, _executor
        scene = context.scene

        if not FASTMCP_AVAILABLE:
            self.report({'ERROR'},
                        "fastmcp not installed. Run: <blender_python> -m pip install fastmcp")
            return {'CANCELLED'}

        if not scene.blendermcp_jwt_token:
            self.report({'ERROR'}, "JWT token required (paste from OAuth login)")
            return {'CANCELLED'}

        try:
            if _executor is None:
                _executor = BlenderCommandExecutor()

            if _client is None:
                uuid_mgr = StickyUUIDManager()
                _client = BlenderMCPClient(
                    server_url=scene.blendermcp_server_url,
                    jwt_token=scene.blendermcp_jwt_token,
                    client_uuid=uuid_mgr.get_client_id(),
                    executor=_executor,  # injected so drainer can expose to scripts
                )
                scene.blendermcp_client_id = _client.client_uuid

            _client.start()
            scene.blendermcp_server_running = True
            self.report({'INFO'}, f"Connecting as {scene.blendermcp_client_id}")
        except Exception as e:
            self.report({'ERROR'}, f"Failed to start client: {e}")
            traceback.print_exc()
            return {'CANCELLED'}

        return {'FINISHED'}


# Disconnect from MCP server
class BLENDERMCP_OT_StopServer(bpy.types.Operator):
    bl_idname = "blendermcp.stop_server"
    bl_label = "Disconnect from BlenderMCP Server"
    bl_description = "Disconnect from the MCP message bus"

    def execute(self, context):
        global _client
        scene = context.scene
        try:
            if _client is not None:
                _client.stop()
                _client = None
            scene.blendermcp_server_running = False
            self.report({'INFO'}, "Disconnected")
        except Exception as e:
            self.report({'ERROR'}, f"Error during disconnect: {e}")
            traceback.print_exc()
        return {'FINISHED'}

# Registration functions
_CLASSES = (
    BLENDERMCP_PT_Panel,
    BLENDERMCP_OT_SetFreeTrialHyper3DAPIKey,
    BLENDERMCP_OT_StartServer,
    BLENDERMCP_OT_StopServer,
)


def register():
    # Connection settings
    bpy.types.Scene.blendermcp_server_url = bpy.props.StringProperty(
        name="Server URL",
        description="BlenderMCP server's Streamable HTTP endpoint",
        default="http://localhost:8000/mcp",
    )
    bpy.types.Scene.blendermcp_jwt_token = bpy.props.StringProperty(
        name="JWT Token",
        description="Bearer token obtained via OAuth login",
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
    global _client, _executor

    # Stop any running client
    if _client is not None:
        try:
            _client.stop()
        except Exception as e:
            print(f"[BlenderMCP] Error stopping client during unregister: {e}")
        _client = None
    _executor = None

    for cls in reversed(_CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            pass

    for prop in (
        "blendermcp_server_url", "blendermcp_jwt_token",
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
