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
# BlenderMCP Client - FastMCP client that subscribes to the server's
# _message_bus log channel, executes received jobs, reports results.
# ============================================================================

# MCP log levels (RFC 5424) repurposed as job priorities (lower = more urgent)
LOG_LEVEL_TO_PRIORITY = {
    "emergency": 0, "alert": 1, "critical": 2, "error": 3,
    "warning": 4, "notice": 5, "info": 6, "debug": 7,
}

_MESSAGE_BUS_LOGGER = "_message_bus"


class BlenderMCPClient:
    """FastMCP client subscribed to server's _message_bus log channel."""

    def __init__(self, server_url: str, jwt_token: str, client_uuid: str):
        self.server_url = server_url
        self.jwt_token = jwt_token
        self.client_uuid = client_uuid

        self.client = None              # fastmcp.Client (inside worker loop)
        self.loop = None                # asyncio loop on worker thread
        self.thread = None
        self.running = False
        self.connected = False
        self.last_error = None

        # Priority queue: (priority_int, timestamp, job_payload)
        self.job_queue = []
        self.queue_lock = threading.Lock()
        self.active_jobs = {}
        self._timer_registered = False

    # --- Lifecycle ----------------------------------------------------------

    def start(self):
        """Start worker thread with its own asyncio loop."""
        if self.running:
            return
        if not FASTMCP_AVAILABLE:
            self.last_error = "fastmcp not installed"
            print(f"[BlenderMCP] {self.last_error}")
            return

        self.running = True
        self.thread = threading.Thread(target=self._thread_main, daemon=True)
        self.thread.start()

        # Register the queue drain timer on Blender's main thread
        if not self._timer_registered:
            bpy.app.timers.register(self._drain_queue, first_interval=0.1)
            self._timer_registered = True

        print(f"[BlenderMCP] Client starting: {self.client_uuid} -> {self.server_url}")

    def stop(self):
        """Signal worker to exit, then join."""
        self.running = False
        self.connected = False

        # Wake the worker loop
        if self.loop and self.loop.is_running():
            try:
                self.loop.call_soon_threadsafe(lambda: None)
            except Exception:
                pass

        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=3.0)
        self.thread = None
        self.loop = None
        self.client = None
        self._timer_registered = False  # timer self-removes when running=False
        print(f"[BlenderMCP] Client stopped: {self.client_uuid}")

    # --- Worker thread / asyncio loop --------------------------------------

    def _thread_main(self):
        """Run the asyncio loop on this thread."""
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._run())
        except Exception as e:
            self.last_error = f"Worker crashed: {e}"
            print(f"[BlenderMCP] {self.last_error}")
            traceback.print_exc()
        finally:
            try:
                self.loop.close()
            except Exception:
                pass

    async def _run(self):
        """Connect, register, subscribe to log notifications, await stop."""
        transport = StreamableHttpTransport(
            url=self.server_url,
            headers={"Authorization": f"Bearer {self.jwt_token}"},
        )

        try:
            async with FastMCPClient(transport, message_handler=self._on_message) as client:
                self.client = client

                # Subscribe to all priority levels
                try:
                    await client.set_logging_level("debug")
                except Exception as e:
                    print(f"[BlenderMCP] set_logging_level failed: {e}")

                # Register with server
                try:
                    await client.call_tool("blender_register_client", {
                        "client_uuid": self.client_uuid,
                        "client_type": "blender",
                        "is_persistent": True,
                        "capabilities": [
                            "python_execution", "modeling", "rendering",
                            "scene_management", "asset_processing",
                        ],
                    })
                    self.connected = True
                    print(f"[BlenderMCP] Registered as {self.client_uuid}")
                except Exception as e:
                    self.last_error = f"register_client failed: {e}"
                    print(f"[BlenderMCP] {self.last_error}")
                    return

                # Wait until stop() flips running
                while self.running:
                    await asyncio.sleep(0.2)

                # Graceful unregister
                try:
                    await client.call_tool("blender_unregister_client",
                                           {"client_uuid": self.client_uuid})
                except Exception as e:
                    print(f"[BlenderMCP] unregister_client failed: {e}")
        except Exception as e:
            self.last_error = f"Connection failed: {e}"
            print(f"[BlenderMCP] {self.last_error}")
        finally:
            self.connected = False
            self.client = None

    # --- Incoming message handler ------------------------------------------

    async def _on_message(self, message):
        """Filter MCP logging notifications on the _message_bus logger."""
        try:
            # Notifications have a .root for Notification union types in mcp.types
            inner = getattr(message, "root", message)
            method = getattr(inner, "method", None)
            if method != "notifications/message":
                return

            params = getattr(inner, "params", None)
            if params is None:
                return

            # params may be a pydantic model or a dict
            logger_name = getattr(params, "logger", None) or (
                params.get("logger") if isinstance(params, dict) else None
            )
            if logger_name != _MESSAGE_BUS_LOGGER:
                return

            level = getattr(params, "level", None) or (
                params.get("level") if isinstance(params, dict) else None
            )
            data = getattr(params, "data", None) or (
                params.get("data") if isinstance(params, dict) else None
            )

            if data is None:
                return

            # Decode data if it's a JSON string
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except json.JSONDecodeError:
                    return

            priority = LOG_LEVEL_TO_PRIORITY.get(str(level).lower(), 6)
            self._enqueue_job(priority, data)
        except Exception as e:
            print(f"[BlenderMCP] _on_message error: {e}")

    def _enqueue_job(self, priority: int, log_data: dict):
        """Add a job to the priority queue."""
        with self.queue_lock:
            heapq.heappush(self.job_queue, (priority, time.time(), log_data))

    # --- Main-thread execution (Blender) -----------------------------------

    def _drain_queue(self):
        """Timer callback on Blender's main thread. Returns next interval or None to stop."""
        if not self.running:
            self._timer_registered = False
            return None  # unregister timer

        with self.queue_lock:
            if not self.job_queue:
                return 0.1
            _, _, log_data = heapq.heappop(self.job_queue)

        # Server wire shape: {user_id, from_uuid, target_uuid, routing, payload,
        #                     job_id, message_id, priority, timestamp}
        # `payload` holds the LLM-sent dict, expected: {message_type, job_id, script, ...}
        target = log_data.get("target_uuid")
        if target and target != self.client_uuid:
            return 0.0  # not for us, check next

        payload = log_data.get("payload", log_data)
        msg_type = payload.get("message_type")
        if msg_type != "job_dispatch":
            return 0.0

        job_id = payload.get("job_id")
        script = payload.get("script", "")
        if not job_id or not script:
            return 0.0

        self._execute_script(job_id, script)
        return 0.0  # immediately check for next

    def _execute_script(self, job_id: str, script: str):
        """Execute script in Blender context; capture stdout; report result."""
        self.active_jobs[job_id] = time.time()
        output = io.StringIO()
        errbuf = io.StringIO()
        exec_globals = {
            "bpy": bpy,
            "bmesh": bmesh,
            "mathutils": mathutils,
            "executor": _executor,  # access to bpy-side helpers (polyhaven, etc.)
            "__name__": "__blender_mcp_job__",
        }
        try:
            with redirect_stdout(output):
                exec(compile(script, f"<job_{job_id}>", "exec"), exec_globals)
            self._submit_job_update(job_id, "completed",
                                    result=output.getvalue(), error="")
        except Exception as e:
            tb = traceback.format_exc()
            self._submit_job_update(job_id, "failed",
                                    result=output.getvalue(),
                                    error=f"{e}\n{tb}")
        finally:
            self.active_jobs.pop(job_id, None)

    def _submit_job_update(self, job_id: str, status: str,
                           result: str = "", error: str = ""):
        """Schedule job_update on the worker loop from Blender's main thread."""
        if not (self.loop and self.client and self.loop.is_running()):
            print(f"[BlenderMCP] Cannot report job {job_id}: client not connected")
            return

        # Server derives caller identity from auth context; only 4 fields accepted.
        coro = self.client.call_tool("blender_job_update", {
            "job_id": job_id,
            "status": status,
            "result": result,
            "error": error,
        })
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)

        # Non-blocking: log errors when they complete
        def _log_err(fut):
            try:
                fut.result(timeout=0)
            except Exception as e:
                print(f"[BlenderMCP] job_update for {job_id} failed: {e}")
        future.add_done_callback(_log_err)


# Module-level singleton for the BlenderCommandExecutor (helpers reachable
# from scripts via globals)
_executor = None
_client = None


# Main command executor class - inherits all existing functionality
class BlenderCommandExecutor:
    """Executes Blender commands - used by both old socket system (compatibility) and new message bus"""
    
    def execute_command(self, command):
        """Execute a command in the main Blender thread"""
        try:
            return self._execute_command_internal(command)

        except Exception as e:
            print(f"Error executing command: {str(e)}")
            traceback.print_exc()
            return {"status": "error", "message": str(e)}

    def _execute_command_internal(self, command):
        """Internal command execution with proper context"""
        cmd_type = command.get("type")
        params = command.get("params", {})

        # Add a handler for checking PolyHaven status
        if cmd_type == "get_polyhaven_status":
            return {"status": "success", "result": self.get_polyhaven_status()}

        # Base handlers that are always available
        handlers = {
            "get_scene_info": self.get_scene_info,
            "get_object_info": self.get_object_info,
            "get_viewport_screenshot": self.get_viewport_screenshot,
            "execute_code": self.execute_code,
            "get_console_output": lambda p=params: self.get_console_output(
                level=p.get("level", "all"),
                page=p.get("page", 1),
                page_size=p.get("page_size", 50)
            ),
            "console_operations": lambda p=params: self.console_operations(
                operation=p.get("operation", "get_info"),
                params=p.get("params", None)
            ),
            "msgbus_clear_by_owner": lambda p=params: self.msgbus_clear_by_owner(
                owner_id=p.get("owner_id", "default")
            ),
            "msgbus_publish_rna": lambda p=params: self.msgbus_publish_rna(
                data_path=p.get("data_path", None),
                key=p.get("key", None)
            ),
            "msgbus_subscribe_rna": lambda p=params: self.msgbus_subscribe_rna(
                owner_id=p.get("owner_id", "default"),
                data_path=p.get("data_path"),
                notify_type=p.get("notify_type", "UPDATE"),
                persistent=p.get("persistent", True)
            ),
            "msgbus_get_notifications": lambda p=params: self.msgbus_get_notifications(
                owner_id=p.get("owner_id", None),
                clear=p.get("clear", False)
            ),
            "msgbus_list_subscriptions": lambda p=params: self.msgbus_list_subscriptions(
                owner_id=p.get("owner_id", None)
            ),
            "browse_data": lambda p=params: self.browse_data(
                collection=p.get("collection", None),
                item_name=p.get("item_name", None),
                page=p.get("page", 1),
                page_size=p.get("page_size", 50),
                detail_level=p.get("detail_level", "summary")
            ),
            "get_polyhaven_status": self.get_polyhaven_status,
            "get_hyper3d_status": self.get_hyper3d_status,
            "get_sketchfab_status": self.get_sketchfab_status,
        }

        # Add Polyhaven handlers only if enabled
        if bpy.context.scene.blendermcp_use_polyhaven:
            polyhaven_handlers = {
                "get_polyhaven_categories": self.get_polyhaven_categories,
                "search_polyhaven_assets": self.search_polyhaven_assets,
                "download_polyhaven_asset": self.download_polyhaven_asset,
                "set_texture": self.set_texture,
            }
            handlers.update(polyhaven_handlers)

        # Add Hyper3d handlers only if enabled
        if bpy.context.scene.blendermcp_use_hyper3d:
            polyhaven_handlers = {
                "create_rodin_job": self.create_rodin_job,
                "poll_rodin_job_status": self.poll_rodin_job_status,
                "import_generated_asset": self.import_generated_asset,
            }
            handlers.update(polyhaven_handlers)

        # Add Sketchfab handlers only if enabled
        if bpy.context.scene.blendermcp_use_sketchfab:
            sketchfab_handlers = {
                "search_sketchfab_models": self.search_sketchfab_models,
                "download_sketchfab_model": self.download_sketchfab_model,
            }
            handlers.update(sketchfab_handlers)

        handler = handlers.get(cmd_type)
        if handler:
            try:
                print(f"Executing handler for {cmd_type}")
                result = handler(**params)
                print(f"Handler execution complete")
                return {"status": "success", "result": result}
            except Exception as e:
                print(f"Error in handler: {str(e)}")
                traceback.print_exc()
                return {"status": "error", "message": str(e)}
        else:
            return {"status": "error", "message": f"Unknown command type: {cmd_type}"}

    # All existing methods from the original class continue here unchanged...



    def get_scene_info(self):
        """Get information about the current Blender scene"""
        try:
            print("Getting scene info...")
            # Simplify the scene info to reduce data size
            scene_info = {
                "name": bpy.context.scene.name,
                "object_count": len(bpy.context.scene.objects),
                "objects": [],
                "materials_count": len(bpy.data.materials),
            }

            # Collect minimal object information (limit to first 10 objects)
            for i, obj in enumerate(bpy.context.scene.objects):
                if i >= 10:  # Reduced from 20 to 10
                    break

                obj_info = {
                    "name": obj.name,
                    "type": obj.type,
                    # Only include basic location data
                    "location": [round(float(obj.location.x), 2),
                                round(float(obj.location.y), 2),
                                round(float(obj.location.z), 2)],
                }
                scene_info["objects"].append(obj_info)

            print(f"Scene info collected: {len(scene_info['objects'])} objects")
            return scene_info
        except Exception as e:
            print(f"Error in get_scene_info: {str(e)}")
            traceback.print_exc()
            return {"error": str(e)}

    @staticmethod
    def _get_aabb(obj):
        """ Returns the world-space axis-aligned bounding box (AABB) of an object. """
        if obj.type != 'MESH':
            raise TypeError("Object must be a mesh")

        # Get the bounding box corners in local space
        local_bbox_corners = [mathutils.Vector(corner) for corner in obj.bound_box]

        # Convert to world coordinates
        world_bbox_corners = [obj.matrix_world @ corner for corner in local_bbox_corners]

        # Compute axis-aligned min/max coordinates
        min_corner = mathutils.Vector(map(min, zip(*world_bbox_corners)))
        max_corner = mathutils.Vector(map(max, zip(*world_bbox_corners)))

        return [
            [*min_corner], [*max_corner]
        ]



    def get_object_info(self, name):
        """Get detailed information about a specific object"""
        obj = bpy.data.objects.get(name)
        if not obj:
            raise ValueError(f"Object not found: {name}")

        # Basic object info
        obj_info = {
            "name": obj.name,
            "type": obj.type,
            "location": [obj.location.x, obj.location.y, obj.location.z],
            "rotation": [obj.rotation_euler.x, obj.rotation_euler.y, obj.rotation_euler.z],
            "scale": [obj.scale.x, obj.scale.y, obj.scale.z],
            "visible": obj.visible_get(),
            "materials": [],
        }

        if obj.type == "MESH":
            bounding_box = self._get_aabb(obj)
            obj_info["world_bounding_box"] = bounding_box

        # Add material slots
        for slot in obj.material_slots:
            if slot.material:
                obj_info["materials"].append(slot.material.name)

        # Add mesh data if applicable
        if obj.type == 'MESH' and obj.data:
            mesh = obj.data
            obj_info["mesh"] = {
                "vertices": len(mesh.vertices),
                "edges": len(mesh.edges),
                "polygons": len(mesh.polygons),
            }

        return obj_info

    def get_viewport_screenshot(self, max_size=800, filepath=None, format="png"):
        """
        Capture a screenshot of the current 3D viewport and save it to the specified path.

        Parameters:
        - max_size: Maximum size in pixels for the largest dimension of the image
        - filepath: Path where to save the screenshot file
        - format: Image format (png, jpg, etc.)

        Returns success/error status
        """
        try:
            if not filepath:
                return {"error": "No filepath provided"}

            # Find the active 3D viewport
            area = None
            for a in bpy.context.screen.areas:
                if a.type == 'VIEW_3D':
                    area = a
                    break

            if not area:
                return {"error": "No 3D viewport found"}

            # Take screenshot with proper context override
            with bpy.context.temp_override(area=area):
                bpy.ops.screen.screenshot_area(filepath=filepath)

            # Load and resize if needed
            img = bpy.data.images.load(filepath)
            width, height = img.size

            if max(width, height) > max_size:
                scale = max_size / max(width, height)
                new_width = int(width * scale)
                new_height = int(height * scale)
                img.scale(new_width, new_height)

                # Set format and save
                img.file_format = format.upper()
                img.save()
                width, height = new_width, new_height

            # Cleanup Blender image data
            bpy.data.images.remove(img)

            return {
                "success": True,
                "width": width,
                "height": height,
                "filepath": filepath
            }

        except Exception as e:
            return {"error": str(e)}

    def execute_code(self, code):
        """Execute arbitrary Blender Python code"""
        # This is powerful but potentially dangerous - use with caution
        try:
            # Create a local namespace for execution
            namespace = {"bpy": bpy}

            # Capture stdout during execution, and return it as result
            capture_buffer = io.StringIO()
            with redirect_stdout(capture_buffer):
                exec(code, namespace)

            captured_output = capture_buffer.getvalue()
            return {"executed": True, "result": captured_output}
        except Exception as e:
            raise Exception(f"Code execution error: {str(e)}")
    
    def console_operations(self, operation, params=None):
        """Execute various console operations using bpy.ops.console
        
        Args:
            operation: The console operation to perform
            params: Optional parameters for the operation
        """
        try:
            import bpy
            
            # Ensure we have a console area
            console_area = None
            for area in bpy.context.screen.areas:
                if area.type == 'CONSOLE':
                    console_area = area
                    break
            
            if not console_area and operation != "create":
                # Try to create a console if it doesn't exist
                for area in bpy.context.screen.areas:
                    if area.type == 'VIEW_3D':  # Convert a 3D view to console
                        area.type = 'CONSOLE'
                        console_area = area
                        break
            
            if not console_area and operation != "create":
                return {"error": "No console area available. Use operation='create' first."}
            
            # Override context for console operations
            if console_area:
                override = {'area': console_area}
                for region in console_area.regions:
                    if region.type == 'WINDOW':
                        override['region'] = region
                        break
            
            result = {}
            
            # Execute the requested operation
            if operation == "create":
                # Create a new console area
                for area in bpy.context.screen.areas:
                    if area.type in ['VIEW_3D', 'TEXT_EDITOR', 'INFO']:
                        area.type = 'CONSOLE'
                        result = {"success": True, "message": "Console area created"}
                        break
                else:
                    result = {"error": "Could not create console area - no suitable area to convert"}
            
            elif operation == "execute":
                # Execute code in console (bpy.ops.console.execute)
                if params and "code" in params:
                    # Set the console input
                    if console_area:
                        with bpy.context.temp_override(**override):
                            # Clear current line
                            bpy.ops.console.clear_line()
                            # Insert the code
                            bpy.ops.console.insert(text=params["code"])
                            # Execute it
                            bpy.ops.console.execute()
                            result = {"success": True, "message": "Code executed in console"}
                else:
                    result = {"error": "No code provided for execution"}
            
            elif operation == "autocomplete":
                # Autocomplete in console (bpy.ops.console.autocomplete)
                with bpy.context.temp_override(**override):
                    bpy.ops.console.autocomplete()
                result = {"success": True, "message": "Autocomplete triggered"}
            
            elif operation == "clear":
                # Clear console output (bpy.ops.console.clear)
                with bpy.context.temp_override(**override):
                    bpy.ops.console.clear(scrollback=params.get("scrollback", True) if params else True,
                                         history=params.get("history", False) if params else False)
                result = {"success": True, "message": "Console cleared"}
            
            elif operation == "clear_line":
                # Clear current line (bpy.ops.console.clear_line)
                with bpy.context.temp_override(**override):
                    bpy.ops.console.clear_line()
                result = {"success": True, "message": "Current line cleared"}
            
            elif operation == "copy":
                # Copy selected text (bpy.ops.console.copy)
                with bpy.context.temp_override(**override):
                    bpy.ops.console.copy()
                result = {"success": True, "message": "Text copied to clipboard"}
            
            elif operation == "copy_as_script":
                # Copy full history as script (bpy.ops.console.copy_as_script)
                with bpy.context.temp_override(**override):
                    bpy.ops.console.copy_as_script()
                result = {"success": True, "message": "Console history copied as script"}
            
            elif operation == "paste":
                # Paste from clipboard (bpy.ops.console.paste)
                with bpy.context.temp_override(**override):
                    bpy.ops.console.paste()
                result = {"success": True, "message": "Text pasted from clipboard"}
            
            elif operation == "history_cycle":
                # Cycle through history (bpy.ops.console.history_cycle)
                direction = params.get("direction", "BACKWARD") if params else "BACKWARD"
                with bpy.context.temp_override(**override):
                    bpy.ops.console.history_cycle(reverse=(direction == "FORWARD"))
                result = {"success": True, "message": f"History cycled {direction}"}
            
            elif operation == "history_append":
                # Append to history (bpy.ops.console.history_append)
                if params and "text" in params:
                    with bpy.context.temp_override(**override):
                        bpy.ops.console.history_append(text=params["text"],
                                                      current_character=params.get("current_character", 0),
                                                      remove_duplicates=params.get("remove_duplicates", True))
                    result = {"success": True, "message": "Added to history"}
                else:
                    result = {"error": "No text provided for history"}
            
            elif operation == "insert":
                # Insert text at cursor (bpy.ops.console.insert)
                if params and "text" in params:
                    with bpy.context.temp_override(**override):
                        bpy.ops.console.insert(text=params["text"])
                    result = {"success": True, "message": "Text inserted"}
                else:
                    result = {"error": "No text provided for insertion"}
            
            elif operation == "indent":
                # Indent current line (bpy.ops.console.indent)
                with bpy.context.temp_override(**override):
                    bpy.ops.console.indent()
                result = {"success": True, "message": "Line indented"}
            
            elif operation == "unindent":
                # Unindent current line (bpy.ops.console.unindent)
                with bpy.context.temp_override(**override):
                    bpy.ops.console.unindent()
                result = {"success": True, "message": "Line unindented"}
            
            elif operation == "select_all":
                # Select all text (bpy.ops.console.select_all)
                with bpy.context.temp_override(**override):
                    bpy.ops.console.select_all()
                result = {"success": True, "message": "All text selected"}
            
            elif operation == "select_word":
                # Select word at cursor (bpy.ops.console.select_word)
                with bpy.context.temp_override(**override):
                    bpy.ops.console.select_word()
                result = {"success": True, "message": "Word selected"}
            
            elif operation == "scrollback_append":
                # Append to scrollback (bpy.ops.console.scrollback_append)
                if params and "text" in params:
                    with bpy.context.temp_override(**override):
                        bpy.ops.console.scrollback_append(text=params["text"],
                                                         type=params.get("type", "OUTPUT"))
                    result = {"success": True, "message": "Added to scrollback"}
                else:
                    result = {"error": "No text provided for scrollback"}
            
            elif operation == "get_info":
                # Get console information
                info = {
                    "has_console": console_area is not None,
                    "console_type": console_area.type if console_area else None,
                }
                
                if console_area:
                    for space in console_area.spaces:
                        if space.type == 'CONSOLE':
                            info["language"] = getattr(space, "language", "python")
                            info["font_size"] = getattr(space, "font_size", 12)
                            info["select_start"] = getattr(space, "select_start", 0)
                            info["select_end"] = getattr(space, "select_end", 0)
                            break
                
                result = {"success": True, "info": info}
            
            else:
                result = {"error": f"Unknown console operation: {operation}"}
            
            return result
            
        except Exception as e:
            return {"error": f"Console operation failed: {str(e)}"}
    
    def msgbus_clear_by_owner(self, owner_id):
        """Clear all message bus subscriptions by owner
        
        Args:
            owner_id: Unique identifier for the owner
        """
        try:
            import bpy
            
            # Clear all subscriptions for this owner
            bpy.msgbus.clear_by_owner(owner_id)
            
            return {
                "success": True,
                "message": f"Cleared all message bus subscriptions for owner: {owner_id}"
            }
        except Exception as e:
            return {"error": f"Failed to clear message bus: {str(e)}"}
    
    def msgbus_publish_rna(self, data_path=None, key=None):
        """Publish an RNA property change to the message bus
        
        Args:
            data_path: Optional data path to publish (e.g., "frame_current")
            key: Optional specific key to publish
        """
        try:
            import bpy
            
            if key:
                # Publish with specific key
                bpy.msgbus.publish_rna(key=key)
                return {
                    "success": True,
                    "message": f"Published RNA message with key: {key}"
                }
            elif data_path:
                # Try to construct key from data path
                # Common patterns for RNA paths
                if data_path == "frame_current":
                    key = (bpy.types.Scene, "frame_current")
                elif data_path == "active_object":
                    key = (bpy.types.ViewLayer, "objects")
                elif data_path == "selected_objects":
                    key = (bpy.types.Object, "select_set")
                elif "." in data_path:
                    # Try to parse complex paths
                    parts = data_path.split(".")
                    return {
                        "error": f"Complex data path '{data_path}' requires manual key construction"
                    }
                else:
                    return {
                        "error": f"Unknown data path '{data_path}'. Please provide a specific key."
                    }
                
                bpy.msgbus.publish_rna(key=key)
                return {
                    "success": True,
                    "message": f"Published RNA message for data path: {data_path}"
                }
            else:
                # Publish all pending messages
                bpy.msgbus.publish_rna()
                return {
                    "success": True,
                    "message": "Published all pending RNA messages"
                }
                
        except Exception as e:
            return {"error": f"Failed to publish RNA message: {str(e)}"}
    
    # Storage for message bus subscriptions and callbacks
    _msgbus_subscriptions = {}
    _msgbus_callbacks = {}
    
    def msgbus_subscribe_rna(self, owner_id, data_path, notify_type="UPDATE", persistent=True):
        """Subscribe to RNA property changes via message bus
        
        Args:
            owner_id: Unique identifier for the subscription owner
            data_path: RNA data path to monitor (e.g., "frame_current", "active_object")
            notify_type: Type of notification (UPDATE, PERSISTENT, etc.)
            persistent: Whether the subscription persists across file loads
        """
        try:
            import bpy
            from bpy.types import Scene, ViewLayer, Object, Material
            
            # Map common data paths to RNA keys
            key = None
            context_obj = None
            
            if data_path == "frame_current":
                key = (Scene, "frame_current")
                context_obj = bpy.context.scene
            elif data_path == "active_object":
                key = (ViewLayer, "objects")
                context_obj = bpy.context.view_layer
            elif data_path == "selected_objects":
                key = (Object, "select_set")
                context_obj = bpy.context.active_object if bpy.context.active_object else None
            elif data_path == "active_material":
                key = (Object, "active_material")
                context_obj = bpy.context.active_object if bpy.context.active_object else None
            elif data_path.startswith("scene."):
                # Scene properties
                prop = data_path.replace("scene.", "")
                key = (Scene, prop)
                context_obj = bpy.context.scene
            elif data_path.startswith("object."):
                # Object properties
                prop = data_path.replace("object.", "")
                key = (Object, prop)
                context_obj = bpy.context.active_object
            else:
                return {
                    "error": f"Unsupported data path: {data_path}. Supported paths: frame_current, active_object, selected_objects, active_material, scene.*, object.*"
                }
            
            if not key:
                return {"error": "Could not determine RNA key for data path"}
            
            # Create a unique subscription ID
            sub_id = f"{owner_id}_{data_path}"
            
            # Store the subscription info
            if owner_id not in self._msgbus_subscriptions:
                self._msgbus_subscriptions[owner_id] = {}
            
            # Create callback function that stores the notification
            def callback(*args):
                # Store the notification in a queue
                if sub_id not in self._msgbus_callbacks:
                    self._msgbus_callbacks[sub_id] = []
                
                import time
                notification = {
                    "timestamp": time.time(),
                    "data_path": data_path,
                    "owner_id": owner_id,
                    "context": str(args) if args else None
                }
                
                # Keep only last 100 notifications per subscription
                self._msgbus_callbacks[sub_id].append(notification)
                if len(self._msgbus_callbacks[sub_id]) > 100:
                    self._msgbus_callbacks[sub_id] = self._msgbus_callbacks[sub_id][-100:]
                
                # Log for debugging
                print(f"Message Bus: {data_path} changed for owner {owner_id}")
            
            # Subscribe to the message bus
            subscribe_options = {
                "key": key,
                "owner": owner_id,
                "args": (sub_id,),
                "notify": callback
            }
            
            if persistent:
                subscribe_options["options"] = {"PERSISTENT"}
            
            bpy.msgbus.subscribe_rna(**subscribe_options)
            
            # Store subscription info
            self._msgbus_subscriptions[owner_id][data_path] = {
                "key": str(key),
                "persistent": persistent,
                "notify_type": notify_type,
                "active": True
            }
            
            return {
                "success": True,
                "message": f"Subscribed to {data_path} for owner {owner_id}",
                "subscription_id": sub_id,
                "key": str(key)
            }
            
        except Exception as e:
            return {"error": f"Failed to subscribe to RNA: {str(e)}"}
    
    def msgbus_get_notifications(self, owner_id=None, clear=False):
        """Get pending message bus notifications
        
        Args:
            owner_id: Optional owner ID to filter notifications
            clear: Whether to clear notifications after reading
        """
        try:
            notifications = []
            
            # Filter by owner if specified
            for sub_id, notifs in self._msgbus_callbacks.items():
                if owner_id and not sub_id.startswith(owner_id + "_"):
                    continue
                notifications.extend(notifs)
            
            # Sort by timestamp
            notifications.sort(key=lambda x: x.get("timestamp", 0))
            
            # Clear if requested
            if clear:
                if owner_id:
                    # Clear only for specific owner
                    keys_to_clear = [k for k in self._msgbus_callbacks.keys() 
                                    if k.startswith(owner_id + "_")]
                    for k in keys_to_clear:
                        self._msgbus_callbacks[k] = []
                else:
                    # Clear all
                    self._msgbus_callbacks.clear()
            
            return {
                "success": True,
                "notifications": notifications,
                "count": len(notifications)
            }
            
        except Exception as e:
            return {"error": f"Failed to get notifications: {str(e)}"}
    
    def msgbus_list_subscriptions(self, owner_id=None):
        """List active message bus subscriptions
        
        Args:
            owner_id: Optional owner ID to filter subscriptions
        """
        try:
            subscriptions = []
            
            if owner_id:
                if owner_id in self._msgbus_subscriptions:
                    for data_path, info in self._msgbus_subscriptions[owner_id].items():
                        subscriptions.append({
                            "owner_id": owner_id,
                            "data_path": data_path,
                            **info
                        })
            else:
                # List all subscriptions
                for owner_id, paths in self._msgbus_subscriptions.items():
                    for data_path, info in paths.items():
                        subscriptions.append({
                            "owner_id": owner_id,
                            "data_path": data_path,
                            **info
                        })
            
            return {
                "success": True,
                "subscriptions": subscriptions,
                "count": len(subscriptions),
                "owners": list(self._msgbus_subscriptions.keys())
            }
            
        except Exception as e:
            return {"error": f"Failed to list subscriptions: {str(e)}"}
    
    def browse_data(self, collection=None, item_name=None, page=1, page_size=50, detail_level="summary"):
        """Browse bpy.data collections with pagination and detail levels
        
        Args:
            collection: Data collection to browse (e.g., "objects", "materials", "scenes")
            item_name: Specific item name to get details for
            page: Page number for pagination
            page_size: Items per page
            detail_level: Level of detail ("summary", "detailed", "full")
        """
        try:
            import bpy
            
            # Map of available data collections
            data_collections = {
                "actions": bpy.data.actions,
                "armatures": bpy.data.armatures,
                "brushes": bpy.data.brushes,
                "cache_files": bpy.data.cache_files,
                "cameras": bpy.data.cameras,
                "collections": bpy.data.collections,
                "curves": bpy.data.curves,
                "fonts": bpy.data.fonts,
                "grease_pencils": bpy.data.grease_pencils,
                "hair_curves": bpy.data.hair_curves,
                "images": bpy.data.images,
                "lattices": bpy.data.lattices,
                "libraries": bpy.data.libraries,
                "lightprobes": bpy.data.lightprobes,
                "lights": bpy.data.lights,
                "linestyles": bpy.data.linestyles,
                "masks": bpy.data.masks,
                "materials": bpy.data.materials,
                "meshes": bpy.data.meshes,
                "metaballs": bpy.data.metaballs,
                "movieclips": bpy.data.movieclips,
                "node_groups": bpy.data.node_groups,
                "objects": bpy.data.objects,
                "paint_curves": bpy.data.paint_curves,
                "palettes": bpy.data.palettes,
                "particles": bpy.data.particles,
                "pointclouds": bpy.data.pointclouds,
                "scenes": bpy.data.scenes,
                "screens": bpy.data.screens,
                "shape_keys": bpy.data.shape_keys,
                "sounds": bpy.data.sounds,
                "speakers": bpy.data.speakers,
                "texts": bpy.data.texts,
                "textures": bpy.data.textures,
                "volumes": bpy.data.volumes,
                "window_managers": bpy.data.window_managers,
                "workspaces": bpy.data.workspaces,
                "worlds": bpy.data.worlds,
            }
            
            # If no collection specified, list available collections
            if not collection:
                collections_info = []
                for name, coll in data_collections.items():
                    try:
                        count = len(coll)
                        collections_info.append({
                            "name": name,
                            "count": count,
                            "type": str(type(coll).__name__)
                        })
                    except:
                        pass
                
                return {
                    "success": True,
                    "collections": collections_info,
                    "total": len(collections_info)
                }
            
            # Check if collection exists
            if collection not in data_collections:
                return {
                    "error": f"Unknown collection: {collection}",
                    "available": list(data_collections.keys())
                }
            
            data_collection = data_collections[collection]
            
            # If specific item requested
            if item_name:
                if item_name in data_collection:
                    item = data_collection[item_name]
                    item_info = self._get_data_item_info(item, collection, detail_level)
                    return {
                        "success": True,
                        "item": item_info,
                        "collection": collection
                    }
                else:
                    return {
                        "error": f"Item '{item_name}' not found in {collection}",
                        "available_count": len(data_collection)
                    }
            
            # Browse collection with pagination
            items = list(data_collection)
            total_items = len(items)
            total_pages = (total_items + page_size - 1) // page_size
            
            # Calculate pagination
            start_idx = (page - 1) * page_size
            end_idx = min(start_idx + page_size, total_items)
            page_items = items[start_idx:end_idx]
            
            # Get info for each item
            items_info = []
            for item in page_items:
                try:
                    item_info = self._get_data_item_info(item, collection, "summary")
                    items_info.append(item_info)
                except Exception as e:
                    items_info.append({
                        "name": getattr(item, "name", "unknown"),
                        "error": str(e)
                    })
            
            return {
                "success": True,
                "collection": collection,
                "items": items_info,
                "page": page,
                "page_size": page_size,
                "total_pages": total_pages,
                "total_items": total_items
            }
            
        except Exception as e:
            return {"error": f"Failed to browse data: {str(e)}"}
    
    def _get_data_item_info(self, item, collection_type, detail_level="summary"):
        """Get information about a data item"""
        info = {
            "name": getattr(item, "name", "unnamed"),
            "type": type(item).__name__,
            "collection": collection_type
        }
        
        # Add common properties
        if hasattr(item, "users"):
            info["users"] = item.users
        if hasattr(item, "use_fake_user"):
            info["use_fake_user"] = item.use_fake_user
        if hasattr(item, "library"):
            info["library"] = item.library.filepath if item.library else None
        
        # Collection-specific info
        if collection_type == "objects":
            info["type_specific"] = item.type
            if detail_level != "summary":
                info["location"] = list(item.location)
                info["rotation"] = list(item.rotation_euler)
                info["scale"] = list(item.scale)
                info["visible"] = item.visible_get()
                if item.data:
                    info["data_name"] = item.data.name
                    info["data_type"] = type(item.data).__name__
        
        elif collection_type == "materials":
            info["node_tree"] = item.node_tree is not None
            if detail_level != "summary":
                info["use_nodes"] = item.use_nodes
                if item.node_tree and detail_level == "full":
                    info["nodes_count"] = len(item.node_tree.nodes)
        
        elif collection_type == "meshes":
            info["vertices"] = len(item.vertices)
            info["edges"] = len(item.edges) 
            info["faces"] = len(item.polygons)
            if detail_level != "summary":
                info["has_custom_normals"] = item.has_custom_normals
                info["materials_count"] = len(item.materials)
        
        elif collection_type == "scenes":
            info["frame_start"] = item.frame_start
            info["frame_end"] = item.frame_end
            info["frame_current"] = item.frame_current
            if detail_level != "summary":
                info["render_engine"] = item.render.engine
                info["camera"] = item.camera.name if item.camera else None
                info["world"] = item.world.name if item.world else None
        
        elif collection_type == "images":
            info["size"] = list(item.size)
            info["filepath"] = item.filepath
            if detail_level != "summary":
                info["source"] = item.source
                info["packed"] = item.packed_file is not None
                info["has_data"] = item.has_data
        
        elif collection_type == "collections":
            info["objects_count"] = len(item.objects)
            info["children_count"] = len(item.children)
            if detail_level != "summary":
                info["hide_viewport"] = item.hide_viewport
                info["hide_render"] = item.hide_render
        
        elif collection_type == "node_groups":
            info["type"] = item.type
            if detail_level != "summary" and item.nodes:
                info["nodes_count"] = len(item.nodes)
                info["links_count"] = len(item.links)
        
        elif collection_type == "texts":
            info["filepath"] = item.filepath
            info["is_dirty"] = item.is_dirty
            info["is_in_memory"] = item.is_in_memory
            if detail_level == "full":
                info["lines_count"] = len(item.lines)
                if detail_level == "full" and len(item.lines) < 100:
                    info["content_preview"] = "\n".join([line.body for line in item.lines[:10]])
        
        elif collection_type == "actions":
            info["frame_range"] = list(item.frame_range)
            if detail_level != "summary":
                info["fcurves_count"] = len(item.fcurves)
                info["groups_count"] = len(item.groups)
        
        elif collection_type == "worlds":
            info["use_nodes"] = item.use_nodes
            if detail_level != "summary" and item.node_tree:
                info["nodes_count"] = len(item.node_tree.nodes)
        
        return info
    
    def get_console_output(self, level="all", page=1, page_size=50):
        """Get recent console output from Blender's internal console with filtering and pagination
        
        Args:
            level: Filter by message level - "all", "info", "warning", "error", "output"
            page: Page number (1-based)
            page_size: Number of lines per page
        """
        try:
            import sys
            import io
            import re
            
            # Store all console lines with their types
            console_lines = []
            
            # Helper function to classify line type
            def classify_line(line):
                """Classify line based on content and Blender's report types"""
                line_lower = line.lower()
                # Check for Blender's standard report prefixes
                if line.startswith("Error:") or 'error' in line_lower or 'exception' in line_lower or 'traceback' in line_lower:
                    return 'error'
                elif line.startswith("Warning:") or 'warning' in line_lower or 'warn' in line_lower:
                    return 'warning'
                elif line.startswith("Info:") or 'info:' in line_lower:
                    return 'info'
                elif line.startswith(">>> ") or line.startswith("... "):  # Python prompt
                    return 'input'
                else:
                    return 'output'
            
            # First, try to get recent operator reports using Blender's report system
            # These are the official reports with proper severity levels
            if hasattr(bpy.context.window_manager, "operators"):
                try:
                    # Get the last operator that was executed
                    for op in reversed(list(bpy.context.window_manager.operators)):
                        if hasattr(op, 'report'):
                            # This operator has reports
                            pass  # Reports are shown in UI but not directly accessible via API
                except:
                    pass
            
            # Access report messages through the Python console's report callback
            # Note: Blender uses these report types: {'DEBUG', 'INFO', 'OPERATOR', 'PROPERTY', 'WARNING', 'ERROR', 'ERROR_INVALID_INPUT', 'ERROR_INVALID_CONTEXT', 'ERROR_OUT_OF_MEMORY'}
            import bpy.types
            
            # Try to capture recent reports if they're stored
            # Unfortunately, Blender doesn't store a history of reports in a directly accessible way
            # But we can intercept them when they happen using handlers
            
            # Try to access the Python console buffer using proper API
            console_found = False
            if hasattr(bpy.context, "screen") and bpy.context.screen:
                for area in bpy.context.screen.areas:
                    if area.type == 'CONSOLE':
                        console_found = True
                        # Access console through proper API
                        try:
                            for space in area.spaces:
                                if space.type == 'CONSOLE':
                                    # Access console history and scrollback
                                    # History contains previously executed commands
                                    if hasattr(space, 'history'):
                                        for item in space.history:
                                            if hasattr(item, 'body'):
                                                text = item.body
                                                console_lines.append({
                                                    'text': text,
                                                    'type': 'input',
                                                    'source': 'history'
                                                })
                                    
                                    # Scrollback contains console output
                                    if hasattr(space, 'scrollback'):
                                        for line in space.scrollback:
                                            if hasattr(line, 'body'):
                                                text = line.body
                                                line_type = classify_line(text)
                                                # Check line type attribute if available
                                                if hasattr(line, 'type'):
                                                    # Blender console line types: OUTPUT, INPUT, INFO, ERROR
                                                    if line.type == 'ERROR':
                                                        line_type = 'error'
                                                    elif line.type == 'INFO':
                                                        line_type = 'info'
                                                    elif line.type == 'INPUT':
                                                        line_type = 'input'
                                                    elif line.type == 'OUTPUT':
                                                        line_type = 'output'
                                                console_lines.append({
                                                    'text': text,
                                                    'type': line_type,
                                                    'source': 'console'
                                                })
                                    break
                        except Exception as e:
                            console_lines.append({
                                'text': f"(Could not access console: {e})",
                                'type': 'error',
                                'source': 'system'
                            })
            
            # Get Info area messages (warnings, errors from operators)
            if hasattr(bpy.context, "screen") and bpy.context.screen:
                for area in bpy.context.screen.areas:
                    if area.type == 'INFO':
                        # Info area contains operator reports
                        # We can't directly access the text, but we know it exists
                        console_lines.append({
                            'text': "(Info area detected - operator messages displayed in UI)",
                            'type': 'info',
                            'source': 'info_area'
                        })
                        break
            
            # On macOS, get system console output
            import platform
            if platform.system() == "Darwin":
                try:
                    import subprocess
                    # Get recent console messages for Blender
                    result = subprocess.run(
                        ["log", "show", "--predicate", "process == 'Blender'", "--last", "1m"],
                        capture_output=True, text=True, timeout=2
                    )
                    if result.stdout:
                        # Parse system log lines
                        for line in result.stdout.split('\n')[-100:]:  # Last 100 lines
                            if line.strip():
                                line_type = classify_line(line)
                                console_lines.append({
                                    'text': line,
                                    'type': line_type,
                                    'source': 'system'
                                })
                except Exception as e:
                    console_lines.append({
                        'text': f"Could not access system console: {e}",
                        'type': 'warning',
                        'source': 'system'
                    })
            
            # Filter by level if specified
            if level != "all":
                console_lines = [line for line in console_lines if line['type'] == level]
            
            # Calculate pagination
            total_lines = len(console_lines)
            total_pages = (total_lines + page_size - 1) // page_size
            start_idx = (page - 1) * page_size
            end_idx = min(start_idx + page_size, total_lines)
            
            # Get the requested page
            page_lines = console_lines[start_idx:end_idx]
            
            # Format output
            formatted_lines = []
            for line in page_lines:
                prefix = f"[{line['type'].upper()}]" if line['type'] != 'output' else ""
                formatted_lines.append(f"{prefix} {line['text']}" if prefix else line['text'])
            
            return {
                "console_output": "\n".join(formatted_lines) if formatted_lines else "No console output available",
                "page": page,
                "page_size": page_size,
                "total_pages": total_pages,
                "total_lines": total_lines,
                "level": level,
                "has_console": console_found,
                "lines": page_lines  # Include structured data
            }
        except Exception as e:
            return {"error": f"Failed to get console output: {str(e)}"}



    def get_polyhaven_categories(self, asset_type):
        """Get categories for a specific asset type from Polyhaven"""
        try:
            if asset_type not in ["hdris", "textures", "models", "all"]:
                return {"error": f"Invalid asset type: {asset_type}. Must be one of: hdris, textures, models, all"}

            response = requests.get(f"https://api.polyhaven.com/categories/{asset_type}", headers=REQ_HEADERS)
            if response.status_code == 200:
                return {"categories": response.json()}
            else:
                return {"error": f"API request failed with status code {response.status_code}"}
        except Exception as e:
            return {"error": str(e)}

    def search_polyhaven_assets(self, asset_type=None, categories=None):
        """Search for assets from Polyhaven with optional filtering"""
        try:
            url = "https://api.polyhaven.com/assets"
            params = {}

            if asset_type and asset_type != "all":
                if asset_type not in ["hdris", "textures", "models"]:
                    return {"error": f"Invalid asset type: {asset_type}. Must be one of: hdris, textures, models, all"}
                params["type"] = asset_type

            if categories:
                params["categories"] = categories

            response = requests.get(url, params=params, headers=REQ_HEADERS)
            if response.status_code == 200:
                # Limit the response size to avoid overwhelming Blender
                assets = response.json()
                # Return only the first 20 assets to keep response size manageable
                limited_assets = {}
                for i, (key, value) in enumerate(assets.items()):
                    if i >= 20:  # Limit to 20 assets
                        break
                    limited_assets[key] = value

                return {"assets": limited_assets, "total_count": len(assets), "returned_count": len(limited_assets)}
            else:
                return {"error": f"API request failed with status code {response.status_code}"}
        except Exception as e:
            return {"error": str(e)}

    def download_polyhaven_asset(self, asset_id, asset_type, resolution="1k", file_format=None):
        try:
            # First get the files information
            files_response = requests.get(f"https://api.polyhaven.com/files/{asset_id}", headers=REQ_HEADERS)
            if files_response.status_code != 200:
                return {"error": f"Failed to get asset files: {files_response.status_code}"}

            files_data = files_response.json()

            # Handle different asset types
            if asset_type == "hdris":
                # For HDRIs, download the .hdr or .exr file
                if not file_format:
                    file_format = "hdr"  # Default format for HDRIs

                if "hdri" in files_data and resolution in files_data["hdri"] and file_format in files_data["hdri"][resolution]:
                    file_info = files_data["hdri"][resolution][file_format]
                    file_url = file_info["url"]

                    # For HDRIs, we need to save to a temporary file first
                    # since Blender can't properly load HDR data directly from memory
                    with tempfile.NamedTemporaryFile(suffix=f".{file_format}", delete=False) as tmp_file:
                        # Download the file
                        response = requests.get(file_url, headers=REQ_HEADERS)
                        if response.status_code != 200:
                            return {"error": f"Failed to download HDRI: {response.status_code}"}

                        tmp_file.write(response.content)
                        tmp_path = tmp_file.name

                    try:
                        # Create a new world if none exists
                        if not bpy.data.worlds:
                            bpy.data.worlds.new("World")

                        world = bpy.data.worlds[0]
                        world.use_nodes = True
                        node_tree = world.node_tree

                        # Clear existing nodes
                        for node in node_tree.nodes:
                            node_tree.nodes.remove(node)

                        # Create nodes
                        tex_coord = node_tree.nodes.new(type='ShaderNodeTexCoord')
                        tex_coord.location = (-800, 0)

                        mapping = node_tree.nodes.new(type='ShaderNodeMapping')
                        mapping.location = (-600, 0)

                        # Load the image from the temporary file
                        env_tex = node_tree.nodes.new(type='ShaderNodeTexEnvironment')
                        env_tex.location = (-400, 0)
                        env_tex.image = bpy.data.images.load(tmp_path)

                        # Use a color space that exists in all Blender versions
                        if file_format.lower() == 'exr':
                            # Try to use Linear color space for EXR files
                            try:
                                env_tex.image.colorspace_settings.name = 'Linear'
                            except:
                                # Fallback to Non-Color if Linear isn't available
                                env_tex.image.colorspace_settings.name = 'Non-Color'
                        else:  # hdr
                            # For HDR files, try these options in order
                            for color_space in ['Linear', 'Linear Rec.709', 'Non-Color']:
                                try:
                                    env_tex.image.colorspace_settings.name = color_space
                                    break  # Stop if we successfully set a color space
                                except:
                                    continue

                        background = node_tree.nodes.new(type='ShaderNodeBackground')
                        background.location = (-200, 0)

                        output = node_tree.nodes.new(type='ShaderNodeOutputWorld')
                        output.location = (0, 0)

                        # Connect nodes
                        node_tree.links.new(tex_coord.outputs['Generated'], mapping.inputs['Vector'])
                        node_tree.links.new(mapping.outputs['Vector'], env_tex.inputs['Vector'])
                        node_tree.links.new(env_tex.outputs['Color'], background.inputs['Color'])
                        node_tree.links.new(background.outputs['Background'], output.inputs['Surface'])

                        # Set as active world
                        bpy.context.scene.world = world

                        # Clean up temporary file
                        try:
                            tempfile._cleanup()  # This will clean up all temporary files
                        except:
                            pass

                        return {
                            "success": True,
                            "message": f"HDRI {asset_id} imported successfully",
                            "image_name": env_tex.image.name
                        }
                    except Exception as e:
                        return {"error": f"Failed to set up HDRI in Blender: {str(e)}"}
                else:
                    return {"error": f"Requested resolution or format not available for this HDRI"}

            elif asset_type == "textures":
                if not file_format:
                    file_format = "jpg"  # Default format for textures

                downloaded_maps = {}

                try:
                    for map_type in files_data:
                        if map_type not in ["blend", "gltf"]:  # Skip non-texture files
                            if resolution in files_data[map_type] and file_format in files_data[map_type][resolution]:
                                file_info = files_data[map_type][resolution][file_format]
                                file_url = file_info["url"]

                                # Use NamedTemporaryFile like we do for HDRIs
                                with tempfile.NamedTemporaryFile(suffix=f".{file_format}", delete=False) as tmp_file:
                                    # Download the file
                                    response = requests.get(file_url, headers=REQ_HEADERS)
                                    if response.status_code == 200:
                                        tmp_file.write(response.content)
                                        tmp_path = tmp_file.name

                                        # Load image from temporary file
                                        image = bpy.data.images.load(tmp_path)
                                        image.name = f"{asset_id}_{map_type}.{file_format}"

                                        # Pack the image into .blend file
                                        image.pack()

                                        # Set color space based on map type
                                        if map_type in ['color', 'diffuse', 'albedo']:
                                            try:
                                                image.colorspace_settings.name = 'sRGB'
                                            except:
                                                pass
                                        else:
                                            try:
                                                image.colorspace_settings.name = 'Non-Color'
                                            except:
                                                pass

                                        downloaded_maps[map_type] = image

                                        # Clean up temporary file
                                        try:
                                            os.unlink(tmp_path)
                                        except:
                                            pass

                    if not downloaded_maps:
                        return {"error": f"No texture maps found for the requested resolution and format"}

                    # Create a new material with the downloaded textures
                    mat = bpy.data.materials.new(name=asset_id)
                    mat.use_nodes = True
                    nodes = mat.node_tree.nodes
                    links = mat.node_tree.links

                    # Clear default nodes
                    for node in nodes:
                        nodes.remove(node)

                    # Create output node
                    output = nodes.new(type='ShaderNodeOutputMaterial')
                    output.location = (300, 0)

                    # Create principled BSDF node
                    principled = nodes.new(type='ShaderNodeBsdfPrincipled')
                    principled.location = (0, 0)
                    links.new(principled.outputs[0], output.inputs[0])

                    # Add texture nodes based on available maps
                    tex_coord = nodes.new(type='ShaderNodeTexCoord')
                    tex_coord.location = (-800, 0)

                    mapping = nodes.new(type='ShaderNodeMapping')
                    mapping.location = (-600, 0)
                    mapping.vector_type = 'TEXTURE'  # Changed from default 'POINT' to 'TEXTURE'
                    links.new(tex_coord.outputs['UV'], mapping.inputs['Vector'])

                    # Position offset for texture nodes
                    x_pos = -400
                    y_pos = 300

                    # Connect different texture maps
                    for map_type, image in downloaded_maps.items():
                        tex_node = nodes.new(type='ShaderNodeTexImage')
                        tex_node.location = (x_pos, y_pos)
                        tex_node.image = image

                        # Set color space based on map type
                        if map_type.lower() in ['color', 'diffuse', 'albedo']:
                            try:
                                tex_node.image.colorspace_settings.name = 'sRGB'
                            except:
                                pass  # Use default if sRGB not available
                        else:
                            try:
                                tex_node.image.colorspace_settings.name = 'Non-Color'
                            except:
                                pass  # Use default if Non-Color not available

                        links.new(mapping.outputs['Vector'], tex_node.inputs['Vector'])

                        # Connect to appropriate input on Principled BSDF
                        if map_type.lower() in ['color', 'diffuse', 'albedo']:
                            links.new(tex_node.outputs['Color'], principled.inputs['Base Color'])
                        elif map_type.lower() in ['roughness', 'rough']:
                            links.new(tex_node.outputs['Color'], principled.inputs['Roughness'])
                        elif map_type.lower() in ['metallic', 'metalness', 'metal']:
                            links.new(tex_node.outputs['Color'], principled.inputs['Metallic'])
                        elif map_type.lower() in ['normal', 'nor']:
                            # Add normal map node
                            normal_map = nodes.new(type='ShaderNodeNormalMap')
                            normal_map.location = (x_pos + 200, y_pos)
                            links.new(tex_node.outputs['Color'], normal_map.inputs['Color'])
                            links.new(normal_map.outputs['Normal'], principled.inputs['Normal'])
                        elif map_type in ['displacement', 'disp', 'height']:
                            # Add displacement node
                            disp_node = nodes.new(type='ShaderNodeDisplacement')
                            disp_node.location = (x_pos + 200, y_pos - 200)
                            links.new(tex_node.outputs['Color'], disp_node.inputs['Height'])
                            links.new(disp_node.outputs['Displacement'], output.inputs['Displacement'])

                        y_pos -= 250

                    return {
                        "success": True,
                        "message": f"Texture {asset_id} imported as material",
                        "material": mat.name,
                        "maps": list(downloaded_maps.keys())
                    }

                except Exception as e:
                    return {"error": f"Failed to process textures: {str(e)}"}

            elif asset_type == "models":
                # For models, prefer glTF format if available
                if not file_format:
                    file_format = "gltf"  # Default format for models

                if file_format in files_data and resolution in files_data[file_format]:
                    file_info = files_data[file_format][resolution][file_format]
                    file_url = file_info["url"]

                    # Create a temporary directory to store the model and its dependencies
                    temp_dir = tempfile.mkdtemp()
                    main_file_path = ""

                    try:
                        # Download the main model file
                        main_file_name = file_url.split("/")[-1]
                        main_file_path = os.path.join(temp_dir, main_file_name)

                        response = requests.get(file_url, headers=REQ_HEADERS)
                        if response.status_code != 200:
                            return {"error": f"Failed to download model: {response.status_code}"}

                        with open(main_file_path, "wb") as f:
                            f.write(response.content)

                        # Check for included files and download them
                        if "include" in file_info and file_info["include"]:
                            for include_path, include_info in file_info["include"].items():
                                # Get the URL for the included file - this is the fix
                                include_url = include_info["url"]

                                # Create the directory structure for the included file
                                include_file_path = os.path.join(temp_dir, include_path)
                                os.makedirs(os.path.dirname(include_file_path), exist_ok=True)

                                # Download the included file
                                include_response = requests.get(include_url, headers=REQ_HEADERS)
                                if include_response.status_code == 200:
                                    with open(include_file_path, "wb") as f:
                                        f.write(include_response.content)
                                else:
                                    print(f"Failed to download included file: {include_path}")

                        # Import the model into Blender
                        if file_format == "gltf" or file_format == "glb":
                            bpy.ops.import_scene.gltf(filepath=main_file_path)
                        elif file_format == "fbx":
                            bpy.ops.import_scene.fbx(filepath=main_file_path)
                        elif file_format == "obj":
                            bpy.ops.import_scene.obj(filepath=main_file_path)
                        elif file_format == "blend":
                            # For blend files, we need to append or link
                            with bpy.data.libraries.load(main_file_path, link=False) as (data_from, data_to):
                                data_to.objects = data_from.objects

                            # Link the objects to the scene
                            for obj in data_to.objects:
                                if obj is not None:
                                    bpy.context.collection.objects.link(obj)
                        else:
                            return {"error": f"Unsupported model format: {file_format}"}

                        # Get the names of imported objects
                        imported_objects = [obj.name for obj in bpy.context.selected_objects]

                        return {
                            "success": True,
                            "message": f"Model {asset_id} imported successfully",
                            "imported_objects": imported_objects
                        }
                    except Exception as e:
                        return {"error": f"Failed to import model: {str(e)}"}
                    finally:
                        # Clean up temporary directory
                        with suppress(Exception):
                            shutil.rmtree(temp_dir)
                else:
                    return {"error": f"Requested format or resolution not available for this model"}

            else:
                return {"error": f"Unsupported asset type: {asset_type}"}

        except Exception as e:
            return {"error": f"Failed to download asset: {str(e)}"}

    def set_texture(self, object_name, texture_id):
        """Apply a previously downloaded Polyhaven texture to an object by creating a new material"""
        try:
            # Get the object
            obj = bpy.data.objects.get(object_name)
            if not obj:
                return {"error": f"Object not found: {object_name}"}

            # Make sure object can accept materials
            if not hasattr(obj, 'data') or not hasattr(obj.data, 'materials'):
                return {"error": f"Object {object_name} cannot accept materials"}

            # Find all images related to this texture and ensure they're properly loaded
            texture_images = {}
            for img in bpy.data.images:
                if img.name.startswith(texture_id + "_"):
                    # Extract the map type from the image name
                    map_type = img.name.split('_')[-1].split('.')[0]

                    # Force a reload of the image
                    img.reload()

                    # Ensure proper color space
                    if map_type.lower() in ['color', 'diffuse', 'albedo']:
                        try:
                            img.colorspace_settings.name = 'sRGB'
                        except:
                            pass
                    else:
                        try:
                            img.colorspace_settings.name = 'Non-Color'
                        except:
                            pass

                    # Ensure the image is packed
                    if not img.packed_file:
                        img.pack()

                    texture_images[map_type] = img
                    print(f"Loaded texture map: {map_type} - {img.name}")

                    # Debug info
                    print(f"Image size: {img.size[0]}x{img.size[1]}")
                    print(f"Color space: {img.colorspace_settings.name}")
                    print(f"File format: {img.file_format}")
                    print(f"Is packed: {bool(img.packed_file)}")

            if not texture_images:
                return {"error": f"No texture images found for: {texture_id}. Please download the texture first."}

            # Create a new material
            new_mat_name = f"{texture_id}_material_{object_name}"

            # Remove any existing material with this name to avoid conflicts
            existing_mat = bpy.data.materials.get(new_mat_name)
            if existing_mat:
                bpy.data.materials.remove(existing_mat)

            new_mat = bpy.data.materials.new(name=new_mat_name)
            new_mat.use_nodes = True

            # Set up the material nodes
            nodes = new_mat.node_tree.nodes
            links = new_mat.node_tree.links

            # Clear default nodes
            nodes.clear()

            # Create output node
            output = nodes.new(type='ShaderNodeOutputMaterial')
            output.location = (600, 0)

            # Create principled BSDF node
            principled = nodes.new(type='ShaderNodeBsdfPrincipled')
            principled.location = (300, 0)
            links.new(principled.outputs[0], output.inputs[0])

            # Add texture nodes based on available maps
            tex_coord = nodes.new(type='ShaderNodeTexCoord')
            tex_coord.location = (-800, 0)

            mapping = nodes.new(type='ShaderNodeMapping')
            mapping.location = (-600, 0)
            mapping.vector_type = 'TEXTURE'  # Changed from default 'POINT' to 'TEXTURE'
            links.new(tex_coord.outputs['UV'], mapping.inputs['Vector'])

            # Position offset for texture nodes
            x_pos = -400
            y_pos = 300

            # Connect different texture maps
            for map_type, image in texture_images.items():
                tex_node = nodes.new(type='ShaderNodeTexImage')
                tex_node.location = (x_pos, y_pos)
                tex_node.image = image

                # Set color space based on map type
                if map_type.lower() in ['color', 'diffuse', 'albedo']:
                    try:
                        tex_node.image.colorspace_settings.name = 'sRGB'
                    except:
                        pass  # Use default if sRGB not available
                else:
                    try:
                        tex_node.image.colorspace_settings.name = 'Non-Color'
                    except:
                        pass  # Use default if Non-Color not available

                links.new(mapping.outputs['Vector'], tex_node.inputs['Vector'])

                # Connect to appropriate input on Principled BSDF
                if map_type.lower() in ['color', 'diffuse', 'albedo']:
                    links.new(tex_node.outputs['Color'], principled.inputs['Base Color'])
                elif map_type.lower() in ['roughness', 'rough']:
                    links.new(tex_node.outputs['Color'], principled.inputs['Roughness'])
                elif map_type.lower() in ['metallic', 'metalness', 'metal']:
                    links.new(tex_node.outputs['Color'], principled.inputs['Metallic'])
                elif map_type.lower() in ['normal', 'nor', 'dx', 'gl']:
                    # Add normal map node
                    normal_map = nodes.new(type='ShaderNodeNormalMap')
                    normal_map.location = (x_pos + 200, y_pos)
                    links.new(tex_node.outputs['Color'], normal_map.inputs['Color'])
                    links.new(normal_map.outputs['Normal'], principled.inputs['Normal'])
                elif map_type.lower() in ['displacement', 'disp', 'height']:
                    # Add displacement node
                    disp_node = nodes.new(type='ShaderNodeDisplacement')
                    disp_node.location = (x_pos + 200, y_pos - 200)
                    disp_node.inputs['Scale'].default_value = 0.1  # Reduce displacement strength
                    links.new(tex_node.outputs['Color'], disp_node.inputs['Height'])
                    links.new(disp_node.outputs['Displacement'], output.inputs['Displacement'])

                y_pos -= 250

            # Second pass: Connect nodes with proper handling for special cases
            texture_nodes = {}

            # First find all texture nodes and store them by map type
            for node in nodes:
                if node.type == 'TEX_IMAGE' and node.image:
                    for map_type, image in texture_images.items():
                        if node.image == image:
                            texture_nodes[map_type] = node
                            break

            # Now connect everything using the nodes instead of images
            # Handle base color (diffuse)
            for map_name in ['color', 'diffuse', 'albedo']:
                if map_name in texture_nodes:
                    links.new(texture_nodes[map_name].outputs['Color'], principled.inputs['Base Color'])
                    print(f"Connected {map_name} to Base Color")
                    break

            # Handle roughness
            for map_name in ['roughness', 'rough']:
                if map_name in texture_nodes:
                    links.new(texture_nodes[map_name].outputs['Color'], principled.inputs['Roughness'])
                    print(f"Connected {map_name} to Roughness")
                    break

            # Handle metallic
            for map_name in ['metallic', 'metalness', 'metal']:
                if map_name in texture_nodes:
                    links.new(texture_nodes[map_name].outputs['Color'], principled.inputs['Metallic'])
                    print(f"Connected {map_name} to Metallic")
                    break

            # Handle normal maps
            for map_name in ['gl', 'dx', 'nor']:
                if map_name in texture_nodes:
                    normal_map_node = nodes.new(type='ShaderNodeNormalMap')
                    normal_map_node.location = (100, 100)
                    links.new(texture_nodes[map_name].outputs['Color'], normal_map_node.inputs['Color'])
                    links.new(normal_map_node.outputs['Normal'], principled.inputs['Normal'])
                    print(f"Connected {map_name} to Normal")
                    break

            # Handle displacement
            for map_name in ['displacement', 'disp', 'height']:
                if map_name in texture_nodes:
                    disp_node = nodes.new(type='ShaderNodeDisplacement')
                    disp_node.location = (300, -200)
                    disp_node.inputs['Scale'].default_value = 0.1  # Reduce displacement strength
                    links.new(texture_nodes[map_name].outputs['Color'], disp_node.inputs['Height'])
                    links.new(disp_node.outputs['Displacement'], output.inputs['Displacement'])
                    print(f"Connected {map_name} to Displacement")
                    break

            # Handle ARM texture (Ambient Occlusion, Roughness, Metallic)
            if 'arm' in texture_nodes:
                separate_rgb = nodes.new(type='ShaderNodeSeparateRGB')
                separate_rgb.location = (-200, -100)
                links.new(texture_nodes['arm'].outputs['Color'], separate_rgb.inputs['Image'])

                # Connect Roughness (G) if no dedicated roughness map
                if not any(map_name in texture_nodes for map_name in ['roughness', 'rough']):
                    links.new(separate_rgb.outputs['G'], principled.inputs['Roughness'])
                    print("Connected ARM.G to Roughness")

                # Connect Metallic (B) if no dedicated metallic map
                if not any(map_name in texture_nodes for map_name in ['metallic', 'metalness', 'metal']):
                    links.new(separate_rgb.outputs['B'], principled.inputs['Metallic'])
                    print("Connected ARM.B to Metallic")

                # For AO (R channel), multiply with base color if we have one
                base_color_node = None
                for map_name in ['color', 'diffuse', 'albedo']:
                    if map_name in texture_nodes:
                        base_color_node = texture_nodes[map_name]
                        break

                if base_color_node:
                    mix_node = nodes.new(type='ShaderNodeMixRGB')
                    mix_node.location = (100, 200)
                    mix_node.blend_type = 'MULTIPLY'
                    mix_node.inputs['Fac'].default_value = 0.8  # 80% influence

                    # Disconnect direct connection to base color
                    for link in base_color_node.outputs['Color'].links:
                        if link.to_socket == principled.inputs['Base Color']:
                            links.remove(link)

                    # Connect through the mix node
                    links.new(base_color_node.outputs['Color'], mix_node.inputs[1])
                    links.new(separate_rgb.outputs['R'], mix_node.inputs[2])
                    links.new(mix_node.outputs['Color'], principled.inputs['Base Color'])
                    print("Connected ARM.R to AO mix with Base Color")

            # Handle AO (Ambient Occlusion) if separate
            if 'ao' in texture_nodes:
                base_color_node = None
                for map_name in ['color', 'diffuse', 'albedo']:
                    if map_name in texture_nodes:
                        base_color_node = texture_nodes[map_name]
                        break

                if base_color_node:
                    mix_node = nodes.new(type='ShaderNodeMixRGB')
                    mix_node.location = (100, 200)
                    mix_node.blend_type = 'MULTIPLY'
                    mix_node.inputs['Fac'].default_value = 0.8  # 80% influence

                    # Disconnect direct connection to base color
                    for link in base_color_node.outputs['Color'].links:
                        if link.to_socket == principled.inputs['Base Color']:
                            links.remove(link)

                    # Connect through the mix node
                    links.new(base_color_node.outputs['Color'], mix_node.inputs[1])
                    links.new(texture_nodes['ao'].outputs['Color'], mix_node.inputs[2])
                    links.new(mix_node.outputs['Color'], principled.inputs['Base Color'])
                    print("Connected AO to mix with Base Color")

            # CRITICAL: Make sure to clear all existing materials from the object
            while len(obj.data.materials) > 0:
                obj.data.materials.pop(index=0)

            # Assign the new material to the object
            obj.data.materials.append(new_mat)

            # CRITICAL: Make the object active and select it
            bpy.context.view_layer.objects.active = obj
            obj.select_set(True)

            # CRITICAL: Force Blender to update the material
            bpy.context.view_layer.update()

            # Get the list of texture maps
            texture_maps = list(texture_images.keys())

            # Get info about texture nodes for debugging
            material_info = {
                "name": new_mat.name,
                "has_nodes": new_mat.use_nodes,
                "node_count": len(new_mat.node_tree.nodes),
                "texture_nodes": []
            }

            for node in new_mat.node_tree.nodes:
                if node.type == 'TEX_IMAGE' and node.image:
                    connections = []
                    for output in node.outputs:
                        for link in output.links:
                            connections.append(f"{output.name} → {link.to_node.name}.{link.to_socket.name}")

                    material_info["texture_nodes"].append({
                        "name": node.name,
                        "image": node.image.name,
                        "colorspace": node.image.colorspace_settings.name,
                        "connections": connections
                    })

            return {
                "success": True,
                "message": f"Created new material and applied texture {texture_id} to {object_name}",
                "material": new_mat.name,
                "maps": texture_maps,
                "material_info": material_info
            }

        except Exception as e:
            print(f"Error in set_texture: {str(e)}")
            traceback.print_exc()
            return {"error": f"Failed to apply texture: {str(e)}"}

    def get_polyhaven_status(self):
        """Get the current status of PolyHaven integration"""
        enabled = bpy.context.scene.blendermcp_use_polyhaven
        if enabled:
            return {"enabled": True, "message": "PolyHaven integration is enabled and ready to use."}
        else:
            return {
                "enabled": False,
                "message": """PolyHaven integration is currently disabled. To enable it:
                            1. In the 3D Viewport, find the BlenderMCP panel in the sidebar (press N if hidden)
                            2. Check the 'Use assets from Poly Haven' checkbox
                            3. Restart the connection to Claude"""
        }

    #region Hyper3D
    def get_hyper3d_status(self):
        """Get the current status of Hyper3D Rodin integration"""
        enabled = bpy.context.scene.blendermcp_use_hyper3d
        if enabled:
            if not bpy.context.scene.blendermcp_hyper3d_api_key:
                return {
                    "enabled": False,
                    "message": """Hyper3D Rodin integration is currently enabled, but API key is not given. To enable it:
                                1. In the 3D Viewport, find the BlenderMCP panel in the sidebar (press N if hidden)
                                2. Keep the 'Use Hyper3D Rodin 3D model generation' checkbox checked
                                3. Choose the right plaform and fill in the API Key
                                4. Restart the connection to Claude"""
                }
            mode = bpy.context.scene.blendermcp_hyper3d_mode
            message = f"Hyper3D Rodin integration is enabled and ready to use. Mode: {mode}. " + \
                f"Key type: {'private' if bpy.context.scene.blendermcp_hyper3d_api_key != RODIN_FREE_TRIAL_KEY else 'free_trial'}"
            return {
                "enabled": True,
                "message": message
            }
        else:
            return {
                "enabled": False,
                "message": """Hyper3D Rodin integration is currently disabled. To enable it:
                            1. In the 3D Viewport, find the BlenderMCP panel in the sidebar (press N if hidden)
                            2. Check the 'Use Hyper3D Rodin 3D model generation' checkbox
                            3. Restart the connection to Claude"""
            }

    def create_rodin_job(self, *args, **kwargs):
        match bpy.context.scene.blendermcp_hyper3d_mode:
            case "MAIN_SITE":
                return self.create_rodin_job_main_site(*args, **kwargs)
            case "FAL_AI":
                return self.create_rodin_job_fal_ai(*args, **kwargs)
            case _:
                return f"Error: Unknown Hyper3D Rodin mode!"

    def create_rodin_job_main_site(
            self,
            text_prompt: str=None,
            images: list[tuple[str, str]]=None,
            bbox_condition=None
        ):
        try:
            if images is None:
                images = []
            """Call Rodin API, get the job uuid and subscription key"""
            files = [
                *[("images", (f"{i:04d}{img_suffix}", img)) for i, (img_suffix, img) in enumerate(images)],
                ("tier", (None, "Sketch")),
                ("mesh_mode", (None, "Raw")),
            ]
            if text_prompt:
                files.append(("prompt", (None, text_prompt)))
            if bbox_condition:
                files.append(("bbox_condition", (None, json.dumps(bbox_condition))))
            response = requests.post(
                "https://hyperhuman.deemos.com/api/v2/rodin",
                headers={
                    "Authorization": f"Bearer {bpy.context.scene.blendermcp_hyper3d_api_key}",
                },
                files=files
            )
            data = response.json()
            return data
        except Exception as e:
            return {"error": str(e)}

    def create_rodin_job_fal_ai(
            self,
            text_prompt: str=None,
            images: list[tuple[str, str]]=None,
            bbox_condition=None
        ):
        try:
            req_data = {
                "tier": "Sketch",
            }
            if images:
                req_data["input_image_urls"] = images
            if text_prompt:
                req_data["prompt"] = text_prompt
            if bbox_condition:
                req_data["bbox_condition"] = bbox_condition
            response = requests.post(
                "https://queue.fal.run/fal-ai/hyper3d/rodin",
                headers={
                    "Authorization": f"Key {bpy.context.scene.blendermcp_hyper3d_api_key}",
                    "Content-Type": "application/json",
                },
                json=req_data
            )
            data = response.json()
            return data
        except Exception as e:
            return {"error": str(e)}

    def poll_rodin_job_status(self, *args, **kwargs):
        match bpy.context.scene.blendermcp_hyper3d_mode:
            case "MAIN_SITE":
                return self.poll_rodin_job_status_main_site(*args, **kwargs)
            case "FAL_AI":
                return self.poll_rodin_job_status_fal_ai(*args, **kwargs)
            case _:
                return f"Error: Unknown Hyper3D Rodin mode!"

    def poll_rodin_job_status_main_site(self, subscription_key: str):
        """Call the job status API to get the job status"""
        response = requests.post(
            "https://hyperhuman.deemos.com/api/v2/status",
            headers={
                "Authorization": f"Bearer {bpy.context.scene.blendermcp_hyper3d_api_key}",
            },
            json={
                "subscription_key": subscription_key,
            },
        )
        data = response.json()
        return {
            "status_list": [i["status"] for i in data["jobs"]]
        }

    def poll_rodin_job_status_fal_ai(self, request_id: str):
        """Call the job status API to get the job status"""
        response = requests.get(
            f"https://queue.fal.run/fal-ai/hyper3d/requests/{request_id}/status",
            headers={
                "Authorization": f"KEY {bpy.context.scene.blendermcp_hyper3d_api_key}",
            },
        )
        data = response.json()
        return data

    @staticmethod
    def _clean_imported_glb(filepath, mesh_name=None):
        # Get the set of existing objects before import
        existing_objects = set(bpy.data.objects)

        # Import the GLB file
        bpy.ops.import_scene.gltf(filepath=filepath)

        # Ensure the context is updated
        bpy.context.view_layer.update()

        # Get all imported objects
        imported_objects = list(set(bpy.data.objects) - existing_objects)
        # imported_objects = [obj for obj in bpy.context.view_layer.objects if obj.select_get()]

        if not imported_objects:
            print("Error: No objects were imported.")
            return

        # Identify the mesh object
        mesh_obj = None

        if len(imported_objects) == 1 and imported_objects[0].type == 'MESH':
            mesh_obj = imported_objects[0]
            print("Single mesh imported, no cleanup needed.")
        else:
            if len(imported_objects) == 2:
                empty_objs = [i for i in imported_objects if i.type == "EMPTY"]
                if len(empty_objs) != 1:
                    print("Error: Expected an empty node with one mesh child or a single mesh object.")
                    return
                parent_obj = empty_objs.pop()
                if len(parent_obj.children) == 1:
                    potential_mesh = parent_obj.children[0]
                    if potential_mesh.type == 'MESH':
                        print("GLB structure confirmed: Empty node with one mesh child.")

                        # Unparent the mesh from the empty node
                        potential_mesh.parent = None

                        # Remove the empty node
                        bpy.data.objects.remove(parent_obj)
                        print("Removed empty node, keeping only the mesh.")

                        mesh_obj = potential_mesh
                    else:
                        print("Error: Child is not a mesh object.")
                        return
                else:
                    print("Error: Expected an empty node with one mesh child or a single mesh object.")
                    return
            else:
                print("Error: Expected an empty node with one mesh child or a single mesh object.")
                return

        # Rename the mesh if needed
        try:
            if mesh_obj and mesh_obj.name is not None and mesh_name:
                mesh_obj.name = mesh_name
                if mesh_obj.data.name is not None:
                    mesh_obj.data.name = mesh_name
                print(f"Mesh renamed to: {mesh_name}")
        except Exception as e:
            print("Having issue with renaming, give up renaming.")

        return mesh_obj

    def import_generated_asset(self, *args, **kwargs):
        match bpy.context.scene.blendermcp_hyper3d_mode:
            case "MAIN_SITE":
                return self.import_generated_asset_main_site(*args, **kwargs)
            case "FAL_AI":
                return self.import_generated_asset_fal_ai(*args, **kwargs)
            case _:
                return f"Error: Unknown Hyper3D Rodin mode!"

    def import_generated_asset_main_site(self, task_uuid: str, name: str):
        """Fetch the generated asset, import into blender"""
        response = requests.post(
            "https://hyperhuman.deemos.com/api/v2/download",
            headers={
                "Authorization": f"Bearer {bpy.context.scene.blendermcp_hyper3d_api_key}",
            },
            json={
                'task_uuid': task_uuid
            }
        )
        data_ = response.json()
        temp_file = None
        for i in data_["list"]:
            if i["name"].endswith(".glb"):
                temp_file = tempfile.NamedTemporaryFile(
                    delete=False,
                    prefix=task_uuid,
                    suffix=".glb",
                )

                try:
                    # Download the content
                    response = requests.get(i["url"], stream=True)
                    response.raise_for_status()  # Raise an exception for HTTP errors

                    # Write the content to the temporary file
                    for chunk in response.iter_content(chunk_size=8192):
                        temp_file.write(chunk)

                    # Close the file
                    temp_file.close()

                except Exception as e:
                    # Clean up the file if there's an error
                    temp_file.close()
                    os.unlink(temp_file.name)
                    return {"succeed": False, "error": str(e)}

                break
        else:
            return {"succeed": False, "error": "Generation failed. Please first make sure that all jobs of the task are done and then try again later."}

        try:
            obj = self._clean_imported_glb(
                filepath=temp_file.name,
                mesh_name=name
            )
            result = {
                "name": obj.name,
                "type": obj.type,
                "location": [obj.location.x, obj.location.y, obj.location.z],
                "rotation": [obj.rotation_euler.x, obj.rotation_euler.y, obj.rotation_euler.z],
                "scale": [obj.scale.x, obj.scale.y, obj.scale.z],
            }

            if obj.type == "MESH":
                bounding_box = self._get_aabb(obj)
                result["world_bounding_box"] = bounding_box

            return {
                "succeed": True, **result
            }
        except Exception as e:
            return {"succeed": False, "error": str(e)}

    def import_generated_asset_fal_ai(self, request_id: str, name: str):
        """Fetch the generated asset, import into blender"""
        response = requests.get(
            f"https://queue.fal.run/fal-ai/hyper3d/requests/{request_id}",
            headers={
                "Authorization": f"Key {bpy.context.scene.blendermcp_hyper3d_api_key}",
            }
        )
        data_ = response.json()
        temp_file = None

        temp_file = tempfile.NamedTemporaryFile(
            delete=False,
            prefix=request_id,
            suffix=".glb",
        )

        try:
            # Download the content
            response = requests.get(data_["model_mesh"]["url"], stream=True)
            response.raise_for_status()  # Raise an exception for HTTP errors

            # Write the content to the temporary file
            for chunk in response.iter_content(chunk_size=8192):
                temp_file.write(chunk)

            # Close the file
            temp_file.close()

        except Exception as e:
            # Clean up the file if there's an error
            temp_file.close()
            os.unlink(temp_file.name)
            return {"succeed": False, "error": str(e)}

        try:
            obj = self._clean_imported_glb(
                filepath=temp_file.name,
                mesh_name=name
            )
            result = {
                "name": obj.name,
                "type": obj.type,
                "location": [obj.location.x, obj.location.y, obj.location.z],
                "rotation": [obj.rotation_euler.x, obj.rotation_euler.y, obj.rotation_euler.z],
                "scale": [obj.scale.x, obj.scale.y, obj.scale.z],
            }

            if obj.type == "MESH":
                bounding_box = self._get_aabb(obj)
                result["world_bounding_box"] = bounding_box

            return {
                "succeed": True, **result
            }
        except Exception as e:
            return {"succeed": False, "error": str(e)}
    #endregion

    #region Sketchfab API
    def get_sketchfab_status(self):
        """Get the current status of Sketchfab integration"""
        enabled = bpy.context.scene.blendermcp_use_sketchfab
        api_key = bpy.context.scene.blendermcp_sketchfab_api_key

        # Test the API key if present
        if api_key:
            try:
                headers = {
                    "Authorization": f"Token {api_key}"
                }

                response = requests.get(
                    "https://api.sketchfab.com/v3/me",
                    headers=headers,
                    timeout=30  # Add timeout of 30 seconds
                )

                if response.status_code == 200:
                    user_data = response.json()
                    username = user_data.get("username", "Unknown user")
                    return {
                        "enabled": True,
                        "message": f"Sketchfab integration is enabled and ready to use. Logged in as: {username}"
                    }
                else:
                    return {
                        "enabled": False,
                        "message": f"Sketchfab API key seems invalid. Status code: {response.status_code}"
                    }
            except requests.exceptions.Timeout:
                return {
                    "enabled": False,
                    "message": "Timeout connecting to Sketchfab API. Check your internet connection."
                }
            except Exception as e:
                return {
                    "enabled": False,
                    "message": f"Error testing Sketchfab API key: {str(e)}"
                }

        if enabled and api_key:
            return {"enabled": True, "message": "Sketchfab integration is enabled and ready to use."}
        elif enabled and not api_key:
            return {
                "enabled": False,
                "message": """Sketchfab integration is currently enabled, but API key is not given. To enable it:
                            1. In the 3D Viewport, find the BlenderMCP panel in the sidebar (press N if hidden)
                            2. Keep the 'Use Sketchfab' checkbox checked
                            3. Enter your Sketchfab API Key
                            4. Restart the connection to Claude"""
            }
        else:
            return {
                "enabled": False,
                "message": """Sketchfab integration is currently disabled. To enable it:
                            1. In the 3D Viewport, find the BlenderMCP panel in the sidebar (press N if hidden)
                            2. Check the 'Use assets from Sketchfab' checkbox
                            3. Enter your Sketchfab API Key
                            4. Restart the connection to Claude"""
            }

    def search_sketchfab_models(self, query, categories=None, count=20, downloadable=True):
        """Search for models on Sketchfab based on query and optional filters"""
        try:
            api_key = bpy.context.scene.blendermcp_sketchfab_api_key
            if not api_key:
                return {"error": "Sketchfab API key is not configured"}

            # Build search parameters with exact fields from Sketchfab API docs
            params = {
                "type": "models",
                "q": query,
                "count": count,
                "downloadable": downloadable,
                "archives_flavours": False
            }

            if categories:
                params["categories"] = categories

            # Make API request to Sketchfab search endpoint
            # The proper format according to Sketchfab API docs for API key auth
            headers = {
                "Authorization": f"Token {api_key}"
            }


            # Use the search endpoint as specified in the API documentation
            response = requests.get(
                "https://api.sketchfab.com/v3/search",
                headers=headers,
                params=params,
                timeout=30  # Add timeout of 30 seconds
            )

            if response.status_code == 401:
                return {"error": "Authentication failed (401). Check your API key."}

            if response.status_code != 200:
                return {"error": f"API request failed with status code {response.status_code}"}

            response_data = response.json()

            # Safety check on the response structure
            if response_data is None:
                return {"error": "Received empty response from Sketchfab API"}

            # Handle 'results' potentially missing from response
            results = response_data.get("results", [])
            if not isinstance(results, list):
                return {"error": f"Unexpected response format from Sketchfab API: {response_data}"}

            return response_data

        except requests.exceptions.Timeout:
            return {"error": "Request timed out. Check your internet connection."}
        except json.JSONDecodeError as e:
            return {"error": f"Invalid JSON response from Sketchfab API: {str(e)}"}
        except Exception as e:
            import traceback
            traceback.print_exc()
            return {"error": str(e)}

    def download_sketchfab_model(self, uid):
        """Download a model from Sketchfab by its UID"""
        try:
            api_key = bpy.context.scene.blendermcp_sketchfab_api_key
            if not api_key:
                return {"error": "Sketchfab API key is not configured"}

            # Use proper authorization header for API key auth
            headers = {
                "Authorization": f"Token {api_key}"
            }

            # Request download URL using the exact endpoint from the documentation
            download_endpoint = f"https://api.sketchfab.com/v3/models/{uid}/download"

            response = requests.get(
                download_endpoint,
                headers=headers,
                timeout=30  # Add timeout of 30 seconds
            )

            if response.status_code == 401:
                return {"error": "Authentication failed (401). Check your API key."}

            if response.status_code != 200:
                return {"error": f"Download request failed with status code {response.status_code}"}

            data = response.json()

            # Safety check for None data
            if data is None:
                return {"error": "Received empty response from Sketchfab API for download request"}

            # Extract download URL with safety checks
            gltf_data = data.get("gltf")
            if not gltf_data:
                return {"error": "No gltf download URL available for this model. Response: " + str(data)}

            download_url = gltf_data.get("url")
            if not download_url:
                return {"error": "No download URL available for this model. Make sure the model is downloadable and you have access."}

            # Download the model (already has timeout)
            model_response = requests.get(download_url, timeout=60)  # 60 second timeout

            if model_response.status_code != 200:
                return {"error": f"Model download failed with status code {model_response.status_code}"}

            # Save to temporary file
            temp_dir = tempfile.mkdtemp()
            zip_file_path = os.path.join(temp_dir, f"{uid}.zip")

            with open(zip_file_path, "wb") as f:
                f.write(model_response.content)

            # Extract the zip file with enhanced security
            with zipfile.ZipFile(zip_file_path, 'r') as zip_ref:
                # More secure zip slip prevention
                for file_info in zip_ref.infolist():
                    # Get the path of the file
                    file_path = file_info.filename

                    # Convert directory separators to the current OS style
                    # This handles both / and \ in zip entries
                    target_path = os.path.join(temp_dir, os.path.normpath(file_path))

                    # Get absolute paths for comparison
                    abs_temp_dir = os.path.abspath(temp_dir)
                    abs_target_path = os.path.abspath(target_path)

                    # Ensure the normalized path doesn't escape the target directory
                    if not abs_target_path.startswith(abs_temp_dir):
                        with suppress(Exception):
                            shutil.rmtree(temp_dir)
                        return {"error": "Security issue: Zip contains files with path traversal attempt"}

                    # Additional explicit check for directory traversal
                    if ".." in file_path:
                        with suppress(Exception):
                            shutil.rmtree(temp_dir)
                        return {"error": "Security issue: Zip contains files with directory traversal sequence"}

                # If all files passed security checks, extract them
                zip_ref.extractall(temp_dir)

            # Find the main glTF file
            gltf_files = [f for f in os.listdir(temp_dir) if f.endswith('.gltf') or f.endswith('.glb')]

            if not gltf_files:
                with suppress(Exception):
                    shutil.rmtree(temp_dir)
                return {"error": "No glTF file found in the downloaded model"}

            main_file = os.path.join(temp_dir, gltf_files[0])

            # Import the model
            bpy.ops.import_scene.gltf(filepath=main_file)

            # Get the names of imported objects
            imported_objects = [obj.name for obj in bpy.context.selected_objects]

            # Clean up temporary files
            with suppress(Exception):
                shutil.rmtree(temp_dir)

            return {
                "success": True,
                "message": "Model imported successfully",
                "imported_objects": imported_objects
            }

        except requests.exceptions.Timeout:
            return {"error": "Request timed out. Check your internet connection and try again with a simpler model."}
        except json.JSONDecodeError as e:
            return {"error": f"Invalid JSON response from Sketchfab API: {str(e)}"}
        except Exception as e:
            import traceback
            traceback.print_exc()
            return {"error": f"Failed to download model: {str(e)}"}
    #endregion

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
