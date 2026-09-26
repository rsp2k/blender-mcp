"""Background worker commands: spawn / stop / offer_reload / worker_exit.

spawn_worker, stop_worker and offer_reload run in the user's GUI Blender;
worker_exit runs inside a worker. Each refuses to run on the wrong side,
so a worker can't spawn workers of its own.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import tempfile
import time
from pathlib import Path

import bpy

from ... import state
from ...worker import (
    is_worker_mode,
    needs_token_refresh,
    new_worker_uuid,
    spawn_env,
    worker_deadline,
)
from ..registry import command

WORKER_MAIN = Path(__file__).resolve().parents[2] / "worker_main.py"
TERMINATE_GRACE_S = 5.0


def _require_gui(name: str) -> None:
    if is_worker_mode():
        raise RuntimeError(f"{name} isn't available inside a background worker")


def _prune_dead() -> None:
    for uuid, info in list(state._workers.items()):
        if info["popen"].poll() is not None:
            state._workers.pop(uuid, None)


def _workers_root() -> Path:
    base = Path(bpy.app.tempdir or tempfile.gettempdir()) / "blender_mcp_workers"
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(base, 0o700)
    except OSError:
        pass
    return base


def _terminate(popen: subprocess.Popen, grace_s: float = TERMINATE_GRACE_S) -> int | None:
    """SIGTERM, then SIGKILL if it hasn't exited within ``grace_s``."""
    if popen.poll() is not None:
        return popen.returncode
    popen.terminate()
    try:
        return popen.wait(timeout=grace_s)
    except subprocess.TimeoutExpired:
        popen.kill()
        try:
            return popen.wait(timeout=grace_s)
        except subprocess.TimeoutExpired:
            return None


def stop_all_workers() -> int:
    """Kill every worker this Blender spawned. Called from unregister()."""
    stopped = 0
    for uuid, info in list(state._workers.items()):
        try:
            _terminate(info["popen"], grace_s=2.0)
            stopped += 1
        except Exception as e:  # noqa: BLE001 - best effort during teardown
            print(f"[BlenderMCP] Could not stop worker {uuid}: {e}")
        state._workers.pop(uuid, None)
    return stopped


def _fresh_token(client) -> tuple[str, int]:
    """The parent's access token, refreshed first if it expires soon.

    Only the access token is ever handed to a worker. The refresh runs on
    the client's own event loop, exactly as its watcher would.
    """
    import asyncio

    if needs_token_refresh(time.time(), client.jwt_expires_at) and client.refresh_token \
            and client.loop and client.loop.is_running():
        fut = asyncio.run_coroutine_threadsafe(client._do_refresh_once(), client.loop)
        if fut.result(timeout=30):
            client._rotate_requested = True
    return client.jwt_token, int(client.jwt_expires_at or 0)


class WorkerHandlersMixin:
    """Headless background workers and the result-reload offer."""

    @command("spawn_worker")
    def spawn_worker(self):
        """Save a copy of the scene and start a headless worker on it."""
        _require_gui("spawn_worker")
        from ...preferences import ADDON_PACKAGE_NAME, get_prefs, get_server_base_url

        client = state._client
        if client is None or not client.connected:
            raise RuntimeError("Not connected to the bus")
        prefs = get_prefs()
        _prune_dead()
        limit = max(1, min(4, int(getattr(prefs, "max_workers", 1) or 1)))
        if len(state._workers) >= limit:
            raise RuntimeError(
                f"{len(state._workers)} worker(s) already running (limit {limit}); "
                "stop one with blender_stop_worker or raise Max workers in the addon preferences"
            )

        token, token_exp = _fresh_token(client)
        now = time.time()
        deadline = worker_deadline(now, token_exp)
        if deadline - now < 60:
            raise RuntimeError("The access token expires too soon to start a worker; log in again")

        worker_uuid = new_worker_uuid()
        wdir = _workers_root() / worker_uuid
        wdir.mkdir(mode=0o700)
        snapshot = wdir / "snapshot.blend"
        # copy=True writes the file without changing this session's filepath
        # or its unsaved-changes flag. Relative paths are remapped to the new
        # location; absolute paths and packed data carry over as-is.
        bpy.ops.wm.save_as_mainfile(filepath=str(snapshot), copy=True)
        log_path = wdir / "worker.log"

        label = f"worker of {client.label or client.client_uuid}"
        env = spawn_env(
            os.environ,
            worker_uuid=worker_uuid,
            parent_uuid=client.client_uuid,
            parent_pid=os.getpid(),
            token=token,
            token_exp=token_exp,
            server=get_server_base_url(prefs),
            bus_id=client.bus_id,
            deadline=deadline,
            label=label,
            addon_module=ADDON_PACKAGE_NAME,
        )
        # The child keeps its own copy of the log descriptor.
        with open(log_path, "ab") as log:
            popen = subprocess.Popen(
                [bpy.app.binary_path, "-b", str(snapshot), "--python", str(WORKER_MAIN)],
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                cwd=str(wdir),
                start_new_session=True,
            )
        state._workers[worker_uuid] = {
            "popen": popen,
            "pid": popen.pid,
            "dir": str(wdir),
            "snapshot": str(snapshot),
            "log": str(log_path),
            "deadline": deadline,
            "label": label,
            "started_at": now,
        }
        print(f"[BlenderMCP] Spawned worker {worker_uuid} (pid {popen.pid})")
        return {
            "worker_uuid": worker_uuid,
            "pid": popen.pid,
            "snapshot_path": str(snapshot),
            "worker_dir": str(wdir),
            "log_path": str(log_path),
            "expires_at": deadline,
        }

    @command("stop_worker")
    def stop_worker(self, worker_uuid: str):
        """Terminate a worker this Blender spawned (SIGTERM, then SIGKILL)."""
        _require_gui("stop_worker")
        info = state._workers.get(worker_uuid)
        if info is None:
            return {"worker_uuid": worker_uuid, "exited": True, "known": False}
        returncode = _terminate(info["popen"])
        exited = info["popen"].poll() is not None
        if exited:
            state._workers.pop(worker_uuid, None)
        return {"worker_uuid": worker_uuid, "pid": info["pid"], "exited": exited,
                "returncode": returncode, "worker_dir": info["dir"], "known": True}

    @command("list_workers")
    def list_workers(self):
        """Workers this Blender spawned that are still running."""
        _require_gui("list_workers")
        _prune_dead()
        return {"workers": [
            {"worker_uuid": uuid, "pid": w["pid"], "worker_dir": w["dir"],
             "log_path": w["log"], "expires_at": w["deadline"]}
            for uuid, w in state._workers.items()
        ]}

    @command("offer_reload")
    def offer_reload(self, path: str, message: str = ""):
        """Show a banner offering to open ``path`` in this Blender. Never
        reloads on its own; the user clicks Reload or Dismiss."""
        _require_gui("offer_reload")
        p = Path(path)
        if p.suffix.lower() != ".blend":
            raise ValueError("path must be a .blend file")
        if not p.is_file():
            raise FileNotFoundError(f"{path} doesn't exist on this machine")
        state._pending_reload = {"path": str(p), "message": message or "",
                                 "offered_at": time.time()}
        with contextlib.suppress(Exception):  # the redraw is cosmetic
            from ...client.bus_client import _request_ui_redraw
            _request_ui_redraw()
        return {"offered": True, "path": str(p), "unsaved_changes": bool(bpy.data.is_dirty)}

    @command("worker_exit")
    def worker_exit(self):
        """Ask this worker to finish: it unregisters and exits after replying."""
        if not is_worker_mode():
            raise RuntimeError("worker_exit only runs inside a background worker")
        state._worker_stop_requested = True
        return {"exiting": True}
