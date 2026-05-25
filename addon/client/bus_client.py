"""BlenderMCPClient — lifecycle + asyncio loop.

The class owns connection state (server URL, JWT, FastMCP client, worker
thread, event loop, priority queue) and orchestrates startup/shutdown.
Per-message logic lives in sibling modules:

    message_pump.handle_message  — filter incoming notifications
    drainer.drain_queue          — main-thread timer callback
    job_reporter.submit_job_update — reply path

The class methods that Blender's timer registers (`_drain_queue`) and
that FastMCP calls back (`_on_message`) are thin shims that delegate
to those functions — that's what lets the heavy lifting be unit-tested
with a stub client.
"""

from __future__ import annotations

import asyncio
import threading
import traceback
from typing import Any, Optional

import bpy

try:
    from fastmcp import Client as FastMCPClient
    from fastmcp.client.transports import StreamableHttpTransport
    FASTMCP_AVAILABLE = True
except ImportError:
    FASTMCP_AVAILABLE = False
    FastMCPClient = None  # type: ignore[assignment]
    StreamableHttpTransport = None  # type: ignore[assignment]
    print("[BlenderMCP] fastmcp not installed - run: <blender_python> -m pip install fastmcp")


class BlenderMCPClient:
    """FastMCP client subscribed to the server's _message_bus log channel."""

    def __init__(
        self,
        server_url: str,
        jwt_token: str,
        client_uuid: str,
        *,
        executor: Optional[Any] = None,
    ) -> None:
        # Canonicalize: FastAPI mounts the FastMCP ASGI app at /mcp, which
        # 307-redirects /mcp -> /mcp/. httpx (under fastmcp.Client) strips
        # the Authorization header on redirect by default, so the redirected
        # request hits the JWT middleware with no Bearer and returns 401.
        # Append the trailing slash up front so the first request hits the
        # mount directly.
        if server_url.endswith("/mcp"):
            server_url = server_url + "/"
        self.server_url = server_url
        self.jwt_token = jwt_token
        self.client_uuid = client_uuid
        # Reference to the BlenderCommandExecutor instance so dispatched
        # scripts can call into the polyhaven/hyper3d/etc. helpers as
        # `executor.search_polyhaven_assets(...)`.
        self.executor = executor

        self.client: Optional[Any] = None   # fastmcp.Client (inside worker loop)
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.thread: Optional[threading.Thread] = None
        self.running = False
        self.connected = False
        self.last_error: Optional[str] = None

        # Priority queue: heap of (priority_int, timestamp, job_payload).
        self.job_queue: list = []
        self.queue_lock = threading.Lock()
        self.active_jobs: dict = {}
        self._timer_registered = False

    # --- Lifecycle ----------------------------------------------------------

    def start(self) -> None:
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

        # Register the queue drain timer on Blender's main thread.
        if not self._timer_registered:
            bpy.app.timers.register(self._drain_queue, first_interval=0.1)
            self._timer_registered = True

        print(f"[BlenderMCP] Client starting: {self.client_uuid} -> {self.server_url}")

    def stop(self) -> None:
        """Signal worker to exit, then join."""
        self.running = False
        self.connected = False

        # Wake the worker loop so its `while self.running` exits promptly.
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
        self._timer_registered = False  # timer self-removes when running flips
        print(f"[BlenderMCP] Client stopped: {self.client_uuid}")

    # --- Delegated callbacks (called by Blender / FastMCP) -----------------

    async def _on_message(self, message: Any) -> None:
        # Import inside to avoid a hot import-time cycle:
        # bus_client <-> message_pump (which TYPE_CHECKING-imports us back).
        from .message_pump import handle_message
        await handle_message(self, message)

    def _drain_queue(self) -> Optional[float]:
        from .drainer import drain_queue
        return drain_queue(self)

    # --- Worker thread / asyncio loop --------------------------------------

    def _thread_main(self) -> None:
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

    async def _run(self) -> None:
        """Connect, register, subscribe to log notifications, await stop."""
        transport = StreamableHttpTransport(
            url=self.server_url,
            headers={"Authorization": f"Bearer {self.jwt_token}"},
        )

        try:
            async with FastMCPClient(transport, message_handler=self._on_message) as client:
                self.client = client

                # Subscribe to all priority levels.
                try:
                    await client.set_logging_level("debug")
                except Exception as e:
                    print(f"[BlenderMCP] set_logging_level failed: {e}")

                # Register with server.
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

                # Wait until stop() flips `running`.
                while self.running:
                    await asyncio.sleep(0.2)

                # Graceful unregister.
                try:
                    await client.call_tool(
                        "blender_unregister_client",
                        {"client_uuid": self.client_uuid},
                    )
                except Exception as e:
                    print(f"[BlenderMCP] unregister_client failed: {e}")
        except Exception as e:
            self.last_error = f"Connection failed: {e}"
            print(f"[BlenderMCP] {self.last_error}")
        finally:
            self.connected = False
            self.client = None
