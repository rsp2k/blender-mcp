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
import collections
import threading
import traceback
from typing import Any, Optional

import bpy

FastMCPClient = None  # type: ignore[assignment]
StreamableHttpTransport = None  # type: ignore[assignment]
FASTMCP_AVAILABLE = False
FASTMCP_IMPORT_ERROR: Optional[str] = None
_FASTMCP_RETRY_S = 30.0
_fastmcp_last_attempt = 0.0


def _root_import_error(exc: BaseException) -> str:
    # fastmcp re-wraps dependency failures as "client support is not
    # installed"; the innermost ImportError names the module that broke.
    innermost = exc
    seen = set()
    cur = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, ImportError):
            innermost = cur
        cur = cur.__cause__ or cur.__context__
    return f"{type(innermost).__name__}: {innermost}"


def ensure_fastmcp(force: bool = False) -> bool:
    """Import fastmcp on first need; retry failed imports at most every 30 s.

    A first install inside a running Blender can't load the bundled
    wheels: Blender imported its own older typing_extensions at startup
    and appends the extension site-packages after its own, so a fastmcp
    dependency fails on `from typing_extensions import sentinel`. A fresh
    start puts the extension site-packages first and everything loads.
    Swapping modules under the live interpreter proved unsafe, so this
    only retries and records the real error for the UI.
    """
    global FastMCPClient, StreamableHttpTransport, FASTMCP_AVAILABLE
    global FASTMCP_IMPORT_ERROR, _fastmcp_last_attempt
    if FASTMCP_AVAILABLE:
        return True
    import time as _time
    now = _time.monotonic()
    if not force and _fastmcp_last_attempt and now - _fastmcp_last_attempt < _FASTMCP_RETRY_S:
        return False
    _fastmcp_last_attempt = now
    try:
        from fastmcp import Client as _client_cls
        from fastmcp.client.transports import StreamableHttpTransport as _transport_cls
    except ImportError as e:
        FASTMCP_IMPORT_ERROR = _root_import_error(e)
        return False
    FastMCPClient, StreamableHttpTransport = _client_cls, _transport_cls
    FASTMCP_AVAILABLE, FASTMCP_IMPORT_ERROR = True, None
    return True


def fastmcp_problem_lines() -> list[str]:
    """User-facing explanation when fastmcp isn't importable."""
    if (__package__ or "").startswith("bl_ext."):
        lines = [
            "Blender MCP's bundled libraries aren't loaded.",
            "Restart Blender; they load at startup.",
        ]
    else:
        lines = [
            "fastmcp not installed.",
            "In Blender's Python console:",
            "  python -m pip install fastmcp",
        ]
    if FASTMCP_IMPORT_ERROR:
        lines.append(FASTMCP_IMPORT_ERROR)
    return lines


ensure_fastmcp(force=True)


def _env_seconds(name: str, default: float) -> float:
    import os
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


# Every await on the network is bounded. The MCP streamable-HTTP client's
# read timeout defaults to 300 s, so without these a stalled initialize or
# ping sat for five minutes (the "Connecting..." that never moved) and a
# dead peer was never noticed. The client-wide `timeout` option isn't used
# because it also becomes the read timeout of the long-lived notification
# stream, which would drop whenever the bus is quiet.
CONNECT_TIMEOUT_S = _env_seconds("BLENDER_MCP_CONNECT_TIMEOUT", 20.0)
REQUEST_TIMEOUT_S = _env_seconds("BLENDER_MCP_REQUEST_TIMEOUT", 30.0)
HEARTBEAT_TIMEOUT_S = _env_seconds("BLENDER_MCP_HEARTBEAT_TIMEOUT", 15.0)
HEARTBEAT_INTERVAL_S = _env_seconds("BLENDER_MCP_HEARTBEAT_INTERVAL", 30.0)
CLOSE_TIMEOUT_S = 5.0


def _request_ui_redraw() -> None:
    from ..connection import request_ui_redraw
    request_ui_redraw()


def _update_state_from_register_response(reg_result: Any) -> None:
    """Read the server's version-hint envelope out of a register_client result.

    The server (bus_tools.register_client) returns a JSON-encoded dict
    that may carry ``server.latest_addon_version`` + ``server.addon_download_url``.
    We compare that semver to ``addon._version.tuple_version`` and set
    the three ``state`` fields the UI panels read. A missing envelope is
    normal (older server, or server built without the addon source on
    disk) and leaves state untouched — the banner just doesn't draw.
    """
    import json

    from .. import _version, state

    # FastMCP's CallToolResult carries a ``content`` list of ContentBlock
    # objects; a JSON-string tool return lands as TextContent(text=...).
    text: Optional[str] = None
    content = getattr(reg_result, "content", None)
    if content:
        first = content[0]
        text = getattr(first, "text", None)
    if not text:
        # data attr sometimes carries the pre-decoded structured content
        text = getattr(reg_result, "data", None)
    if not text:
        return

    if isinstance(text, str):
        payload = json.loads(text)
    else:
        payload = text  # already a dict on the structured-content path
    server_env = payload.get("server") if isinstance(payload, dict) else None
    if not isinstance(server_env, dict):
        return

    latest = server_env.get("latest_addon_version")
    url = server_env.get("addon_download_url")
    if not isinstance(latest, str):
        return

    try:
        latest_tuple = tuple(int(p) for p in latest.split("."))
    except ValueError:
        return

    state._latest_addon_version = latest
    state._addon_download_url = url if isinstance(url, str) else None
    state._update_available = latest_tuple > _version.tuple_version


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
        # last_error: transient error from the most recent attempt; cleared
        # on successful registration. Surfaced as "Last error: ..." (truncated)
        # in the panel — not actionable by itself.
        # fatal_error: terminal failure that stopped the worker (auth-fatal,
        # config mistake). Panel surfaces this prominently with a Login button.
        # Cleared by the user clicking Login or Dismiss.
        self.last_error: Optional[str] = None
        self.fatal_error: Optional[str] = None
        # Reconnect visibility state — surfaced in the sidebar's Status
        # section so users can see the addon is actively retrying rather
        # than silently stuck. `reconnect_attempt` counts consecutive
        # failed reconnects and resets on successful registration.
        # `next_retry_at` is a unix epoch the sidebar subtracts from
        # time.time() to render the countdown; None outside a scheduled
        # sleep window.
        self.reconnect_attempt = 0
        self.next_retry_at: Optional[float] = None
        # True once this client has registered at least once. The connection
        # supervisor uses it to tell a failed start from a run that ended.
        self.ever_connected = False
        # Set by _refresh_watcher to signal that _run should tear down the
        # current FastMCP Client and reopen with the rotated JWT. Cleared
        # after reconnect completes.
        self._rotate_requested = False

        # Priority queue: heap of (priority_int, timestamp, job_payload).
        self.job_queue: list = []
        self.queue_lock = threading.Lock()
        self.active_jobs: dict = {}
        # job_updates waiting for a connection (see job_reporter.flush_outbox).
        self.job_outbox = collections.deque()
        self._timer_registered = False
        # bpy.app.timers compares callables by identity, and every
        # `self._drain_queue` access builds a fresh bound-method object,
        # so is_registered(self._drain_queue) is always False. Hold one
        # reference and use it for every register/is_registered/unregister.
        self._drain_timer_fn = self._drain_queue

    # --- Lifecycle ----------------------------------------------------------

    def start(self) -> None:
        """Start worker thread with its own asyncio loop."""
        if self.running:
            return
        if not ensure_fastmcp():
            self.last_error = " ".join(fastmcp_problem_lines())
            print(f"[BlenderMCP] {self.last_error}")
            return

        self.running = True
        self.thread = threading.Thread(target=self._thread_main, daemon=True)
        self.thread.start()

        self.ensure_drain_timer()

        print(f"[BlenderMCP] Client starting: {self.client_uuid} -> {self.server_url}")

    def ensure_drain_timer(self) -> bool:
        """Register the queue drain timer if it isn't already. Returns True
        if a registration happened.

        persistent=True because Blender drops non-persistent timers on every
        file load, which silently stopped job dispatch after File > Open or
        a bus-driven wm.open_mainfile. bpy.app.timers.register is documented
        thread-safe, so the heartbeat watchdog can call this from the worker.
        """
        if bpy.app.timers.is_registered(self._drain_timer_fn):
            self._timer_registered = True
            return False
        bpy.app.timers.register(
            self._drain_timer_fn, first_interval=0.1, persistent=True
        )
        self._timer_registered = True
        return True

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
        # drain_queue also self-removes once running is False; unregistering
        # here just makes a quick stop()/start() cycle deterministic.
        try:
            if bpy.app.timers.is_registered(self._drain_timer_fn):
                bpy.app.timers.unregister(self._drain_timer_fn)
        except Exception:
            pass
        self._timer_registered = False
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
        if not new_jwt:
            self.last_error = "Refresh response malformed (no access_token)"
            print(f"[BlenderMCP] {self.last_error}")
            return False

        new_expires_in = int(payload.get("expires_in", 0))
        if not new_expires_in:
            # Same OIDCProxy-omits-expires_in fallback as OAuth login.
            # Decode the fresh JWT's exp claim so the watcher has a real
            # deadline for the NEXT rotation. Without this, a rotation
            # succeeds once, then jwt_expires_at is 0, watcher sleeps
            # an hour, and the (new) token expires unrefreshed.
            from ..auth.oauth_pkce import _decode_jwt_payload
            claims = _decode_jwt_payload(new_jwt) or {}
            exp = claims.get("exp")
            if isinstance(exp, (int, float)):
                new_expires_in = int(exp - time.time())
                print(
                    f"[BlenderMCP] Refresh response omitted expires_in; "
                    f"using JWT exp claim ({new_expires_in}s from now)"
                )
            else:
                self.last_error = "Refresh response has no expires_in and no JWT exp"
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
            from ..preferences import get_prefs, persist_prefs
            prefs = get_prefs()
            prefs.jwt_token = self.jwt_token
            prefs.jwt_expires_at = str(self.jwt_expires_at)
            # Persist the rotated refresh token too (OAuth refresh rotates
            # the refresh_token per spec; without persisting we'd lose the
            # new one across Blender restarts).
            if self.refresh_token:
                prefs.refresh_token = self.refresh_token
            # Worker thread — persist_prefs hops the wm operator call to
            # the main thread via bpy.app.timers.
            persist_prefs()
        except Exception as e:
            print(f"[BlenderMCP] Could not persist rotated JWT to prefs: {e}")

    def _drain_queue(self) -> Optional[float]:
        from .drainer import drain_queue
        return drain_queue(self)

    # --- Registration ------------------------------------------------------

    def _registration_args(self, blend_file_if_empty: Optional[str] = None) -> dict:
        """Build blender_register_client arguments.

        ``blend_file_if_empty`` is what to send when bpy.data.filepath is
        empty: None (omit, server keeps the old value) on initial connect,
        "" on a metadata refresh so switching to an unsaved file clears it.
        """
        reg_args = {
            "client_uuid": self.client_uuid,
            "client_type": "blender",
            "is_persistent": True,
            "capabilities": [
                "python_execution", "modeling", "rendering",
                "scene_management", "asset_processing",
            ],
        }
        # Server treats a missing label on re-registration as "keep it".
        if self.label:
            reg_args["label"] = self.label
        if self.bus_id:
            reg_args["bus_id"] = self.bus_id
        # The server sends the update hint only to clients that report a
        # version, which keeps it away from builds whose Update now crashes.
        from .. import _version
        reg_args["addon_version"] = _version.__version__
        # Per-process disambiguation metadata surfaced on
        # list_available_clients. Optional: never block a register on it.
        try:
            import os as _os
            import socket as _socket
            reg_args["pid"] = _os.getpid()
            reg_args["hostname"] = _socket.gethostname()
            blend = getattr(bpy.data, "filepath", "") or blend_file_if_empty
            if blend is not None:
                reg_args["blend_file"] = blend
        except Exception as meta_exc:
            print(f"[BlenderMCP] Metadata build failed (non-fatal): {meta_exc}")
        return reg_args

    def refresh_registration(self) -> bool:
        """Re-send registration metadata over the live connection.

        Used after a .blend load so the server's ClientInfo.blend_file
        follows the open file. The server updates a same-uuid registration
        in place, so this replaces the old stop()/start() soft-reconnect,
        which ran inside wm.open_mainfile (load_post fires synchronously
        there) and tore down the loop before the drainer could send the
        job reply for the very call that opened the file.

        Must be called on the main thread (reads bpy.data). Returns False
        if there is no live connection to send on.
        """
        loop, client = self.loop, self.client
        if not (self.connected and loop and client and loop.is_running()):
            return False
        reg_args = self._registration_args(blend_file_if_empty="")
        future = asyncio.run_coroutine_threadsafe(
            client.call_tool(
                "blender_register_client", reg_args, timeout=REQUEST_TIMEOUT_S
            ),
            loop,
        )

        def _log_err(fut):
            try:
                fut.result(timeout=0)
            except Exception as e:
                print(f"[BlenderMCP] Metadata refresh failed: {e}")

        future.add_done_callback(_log_err)
        return True

    # --- Worker thread / asyncio loop --------------------------------------

    async def _close_client(self, client: Any) -> None:
        """Exit a FastMCP client without hanging on a dead socket."""
        try:
            await asyncio.wait_for(
                client.__aexit__(None, None, None), CLOSE_TIMEOUT_S
            )
        except asyncio.TimeoutError:
            print(f"[BlenderMCP] Client close timed out after {CLOSE_TIMEOUT_S:g}s; abandoning")
        except Exception as e:
            print(f"[BlenderMCP] Client close error (ignored): {e!r}")

    def _thread_main(self) -> None:
        """Run the asyncio loop on this thread."""
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        stopped_on_request = False
        try:
            self.loop.run_until_complete(self._run())
            stopped_on_request = not self.running
        except Exception as e:
            self.last_error = f"Worker crashed: {e}"
            print(f"[BlenderMCP] {self.last_error}")
            traceback.print_exc()
        finally:
            if stopped_on_request and not self.fatal_error:
                print(f"[BlenderMCP] Worker exited: stopped ({self.client_uuid})")
            elif self.fatal_error:
                print(f"[BlenderMCP] Worker exited: {self.fatal_error}")
            else:
                print(f"[BlenderMCP] Worker exited unexpectedly "
                      f"({self.last_error or 'no error recorded'})")
            # Whatever ended the loop, this client is done; clear the flags so
            # the panel and the connection supervisor don't see it as alive.
            self.running = False
            self.connected = False
            _request_ui_redraw()
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
                    client = FastMCPClient(
                        transport,
                        message_handler=self._on_message,
                        init_timeout=CONNECT_TIMEOUT_S,
                    )
                    # init_timeout bounds the MCP handshake; the outer
                    # wait_for also covers TCP/TLS connect and anything
                    # fastmcp does before initialize.
                    try:
                        await asyncio.wait_for(
                            client.__aenter__(), CONNECT_TIMEOUT_S + 5.0
                        )
                    except asyncio.TimeoutError:
                        await self._close_client(client)
                        raise ConnectionError(
                            f"connect timed out after {CONNECT_TIMEOUT_S:g}s"
                        ) from None
                    except BaseException:
                        await self._close_client(client)
                        raise
                    try:
                        self.client = client

                        try:
                            await asyncio.wait_for(
                                client.set_logging_level("debug"), REQUEST_TIMEOUT_S
                            )
                        except Exception as e:
                            print(f"[BlenderMCP] set_logging_level failed: {e!r}")

                        try:
                            reg_args = self._registration_args()
                            reg_result = await client.call_tool(
                                "blender_register_client", reg_args,
                                timeout=REQUEST_TIMEOUT_S,
                            )
                            self.connected = True
                            self.ever_connected = True
                            backoff = 1.0  # successful registration → reset backoff
                            self.reconnect_attempt = 0
                            self.next_retry_at = None
                            self.last_error = None
                            print(f"[BlenderMCP] Registered as {self.client_uuid}")
                            _request_ui_redraw()
                            # Results of jobs that finished while we were
                            # disconnected go out now that the server knows us.
                            try:
                                from .job_reporter import flush_outbox
                                flush_outbox(self)
                            except Exception as _fo_exc:
                                print(f"[BlenderMCP] Outbox flush failed: {_fo_exc}")
                            # Server-advertised update hint. Failures here are
                            # never fatal — a missing envelope just means the
                            # server predates the version-hint field.
                            try:
                                _update_state_from_register_response(reg_result)
                            except Exception as vh_exc:
                                print(f"[BlenderMCP] update-hint parse skipped: {vh_exc}")
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

                        # Wait until stop() flips `running` OR rotation is
                        # requested OR our heartbeat detects a dead transport.
                        #
                        # Why the heartbeat: FastMCP's SSE stream can die
                        # silently (server restart, network blip, TCP idle
                        # timeout — typically ~2h on Linux, way too slow).
                        # Without a probe, self.connected stays True from
                        # the last register_client and the sidebar shows
                        # "Connected" while the server has no record of us.
                        # ping() is the spec-blessed MCP heartbeat — zero
                        # side effects, returns bool, raises on transport
                        # failure → out of the inner while, into the outer
                        # reconnect path.
                        import time as _time
                        last_heartbeat = _time.monotonic()
                        HEARTBEAT_INTERVAL = HEARTBEAT_INTERVAL_S
                        # The server evicts Blender clients silent for an
                        # hour, and touch() on an evicted uuid is a no-op,
                        # so a client whose transport survived a long stall
                        # would heartbeat forever while invisible on the bus.
                        # Re-sending registration periodically re-adds it.
                        last_reregister = last_heartbeat
                        REREGISTER_INTERVAL = 600.0

                        def _reregister_on_main():
                            try:
                                self.refresh_registration()
                            except Exception as _rr_exc:
                                print(f"[BlenderMCP] Periodic re-register failed: {_rr_exc}")
                            return None

                        while self.running and not self._rotate_requested:
                            await asyncio.sleep(0.2)
                            now = _time.monotonic()
                            if now - last_heartbeat >= HEARTBEAT_INTERVAL:
                                try:
                                    try:
                                        await asyncio.wait_for(
                                            client.ping(), HEARTBEAT_TIMEOUT_S
                                        )
                                    except asyncio.TimeoutError:
                                        raise ConnectionError(
                                            f"heartbeat timed out after "
                                            f"{HEARTBEAT_TIMEOUT_S:g}s"
                                        ) from None
                                    last_heartbeat = now
                                    if now - last_reregister >= REREGISTER_INTERVAL:
                                        last_reregister = now
                                        # refresh_registration reads bpy.data,
                                        # so hop to the main thread.
                                        bpy.app.timers.register(
                                            _reregister_on_main, first_interval=0.0
                                        )
                                    # Drainer-watchdog: verified against
                                    # feedback bug-iDJHVyy4e2Q (queue stall
                                    # with heartbeat still healthy). If the
                                    # main-thread timer that pops jobs off
                                    # client.job_queue has been unregistered
                                    # for any reason (an unhandled exception
                                    # that Blender caught, a module reload
                                    # from an extension update, etc.), the
                                    # bus stays "connected" but no dispatch
                                    # ever completes. Re-register from here
                                    # — bpy.app.timers.register is documented
                                    # thread-safe.
                                    try:
                                        if self.ensure_drain_timer():
                                            print(
                                                "[BlenderMCP] Drainer timer was "
                                                "not registered; re-registered "
                                                "from heartbeat watchdog."
                                            )
                                    except Exception as _wd_exc:
                                        print(
                                            f"[BlenderMCP] Drainer watchdog "
                                            f"check failed: {_wd_exc}"
                                        )
                                except Exception as hb_exc:
                                    self.last_error = (
                                        f"Heartbeat failed: {hb_exc}"
                                    )
                                    print(
                                        f"[BlenderMCP] {self.last_error}"
                                        f" — reconnecting"
                                    )
                                    raise  # exit inner while via outer except

                        # If we're rotating, don't unregister (we'll re-register
                        # under the new JWT in the next iteration). Only unregister
                        # on full shutdown.
                        if not self.running:
                            try:
                                await client.call_tool(
                                    "blender_unregister_client",
                                    {"client_uuid": self.client_uuid},
                                    timeout=CLOSE_TIMEOUT_S,
                                )
                            except Exception as e:
                                print(f"[BlenderMCP] unregister_client failed: {e!r}")
                    finally:
                        await self._close_client(client)
                except Exception as e:
                    # Some transport errors stringify to "" (TimeoutError,
                    # anyio's ClosedResourceError); fall back to the type.
                    self.last_error = f"Connection failed: {str(e) or type(e).__name__}"
                    print(f"[BlenderMCP] {self.last_error}")

                    # 401 paths split into three cases:
                    #   (a) JWT chronologically expired — _refresh_watcher
                    #       missed the window. Refresh will succeed.
                    #   (b) JTI mapping wiped by server restart — JWT is
                    #       still time-valid but server has no record. Refresh
                    #       will succeed (refresh_tokens are persisted differently).
                    #   (c) Refresh token ALSO dead — user genuinely needs re-Login.
                    # Strategy: try refresh first; only go fatal if refresh
                    # also fails (case c). The chronological pre-connect check
                    # at the top of _run() only catches case (a); this catches
                    # (b) too, which is the common "I restarted Blender after
                    # the prod server bounced" scenario.
                    msg = str(e).lower()
                    is_auth_error = any(
                        s in msg for s in ("401", "403", "unauthorized", "invalid_token")
                    )
                    if is_auth_error and self.refresh_token:
                        print("[BlenderMCP] 401 on connect — trying refresh before giving up")
                        if await self._do_refresh_once():
                            print("[BlenderMCP] Refresh succeeded; reconnecting with new token")
                            # Skip backoff sleep — we have a fresh token, retry now.
                            backoff = 1.0
                            self.last_error = None
                            continue  # outer while: next iteration uses new self.jwt_token
                        # Refresh failed → fall through to fatal path below.
                        print("[BlenderMCP] Refresh also failed — going fatal")

                    if is_auth_error:
                        print("[BlenderMCP] Auth failure — stopping retries; please re-Login")
                        self.fatal_error = (
                            "Authentication failed — your session is no longer valid. "
                            "Click Login to re-authenticate."
                        )
                        # Clear stored JWT so the Login section flips back to
                        # "Not logged in" and the user has an obvious next action.
                        # Bus client worker thread — bpy property writes from
                        # background threads work for AddonPreferences StringProperty
                        # (no mesh/scene mutation), per the convention already used
                        # by _persist_rotated_jwt_to_prefs above.
                        try:
                            from ..preferences import get_prefs, persist_prefs
                            prefs = get_prefs()
                            prefs.jwt_token = ""
                            prefs.refresh_token = ""
                            prefs.jwt_expires_at = "0"
                            # Persist the cleared state so the "Re-login"
                            # sidebar prompt survives a Blender restart.
                            persist_prefs()
                        except Exception as exc:
                            print(f"[BlenderMCP] Could not clear stale JWT from prefs: {exc}")
                        self.running = False
                        return
                finally:
                    was_connected = self.connected
                    self.connected = False
                    self.client = None
                    if was_connected:
                        _request_ui_redraw()

                if not self.running:
                    break
                # Reconnect path — sleep with exponential backoff, then loop.
                # _rotate_requested takes precedence: if the watcher rotated
                # the JWT mid-failure, skip the backoff and reconnect now.
                if self._rotate_requested:
                    print("[BlenderMCP] Reconnecting with rotated JWT")
                else:
                    import time as _time
                    self.reconnect_attempt += 1
                    self.next_retry_at = _time.time() + backoff
                    print(
                        f"[BlenderMCP] Reconnect attempt {self.reconnect_attempt} in "
                        f"{backoff:.0f}s"
                    )
                    _request_ui_redraw()
                    sleep_remaining = backoff
                    # Sleep in 0.5s chunks so stop() takes effect promptly
                    # without making the user wait for the full backoff.
                    # Redraw each second so the panel countdown moves.
                    ticks = 0
                    while sleep_remaining > 0 and self.running:
                        chunk = min(0.5, sleep_remaining)
                        await asyncio.sleep(chunk)
                        sleep_remaining -= chunk
                        ticks += 1
                        if ticks % 2 == 0:
                            _request_ui_redraw()
                    # Sleep is over; the loop iterates and either succeeds
                    # (clearing next_retry_at above) or lands back here.
                    self.next_retry_at = None
                    backoff = min(backoff * 2, BACKOFF_MAX)

                if self._rotate_requested and self.running:
                    print(f"[BlenderMCP] Reconnecting with rotated JWT (exp={self.jwt_expires_at})")
        finally:
            if refresh_task is not None:
                refresh_task.cancel()
