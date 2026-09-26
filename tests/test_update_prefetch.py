"""Update prefetch: entry selection, URL resolution, verify/place, real download."""

import hashlib
import http.server
import os
import threading
import time

import pytest

from addon import update_prefetch as up

INDEX = {
    "version": "v1",
    "data": [
        {"id": "blender_mcp", "version": "9.9.9", "platforms": ["linux-x64"],
         "archive_url": "./blender_mcp-9.9.9-linux_x64.zip", "archive_size": 3,
         "archive_hash": "sha256:aa"},
        {"id": "blender_mcp", "version": "9.9.9", "platforms": ["windows-x64"],
         "archive_url": "./blender_mcp-9.9.9-windows_x64.zip", "archive_size": 4,
         "archive_hash": "sha256:bb"},
        {"id": "other", "version": "1.0.0", "platforms": ["linux-x64"],
         "archive_url": "./other.zip", "archive_size": 1, "archive_hash": "sha256:cc"},
    ],
}


def test_select_entry_by_platform():
    assert up.select_entry(INDEX, "blender_mcp", "linux-x64")["archive_size"] == 3
    assert up.select_entry(INDEX, "blender_mcp", "windows-x64")["archive_size"] == 4
    assert up.select_entry(INDEX, "blender_mcp", "macos-arm64") is None


def test_select_entry_falls_back_to_universal():
    idx = {"data": [{"id": "blender_mcp", "platforms": [], "archive_url": "./x.zip",
                     "archive_size": 1, "archive_hash": "sha256:00"}]}
    assert up.select_entry(idx, "blender_mcp", "linux-x64")["archive_url"] == "./x.zip"


@pytest.mark.parametrize("remote,expected", [
    ("https://mcp.blender.bet/extensions/index.json", "https://mcp.blender.bet/extensions/a.zip"),
    ("https://mcp.blender.bet/extensions/index.json?blender_version=5.2.2&platform=linux-x64",
     "https://mcp.blender.bet/extensions/a.zip"),
    ("https://example.org/repo/", "https://example.org/repo/a.zip"),
    ("https://example.org/repo", "https://example.org/repo/a.zip"),
])
def test_resolve_relative_archive_url(remote, expected):
    assert up.resolve_archive_url(remote, "./a.zip") == expected


def test_resolve_absolute_archive_url_untouched():
    assert up.resolve_archive_url("https://x/index.json", "https://cdn/a.zip") == "https://cdn/a.zip"


def test_cache_paths_match_bl_pkg_layout(tmp_path):
    final, tmp = up.cache_paths(str(tmp_path), "blender_mcp")
    assert final == os.path.join(str(tmp_path), ".blender_ext", "cache", "blender_mcp.zip")
    assert os.path.dirname(tmp) == os.path.dirname(final)


def test_verify_and_place(tmp_path):
    data = b"abc"
    digest = hashlib.sha256(data).hexdigest()
    part, final = tmp_path / "x.part", tmp_path / "x.zip"
    part.write_bytes(data)
    ok, _ = up.verify_and_place(str(part), str(final), 3, digest, 3, "sha256:" + digest.upper())
    assert ok and final.read_bytes() == data and not part.exists()

    part.write_bytes(data)
    ok, reason = up.verify_and_place(str(part), str(final), 3, digest, 3, "sha256:" + "0" * 64)
    assert not ok and "sha256" in reason and not part.exists()

    part.write_bytes(data)
    ok, reason = up.verify_and_place(str(part), str(final), 3, digest, 4, "sha256:" + digest)
    assert not ok and "size" in reason


@pytest.fixture
def server(tmp_path):
    served = tmp_path / "served"
    served.mkdir()
    requests = []

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(served), **kw)

        def log_message(self, fmt, *args):
            requests.append(self.path)

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield served, f"http://127.0.0.1:{httpd.server_address[1]}", requests
    httpd.shutdown()


def _wait(pf, timeout=10):
    deadline = time.time() + timeout
    while pf.active and time.time() < deadline:
        time.sleep(0.02)


def test_prefetch_downloads_verifies_and_places(tmp_path, server):
    served, base, _ = server
    data = os.urandom(700_000)
    (served / "a.zip").write_bytes(data)
    repo_dir = tmp_path / "repo"
    pf = up.Prefetch(url=f"{base}/a.zip", repo_index=0, pkg_id="blender_mcp",
                     repo_dir=str(repo_dir), expected_size=len(data),
                     expected_hash="sha256:" + hashlib.sha256(data).hexdigest(), timeout=5)
    pf.start()
    _wait(pf)
    assert pf.state == "verified", pf.error
    assert pf.fraction == 1.0
    assert (repo_dir / ".blender_ext" / "cache" / "blender_mcp.zip").read_bytes() == data
    assert not os.path.exists(pf.tmp_path)


def test_prefetch_rejects_corrupted_archive(tmp_path, server):
    served, base, _ = server
    (served / "a.zip").write_bytes(b"not the real archive")
    pf = up.Prefetch(url=f"{base}/a.zip", repo_index=0, pkg_id="blender_mcp",
                     repo_dir=str(tmp_path), expected_size=20,
                     expected_hash="sha256:" + "0" * 64, timeout=5)
    pf.start()
    _wait(pf)
    assert pf.state == "failed" and "sha256" in pf.error
    assert not os.path.exists(pf.final_path) and not os.path.exists(pf.tmp_path)


def test_prefetch_network_error_fails_cleanly(tmp_path, server):
    _, base, _ = server
    pf = up.Prefetch(url=f"{base}/missing.zip", repo_index=0, pkg_id="blender_mcp",
                     repo_dir=str(tmp_path), expected_size=1, expected_hash="sha256:00", timeout=5)
    pf.start()
    _wait(pf)
    assert pf.state == "failed" and "404" in pf.error
    assert not os.path.exists(pf.tmp_path)


def test_prefetch_cancel_removes_partial(tmp_path, server):
    served, base, _ = server
    data = os.urandom(8 * 1024 * 1024)
    (served / "big.zip").write_bytes(data)
    pf = up.Prefetch(url=f"{base}/big.zip", repo_index=0, pkg_id="blender_mcp",
                     repo_dir=str(tmp_path), expected_size=len(data),
                     expected_hash="sha256:" + hashlib.sha256(data).hexdigest(), timeout=5)
    pf.cancel()  # cancel before the first chunk lands
    pf.start()
    _wait(pf)
    assert pf.state == "cancelled"
    assert not os.path.exists(pf.tmp_path) and not os.path.exists(pf.final_path)
