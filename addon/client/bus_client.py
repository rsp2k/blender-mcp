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
        refresh_token: str = "",
        jwt_expires_at: int = 0,
        label: Optional[str] = None,
        bus_id: Optional[str] = None,
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
        self.label = label
        # Phase I7: empty string / None → server defaults to user's personal
        # bus. Non-empty string targets a specific (shared) bus_id from
        # bus_list_buses. Set via prefs.default_bus_id.
        self.bus_id = bus_id or None
        # Reference to the BlenderCommandExecutor instance so dispatched
        # scripts can call into the polyhaven/hyper3d/etc. helpers as
        # `executor.search_polyhaven_assets(...)`.
        self.executor = executor
        # Refresh-flow state: refresh_token + epoch-seconds expiry. When
        # populated, the worker pre-emptively rotates the JWT ~60s before
        # expiry to avoid mid-session 401 wedges. Empty refresh_token =>
        # rotation disabled (best-effort backward compat for older prefs).
        self.refresh_token = refresh_token
        self.jwt_expires_at = jwt_expires_at  # 0 = unknown, treat as no-rotate

        self.client: Optional[Any] = None   # fastmcp.Client (inside worker loop)
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.thread: Optional[threading.Thread] = None
        self.running = False
        self.connected = False
        self.last_error: Optional[str] = None
        # Set by _refresh_watcher to signal that _run should tear down the
        # current FastMCP Client and reopen with the rotated JWT. Cleared
        # after reconnect completes.
        self._rotate_requested = False

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

    # --- JWT auto-rotation -------------------------------------------------

    async def _do_refresh_once(self) -> bool:
        """Rotate the access token. Returns True on success, False on failure.

        Chooses the refresh endpoint based on which login path produced the
        current token:
          - If ``oauth_client_id`` is set in prefs → OAuth /mcp/token endpoint
          - Otherwise → legacy /auth/refresh (only valid for
            AUTH_BACKEND=inmemory dev servers)

        Used by both the pre-connect rescue path and the periodic watcher.
        Does NOT set _rotate_requested — that's the caller's job.
        """
        import time

        from ..auth import LoginError, OAuthError
        from ..auth import refresh_token as do_legacy_refresh
        from ..auth import refresh_oauth_token
        from ..preferences import get_prefs

        prefs = get_prefs()
        oauth_client_id = getattr(prefs, "oauth_client_id", "") or ""

        try:
            if oauth_client_id:
                payload = await asyncio.to_thread(
                    refresh_oauth_token,
                    self.server_url,
                    self.refresh_token,
                    oauth_client_id,
                )
            else:
                payload = await asyncio.to_thread(
                    do_legacy_refresh, self.server_url, self.refresh_token,
                )
        except (LoginError, OAuthError) as e:
            self.last_error = f"JWT refresh failed: {e}"
            print(f"[BlenderMCP] {self.last_error} — re-Login required")
            return False
        except Exception as e:
            self.last_error = f"JWT refresh crashed: {e}"
            print(f"[BlenderMCP] {self.last_error}")
            return False

        new_jwt = payload.get("access_token", "")
        new_expires_in = int(payload.get("expires_in", 0))
        if not new_jwt or not new_expires_in:
            self.last_error = "Refresh response malformed"
            print(f"[BlenderMCP] {self.last_error}")
            return False

        # OAuth refresh rotates the refresh_token too (per spec — old one
        # is immediately invalidated). Capture the new one if returned.
        new_refresh = payload.get("refresh_token")
        if new_refresh:
            self.refresh_token = new_refresh

        self.jwt_token = new_jwt
        self.jwt_expires_at = int(time.time()) + new_expires_in
        self._persist_rotated_jwt_to_prefs()
        print(f"[BlenderMCP] JWT rotated; new exp in {new_expires_in}s")
        return True

    async def _refresh_watcher(self) -> None:
        """Wake ~60s before JWT expiry, swap in a fresh access token.

        Self-cancels via ``self.running``. On refresh success: sets
        ``_rotate_requested`` to bounce the FastMCP Client onto the new
        bearer. On refresh failure: gives up (LoginError) or sleeps + retries
        (network blip).
        """
        import time

        while self.running:
            if not self.jwt_expires_at:
                # Unknown expiry — best-effort sleep for an hour and recheck.
                # Avoids busy-looping when started with stale/missing prefs.
                await asyncio.sleep(3600)
                continue

            # Refresh 60s before the server would reject; tolerate slow clocks.
            now = int(time.time())
            seconds_until_refresh = max(1, self.jwt_expires_at - now - 60)
            await asyncio.sleep(seconds_until_refresh)
            if not self.running:
                return

            if await self._do_refresh_once():
                self._rotate_requested = True
                continue

            # Refresh failed. LoginError-class failures (refresh token itself
            # expired) are fatal — bus connection is dead in the water without
            # a way to re-auth. Transient failures (network blip) leave
            # last_error populated but we'll retry in 30s instead of killing
            # the session.
            if "JWT refresh failed" in (self.last_error or ""):
                self.running = False
                return
            await asyncio.sleep(30)

    def _persist_rotated_jwt_to_prefs(self) -> None:
        """Best-effort save of the rotated JWT/expiry to AddonPreferences.

        Called from the worker thread; bpy is documented as not thread-safe
        for many ops, but AddonPreferences writes through a property descriptor
        that's fine to set from any thread (no mesh/scene mutation involved).
        Wrapped in a try anyway so a write failure can't kill the worker.
        """
        try:
            from ..preferences import get_prefs
            prefs = get_prefs()
            prefs.jwt_token = self.jwt_token
            prefs.jwt_expires_at = str(self.jwt_expires_at)
            # Persist the rotated refresh token too (OAuth refresh rotates
            # the refresh_token per spec; without persisting we'd lose the
            # new one across Blender restarts).
            if self.refresh_token:
                prefs.refresh_token = self.refresh_token
        except Exception as e:
            print(f"[BlenderMCP] Could not persist rotated JWT to prefs: {e}")

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
        """Connect, register, subscribe to log notifications, await stop.

        Outer loop reconnects on:
          - JWT rotation (``_rotate_requested`` set by ``_refresh_watcher``)
          - Transient connection failure (server restart, network blip)
        with exponential backoff (1s → 30s cap), reset to 1s on every
        successful registration. Auth-fatal errors (401/403/Unauthorized
        in the exception text) skip the retry loop and exit so the user
        knows to re-Login — burning CPU against a token that will never
        be accepted is worse than failing visibly.

        Before the very first connect, if the stored JWT is past (or near)
        its expiry but a refresh_token is on hand, rotate proactively. This
        is the "Blender restart after long session" case: prefs have a stale
        access token saved from the prior run, but the refresh token is still
        within its 7-day window. Refreshing here means the user doesn't see
        a 401 from the bus client and isn't forced to re-Login.
        """
        if self.refresh_token and self.jwt_expires_at:
            import time
            if self.jwt_expires_at <= int(time.time()) + 60:
                print("[BlenderMCP] Stored JWT is stale; refreshing before connect")
                if not await self._do_refresh_once():
                    return  # last_error already set + printed

        backoff = 1.0
        BACKOFF_MAX = 30.0
        refresh_task: Optional[asyncio.Task] = None
        try:
            while self.running:
                self._rotate_requested = False
                transport = StreamableHttpTransport(
                    url=self.server_url,
                    headers={"Authorization": f"Bearer {self.jwt_token}"},
                )

                try:
                    async with FastMCPClient(
                        transport, message_handler=self._on_message,
                    ) as client:
                        self.client = client

                        try:
                            await client.set_logging_level("debug")
                        except Exception as e:
                            print(f"[BlenderMCP] set_logging_level failed: {e}")

                        try:
                            reg_args = {
                                "client_uuid": self.client_uuid,
                                "client_type": "blender",
                                "is_persistent": True,
                                "capabilities": [
                                    "python_execution", "modeling", "rendering",
                                    "scene_management", "asset_processing",
                                ],
                            }
                            # Only send `label` if we have one — the server
                            # treats None on re-registration as "keep the
                            # existing label."
                            if self.label:
                                reg_args["label"] = self.label
                            if self.bus_id:
                                reg_args["bus_id"] = self.bus_id
                            await client.call_tool("blender_register_client", reg_args)
                            self.connected = True
                            backoff = 1.0  # successful registration → reset backoff
                            self.last_error = None
                            print(f"[BlenderMCP] Registered as {self.client_uuid}")
                        except Exception as e:
                            # Register failed but transport is up. Fall through
                            # to the outer except via a raise — same backoff +
                            # reconnect logic handles both.
                            self.last_error = f"register_client failed: {e}"
                            print(f"[BlenderMCP] {self.last_error}")
                            raise

                        # Start the refresh watcher (only one — it runs across
                        # the lifetime of the BlenderMCPClient, not per-reconnect).
                        if refresh_task is None and self.refresh_token:
                            refresh_task = asyncio.create_task(self._refresh_watcher())

                        # Wait until stop() flips `running` OR rotation is requested.
                        while self.running and not self._rotate_requested:
                            await asyncio.sleep(0.2)

                        # If we're rotating, don't unregister (we'll re-register
                        # under the new JWT in the next iteration). Only unregister
                        # on full shutdown.
                        if not self.running:
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

                    # Auth-fatal exceptions: don't retry, surface to user so
                    # they re-Login. Heuristic — FastMCP/httpx exceptions
                    # carry status codes as part of the message text.
                    msg = str(e).lower()
                    if any(s in msg for s in ("401", "403", "unauthorized", "invalid_token")):
                        print("[BlenderMCP] Auth failure — stopping retries; please re-Login")
                        self.running = False
                        return
                finally:
                    self.connected = False
                    self.client = None

                if not self.running:
                    break
                # Reconnect path — sleep with exponential backoff, then loop.
                # _rotate_requested takes precedence: if the watcher rotated
                # the JWT mid-failure, skip the backoff and reconnect now.
                if self._rotate_requested:
                    print("[BlenderMCP] Reconnecting with rotated JWT")
                else:
                    print(f"[BlenderMCP] Reconnecting in {backoff:.0f}s")
                    sleep_remaining = backoff
                    # Sleep in 0.5s chunks so stop() takes effect promptly
                    # without making the user wait for the full backoff.
                    while sleep_remaining > 0 and self.running:
                        chunk = min(0.5, sleep_remaining)
                        await asyncio.sleep(chunk)
                        sleep_remaining -= chunk
                    backoff = min(backoff * 2, BACKOFF_MAX)

                if self._rotate_requested and self.running:
                    print(f"[BlenderMCP] Reconnecting with rotated JWT (exp={self.jwt_expires_at})")
        finally:
            if refresh_task is not None:
                refresh_task.cancel()
