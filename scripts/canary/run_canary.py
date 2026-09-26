# /// script
# requires-python = ">=3.11"
# dependencies = ["fastmcp>=3.3.1,<4"]
# ///
"""GUI canary for the Blender MCP addon against a live containerized Blender.

Replaces the manual rounds run by hand on 2026-09-26: install a build into a
real GUI Blender, then check boot auto-connect, uuid stability across
restart and container recreate, network-drop recovery, worker-death
recovery, long-running jobs (a dispatch outliving its wait is polled to
completion and a queued job is cancelled; SKIPs until the server has the
job API), background workers (spawn, a progress-reporting job on the worker
while the GUI stays responsive, the reload offer, stop; SKIPs until server
and addon support them), the one-click Update now path, and the token
boundary. Every step
prints PASS / FAIL / SKIP with evidence; the exit code is non-zero on any FAIL.

The Blender is driven through the bus itself: blender_execute_code runs
Python in the GUI Blender's main thread. Anything that disables or reloads
the addon is scheduled from a bpy.app.timers callback defined in the
executed snippet, never called inline, because an addon unregistering while
its own job is on the stack segfaults Blender.

Usage (token for the instance's logged-in user in BLENDER_MCP_TOKEN, or in
<compose-dir>/.env.mcp):

    uv run scripts/canary/run_canary.py                      # test what's installed
    uv run scripts/canary/run_canary.py --zip dist/extensions/blender_mcp-X.zip
    uv run scripts/canary/run_canary.py --network            # adds the iptables drop (sudo)
    make canary / make canary-full

Targets ~/claude/blender-docker (service blender-desktop) by default; override
with --compose-dir/--service or CANARY_COMPOSE_DIR/CANARY_SERVICE. For a
dedicated instance instead of the shared reference one:

    blender-instance new <dir>/blender --name canary \\
        --repos "blender_mcp=https://mcp.blender.bet/extensions/index.json" --up

then log in once in that container's Firefox as the token's user.

Only `docker compose restart`, `docker compose up -d --force-recreate` and
`docker compose exec` are run in the compose directory; nothing there is
edited or rebuilt.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MCP_URL = os.environ.get("CANARY_MCP_URL", "https://mcp.blender.bet/")
INDEX_URL = os.environ.get("CANARY_INDEX_URL", "https://mcp.blender.bet/extensions/index.json")
PKG_MODULE = "bl_ext.blender_mcp.blender_mcp"
MARK = "CANARY_JSON:"


# ---------------------------------------------------------------- helpers

def vtuple(v: str) -> tuple[int, ...]:
    return tuple(int(p) for p in v.split("."))


def now_utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Result:
    name: str
    status: str  # PASS / FAIL / SKIP
    detail: str
    seconds: float


@dataclass
class Ctx:
    compose_dir: Path
    service: str
    token: str
    artifacts: Path
    uuid: str | None = None
    hostname: str | None = None
    expected_version: str | None = None
    results: list[Result] = field(default_factory=list)


def compose(ctx: Ctx, *args: str, timeout: float = 600) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "compose", *args], cwd=ctx.compose_dir,
        capture_output=True, text=True, timeout=timeout, check=False,
    )


def container_name(ctx: Ctx) -> str:
    out = compose(ctx, "ps", "-q", ctx.service).stdout.strip()
    if not out:
        raise RuntimeError(f"service {ctx.service} is not running in {ctx.compose_dir}")
    return subprocess.run(
        ["docker", "inspect", "-f", "{{.Name}}", out], capture_output=True, text=True, check=False
    ).stdout.strip().lstrip("/")


def container_logs(ctx: Ctx, since: str) -> str:
    name = container_name(ctx)
    r = subprocess.run(["docker", "logs", "--since", since, name], capture_output=True, text=True, check=False)
    return r.stdout + r.stderr


def container_hostname(ctx: Ctx) -> str:
    return compose(ctx, "exec", "-T", ctx.service, "hostname").stdout.strip()


def crash_file_stat(ctx: Ctx) -> str:
    r = compose(ctx, "exec", "-T", ctx.service, "sh", "-c",
                "stat -c '%s %Y' /tmp/blender.crash.txt 2>/dev/null || echo none")
    return r.stdout.strip()


# ------------------------------------------------------------- bus access

async def call(ctx: Ctx, tool: str, args: dict | None = None, timeout: float = 60) -> Any:
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    transport = StreamableHttpTransport(MCP_URL, headers={"Authorization": f"Bearer {ctx.token}"})
    async with Client(transport, timeout=timeout) as c:
        r = await c.call_tool(tool, args or {}, raise_on_error=False)
        text = r.content[0].text if r.content else json.dumps(r.data)
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return text


def _version_tuple(v: str | None) -> tuple[int, ...]:
    try:
        return tuple(int(p) for p in str(v).split("."))
    except (TypeError, ValueError):
        return ()


async def bus_addon_version(ctx: Ctx) -> tuple[int, ...]:
    """The target client's addon version as the bus reports it; () if unknown.
    Read from the bus rather than via execute_code so a busy GUI can't block it."""
    for c in await clients(ctx):
        if c.get("uuid") == ctx.uuid:
            return _version_tuple(c.get("addon_version"))
    return ()


async def drain_job(ctx: Ctx, job_id: str | None, max_wait: float = 60) -> None:
    """Wait for a job this step started, so a failed step can't leave work queued
    in the GUI that stalls the steps after it."""
    if not job_id:
        return
    deadline = time.time() + max_wait
    while time.time() < deadline:
        try:
            s = await call(ctx, "blender_job_status", {"job_id": job_id, "wait_seconds": 20}, timeout=40)
        except Exception:  # noqa: BLE001 - best-effort cleanup
            return
        if not isinstance(s, dict) or s.get("done") or s.get("status") in (
            "completed", "failed", "cancelled", "lost",
        ):
            return


async def clients(ctx: Ctx) -> list[dict]:
    data = await call(ctx, "blender_list_available_clients", {"include_stale": True})
    if not isinstance(data, dict):
        return []
    out = []
    for key in ("persistent", "ephemeral", "clients"):
        out.extend(c for c in data.get(key, []) or [] if isinstance(c, dict))
    return [c for c in out if c.get("client_type", "blender") == "blender"]


def is_live(c: dict) -> bool:
    if c.get("stale"):
        return False
    ago = c.get("last_seen_seconds_ago")
    return ago is None or ago < 90


async def find_client(ctx: Ctx) -> dict | None:
    live = [c for c in await clients(ctx) if is_live(c)]
    if ctx.hostname:
        by_host = [c for c in live if c.get("hostname") == ctx.hostname]
        if by_host:
            live = by_host
    if ctx.uuid:
        by_uuid = [c for c in live if c.get("uuid") == ctx.uuid]
        if by_uuid:
            return by_uuid[0]
        return None
    return max(live, key=lambda c: c.get("last_seen", 0) or 0) if live else None


async def run_code(ctx: Ctx, code: str, timeout: float = 60) -> dict:
    """Run code in the GUI Blender; the snippet prints MARK + json on one line."""
    data = await call(ctx, "blender_execute_code",
                      {"code": code, "target_uuid": ctx.uuid, "_timeout": timeout},
                      timeout=timeout + 15)
    if not isinstance(data, dict) or data.get("status") not in ("completed", "success", "ok"):
        raise RuntimeError(f"execute_code failed: {str(data)[:300]}")

    def strings(obj):
        if isinstance(obj, str):
            if obj.lstrip().startswith(("{", "[")):
                try:
                    yield from strings(json.loads(obj))
                    return
                except ValueError:
                    pass
            yield obj
        elif isinstance(obj, dict):
            for v in obj.values():
                yield from strings(v)
        elif isinstance(obj, list):
            for v in obj:
                yield from strings(v)

    for text in strings(data.get("result")):
        for line in reversed(text.splitlines()):
            if line.startswith(MARK):
                return json.loads(line[len(MARK):])
    raise RuntimeError(f"no {MARK} in result: {str(data.get('result'))[:300]}")


ADDON_STATE = f"""
import sys, json, os
def _proc_start():
    try:
        with open('/proc/stat') as f:
            btime = next(int(l.split()[1]) for l in f if l.startswith('btime '))
        with open('/proc/self/stat') as f:
            ticks = int(f.read().rsplit(')', 1)[1].split()[19])
        return btime + ticks / os.sysconf('SC_CLK_TCK')
    except Exception:
        return None
m = sys.modules.get({PKG_MODULE!r})
st = sys.modules.get({PKG_MODULE!r} + '.state')
ver = sys.modules.get({PKG_MODULE!r} + '._version')
c = getattr(st, '_client', None) if st else None
print({MARK!r} + json.dumps({{
    'version': getattr(ver, '__version__', None),
    'client_id': id(c) if c is not None else None,
    'uuid': getattr(c, 'client_uuid', None),
    'running': bool(getattr(c, 'running', False)),
    'connected': bool(getattr(c, 'connected', False)),
    'proc_start': _proc_start(),
}}))
"""


async def addon_state(ctx: Ctx, timeout: float = 60) -> dict:
    return await run_code(ctx, ADDON_STATE, timeout=timeout)


async def wait_for_client(ctx: Ctx, timeout: float, version: str | None = None,
                          started_after: float = 0.0) -> dict | None:
    """Poll until the expected client is live on the bus and answers a job.

    ``started_after``: require the Blender process to have started after
    this epoch time (proves a fresh process registered, since connected_at
    survives re-registration). ``version``: require that addon version.
    Returns the bus client dict merged with the in-process addon state.
    """
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        try:
            c = await find_client(ctx)
            if c:
                saved, ctx.uuid = ctx.uuid, c["uuid"]
                try:
                    # Short: a job sent while Blender restarts waits the full
                    # timeout, and the old registration still looks live.
                    st = await addon_state(ctx, timeout=8)
                finally:
                    ctx.uuid = saved
                merged = {**c, "addon": st}
                last = merged
                ok = st.get("connected") and st.get("uuid") == c["uuid"]
                if started_after:
                    ok = ok and (st.get("proc_start") or 0) >= started_after - 2
                if version:
                    ok = ok and st.get("version") == version
                if ok:
                    return merged
        except Exception as e:  # server/bus hiccups while Blender restarts
            last = {"error": str(e)[:200]}
        await asyncio.sleep(3)
    return {"_timeout_last_seen": last}


# ------------------------------------------------------------------ steps

async def step(ctx: Ctx, name: str, fn) -> None:
    t0 = time.monotonic()
    try:
        status, detail = await fn()
    except Exception as e:
        status, detail = "FAIL", f"{type(e).__name__}: {e}"
    r = Result(name, status, detail, time.monotonic() - t0)
    ctx.results.append(r)
    print(f"[{r.status:4}] {name:16} {r.seconds:6.1f}s  {detail}", flush=True)


async def s_install(ctx: Ctx, zip_path: Path | None):
    if zip_path is None:
        return "SKIP", "no --zip; testing the installed build"
    with zipfile.ZipFile(zip_path) as zf:
        manifest = zf.read("blender_manifest.toml").decode()
    ctx.expected_version = next(
        line.split("=", 1)[1].strip().strip('"')
        for line in manifest.splitlines() if line.strip().startswith("version")
    )
    projects = ctx.compose_dir / "projects"
    rel = f"projects/{zip_path.name}"
    ig = subprocess.run(["git", "-C", str(ctx.compose_dir), "check-ignore", "-q", rel], check=False)
    if ig.returncode not in (0, 128):  # 128: not a git repo
        return "FAIL", f"{rel} is not gitignored in {ctx.compose_dir}; refusing to copy"
    dest = projects / zip_path.name
    shutil.copy2(zip_path, dest)
    try:
        r = compose(ctx, "exec", "-T", ctx.service, "blender", "-b", "--command",
                    "extension", "install-file", "-r", "blender_mcp", "--enable",
                    f"/home/blender/projects/{zip_path.name}")
        out = (r.stdout + r.stderr)[-400:]
        if r.returncode != 0 or ("Installed" not in out and "Reinstalled" not in out):
            return "FAIL", f"install-file rc={r.returncode}: {out.strip()}"
    finally:
        dest.unlink(missing_ok=True)
    compose(ctx, "restart", ctx.service)
    return "PASS", f"installed {ctx.expected_version} into repo slot blender_mcp, restarted"


async def s_boot(ctx: Ctx):
    t0 = time.time()
    ctx.hostname = container_hostname(ctx)
    c = await wait_for_client(ctx, 90, version=ctx.expected_version)
    if "_timeout_last_seen" in c:
        return "FAIL", f"no live client (hostname={ctx.hostname}) within 90s; last={c}"
    ctx.uuid = c["uuid"]
    st = await addon_state(ctx)
    scene = await call(ctx, "blender_get_scene_info", {"target_uuid": ctx.uuid})
    ok = isinstance(scene, dict) and scene.get("status") in ("completed", "success", "ok")
    if not ok:
        return "FAIL", f"dispatch failed: {str(scene)[:200]}"
    return "PASS", (f"uuid={ctx.uuid} version={st.get('version')} host={ctx.hostname} "
                    f"live after {time.time() - t0:.0f}s, get_scene_info ok")


async def _identity_after(ctx: Ctx, action: list[str], label: str):
    before = ctx.uuid
    t_action = time.time()
    r = compose(ctx, *action)
    if r.returncode != 0:
        return "FAIL", f"{' '.join(action)} rc={r.returncode}: {r.stderr[-200:]}"
    ctx.hostname = container_hostname(ctx)
    ctx.uuid = None  # accept whatever registers, then compare
    c = await wait_for_client(ctx, 120, started_after=t_action)
    ctx.uuid = before
    if "_timeout_last_seen" in c:
        return "FAIL", f"no re-registration within 120s after {label}; last={c}"
    if c["uuid"] != before:
        return "FAIL", f"uuid changed across {label}: {before} -> {c['uuid']}"
    return "PASS", f"same uuid after {label}, re-registered in {time.time() - t_action:.0f}s"


async def s_restart(ctx: Ctx):
    return await _identity_after(ctx, ["restart", ctx.service], "restart")


async def s_recreate(ctx: Ctx):
    return await _identity_after(ctx, ["up", "-d", "--force-recreate", ctx.service], "recreate")


async def s_network(ctx: Ctx, enabled: bool):
    if not enabled:
        return "SKIP", "pass --network to run (needs passwordless sudo)"
    if subprocess.run(["sudo", "-n", "true"], capture_output=True, check=False).returncode != 0:
        return "SKIP", "passwordless sudo unavailable"
    ip = socket.gethostbyname(MCP_URL.split("//", 1)[1].split("/", 1)[0])
    pid = subprocess.run(["docker", "inspect", "-f", "{{.State.Pid}}", container_name(ctx)],
                         capture_output=True, text=True, check=False).stdout.strip()
    rule = ["OUTPUT", "-d", ip, "-j", "DROP"]
    nsenter = ["sudo", "-n", "nsenter", "-t", pid, "-n", "iptables"]
    since = now_utc()
    subprocess.run(nsenter + ["-A", *rule], check=True)
    try:
        deadline = time.monotonic() + 90
        seen = False
        while time.monotonic() < deadline and not seen:
            await asyncio.sleep(5)
            seen = "heartbeat timed out" in container_logs(ctx, since)
    finally:
        subprocess.run(nsenter + ["-D", *rule], check=False)
    if not seen:
        return "FAIL", f"no 'heartbeat timed out' within 90s of dropping {ip}"
    t_restore = time.time()
    restore_since = now_utc()
    want = f"Registered as {ctx.uuid}"
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        await asyncio.sleep(3)
        if want in container_logs(ctx, restore_since):
            st = await addon_state(ctx)
            if st.get("connected") and st.get("uuid") == ctx.uuid:
                return "PASS", (f"heartbeat timeout detected, re-registered "
                                f"{time.time() - t_restore:.0f}s after restoring {ip}")
    return "FAIL", f"no '{want}' within 120s of restoring {ip}"


KILL_WORKER = f"""
import sys, json, bpy
st = sys.modules[{PKG_MODULE!r} + '.state']
old = st._client
def _kill():
    try:
        old.running = False
    except Exception as e:
        print('kill failed', e)
    return None
bpy.app.timers.register(_kill, first_interval=0.5)
print({MARK!r} + json.dumps({{'old_client_id': id(old)}}))
"""


async def s_worker_death(ctx: Ctx):
    before = await run_code(ctx, KILL_WORKER)
    t0 = time.time()
    deadline = time.monotonic() + 90
    st = {}
    while time.monotonic() < deadline:
        await asyncio.sleep(3)
        try:
            st = await addon_state(ctx)
        except Exception:
            continue
        if st.get("client_id") and st["client_id"] != before["old_client_id"] and st.get("connected"):
            break
    else:
        return "FAIL", f"supervisor did not replace the client within 90s; state={st}"
    if st.get("uuid") != ctx.uuid:
        return "FAIL", f"replacement client has a different uuid: {st.get('uuid')}"
    scene = await call(ctx, "blender_get_scene_info", {"target_uuid": ctx.uuid})
    if not (isinstance(scene, dict) and scene.get("status") in ("completed", "success", "ok")):
        return "FAIL", f"dispatch after restart failed: {str(scene)[:200]}"
    return "PASS", f"new client object, same uuid, connected in {time.time() - t0:.0f}s, dispatch ok"


def published_version() -> str | None:
    try:
        with urllib.request.urlopen(INDEX_URL, timeout=15) as r:
            data = json.load(r)
        return next(e["version"] for e in data["data"] if e["id"] == "blender_mcp")
    except Exception:
        return None


UPDATE_NOW = f"""
import sys, json, bpy
def _click():
    try:
        bpy.ops.blendermcp.install_update()
    except Exception as e:
        print('install_update failed', e)
    return None
bpy.app.timers.register(_click, first_interval=0.5)
print({MARK!r} + json.dumps({{'scheduled': True}}))
"""


async def s_update_now(ctx: Ctx, target: str | None):
    installed = (await addon_state(ctx)).get("version")
    offered = target or published_version()
    if not offered:
        return "SKIP", "could not read the published version"
    if not installed or vtuple(offered) <= vtuple(installed):
        return "SKIP", f"no newer version to update to (installed {installed}, published {offered})"
    crash_before = crash_file_stat(ctx)
    t0 = time.time()
    await run_code(ctx, UPDATE_NOW)
    c = await wait_for_client(ctx, 240, version=offered)
    if "_timeout_last_seen" in c:
        return "FAIL", f"did not come back on {offered} within 240s; last={c}"
    if crash_file_stat(ctx) != crash_before:
        return "FAIL", "Blender crash file changed during the update"
    if c.get("uuid") != ctx.uuid:
        return "FAIL", f"uuid changed across update: {c.get('uuid')}"
    return "PASS", f"{installed} -> {offered} in {time.time() - t0:.0f}s, same uuid, no crash"


HAS_CANCEL = f"""
import sys, json
mod = sys.modules.get('bl_ext.blender_mcp.blender_mcp.client.message_pump')
print({MARK!r} + json.dumps({{"cancel": bool(mod and hasattr(mod, 'cancel_queued_job'))}}))
"""


async def s_jobs(ctx: Ctx):
    """A dispatch outliving its wait becomes a job; poll it to completion; cancel a queued one."""
    probe = await call(ctx, "blender_job_status", {"job_id": "j-canaryprobe0"})
    if not isinstance(probe, dict) or probe.get("error") != "job_not_found":
        return "SKIP", f"server has no job API yet ({str(probe)[:80]})"
    addon_cancels = (await run_code(ctx, HAS_CANCEL)).get("cancel", False)

    t0 = time.time()
    first = await call(ctx, "blender_execute_code", {
        "code": "import time; time.sleep(25); print('job-ok')",
        "target_uuid": ctx.uuid, "_timeout": 5,
    }, timeout=30)
    if not isinstance(first, dict) or first.get("status") not in ("queued", "running"):
        return "FAIL", f"timed-out dispatch didn't become a job: {str(first)[:200]}"
    job_id = first["job_id"]

    cancel_note = "cancel skipped (addon predates job_cancel)"
    if addon_cancels:
        queued = await call(ctx, "blender_submit", {
            "command": "execute_code", "params": {"code": "print('should not run')"},
            "target_uuid": ctx.uuid,
        })
        queued_id = queued.get("job_id") if isinstance(queued, dict) else None
        if not queued_id:
            await drain_job(ctx, job_id)
            return "FAIL", f"blender_submit returned no job_id: {str(queued)[:300]}"
        c = await call(ctx, "blender_job_cancel", {"job_id": queued_id})
        if not (isinstance(c, dict) and c.get("job_status") == "cancelled"):
            await drain_job(ctx, job_id)
            await drain_job(ctx, queued_id)
            return "FAIL", f"cancel of queued job failed: {str(c)[:200]}"
        cancel_note = "queued job cancelled"

    status = {}
    for _ in range(3):
        status = await call(ctx, "blender_job_status", {"job_id": job_id, "wait_seconds": 40},
                            timeout=60)
        if status.get("done"):
            break
    result = await call(ctx, "blender_job_result", {"job_id": job_id})
    if result.get("status") != "completed" or "job-ok" not in (result.get("result") or ""):
        return "FAIL", f"job {job_id} ended {result.get('status')}: {str(result)[:200]}"
    return "PASS", (f"execute_code outlived its 5s wait as {first['status']} {job_id}, "
                    f"completed after {time.time() - t0:.0f}s; {cancel_note}")


async def has_tool(ctx: Ctx, name: str) -> bool:
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    transport = StreamableHttpTransport(MCP_URL, headers={"Authorization": f"Bearer {ctx.token}"})
    async with Client(transport, timeout=60) as c:
        return any(t.name == name for t in await c.list_tools())


HAS_WORKERS = f"""
import sys, json
reg = sys.modules.get({PKG_MODULE!r} + '.executor.registry')
print({MARK!r} + json.dumps({{"workers": bool(reg and 'spawn_worker' in reg.COMMAND_REGISTRY)}}))
"""

WORKER_JOB = """
import os, time
import bpy
for i in range(10):
    time.sleep(2)
    report_progress((i + 1) / 10, f"step {i + 1}/10")
out = os.path.join(os.path.dirname(bpy.data.filepath), "canary_result.blend")
bpy.ops.wm.save_as_mainfile(filepath=out, copy=True)
print("RESULT " + out)
"""


def gui_worker_state(uuid: str, pid: int) -> str:
    return f"""
import sys, json, os
st = sys.modules.get({PKG_MODULE!r} + '.state')
pending = getattr(st, '_pending_reload', None) if st else None
try:
    os.kill({pid}, 0)
    alive = True
except ProcessLookupError:
    alive = False
except PermissionError:
    alive = True
print({MARK!r} + json.dumps({{
    'tracked': {uuid!r} in (getattr(st, '_workers', {{}}) or {{}}),
    'pid_alive': alive,
    'pending_reload': pending,
}}))
"""


CLEAR_RELOAD = f"""
import sys, json
st = sys.modules.get({PKG_MODULE!r} + '.state')
st._pending_reload = None
print({MARK!r} + json.dumps({{"cleared": True}}))
"""


# Pickup includes the harness's own overhead: each call opens a fresh MCP
# session (~0.5-1 s over the internet), so ~1.5-2 s is the floor seen here for
# a trivial job delivered on the event stream. The pull fallback polls every
# 10 s, so a pickup above 5 s means the stream missed the dispatch.
LATENCY_LIMIT_S = 5.0
PULLED_MARK = "recovered by pull"


async def s_dispatch_latency(ctx: Ctx):
    """Time from dispatch to Blender picking the job up, for trivial jobs, and
    which path delivered it (event stream vs the pull fallback).

    Also checks each job reaches a terminal state: a lost update once left a
    completed job stuck as "running" (its "running" report committed after the
    completion), which the old version of this step counted as a pickup.
    """
    if not await has_tool(ctx, "blender_submit"):
        return "SKIP", "server has no job API yet"
    since = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - 2)) + "Z"
    results = []  # (job_id, pickup seconds, final status)
    for i in range(3):
        t0 = time.time()
        sub = await call(ctx, "blender_submit", {
            "command": "execute_code", "params": {"code": f"print('latency-{i}')"},
            "target_uuid": ctx.uuid,
        })
        job_id = sub.get("job_id") if isinstance(sub, dict) else None
        if not job_id:
            return "FAIL", f"blender_submit returned no job_id: {str(sub)[:300]}"
        picked, status = None, None
        while time.time() - t0 < LATENCY_LIMIT_S + 25:
            st = await call(ctx, "blender_job_status", {"job_id": job_id, "wait_seconds": 5},
                            timeout=30)
            status = st.get("status") if isinstance(st, dict) else None
            if picked is None and status not in ("queued", None):
                picked = time.time() - t0
            if isinstance(st, dict) and st.get("done"):
                break
        if picked is None:
            return "FAIL", f"job {job_id} still queued after {LATENCY_LIMIT_S + 25:.0f}s"
        results.append((job_id, picked, status))
        await asyncio.sleep(1)

    logs = container_logs(ctx, since)
    paths = ["pull" if f"{job_id} {PULLED_MARK}" in logs else "stream" for job_id, _p, _s in results]
    detail = "pickup " + ", ".join(
        f"{p:.1f}s/{path}" for (_j, p, _s), path in zip(results, paths)
    )
    stuck = [(j, s) for j, _p, s in results if s != "completed"]
    if stuck:
        return "FAIL", f"{detail}; not completed: {stuck}"
    if max(p for _j, p, _s in results) > LATENCY_LIMIT_S:
        return "FAIL", f"{detail} (limit {LATENCY_LIMIT_S:g}s)"
    return "PASS", detail


async def s_worker(ctx: Ctx):
    """Spawn a background worker, run a progress-reporting job there while the GUI
    stays responsive, offer the result for reload, then stop the worker."""
    if not await has_tool(ctx, "blender_spawn_worker"):
        return "SKIP", "server has no worker tools yet"
    version = await bus_addon_version(ctx)
    if version and version < (2026, 926, 11):
        return "SKIP", "installed addon predates background workers"
    if not version and not (await run_code(ctx, HAS_WORKERS)).get("workers"):
        return "SKIP", "installed addon predates background workers"

    t0 = time.time()
    spawn = await call(ctx, "blender_spawn_worker", {"target_uuid": ctx.uuid, "timeout_s": 120},
                       timeout=180)
    if not isinstance(spawn, dict) or spawn.get("status") != "ok":
        return "FAIL", f"spawn failed: {str(spawn)[:300]}"
    worker_uuid, pid = spawn["worker_uuid"], spawn["pid"]
    spawned_in = time.time() - t0
    try:
        listed = [c for c in await clients(ctx) if c.get("uuid") == worker_uuid]
        if not listed or listed[0].get("role") != "worker" or listed[0].get("parent_uuid") != ctx.uuid:
            return "FAIL", f"worker not listed with role/parent: {str(listed)[:300]}"

        job = await call(ctx, "blender_submit", {
            "command": "execute_code", "params": {"code": WORKER_JOB}, "target_uuid": worker_uuid,
        })
        if not isinstance(job, dict) or job.get("status") != "queued":
            return "FAIL", f"submit to worker failed: {str(job)[:300]}"
        job_id = job["job_id"]

        await asyncio.sleep(4)
        g0 = time.time()
        gui = await call(ctx, "blender_get_scene_info", {"target_uuid": ctx.uuid, "_timeout": 15},
                         timeout=30)
        gui_s = time.time() - g0
        if not isinstance(gui, dict) or gui.get("status") != "completed" or gui_s > 5:
            return "FAIL", f"GUI not responsive while worker ran ({gui_s:.1f}s): {str(gui)[:200]}"

        progress_seen = None
        status = {}
        for _ in range(8):
            status = await call(ctx, "blender_job_status", {"job_id": job_id, "wait_seconds": 10},
                                timeout=30)
            if status.get("progress") and not status.get("done"):
                progress_seen = progress_seen or status["progress"]
            if status.get("done"):
                break
        result = await call(ctx, "blender_job_result", {"job_id": job_id})
        text = result.get("result") or ""
        if result.get("status") != "completed" or "RESULT " not in text:
            return "FAIL", f"worker job ended {result.get('status')}: {str(result)[:300]}"
        if not progress_seen:
            return "FAIL", "job completed but no progress was ever reported"
        # The result is JSON-encoded, so the print's newline arrives as a
        # literal backslash-n; capture exactly up to the .blend suffix.
        m = re.search(r"RESULT (\S+?\.blend)", text)
        if not m:
            return "FAIL", f"no .blend path in worker output: {text[:300]}"
        result_path = m.group(1)

        offer = await call(ctx, "blender_offer_reload", {
            "path": result_path, "message": "canary worker result", "target_uuid": ctx.uuid,
        })
        if not isinstance(offer, dict) or offer.get("status") != "completed":
            return "FAIL", f"offer_reload failed: {str(offer)[:300]}"
        st = await run_code(ctx, gui_worker_state(worker_uuid, pid))
        if (st.get("pending_reload") or {}).get("path") != result_path:
            return "FAIL", f"banner state not set: {st}"
        await run_code(ctx, CLEAR_RELOAD)
    finally:
        stop = await call(ctx, "blender_stop_worker", {"worker_uuid": worker_uuid}, timeout=90)

    if not (isinstance(stop, dict) and stop.get("stopped")):
        return "FAIL", f"stop failed: {str(stop)[:300]}"
    await asyncio.sleep(2)
    if any(c.get("uuid") == worker_uuid for c in await clients(ctx)):
        return "FAIL", "worker still registered after stop"
    st = await run_code(ctx, gui_worker_state(worker_uuid, pid))
    if st.get("pid_alive") or st.get("tracked"):
        return "FAIL", f"worker process survived stop: {st}"
    return "PASS", (f"spawned {worker_uuid} in {spawned_in:.0f}s; GUI answered in {gui_s:.1f}s "
                    f"mid-job; progress {progress_seen.get('fraction')} "
                    f"'{progress_seen.get('message')}'; result offered; stopped "
                    f"({stop.get('how')}), process gone")


async def s_token_boundary(ctx: Ctx):
    r = await call(ctx, "blender_create_access_token", {"name": "canary-should-be-refused"})
    if isinstance(r, dict) and r.get("error") == "oauth_required":
        return "PASS", "token-authenticated mint refused (oauth_required)"
    return "FAIL", f"expected oauth_required, got {str(r)[:200]}"


# ------------------------------------------------------------------- main

def load_token(compose_dir: Path) -> str:
    tok = os.environ.get("BLENDER_MCP_TOKEN")
    env_file = compose_dir / ".env.mcp"
    if not tok and env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.startswith("BLENDER_MCP_TOKEN="):
                tok = line.split("=", 1)[1].strip()
    if not tok:
        sys.exit(f"no BLENDER_MCP_TOKEN in env or {env_file}")
    return tok


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--compose-dir", default=os.environ.get("CANARY_COMPOSE_DIR", "~/claude/blender-docker"))
    ap.add_argument("--service", default=os.environ.get("CANARY_SERVICE", "blender-desktop"))
    ap.add_argument("--zip", type=Path, help="extension zip to install first")
    ap.add_argument("--network", action="store_true", help="run the iptables network-drop step (sudo)")
    ap.add_argument("--update-to-version", help="version Update now should reach (default: published index)")
    ap.add_argument("--artifacts", default=os.environ.get("CANARY_ARTIFACTS", "artifacts/canary"))
    args = ap.parse_args()

    compose_dir = Path(args.compose_dir).expanduser().resolve()
    run_dir = Path(args.artifacts) / datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    ctx = Ctx(compose_dir, args.service, load_token(compose_dir), run_dir)
    since = now_utc()
    t_all = time.monotonic()

    await step(ctx, "install", lambda: s_install(ctx, args.zip.resolve() if args.zip else None))
    await step(ctx, "boot", lambda: s_boot(ctx))
    if ctx.uuid:
        await step(ctx, "restart-uuid", lambda: s_restart(ctx))
        await step(ctx, "recreate-uuid", lambda: s_recreate(ctx))
        await step(ctx, "network-drop", lambda: s_network(ctx, args.network))
        await step(ctx, "worker-death", lambda: s_worker_death(ctx))
        await step(ctx, "jobs", lambda: s_jobs(ctx))
        await step(ctx, "dispatch-latency", lambda: s_dispatch_latency(ctx))
        await step(ctx, "worker", lambda: s_worker(ctx))
        await step(ctx, "update-now", lambda: s_update_now(ctx, args.update_to_version))
    await step(ctx, "token-boundary", lambda: s_token_boundary(ctx))

    logs = container_logs(ctx, since)
    (run_dir / "container.log").write_text(logs)
    tb = [ln for ln in logs.splitlines() if "Traceback" in ln]
    addon_tb = "blender_mcp" in logs and tb
    ctx.results.append(Result(
        "log-scan", "FAIL" if addon_tb else "PASS",
        f"{len(tb)} Traceback line(s) in container log" if tb else "no tracebacks in container log",
        0.0,
    ))
    print(f"[{ctx.results[-1].status:4}] {'log-scan':16} {'':>6}   {ctx.results[-1].detail}")

    summary = {
        "started": since, "seconds": round(time.monotonic() - t_all, 1),
        "compose_dir": str(compose_dir), "uuid": ctx.uuid,
        "results": [r.__dict__ for r in ctx.results],
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    fails = [r for r in ctx.results if r.status == "FAIL"]
    print(f"\n{len(ctx.results) - len(fails)} ok, {len(fails)} failed in {summary['seconds']}s; artifacts: {run_dir}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
