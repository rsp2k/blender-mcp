"""The Blender side of tool servers: preferences in, reports and replies out.

Threads, and what runs where:

  main thread     snapshot_specs() reads the preferences (the only place
                  bpy props are read), sync_from_prefs(), the Test button,
                  and handle_user_tool_call() called by the drainer.
  runner thread   every tool-server await (runner.py); call replies go
                  back through submit_job_update, and status changes
                  trigger report() and a UI redraw.
  bus loop        blender_report_user_tools is scheduled onto it with
                  run_coroutine_threadsafe; nothing here waits for it.
"""

from __future__ import annotations

import asyncio
import atexit
import json
import threading
from typing import Any

from .config import DEFAULT_TIMEOUT_S, ServerSpec, snapshot_problems
from .report import build_report
from .results import failure
from .runner import ToolServerRunner

USER_TOOL_CALL = "user_tool_call"
REPORT_TOOL = "blender_report_user_tools"
REPORT_TIMEOUT_S = 30.0
SYNC_DELAY_S = 0.3


def snapshot_specs(prefs: Any) -> list[ServerSpec]:
    """Copy the tool-server preferences into plain specs. Main thread only."""
    specs = []
    for item in getattr(prefs, "tool_servers", None) or []:
        try:
            timeout = float(item.timeout_s)
        except (TypeError, ValueError):
            timeout = DEFAULT_TIMEOUT_S
        specs.append(ServerSpec(
            name=str(item.name or "").strip(),
            start=str(item.start or ""),
            env_text=str(item.env_text or ""),
            token=str(item.token or ""),
            trusted=bool(item.trusted),
            enabled=bool(item.enabled),
            timeout_s=max(1.0, timeout),
        ))
    return specs


def resolve_call(specs: list[ServerSpec], params: Any) -> tuple[ServerSpec | None, str, dict, float, dict | None]:
    """Validate a user_tool_call against the user's own specs.

    Returns (spec, tool, arguments, timeout_s, refusal). Only the server
    NAME is taken from the params; how to start or reach it comes from
    the specs. A refusal is the reply to send instead of calling.
    """
    params = params if isinstance(params, dict) else {}
    server = params.get("server")
    tool = params.get("tool")
    arguments = params.get("arguments")
    if not isinstance(server, str) or not server:
        return None, "", {}, 0.0, failure("unknown tool server")
    matches = [s for s in specs if s.name == server]
    if not matches:
        return None, "", {}, 0.0, failure("unknown tool server")
    problem = snapshot_problems(specs).get(server)
    if problem:
        return None, "", {}, 0.0, failure(f"tool server {server}: {problem}")
    spec = matches[0]
    if not spec.enabled:
        return None, "", {}, 0.0, failure(f"tool server {server} is turned off in Blender's preferences")
    if not isinstance(tool, str) or not tool:
        return None, "", {}, 0.0, failure("no tool named in the call")
    if not isinstance(arguments, dict):
        arguments = {}
    timeout = spec.timeout_s
    asked = params.get("timeout_s")
    if isinstance(asked, (int, float)) and not isinstance(asked, bool) and asked > 0:
        # Answer before the server stops waiting.
        timeout = min(timeout, float(asked))
    return spec, tool, arguments, timeout, None


class ToolServerBridge:
    """One per Blender (state._tool_servers). Owns the runner."""

    def __init__(self) -> None:
        self.runner = ToolServerRunner(on_change=self._on_change)
        self.active = False  # set at the first registration: servers start then
        self._lock = threading.Lock()
        self._last_signature: str | None = None
        self._unsupported_client: Any = None
        # Last report outcome for the preferences: {"sent", "accepted", "dropped", "error"}.
        self.last_report: dict = {}
        atexit.register(self.shutdown)

    # --- lifecycle -----------------------------------------------------------

    def shutdown(self) -> None:
        try:
            atexit.unregister(self.shutdown)
        except Exception:  # noqa: BLE001, S110
            pass
        self.active = False
        self.runner.stop()

    def _on_change(self) -> None:
        """Runner thread: a server's status or tool list changed."""
        _redraw()
        if self.active:
            self.report()

    # --- main thread -----------------------------------------------------------

    def sync(self, specs: list[ServerSpec]) -> None:
        if not self.active:
            return
        if not specs and not self.runner.running:
            return  # nothing configured: don't start the loop thread at all
        self.runner.apply(specs)
        # Trust or enable changes alter the report even with no restart;
        # apply() notifies when it's done, and report() skips repeats.

    def test(self, spec: ServerSpec) -> None:
        self.runner.restart(spec)

    def handle_call(self, client: Any, job_id: str, specs: list[ServerSpec], params: Any) -> None:
        from ..client.job_reporter import submit_job_update

        def reply(result: dict) -> None:
            submit_job_update(client, job_id, "completed", result=json.dumps(result), error="")

        spec, tool, arguments, timeout, refusal = resolve_call(specs, params)
        if refusal is not None:
            reply(refusal)
            return
        # Returns at once; `reply` runs on the runner thread when the call ends.
        self.runner.call(spec, tool, arguments, timeout, reply)

    # --- any thread ------------------------------------------------------------

    def report(self, force: bool = False) -> bool:
        """Send blender_report_user_tools if the set changed (or `force`)."""
        from .. import state

        client = state._client
        if client is None or getattr(client, "worker_mode", False):
            return False
        loop, mcp = getattr(client, "loop", None), getattr(client, "client", None)
        if not (getattr(client, "connected", False) and loop and mcp and loop.is_running()):
            return False
        tools, dropped = build_report(self.runner.report_entries())
        signature = json.dumps(tools, sort_keys=True, default=str)
        with self._lock:
            if self._unsupported_client is client:
                return False
            if not force and signature == self._last_signature:
                return False
            self._last_signature = signature
        try:
            fut = asyncio.run_coroutine_threadsafe(
                mcp.call_tool(REPORT_TOOL, {"tools": tools}, timeout=REPORT_TIMEOUT_S,
                              raise_on_error=False),
                loop,
            )
        except Exception as e:  # noqa: BLE001
            print(f"[BlenderMCP] Couldn't report tool-server tools: {e}")
            with self._lock:
                self._last_signature = None
            return False
        fut.add_done_callback(lambda f: self._on_report_done(f, client, len(tools), dropped))
        return True

    def _on_report_done(self, fut: Any, client: Any, sent: int, dropped: list) -> None:
        from ..chat.state import decode_tool_result, tool_error_text

        outcome: dict = {"sent": sent, "dropped": list(dropped)}
        try:
            result = fut.result()
        except Exception as e:  # noqa: BLE001
            outcome["error"] = str(e) or type(e).__name__
        else:
            if getattr(result, "is_error", False) or getattr(result, "isError", False):
                outcome["error"] = tool_error_text(result)
            else:
                payload = decode_tool_result(result) or {}
                outcome["accepted"] = payload.get("accepted")
                for d in payload.get("dropped") or []:
                    if isinstance(d, dict) and d not in outcome["dropped"]:
                        outcome["dropped"].append(d)
        err = outcome.get("error") or ""
        if err:
            if "unknown tool" in err.lower():
                # An older server: stay quiet until the next registration.
                with self._lock:
                    self._unsupported_client = client
                outcome["error"] = "this server doesn't take tool-server tools yet"
            else:
                with self._lock:
                    self._last_signature = None  # try again on the next change
            print(f"[BlenderMCP] Tool-server report: {outcome['error']}")
        self.last_report = outcome
        _redraw()

    def on_registered(self) -> None:
        """A (re-)registration answered: the server forgot our tools."""
        with self._lock:
            self._unsupported_client = None
            self._last_signature = None


# --- module entry points (the drainer, bus client and operators call these) ---


def get_bridge(create: bool = True) -> ToolServerBridge | None:
    from .. import state
    bridge = getattr(state, "_tool_servers", None)
    if bridge is None and create:
        bridge = ToolServerBridge()
        state._tool_servers = bridge
    return bridge


def _prefs_specs() -> list[ServerSpec]:
    from ..preferences import get_prefs
    try:
        return snapshot_specs(get_prefs())
    except Exception as e:  # noqa: BLE001
        print(f"[BlenderMCP] Couldn't read tool-server preferences: {e}")
        return []


def handle_user_tool_call(client: Any, job_id: str, params: Any) -> None:
    """Drainer hook, main thread. Never waits on the tool server."""
    from ..worker import is_worker_mode
    if is_worker_mode():
        from ..client.job_reporter import submit_job_update
        submit_job_update(client, job_id, "completed", error="",
                          result=json.dumps(failure("tool servers run only in the main Blender")))
        return
    get_bridge().handle_call(client, job_id, _prefs_specs(), params)


def sync_from_prefs() -> None:
    """Main thread: start/stop servers to match the preferences."""
    bridge = get_bridge(create=False)
    if bridge is not None and bridge.active:
        bridge.sync(_prefs_specs())


def _sync_timer() -> None:
    sync_from_prefs()


def schedule_sync() -> None:
    """Debounced sync after a preference edit. Any thread."""
    try:
        import bpy
        if bpy.app.timers.is_registered(_sync_timer):
            bpy.app.timers.unregister(_sync_timer)
        bpy.app.timers.register(_sync_timer, first_interval=SYNC_DELAY_S)
    except Exception as e:  # noqa: BLE001
        print(f"[BlenderMCP] Couldn't schedule a tool-server sync: {e}")


def _registered_timer() -> None:
    bridge = get_bridge()
    bridge.on_registered()
    bridge.active = True
    bridge.sync(_prefs_specs())
    # Report what is up now (an empty list clears the server's set); servers
    # still starting report again when they come up.
    bridge.report(force=True)


def on_registered() -> None:
    """Bus loop thread, after every registration: hop to the main thread."""
    try:
        import bpy
        bpy.app.timers.register(_registered_timer, first_interval=0.0)
    except Exception as e:  # noqa: BLE001
        print(f"[BlenderMCP] Tool-server start skipped: {e}")


def test_server(index: int) -> str | None:
    """Main thread: (re)start one server now. Returns a problem, or None."""
    specs = _prefs_specs()
    if not 0 <= index < len(specs):
        return "no server selected"
    get_bridge().test(specs[index])
    return None


def shutdown() -> None:
    from .. import state
    bridge = getattr(state, "_tool_servers", None)
    state._tool_servers = None
    if bridge is not None:
        bridge.shutdown()


def status_for(name: str) -> dict | None:
    bridge = get_bridge(create=False)
    return bridge.runner.status(name) if bridge is not None else None


def _redraw() -> None:
    try:
        from ..connection import request_ui_redraw
        request_ui_redraw()
    except Exception:  # noqa: BLE001, S110
        pass
