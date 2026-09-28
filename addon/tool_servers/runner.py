"""Run the user's tool servers on an asyncio loop thread of their own.

Neither Blender's main thread nor the bus client's loop ever waits on a
tool server: every public method here only schedules work on this
runner's loop and returns. Results come back through callbacks that run
on the runner thread.

Each server has an owner task that opens the fastmcp Client, lists the
tools, and holds the session open until it is told to stop (entering
and leaving the client in one task keeps anyio's cancel scopes happy).
Calls run in their own tasks against that session. A server whose owner
task has ended (crashed, failed to start) is started again by its next
call.

No bpy here. Specs arrive as ServerSpec copies made on the main thread.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from collections.abc import Callable
from typing import Any

from .config import (
    DEFAULT_TIMEOUT_S,
    ConfigError,
    Launch,
    ServerSpec,
    build_launch,
    snapshot_problems,
)
from .results import failure, shape_result


def _env_seconds(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


# A first `uvx some-server` may download a lot before it answers.
START_TIMEOUT_S = _env_seconds("BLENDER_MCP_TOOL_SERVER_START_TIMEOUT", 300.0)
LIST_TIMEOUT_S = 30.0
STOP_TIMEOUT_S = 5.0
WATCH_INTERVAL_S = 1.0

STARTING, RUNNING, ERROR, OFF = "starting", "running", "error", "off"


def default_client_factory(launch: Launch, start_timeout_s: float) -> Any:
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport, StreamableHttpTransport

    if launch.kind == "http":
        transport = StreamableHttpTransport(url=launch.url, headers=launch.headers or None)
    else:
        transport = StdioTransport(
            command=launch.command, args=list(launch.args), env=dict(launch.env),
            keep_alive=False,
        )
    return Client(transport, init_timeout=start_timeout_s)


class _Server:
    def __init__(self, spec: ServerSpec, loop: asyncio.AbstractEventLoop) -> None:
        self.spec = spec
        self.launch_key = spec.launch_key()
        self.client: Any = None
        self.tools: list = []
        self.ready: asyncio.Future = loop.create_future()
        # Nobody may be waiting when a start fails; mark the error retrieved.
        self.ready.add_done_callback(lambda f: f.cancelled() or f.exception())
        self.stop_event = asyncio.Event()
        self.task: asyncio.Task | None = None

    def alive(self) -> bool:
        return self.task is not None and not self.task.done()


class ToolServerRunner:
    def __init__(
        self,
        client_factory: Callable[[Launch, float], Any] = default_client_factory,
        on_change: Callable[[], None] | None = None,
        start_timeout_s: float = START_TIMEOUT_S,
    ) -> None:
        self._client_factory = client_factory
        self.on_change = on_change
        self.start_timeout_s = start_timeout_s
        self.loop: asyncio.AbstractEventLoop | None = None
        self.thread: threading.Thread | None = None
        self._servers: dict[str, _Server] = {}  # loop thread only
        self._lock = threading.Lock()
        # name -> {"state", "error", "tools", "trusted", "enabled", "since"}; any thread.
        self._public: dict[str, dict] = {}

    # --- lifecycle (any thread) -------------------------------------------

    @property
    def running(self) -> bool:
        return bool(self.loop and self.thread and self.thread.is_alive() and self.loop.is_running())

    def start(self) -> None:
        if self.running:
            return
        ready = threading.Event()

        def _main() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self.loop = loop
            loop.call_soon(ready.set)
            try:
                loop.run_forever()
            finally:
                try:
                    loop.run_until_complete(loop.shutdown_asyncgens())
                except Exception:  # noqa: BLE001, S110
                    pass
                loop.close()

        self.thread = threading.Thread(target=_main, name="blendermcp-tool-servers", daemon=True)
        self.thread.start()
        ready.wait(5.0)

    def stop(self, timeout: float = STOP_TIMEOUT_S + 2.0) -> None:
        """Stop every server and the loop. Blocks up to `timeout` (unregister/quit only)."""
        loop, thread = self.loop, self.thread
        if loop is None or thread is None:
            return
        if loop.is_running():
            fut = asyncio.run_coroutine_threadsafe(self._shutdown(), loop)
            try:
                fut.result(timeout)
            except Exception as e:  # noqa: BLE001
                print(f"[BlenderMCP] Tool servers didn't stop cleanly: {e!r}")
            loop.call_soon_threadsafe(loop.stop)
        if thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self.loop = None
        self.thread = None
        with self._lock:
            self._public.clear()

    def _submit(self, coro) -> Any | None:
        self.start()
        loop = self.loop
        if loop is None or not loop.is_running():
            coro.close()
            return None
        return asyncio.run_coroutine_threadsafe(coro, loop)

    # --- public API (any thread, never blocks) ----------------------------

    def apply(self, specs: list[ServerSpec]):
        """Make the running set match `specs`: start enabled servers, stop the rest."""
        return self._submit(self._apply(list(specs)))

    def restart(self, spec: ServerSpec):
        """(Re)start one server, e.g. for the Test button."""
        return self._submit(self._restart(spec))

    def call(self, spec: ServerSpec, tool: str, arguments: dict, timeout_s: float,
             callback: Callable[[dict], None]) -> None:
        """Call a tool; `callback(reply)` runs on the runner thread when done."""
        fut = self._submit(self._call(spec, tool, arguments, timeout_s))
        if fut is None:
            callback(failure("tool servers aren't running"))
            return

        def _done(f) -> None:
            try:
                reply = f.result()
            except BaseException as e:  # noqa: BLE001
                reply = failure(f"{spec.name}: {str(e) or type(e).__name__}")
            try:
                callback(reply)
            except Exception as e:  # noqa: BLE001
                print(f"[BlenderMCP] tool call reply failed: {e!r}")

        fut.add_done_callback(_done)

    def status(self, name: str) -> dict | None:
        with self._lock:
            st = self._public.get(name)
            return dict(st) if st else None

    def statuses(self) -> dict[str, dict]:
        with self._lock:
            return {k: dict(v) for k, v in self._public.items()}

    def report_entries(self) -> list[tuple[str, bool, list, float]]:
        """[(server, trusted, tools, timeout_s)] for enabled servers that have listed tools.

        A server that crashed keeps its tools here: its next call restarts it.
        """
        with self._lock:
            return [
                (name, bool(st.get("trusted")), list(st.get("tools") or []),
                 float(st.get("timeout_s") or DEFAULT_TIMEOUT_S))
                for name, st in sorted(self._public.items())
                if st.get("enabled") and st.get("tools")
            ]

    # --- loop thread --------------------------------------------------------

    def _set_status(self, name: str, notify: bool = True, **fields: Any) -> None:
        with self._lock:
            st = self._public.setdefault(name, {"state": OFF, "error": "", "tools": [],
                                                "trusted": False, "enabled": False})
            st.update(fields)
            st["since"] = time.time()
        if notify:
            self._notify()

    def _notify(self) -> None:
        cb = self.on_change
        if cb is None:
            return
        try:
            cb()
        except Exception as e:  # noqa: BLE001
            print(f"[BlenderMCP] tool server change hook failed: {e!r}")

    async def _apply(self, specs: list[ServerSpec]) -> None:
        problems = snapshot_problems(specs)
        wanted: dict[str, ServerSpec] = {}
        for spec in specs:
            if spec.name and spec.name not in problems:
                wanted[spec.name] = spec
        for name, srv in list(self._servers.items()):
            spec = wanted.get(name)
            if spec is None or not spec.enabled or spec.launch_key() != srv.launch_key:
                await self._stop_server(name)
                self._set_status(name, notify=False, tools=[])
        with self._lock:
            for name in list(self._public):
                if name not in wanted and name not in problems:
                    del self._public[name]
        for name, problem in problems.items():
            self._set_status(name, notify=False, state=ERROR, error=problem, tools=[],
                             enabled=False, trusted=False)
        for name, spec in wanted.items():
            if not spec.enabled:
                self._set_status(name, notify=False, state=OFF, error="", tools=[],
                                 enabled=False, trusted=spec.trusted, timeout_s=spec.timeout_s)
                continue
            srv = self._servers.get(name)
            if srv is None:
                self._start_server(spec)
            else:
                srv.spec = spec
                self._set_status(name, notify=False, trusted=spec.trusted, enabled=True,
                                 timeout_s=spec.timeout_s)
        self._notify()

    @staticmethod
    def _usable(srv: _Server | None, spec: ServerSpec) -> bool:
        """Running (or starting) with the same launch settings, session not dead."""
        if srv is None or srv.launch_key != spec.launch_key() or not srv.alive():
            return False
        return not (srv.ready.done() and srv.client is not None and session_dead(srv.client))

    def _start_server(self, spec: ServerSpec) -> _Server:
        old = self._servers.get(spec.name)
        if old is not None:
            old.stop_event.set()  # never leave an owner task without a way to stop it
        srv = _Server(spec, asyncio.get_running_loop())
        self._servers[spec.name] = srv
        # Tools stay listed through a restart; the new list replaces them.
        self._set_status(spec.name, state=STARTING, error="",
                         trusted=spec.trusted, enabled=spec.enabled, timeout_s=spec.timeout_s)
        srv.task = asyncio.get_running_loop().create_task(self._serve(srv))
        return srv

    async def _stop_server(self, name: str) -> None:
        srv = self._servers.pop(name, None)
        if srv is None:
            return
        srv.stop_event.set()
        if srv.task is not None and not srv.task.done():
            try:
                await asyncio.wait_for(asyncio.shield(srv.task), STOP_TIMEOUT_S + 1.0)
            except (TimeoutError, asyncio.CancelledError):
                srv.task.cancel()
            except Exception:  # noqa: BLE001, S110
                pass

    async def _restart(self, spec: ServerSpec) -> dict:
        await self._stop_server(spec.name)
        # Test works on a switched-off server too; it just isn't reported,
        # and the next apply() stops it again.
        srv = self._start_server(spec)
        try:
            await asyncio.wait_for(asyncio.shield(srv.ready), self.start_timeout_s + 5.0)
        except Exception:  # noqa: BLE001, S110
            pass
        return self.status(spec.name) or {}

    async def _close_client(self, client: Any) -> None:
        try:
            await asyncio.wait_for(client.__aexit__(None, None, None), STOP_TIMEOUT_S)
        except Exception as e:  # noqa: BLE001
            print(f"[BlenderMCP] tool server close: {e!r}")

    async def _serve(self, srv: _Server) -> None:
        name = srv.spec.name
        entered = False
        client = None
        try:
            launch = build_launch(srv.spec)
            client = self._client_factory(launch, self.start_timeout_s)
            srv.client = client
            try:
                await asyncio.wait_for(client.__aenter__(), self.start_timeout_s + 5.0)
            except TimeoutError:
                raise ConnectionError(f"didn't answer within {self.start_timeout_s:g} s") from None
            entered = True
            tools = await asyncio.wait_for(client.list_tools(), LIST_TIMEOUT_S)
            srv.tools = list(tools)
            if self._servers.get(name) is srv:
                self._set_status(name, state=RUNNING, error="", tools=srv.tools,
                                 trusted=srv.spec.trusted, enabled=srv.spec.enabled)
            if not srv.ready.done():
                srv.ready.set_result(True)
            while not srv.stop_event.is_set():
                try:
                    await asyncio.wait_for(srv.stop_event.wait(), WATCH_INTERVAL_S)
                except TimeoutError:
                    pass
                if session_dead(client):
                    raise ConnectionError("the server stopped")
            # A deliberate stop: whoever stopped it sets the status that follows.
        except asyncio.CancelledError:
            raise
        except BaseException as e:  # noqa: BLE001 - anyio groups, process errors
            reason = _reason(e)
            if isinstance(e, ConfigError):
                reason = str(e)
            # Keep the last tool list: the next call restarts the server.
            # A replaced server's late failure mustn't overwrite its successor.
            if self._servers.get(name) is srv:
                self._set_status(name, state=ERROR, error=reason)
            if not srv.ready.done():
                srv.ready.set_exception(ConnectionError(reason))
            print(f"[BlenderMCP] Tool server {name}: {reason}")
        finally:
            if client is not None and entered:
                await self._close_client(client)
            if not srv.ready.done():
                srv.ready.set_exception(ConnectionError("stopped"))

    async def _call(self, spec: ServerSpec, tool: str, arguments: dict,
                    timeout_s: float) -> dict:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        srv = self._servers.get(spec.name)
        if not self._usable(srv, spec):
            if srv is not None:
                await self._stop_server(spec.name)
            # Another call may have started it while we waited for the stop.
            srv = self._servers.get(spec.name)
            if not self._usable(srv, spec):
                srv = self._start_server(spec)
        srv.spec = spec
        try:
            await asyncio.wait_for(asyncio.shield(srv.ready), max(0.1, deadline - loop.time()))
        except TimeoutError:
            return failure(f"{spec.name} didn't start within {timeout_s:g} s")
        except Exception as e:  # noqa: BLE001
            return failure(f"{spec.name} couldn't start: {e}")
        try:
            result = await asyncio.wait_for(
                srv.client.call_tool(tool, arguments or {}, raise_on_error=False),
                max(0.1, deadline - loop.time()),
            )
        except TimeoutError:
            return failure(f"{spec.name} · {tool} timed out after {timeout_s:g} s")
        except Exception as e:  # noqa: BLE001
            reason = _reason(e)
            if type(e).__name__ == "McpError" and not session_dead(srv.client):
                return failure(f"{spec.name} · {tool}: {reason}")
            # The session is broken; drop it so the next call starts fresh.
            await self._stop_server(spec.name)
            self._set_status(spec.name, state=ERROR, error=reason)
            return failure(f"{spec.name} failed ({reason}); it restarts on the next call")
        return shape_result(result)

    async def _shutdown(self) -> None:
        for name in list(self._servers):
            await self._stop_server(name)


def describe(status: dict | None) -> tuple[str, str]:
    """(state, text) for a status line: "running · 3 tools", an error, and so on."""
    if not status:
        return "none", "not started"
    state = status.get("state")
    if state == STARTING:
        return state, "starting…"
    if state == RUNNING:
        n = len(status.get("tools") or [])
        return state, f"running · {n} tool{'' if n == 1 else 's'}"
    if state == ERROR:
        return state, str(status.get("error") or "failed")
    return OFF, "off"


def session_dead(client: Any) -> bool:
    """True once a client's session can no longer carry calls.

    fastmcp keeps `is_connected()` True after a stdio server exits; its
    StdioTransport notices the closed pipes in `_is_session_dead`, so ask
    that when it exists (fastmcp 3.x) and fall back to is_connected().
    """
    probe = getattr(getattr(client, "transport", None), "_is_session_dead", None)
    if callable(probe):
        try:
            if probe():
                return True
        except Exception:  # noqa: BLE001, S110
            pass
    try:
        return not client.is_connected()
    except Exception:  # noqa: BLE001
        return True


def _reason(e: BaseException) -> str:
    """Readable one-line reason, digging into exception groups."""
    seen = 0
    while isinstance(e, BaseExceptionGroup) and e.exceptions and seen < 5:
        e = e.exceptions[0]
        seen += 1
    text = str(e).strip().splitlines()
    return (text[0] if text else type(e).__name__)[:300]
