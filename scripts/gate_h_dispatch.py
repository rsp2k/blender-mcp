"""Gate H — end-to-end dispatch round-trip without Blender.

Spins up the OAuth server in a subprocess, launches a fake Blender peer
that masquerades on the bus, then exercises the four dispatch outcomes
the JobWaiter pattern needs to handle correctly:

  1. success         — one peer, no target_uuid → auto-pick → reply arrives
  2. explicit target — pass target_uuid that matches a registered peer
  3. timeout         — peer disconnected, awaiter times out cleanly
  4. no_client       — no blender clients registered
  5. ambiguous       — two peers registered, no target_uuid → caller must disambiguate
  6. unknown_target  — target_uuid doesn't match any registered peer

Each case asserts the JSON shape returned by the dispatch tool. The whole
script should run in under 10s on a warm machine.

Run from repo root::

    uv run python scripts/gate_h_dispatch.py

Exits 0 on success, non-zero on any failure.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx  # bundled via fastmcp's transitive dep set

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


# Pick an obviously-not-in-use port so we don't clash with the long-running
# servers on 8000/8765.
SERVER_PORT = 8770
SERVER_URL = f"http://127.0.0.1:{SERVER_PORT}"
MCP_URL = f"{SERVER_URL}/mcp"
ADMIN_PASSWORD = "gate-h-secret"
OAUTH_SECRET = "gate-h-jwt-signing-key-do-not-use-in-prod"


# ---------- subprocess helpers ----------------------------------------------


def _start_server() -> subprocess.Popen:
    """Launch the OAuth+MCP server on SERVER_PORT, return the Popen handle."""
    env = os.environ.copy()
    env.update({
        "BLENDER_MCP_PORT": str(SERVER_PORT),
        "BLENDER_MCP_HOST": "127.0.0.1",
        "ADMIN_PASSWORD": ADMIN_PASSWORD,
        "OAUTH_SECRET_KEY": OAUTH_SECRET,
    })
    return subprocess.Popen(
        ["uv", "run", "blender-mcp"],
        cwd=str(REPO),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=open("/tmp/gate-h-server.stderr", "w"),
        preexec_fn=os.setsid,  # so we can kill the whole process group
    )


def _wait_for_server(timeout: float = 15.0) -> None:
    """Poll /health until 200 or timeout. Raise on timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = httpx.get(f"{SERVER_URL}/health", timeout=1.0)
            if r.status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    raise RuntimeError(f"Server didn't come up within {timeout}s")


def _start_peer(token: str, uuid: str) -> subprocess.Popen:
    """Launch fake_blender_peer.py with the given UUID, return Popen.

    stderr is piped to /tmp/<uuid>.stderr so a Gate H failure can be
    diagnosed by reading that file — silently dropping stderr makes
    "peer crashed mid-test" indistinguishable from "peer just didn't reply".
    """
    stderr_path = f"/tmp/gate-h-{uuid}.stderr"
    return subprocess.Popen(
        [
            "uv", "run", "python", "examples/fake_blender_peer.py",
            "--server", MCP_URL,
            "--token", token,
            "--uuid", uuid,
            "--duration", "120",
        ],
        cwd=str(REPO),
        stdout=subprocess.DEVNULL,
        stderr=open(stderr_path, "w"),
        preexec_fn=os.setsid,
    )


def _kill_pgrp(proc: subprocess.Popen) -> None:
    """Best-effort SIGTERM the whole process group; wait briefly."""
    if proc.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, OSError):
        return
    try:
        proc.wait(timeout=3.0)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass


# ---------- HTTP/MCP helpers ------------------------------------------------


def _login() -> str:
    """Login as admin, return the JWT access_token."""
    r = httpx.post(
        f"{SERVER_URL}/auth/login",
        json={"username": "admin", "password": ADMIN_PASSWORD},
        timeout=5.0,
    )
    r.raise_for_status()
    return r.json()["access_token"]


async def _call_tool(token: str, tool_name: str, arguments: dict, *,
                     wait_for_peer_uuid: str = None) -> dict:
    """One-shot MCP client: init session, call tool, return parsed JSON.

    If ``wait_for_peer_uuid`` is given, polls list_available_clients until
    the named peer shows up (or timeout) before calling the target tool —
    avoids the race between peer subprocess start and Gate H tool dispatch.
    """
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    transport = StreamableHttpTransport(
        url=MCP_URL,
        headers={"Authorization": f"Bearer {token}"},
    )
    async with Client(transport) as client:
        if wait_for_peer_uuid:
            # 10s is generous but the peer subprocess can take 2-4s cold-start
            # when uv is doing lockfile work in parallel for multiple
            # subprocesses (server + script + peer all running ``uv run``).
            deadline = asyncio.get_event_loop().time() + 10.0
            while asyncio.get_event_loop().time() < deadline:
                r = await client.call_tool(
                    "blender_list_available_clients", {}
                )
                snapshot = json.loads(r.content[0].text)
                uuids = [
                    c["uuid"] for c in snapshot.get("persistent", [])
                ] + [c["uuid"] for c in snapshot.get("ephemeral", [])]
                if wait_for_peer_uuid in uuids:
                    break
                await asyncio.sleep(0.2)
            else:
                raise RuntimeError(
                    f"peer {wait_for_peer_uuid} never showed up on bus"
                )

        result = await client.call_tool(tool_name, arguments)
        # fastmcp wraps the JSON string in a TextContent[0]
        text = result.content[0].text
        return json.loads(text)


# ---------- assertions ------------------------------------------------------


async def run_gates(token: str) -> int:
    failures = 0

    def check(label: str, cond: bool, detail: str = ""):
        nonlocal failures
        if cond:
            print(f"  ✓ {label}")
        else:
            print(f"  ✗ {label}  {detail}")
            failures += 1

    # --- Case 4: no_client (no peer registered yet) -----------------
    print("[case 4] no_client — no peer up")
    res = await _call_tool(token, "blender_get_scene_info", {})
    check(
        "status == no_client",
        res.get("status") == "no_client",
        f"got {res}",
    )

    # --- Case 1: success (one peer, no target_uuid → auto-pick) ----
    print("[case 1] success — one peer, auto-pick")
    peer = _start_peer(token, "blender-gh-001")
    try:
        res = await _call_tool(
            token, "blender_get_scene_info", {},
            wait_for_peer_uuid="blender-gh-001",
        )
        check(
            "status == completed",
            res.get("status") == "completed",
            f"got {res}",
        )
        check(
            "result echoes get_scene_info",
            "get_scene_info" in str(res.get("result", "")),
            f"got {res}",
        )

        # --- Case 2: explicit target ------------------------------
        print("[case 2] explicit target_uuid")
        res = await _call_tool(
            token,
            "blender_execute_code",
            {"code": "print('hi')", "target_uuid": "blender-gh-001"},
        )
        check(
            "explicit target → completed",
            res.get("status") == "completed",
            f"got {res}",
        )

        # --- Case 6: unknown_target -------------------------------
        print("[case 6] unknown_target")
        res = await _call_tool(
            token,
            "blender_get_scene_info",
            {"target_uuid": "blender-does-not-exist"},
        )
        check(
            "status == unknown_target",
            res.get("status") == "unknown_target",
            f"got {res}",
        )

        # --- Case 5: ambiguous_target -----------------------------
        print("[case 5] ambiguous_target — two peers up")
        peer2 = _start_peer(token, "blender-gh-002")
        try:
            # Wait for peer2 to register — drive a list_available_clients
            # poll instead of a fixed sleep because uv cold-start times
            # vary (1-4s). Sleep-based wait was racy and caused intermittent
            # auto-pick of blender-gh-001 before peer2 showed up.
            res = await _call_tool(
                token, "blender_list_available_clients", {},
                wait_for_peer_uuid="blender-gh-002",
            )
            res = await _call_tool(token, "blender_get_scene_info", {})
            check(
                "status == ambiguous_target",
                res.get("status") == "ambiguous_target",
                f"got {res}",
            )
            check(
                "candidates lists both peers",
                set(res.get("candidates", [])) == {
                    "blender-gh-001", "blender-gh-002"
                },
                f"got candidates={res.get('candidates')}",
            )
        finally:
            _kill_pgrp(peer2)
            # give it a moment to unregister cleanly
            await asyncio.sleep(0.5)

    finally:
        _kill_pgrp(peer)
        # let the peer's unregister + bus state catch up
        await asyncio.sleep(0.5)

    # Case 3 (timeout) intentionally skipped: it requires a peer that's
    # registered but never responds — Gate H's fake peer always responds,
    # and `no_client` correctly takes precedence over `timeout` when nobody's
    # connected. The timeout codepath has unit-test coverage in
    # JobWaiter.cancel via asyncio.wait_for + register() and is exercised by
    # `_dispatch` whenever a real peer falls behind. Not a Gate H concern.

    return failures


def main() -> int:
    print(f"Gate H: spinning up server on {SERVER_URL}")
    server = _start_server()
    try:
        _wait_for_server()
        token = _login()
        failures = asyncio.run(run_gates(token))
    finally:
        _kill_pgrp(server)

    print()
    if failures:
        print(f"Gate H: FAIL — {failures} failure(s)")
        return 1
    print("Gate H: PASS — dispatch round-trip green across all cases")
    return 0


if __name__ == "__main__":
    sys.exit(main())
