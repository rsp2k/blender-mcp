"""Live round trip against a throwaway Garage (and a Caddy in front of it).

Skipped when docker isn't usable, or with BLENDER_MCP_SKIP_DOCKER_TESTS=1.
Covers what the unit tests can't: Garage bootstrap (layout, key, bucket),
lifecycle rule, age-based pruning that really deletes, presigned PUT
and GET through a real S3 implementation, the addon's upload/download helper
against those URLs, and signatures surviving a Caddy reverse proxy (the
signed Host header has to reach Garage unchanged).
"""

import asyncio
import hashlib
import os
import shutil
import subprocess
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import requests

from addon import object_store as addon_store
from blender_mcp import garage_admin
from blender_mcp import object_storage as os_

GARAGE_IMAGE = "dxflrs/garage:v2.4.1"
CADDY_IMAGE = "caddy:2.10-alpine"
GARAGE_TOML = Path(__file__).resolve().parents[1] / "packaging" / "garage.toml"
ACCESS = "GK" + "ab12" * 6
SECRET = "cd34" * 16
ADMIN_TOKEN = "test-admin-token"


def _docker_ok() -> bool:
    if os.environ.get("BLENDER_MCP_SKIP_DOCKER_TESTS") or not shutil.which("docker"):
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=15,
                              check=False).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


pytestmark = pytest.mark.skipif(not _docker_ok(), reason="docker not available")


def _run(*args, check=True) -> str:
    r = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=300,
                       check=False)
    if check and r.returncode:
        pytest.skip(f"docker {' '.join(args[:2])} failed: {r.stderr.strip()[:200]}")
    return r.stdout.strip()


def _port(name: str, inner: int) -> int:
    return int(_run("port", name, f"{inner}/tcp").splitlines()[0].rsplit(":", 1)[1])


def _wait(url: str, timeout: float = 60, ok_below: int = 500) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if requests.get(url, timeout=2).status_code < ok_below:
                return
        except requests.RequestException:
            pass
        time.sleep(0.5)
    pytest.skip(f"{url} never came up")


@pytest.fixture(scope="module")
def stack():
    tag = uuid.uuid4().hex[:8]
    net, garage, caddy = f"bmcp-test-{tag}", f"bmcp-test-garage-{tag}", f"bmcp-test-caddy-{tag}"
    _run("network", "create", net)
    try:
        _run("run", "-d", "--rm", "--name", garage, "--network", net,
             "-p", "127.0.0.1::3900", "-p", "127.0.0.1::3903",
             "-e", f"GARAGE_RPC_SECRET={os.urandom(32).hex()}",
             "-e", f"GARAGE_ADMIN_TOKEN={ADMIN_TOKEN}",
             "-v", f"{GARAGE_TOML}:/etc/garage.toml:ro", GARAGE_IMAGE)
        # Same shape as the caddy-docker-proxy label (reverse_proxy to the
        # upstream, Host passed through untouched).
        _run("run", "-d", "--rm", "--name", caddy, "--network", net, "-p", "127.0.0.1::80",
             CADDY_IMAGE, "caddy", "reverse-proxy", "--from", ":80", "--to", f"{garage}:3900")
        direct = f"http://127.0.0.1:{_port(garage, 3900)}"
        admin = f"http://127.0.0.1:{_port(garage, 3903)}"
        proxied = f"http://127.0.0.1:{_port(caddy, 80)}"
        _wait(f"{admin}/health", ok_below=600)
        _wait(proxied, ok_below=500)
        yield {"direct": direct, "proxied": proxied, "admin": admin}
    finally:
        _run("rm", "-f", caddy, garage, check=False)
        _run("network", "rm", net, check=False)


def make_store(stack, public: str | None = None, **extra) -> os_.ObjectStore:
    env = {"S3_ENDPOINT": stack["direct"], "S3_PUBLIC_URL": public or stack["proxied"],
           "S3_ACCESS_KEY": ACCESS, "S3_SECRET_KEY": SECRET,
           "GARAGE_ADMIN_URL": stack["admin"], "GARAGE_ADMIN_TOKEN": ADMIN_TOKEN,
           "GARAGE_CAPACITY_GB": "1", "STORAGE_PRUNE_INTERVAL_S": "0",
           "S3_BUCKET": "blender-mcp-test", **extra}
    return os_.ObjectStore(os_.load_config(env))


async def test_bootstrap_bucket_and_lifecycle_rule(stack):
    s = make_store(stack, STORAGE_RETENTION_DAYS="3")
    await s.ensure_ready()
    # Running the bootstrap again changes nothing.
    again = await asyncio.to_thread(
        garage_admin.bootstrap, stack["admin"], ADMIN_TOKEN, access_key=ACCESS,
        secret_key=SECRET, bucket="blender-mcp-test", capacity_bytes=1024**3)
    assert again == {"changed": []}
    await make_store(stack, STORAGE_RETENTION_DAYS="3").ensure_ready()
    rules = s._admin.get_bucket_lifecycle("blender-mcp-test").rules
    mine = [r for r in rules if r.rule_id == os_.LIFECYCLE_RULE_ID]
    assert len(mine) == 1 and mine[0].expiration.days == 3
    # Changing the retention replaces the rule instead of adding a second one.
    await make_store(stack, STORAGE_RETENTION_DAYS="7").ensure_ready()
    rules = s._admin.get_bucket_lifecycle("blender-mcp-test").rules
    assert [r.expiration.days for r in rules if r.rule_id == os_.LIFECYCLE_RULE_ID] == [7]


async def test_prune_deletes_only_expired_objects(stack):
    s = make_store(stack)
    await s.ensure_ready()
    key = os_.make_key("bus-prune", "old.bin")
    r = await asyncio.to_thread(requests.put, s.presign_put(key), data=b"x" * 10, timeout=30)
    assert r.status_code == 200
    assert await s.prune() == 0  # fresh object, 7-day retention
    assert (await s.stat(key))["size"] == 10
    later = datetime.now(UTC) + timedelta(days=8)
    assert await s.prune(now=later) >= 1
    with pytest.raises(os_.StorageError) as e:
        await s.stat(key)
    assert e.value.code == "object_not_found"


async def test_presigned_round_trip_through_caddy(stack):
    s = make_store(stack)
    await s.ensure_ready()
    key = os_.make_key("bus-1", "payload.bin")
    data = os.urandom(300_000)
    r = await asyncio.to_thread(requests.put, s.presign_put(key), data=data, timeout=30)
    assert r.status_code == 200, r.text
    got = await asyncio.to_thread(requests.get, s.presign_get(key), timeout=30)
    assert got.status_code == 200 and got.content == data
    info = await s.stat(key)
    assert info["size"] == len(data) and info["name"] == "payload.bin"
    assert [o["object_key"] for o in await s.list("bus-1")] == [key]
    assert await s.list("bus-2") == []
    await s.delete(key)
    with pytest.raises(os_.StorageError) as e:
        await s.stat(key)
    assert e.value.code == "object_not_found"


async def test_signature_is_bound_to_the_public_host(stack):
    """The signer signs for S3_PUBLIC_URL's host. A proxy that forwards that
    Host works; one that rewrites it (or a client hitting another name) fails."""
    s = make_store(stack, public="http://files.example.test")
    await s.ensure_ready()
    key = os_.make_key("bus-1", "h.txt")
    url = s.presign_put(key).replace("http://files.example.test", stack["proxied"])
    ok = await asyncio.to_thread(requests.put, url, data=b"hello",
                               headers={"Host": "files.example.test"}, timeout=30)
    assert ok.status_code == 200, ok.text
    bad = await asyncio.to_thread(requests.put, url, data=b"hello",
                                headers={"Host": "other.example.test"}, timeout=30)
    assert bad.status_code == 403 and "Invalid signature" in bad.text


async def test_addon_helper_uploads_and_downloads(stack, tmp_path, monkeypatch):
    s = make_store(stack)
    await s.ensure_ready()
    src = tmp_path / "render.png"
    src.write_bytes(os.urandom(2_500_000))
    digest = hashlib.sha256(src.read_bytes()).hexdigest()
    key = os_.make_key("bus-1", "render.png")

    # What the executor does for a store=True command, then what the server adds.
    result = addon_store.attach_stored({"filepath": str(src)},
                                       {"url": s.presign_put(key), "object_key": key})
    assert result["stored"]["state"] == "uploaded" and result["stored"]["sha256"] == digest
    assert (await s.stat(key))["size"] == src.stat().st_size

    # fetch_object: into a per-bus dir, sanitised name, verified size.
    base = tmp_path / "uploads"
    out = addon_store.get_to_dir(s.presign_get(key), base, "../back.png",
                                 expected_size=src.stat().st_size)
    assert out["name"] == "back.png" and out["sha256"] == digest
    assert (base / "back.png").read_bytes() == src.read_bytes()
    assert not list(base.glob(".*.part"))

    # Large files go to a background thread; progress is readable meanwhile.
    monkeypatch.setattr(addon_store, "SYNC_LIMIT", 0)
    t = addon_store.run_transfer(
        "download", "bg.png", src.stat().st_size,
        lambda progress: addon_store.get_to_dir(s.presign_get(key), base, "bg.png",
                                                progress=progress))
    assert t["state"] == "running"
    deadline = time.time() + 30
    while addon_store.TRANSFERS.get(t["transfer_id"])["state"] == "running":
        assert time.time() < deadline
        await asyncio.sleep(0.05)
    done = addon_store.TRANSFERS.get(t["transfer_id"])
    assert done["state"] == "done" and done["bytes"] == src.stat().st_size
    assert done["result"]["sha256"] == digest


async def test_expired_or_wrong_url_fails_cleanly(stack, tmp_path):
    s = make_store(stack)
    await s.ensure_ready()
    missing = os_.make_key("bus-1", "nope.bin")
    with pytest.raises(RuntimeError, match="HTTP 404"):
        addon_store.get_to_dir(s.presign_get(missing), tmp_path, "nope.bin")
    assert not list(tmp_path.glob(".*.part")) and not (tmp_path / "nope.bin").exists()
