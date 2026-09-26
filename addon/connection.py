"""Connection intent: keep the bus client connected while the user wants it.

``prefs.auto_connect`` is the user's intent ("stay connected"). The
supervisor is a persistent bpy.app.timers callback that, while armed and
logged in, starts a client whenever none is alive. Inside a live client,
bus_client._run already retries transient failures forever with 1 s to
30 s backoff; the supervisor covers what ends a client outright (worker
crash, refresh-watcher giving up, a stop from elsewhere) and backs off
itself when restarts keep failing before the client ever registers.

bpy is imported lazily so ``decide`` can be unit-tested outside Blender.
"""

from __future__ import annotations

import time
from typing import Any, Optional

from . import state

SUPERVISOR_INTERVAL_S = 10.0
FIRST_TICK_S = 2.0
RESTART_BACKOFF_MAX_S = 300.0

# decide() outcomes
DISARMED = "disarmed"
NO_TOKEN = "no_token"
AUTH_IN_PROGRESS = "auth_in_progress"
ALIVE = "alive"
BACKOFF = "backoff"
START = "start"


def client_alive(client: Any) -> bool:
    """True when the client's worker is actually running.

    ``running`` alone isn't enough: a worker that died leaves it set if
    the crash path didn't clear it, so also require a live thread.
    """
    if client is None or not getattr(client, "running", False):
        return False
    thread = getattr(client, "thread", None)
    return thread is not None and thread.is_alive()


def decide(armed: bool, has_token: bool, auth_in_progress: bool,
           alive: bool, now: float, next_attempt_at: float) -> str:
    """Pure supervisor decision for one tick."""
    if not armed:
        return DISARMED
    if not has_token:
        return NO_TOKEN
    if auth_in_progress:
        return AUTH_IN_PROGRESS
    if alive:
        return ALIVE
    if now < next_attempt_at:
        return BACKOFF
    return START


def restart_delay(consecutive_failures: int) -> float:
    """Wait before the next supervisor restart: 0 on the first try, then
    10 s, 20 s, 40 s ... capped at five minutes."""
    if consecutive_failures <= 0:
        return 0.0
    return min(SUPERVISOR_INTERVAL_S * (2 ** (consecutive_failures - 1)), RESTART_BACKOFF_MAX_S)


def start_client() -> tuple[bool, str]:
    """Build (if needed) and start the bus client from prefs.

    No operator context required, so the supervisor timer can call it at
    startup. A client that exists but isn't alive is discarded first, so
    a restart always picks up the current prefs.jwt_token.
    """
    from .client import BlenderMCPClient
    from .client.bus_client import FASTMCP_AVAILABLE
    from .executor import BlenderCommandExecutor
    from .identity import StickyUUIDManager
    from .preferences import get_client_label, get_prefs, get_server_base_url

    if not FASTMCP_AVAILABLE:
        return False, "fastmcp not installed. Run: <blender_python> -m pip install fastmcp"
    prefs = get_prefs()
    if not prefs.jwt_token:
        return False, "Not logged in. Click Login first."

    if state._client is not None and not client_alive(state._client):
        stop_client()

    if state._executor is None:
        state._executor = BlenderCommandExecutor()

    if state._client is None:
        expires_at = 0
        if prefs.jwt_expires_at:
            try:
                expires_at = int(prefs.jwt_expires_at)
            except ValueError:
                pass
        state._client = BlenderMCPClient(
            server_url=get_server_base_url(prefs),
            jwt_token=prefs.jwt_token,
            client_uuid=StickyUUIDManager().get_client_id(),
            executor=state._executor,
            refresh_token=prefs.refresh_token,
            jwt_expires_at=expires_at,
            label=get_client_label(prefs),
            bus_id=prefs.default_bus_id or None,
        )

    state._client.start()
    _set_scene_hints(True, state._client.client_uuid)
    return True, f"Connecting as {state._client.client_uuid}"


def stop_client() -> None:
    """Stop and drop the bus client. Does not change the user's intent."""
    client = state._client
    state._client = None
    sup = getattr(state, "_supervisor", None)
    if sup is not None and sup._watched_client is client:
        sup._watched_client = None  # a deliberate stop is not a failed start
    if client is not None:
        try:
            client.stop()
        except Exception as e:
            print(f"[BlenderMCP] Error stopping client: {e}")
    _set_scene_hints(False)
    request_ui_redraw()


def request_ui_redraw() -> None:
    """Tag the 3D-view sidebar and Preferences for redraw. Any thread.

    Blender only redraws a region on input events or an explicit tag, so a
    state change made off the main thread (worker connects, heartbeat
    fails) stays invisible until the user moves the mouse over the panel.
    The tag has to happen on the main thread, hence the timer hop.
    """
    try:
        import bpy
    except ImportError:
        return

    def _tag():
        try:
            for window in bpy.context.window_manager.windows:
                for area in window.screen.areas:
                    if area.type in {'VIEW_3D', 'PREFERENCES'}:
                        area.tag_redraw()
        except Exception:
            pass
        return None

    try:
        bpy.app.timers.register(_tag, first_interval=0.0)
    except Exception:
        pass


def _set_scene_hints(running: bool, client_id: Optional[str] = None) -> None:
    # Display-only mirrors on the Scene. Never read them for decisions: Scene
    # properties serialize into .blend files, startup.blend included.
    try:
        import bpy
        scene = bpy.context.scene
        scene.blendermcp_server_running = running
        if client_id:
            scene.blendermcp_client_id = client_id
    except Exception:
        pass


def set_armed(armed: bool) -> None:
    """Record the user's intent; the prefs update callback does the work."""
    from .preferences import get_prefs

    prefs = get_prefs()
    if prefs.auto_connect != armed:
        prefs.auto_connect = armed
    elif armed:
        # Already armed: still start now instead of waiting for a tick.
        supervisor().poke()


class ConnectionSupervisor:
    """Persistent timer that keeps a client alive while armed."""

    def __init__(self) -> None:
        # bpy.app.timers compares callables by identity; keep one reference.
        self._tick_fn = self._tick
        self.consecutive_failures = 0
        self.next_attempt_at = 0.0
        self._last_decision: Optional[str] = None
        self._watched_client: Any = None
        self._start_reason: Optional[str] = None

    # --- lifecycle ---

    def install(self) -> None:
        import bpy
        if not bpy.app.timers.is_registered(self._tick_fn):
            bpy.app.timers.register(self._tick_fn, first_interval=FIRST_TICK_S, persistent=True)

    def uninstall(self) -> None:
        import bpy
        try:
            if bpy.app.timers.is_registered(self._tick_fn):
                bpy.app.timers.unregister(self._tick_fn)
        except Exception:
            pass

    def poke(self, reason: str = "armed") -> None:
        """Run a decision now and clear any restart backoff."""
        self.reset_backoff()
        self._start_reason = reason
        self._tick()

    def check_now(self) -> None:
        """Run a decision now, respecting any restart backoff."""
        self._tick()

    def reset_backoff(self) -> None:
        self.consecutive_failures = 0
        self.next_attempt_at = 0.0

    def seconds_until_next_attempt(self) -> Optional[float]:
        if self.next_attempt_at <= 0:
            return None
        return max(0.0, self.next_attempt_at - time.time())

    # --- the tick ---

    def _tick(self) -> float:
        try:
            self._evaluate()
        except Exception as e:
            print(f"[BlenderMCP] Connection supervisor error (non-fatal): {e}")
        # While waiting out a restart backoff the panel shows a countdown,
        # so tick (and redraw) every second instead of every ten.
        if self._last_decision == BACKOFF:
            request_ui_redraw()
            return 1.0
        return SUPERVISOR_INTERVAL_S

    def _evaluate(self) -> None:
        from .preferences import get_prefs

        prefs = get_prefs()
        client = state._client
        alive = client_alive(client)
        if prefs.auto_connect:
            self._account_for_previous_client(client, alive)
        else:
            self._watched_client = None
            self.reset_backoff()

        now = time.time()
        decision = decide(
            armed=bool(prefs.auto_connect),
            has_token=bool(prefs.jwt_token),
            auth_in_progress=bool(getattr(state, "_auth_in_progress", False)),
            alive=alive,
            now=now,
            next_attempt_at=self.next_attempt_at,
        )
        self._log_change(decision)
        if decision != START:
            return

        reason = self._start_reason or "no live client"
        self._start_reason = None
        ok, message = start_client()
        self._watched_client = state._client if ok else None
        if ok:
            print(f"[BlenderMCP] Stay-connected: starting client ({reason}); {message}")
        else:
            self._record_failure(now)
            print(f"[BlenderMCP] Stay-connected: could not start ({message}); "
                  f"next try in {int(self.seconds_until_next_attempt() or 0)}s")
        request_ui_redraw()

    def _account_for_previous_client(self, client: Any, alive: bool) -> None:
        # A client this supervisor started has ended. If it registered at
        # least once it counts as a good run; otherwise it's a failed start
        # and the next restart waits longer.
        watched = self._watched_client
        if watched is None or (watched is client and alive):
            if watched is not None and getattr(watched, "ever_connected", False):
                self.reset_backoff()
            return
        self._watched_client = None
        last_error = getattr(watched, "last_error", None) or "no error recorded"
        if getattr(watched, "ever_connected", False):
            self.reset_backoff()
            self._start_reason = "previous client ended"
            print(f"[BlenderMCP] Stay-connected: client ended after connecting "
                  f"({last_error}); restarting")
        else:
            self._record_failure(time.time())
            print(f"[BlenderMCP] Stay-connected: client ended before registering "
                  f"({last_error}); restarting in "
                  f"{int(self.seconds_until_next_attempt() or 0)}s")
        request_ui_redraw()

    def _record_failure(self, now: float) -> None:
        self.consecutive_failures += 1
        self.next_attempt_at = now + restart_delay(self.consecutive_failures)

    def _log_change(self, decision: str) -> None:
        if decision == self._last_decision or decision == START:
            return
        self._last_decision = decision
        request_ui_redraw()
        reasons = {
            DISARMED: "off (click Connect to stay connected)",
            NO_TOKEN: "waiting for Login",
            AUTH_IN_PROGRESS: "waiting for the browser login to finish",
            ALIVE: "client running",
            BACKOFF: f"restart failed {self.consecutive_failures}x, next try in "
                     f"{int(self.seconds_until_next_attempt() or 0)}s",
        }
        print(f"[BlenderMCP] Stay-connected: {reasons.get(decision, decision)}")


def supervisor() -> ConnectionSupervisor:
    """The supervisor instance, kept on the state module with the client."""
    sup = getattr(state, "_supervisor", None)
    if sup is None:
        sup = ConnectionSupervisor()
        state._supervisor = sup
    return sup
