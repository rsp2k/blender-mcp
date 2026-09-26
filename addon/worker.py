"""Headless background workers.

A GUI Blender can spawn a headless ``blender -b`` on a saved copy of its
scene. The worker joins the bus as its own client (role ``worker``,
``parent_uuid`` = the spawner), so heavy jobs run there while the GUI
stays responsive.

The worker process is configured entirely through environment variables
set by the parent (see ``spawn_env``). Worker mode never reads or writes
the user's AddonPreferences tokens or identity files: it uses the
parent's short-lived access token from the environment and an ephemeral
uuid.

``blender -b`` runs no event loop, so ``bpy.app.timers`` never fire and
Blender exits once its command-line scripts return. The worker therefore
runs ``worker_main.py`` via ``--python``, which calls ``run_worker_loop``:
a plain loop on the main thread that drives the job drainer and checks
the exit conditions.

The top of this module imports nothing from bpy so the decision logic
can be unit-tested outside Blender.
"""

from __future__ import annotations

import os
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

ENV_FLAG = "BLENDER_MCP_WORKER"
ENV_UUID = "BLENDER_MCP_WORKER_UUID"
ENV_PARENT = "BLENDER_MCP_WORKER_PARENT"
ENV_PARENT_PID = "BLENDER_MCP_WORKER_PARENT_PID"
ENV_TOKEN = "BLENDER_MCP_WORKER_TOKEN"
ENV_TOKEN_EXP = "BLENDER_MCP_WORKER_TOKEN_EXP"
ENV_SERVER = "BLENDER_MCP_SERVER"
ENV_BUS = "BLENDER_MCP_BUS_ID"
ENV_DEADLINE = "BLENDER_MCP_WORKER_DEADLINE"
ENV_LABEL = "BLENDER_MCP_WORKER_LABEL"
ENV_ADDON_MODULE = "BLENDER_MCP_ADDON_MODULE"

ROLE_WORKER = "worker"
MAX_LIFETIME_S = 2 * 3600
TOKEN_MARGIN_S = 5 * 60
REFRESH_BELOW_S = 30 * 60
PARENT_CHECK_S = 10.0
LOOP_SLEEP_S = 0.1


def is_worker_mode(env: Mapping[str, str] | None = None) -> bool:
    env = os.environ if env is None else env
    return env.get(ENV_FLAG) == "1"


def new_worker_uuid() -> str:
    return f"blender-worker-{secrets.token_hex(6)}"


@dataclass(frozen=True)
class WorkerConfig:
    uuid: str
    parent_uuid: str
    parent_pid: int
    token: str
    token_exp: int
    server: str
    bus_id: str | None
    deadline: float
    label: str


class WorkerConfigError(ValueError):
    pass


def parse_env(env: Mapping[str, str] | None = None) -> WorkerConfig:
    """Read the worker configuration the parent put in the environment."""
    env = os.environ if env is None else env
    if not is_worker_mode(env):
        raise WorkerConfigError(f"{ENV_FLAG} is not set")
    missing = [k for k in (ENV_UUID, ENV_PARENT, ENV_PARENT_PID, ENV_TOKEN, ENV_SERVER,
                           ENV_DEADLINE) if not env.get(k)]
    if missing:
        raise WorkerConfigError(f"missing {', '.join(missing)}")
    try:
        parent_pid = int(env[ENV_PARENT_PID])
        deadline = float(env[ENV_DEADLINE])
        token_exp = int(env.get(ENV_TOKEN_EXP) or 0)
    except ValueError as e:
        raise WorkerConfigError(f"bad numeric value: {e}") from None
    return WorkerConfig(
        uuid=env[ENV_UUID],
        parent_uuid=env[ENV_PARENT],
        parent_pid=parent_pid,
        token=env[ENV_TOKEN],
        token_exp=token_exp,
        server=env[ENV_SERVER],
        bus_id=env.get(ENV_BUS) or None,
        deadline=deadline,
        label=env.get(ENV_LABEL) or "background worker",
    )


def worker_deadline(now: float, token_exp: int) -> float:
    """Latest time a worker may run: two hours, and never past the token's
    expiry minus a margin (workers can't refresh the parent's token)."""
    deadline = now + MAX_LIFETIME_S
    if token_exp:
        deadline = min(deadline, token_exp - TOKEN_MARGIN_S)
    return deadline


def needs_token_refresh(now: float, token_exp: int) -> bool:
    return bool(token_exp) and token_exp - now < REFRESH_BELOW_S


def exit_reason(
    cfg: WorkerConfig,
    now: float,
    parent_alive: bool,
    stop_requested: bool,
    client_running: bool,
) -> str | None:
    """Why the worker should exit now, or None to keep running."""
    if stop_requested:
        return "stop requested"
    if now >= cfg.deadline:
        return "deadline reached"
    if not parent_alive:
        return f"parent process {cfg.parent_pid} is gone"
    if not client_running:
        return "bus client stopped"
    return None


def spawn_env(
    base_env: Mapping[str, str],
    *,
    worker_uuid: str,
    parent_uuid: str,
    parent_pid: int,
    token: str,
    token_exp: int,
    server: str,
    bus_id: str | None,
    deadline: float,
    label: str,
    addon_module: str,
) -> dict:
    """Environment for a worker process. Only the access token is passed:
    handing over the refresh token would let the worker rotate it and log
    the parent out."""
    env = dict(base_env)
    env.update({
        ENV_FLAG: "1",
        ENV_UUID: worker_uuid,
        ENV_PARENT: parent_uuid,
        ENV_PARENT_PID: str(parent_pid),
        ENV_TOKEN: token,
        ENV_TOKEN_EXP: str(int(token_exp or 0)),
        ENV_SERVER: server,
        ENV_DEADLINE: str(int(deadline)),
        ENV_LABEL: label,
        ENV_ADDON_MODULE: addon_module,
    })
    if bus_id:
        env[ENV_BUS] = bus_id
    else:
        env.pop(ENV_BUS, None)
    return env


def reload_banner_lines(pending: dict | None, is_dirty: bool) -> list[str]:
    """Text for the "background result ready" banner; empty = no banner."""
    if not pending:
        return []
    lines = [f"Background result ready: {pending.get('message') or 'a background job finished'}"]
    path = pending.get("path") or ""
    if path:
        lines.append(f"File: {os.path.basename(path)}")
    if is_dirty:
        lines.append("Reloading discards your unsaved changes.")
    return lines


# ---- inside the worker process -------------------------------------------

def run_worker_loop(
    cfg: WorkerConfig | None = None,
    *,
    pid_alive: Callable[[int], bool] | None = None,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    client_factory: Callable[..., object] | None = None,
    executor_factory: Callable[[], object] | None = None,
) -> str:
    """Connect as a worker client and serve jobs until an exit condition.

    Runs on Blender's main thread from worker_main.py. Returns the exit
    reason. The client unregisters from the bus on the way out.
    """
    from . import state

    if client_factory is None:
        from .client import BlenderMCPClient as client_factory
    if executor_factory is None:
        from .executor import BlenderCommandExecutor as executor_factory
    if pid_alive is None:
        from .identity import _pid_alive as pid_alive

    cfg = cfg or parse_env()
    state._worker_stop_requested = False

    state._executor = executor_factory()
    client = client_factory(
        server_url=cfg.server,
        jwt_token=cfg.token,
        client_uuid=cfg.uuid,
        executor=state._executor,
        refresh_token="",
        jwt_expires_at=cfg.token_exp,
        label=cfg.label,
        bus_id=cfg.bus_id,
    )
    client.role = ROLE_WORKER
    client.parent_uuid = cfg.parent_uuid
    client.worker_mode = True
    state._client = client
    client.start()
    print(f"[BlenderMCP] Worker {cfg.uuid} starting (parent {cfg.parent_uuid}, "
          f"deadline in {int(cfg.deadline - clock())}s)")

    parent_ok = True
    last_parent_check = 0.0
    reason = None
    try:
        while True:
            now = clock()
            if now - last_parent_check >= PARENT_CHECK_S:
                parent_ok = pid_alive(cfg.parent_pid)
                last_parent_check = now
            reason = exit_reason(
                cfg, now, parent_ok,
                stop_requested=bool(state._worker_stop_requested),
                client_running=client.running,
            )
            if reason:
                break
            interval = client._drain_queue() if client.running else LOOP_SLEEP_S
            if interval is None:
                interval = LOOP_SLEEP_S
            if interval > 0:
                sleep(min(interval, LOOP_SLEEP_S))
    finally:
        if reason == "stop requested":
            # Let the worker_exit reply reach the server before disconnecting.
            sleep(1.0)
        print(f"[BlenderMCP] Worker {cfg.uuid} exiting: {reason or 'error'}")
        try:
            client.stop()
        except Exception as e:  # noqa: BLE001 - exiting anyway
            print(f"[BlenderMCP] Worker client stop failed: {e}")
    return reason or "error"
