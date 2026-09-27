"""Live round trip against a throwaway MinIO (and a Caddy in front of it).

Skipped when docker isn't usable, or with BLENDER_MCP_SKIP_DOCKER_TESTS=1.
Covers what the unit tests can't: bucket + lifecycle setup, presigned PUT
and GET through a real S3 implementation, the addon's upload/download helper
against those URLs, and signatures surviving a Caddy reverse proxy (the
signed Host header has to reach MinIO unchanged).
"""

import asyncio
import hashlib
import os
import shutil
import subprocess
import time
import uuid

import pytest
import requests

from addon import object_store as addon_store
from blender_mcp import object_storage as os_

MINIO_IMAGE = "pgsty/minio:RELEASE.2026-08-04T00-00-00Z"
CADDY_IMAGE = "caddy:2.10-alpine"
USER, PASSWORD = "testroot", "testroot-password"


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


def _wait(url: str, timeout: float = 60) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if requests.get(url, timeout=2).status_code < 500:
                return
        except requests.RequestException:
            pass
        time.sleep(0.5)
    pytest.skip(f"{url} never came up")


@pytest.fixture(scope="module")
def stack():
    tag = uuid.uuid4().hex[:8]
    net, minio, caddy = f"bmcp-test-{tag}", f"bmcp-test-minio-{tag}", f"bmcp-test-caddy-{tag}"
    _run("network", "create", net)
    try:
        _run("run", "-d", "--rm", "--name", minio, "--network", net, "-p", "127.0.0.1::9000",
             "-e", f"MINIO_ROOT_USER={USER}", "-e", f"MINIO_ROOT_PASSWORD={PASSWORD}",
             MINIO_IMAGE, "server", "/data")
        # Same shape as the caddy-docker-proxy label (reverse_proxy to the
        # upstream, Host passed through untouched).
        _run("run", "-d", "--rm", "--name", caddy, "--network", net, "-p", "127.0.0.1::80",
             CADDY_IMAGE, "caddy", "reverse-proxy", "--from", ":80", "--to", f"{minio}:9000")
        direct = f"http://127.0.0.1:{_port(minio, 9000)}"
        proxied = f"http://127.0.0.1:{_port(caddy, 80)}"
        _wait(f"{direct}/minio/health/live")
        _wait(f"{proxied}/minio/health/live")
        yield {"direct": direct, "proxied": proxied}
    finally:
        _run("rm", "-f", caddy, minio, check=False)
        _run("network", "rm", net, check=False)


def make_store(internal: str, public: str, **extra) -> os_.ObjectStore:
    env = {"MINIO_ENDPOINT": internal, "MINIO_PUBLIC_URL": public,
           "MINIO_ROOT_USER": USER, "MINIO_ROOT_PASSWORD": PASSWORD,
           "STORAGE_BUCKET": "blender-mcp-test", **extra}
    return os_.ObjectStore(os_.load_config(env))


async def test_bucket_and_lifecycle_rule_are_created(stack):
    s = make_store(stack["direct"], stack["direct"], STORAGE_RETENTION_DAYS="3")
    await s.ensure_ready()
    await make_store(stack["direct"], stack["direct"], STORAGE_RETENTION_DAYS="3").ensure_ready()
    rules = s._admin.get_bucket_lifecycle("blender-mcp-test").rules
    mine = [r for r in rules if r.rule_id == os_.LIFECYCLE_RULE_ID]
    assert len(mine) == 1 and mine[0].expiration.days == 3
    # Changing the retention replaces the rule instead of adding a second one.
    await make_store(stack["direct"], stack["direct"], STORAGE_RETENTION_DAYS="7").ensure_ready()
    rules = s._admin.get_bucket_lifecycle("blender-mcp-test").rules
    assert [r.expiration.days for r in rules if r.rule_id == os_.LIFECYCLE_RULE_ID] == [7]


async def test_presigned_round_trip_through_caddy(stack):
    s = make_store(stack["direct"], stack["proxied"])
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
    """The signer signs for MINIO_PUBLIC_URL's host. A proxy that forwards that
    Host works; one that rewrites it (or a client hitting another name) fails."""
    s = make_store(stack["direct"], "http://files.example.test")
    await s.ensure_ready()
    key = os_.make_key("bus-1", "h.txt")
    url = s.presign_put(key).replace("http://files.example.test", stack["proxied"])
    ok = await asyncio.to_thread(requests.put, url, data=b"hello",
                               headers={"Host": "files.example.test"}, timeout=30)
    assert ok.status_code == 200, ok.text
    bad = await asyncio.to_thread(requests.put, url, data=b"hello",
                                headers={"Host": "other.example.test"}, timeout=30)
    assert bad.status_code == 403 and "SignatureDoesNotMatch" in bad.text


async def test_addon_helper_uploads_and_downloads(stack, tmp_path, monkeypatch):
    s = make_store(stack["direct"], stack["proxied"])
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
    s = make_store(stack["direct"], stack["proxied"])
    await s.ensure_ready()
    missing = os_.make_key("bus-1", "nope.bin")
    with pytest.raises(RuntimeError, match="HTTP 404"):
        addon_store.get_to_dir(s.presign_get(missing), tmp_path, "nope.bin")
    assert not list(tmp_path.glob(".*.part")) and not (tmp_path / "nope.bin").exists()
