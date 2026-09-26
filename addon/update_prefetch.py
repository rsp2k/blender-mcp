"""Download an addon update ourselves, then let Blender install it from cache.

Blender's package_install disables the addon it upgrades before it starts
downloading, so during the whole download the BlenderMCP sidebar is gone
and the only feedback is raw PROGRESS text in the status bar. Instead the
addon downloads the archive while it's still loaded (progress bar in its
own banner), verifies size and sha256 against the repo index, and drops it
at ``<repo dir>/.blender_ext/cache/<pkg_id>.zip``. With the repo's
``use_cache`` on, bl_pkg finds a cached archive whose (size, hash) match
the index entry and installs it without downloading again, so the addon
is only unloaded for the second the install takes.

Nothing here imports bpy, so selection, URL and verify logic are unit-testable.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import urllib.parse
import urllib.request

CHUNK = 256 * 1024
REPO_PRIVATE_DIR = ".blender_ext"
INDEX_FILENAME = "index.json"

_SYSTEM = {"darwin": "macos"}
_MACHINE = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "aarch32": "arm32"}


def platform_token() -> str:
    """Blender's platform token for this machine (e.g. ``linux-x64``)."""
    try:
        from bl_pkg.cli.blender_ext import platform_from_this_system
        return platform_from_this_system()
    except Exception:  # noqa: BLE001 - bl_pkg missing or changed; use the same mapping
        import platform
        system = platform.system().lower()
        machine = platform.machine().lower()
        return f"{_SYSTEM.get(system, system)}-{_MACHINE.get(machine, machine)}"


def select_entry(index: dict, pkg_id: str, platform: str) -> dict | None:
    """The index entry Blender would install here: same id, and either listing
    this platform or listing no platforms at all (a universal archive)."""
    universal = None
    for entry in (index or {}).get("data", []) or []:
        if not isinstance(entry, dict) or entry.get("id") != pkg_id:
            continue
        platforms = entry.get("platforms") or []
        if platform in platforms:
            return entry
        if not platforms and universal is None:
            universal = entry
    return universal


def resolve_archive_url(remote_url: str, archive_url: str) -> str:
    """Resolve an entry's archive_url the way bl_pkg does: relative ``./x.zip``
    is taken against the repo URL, with a trailing ``/index.json`` removed."""
    if not archive_url.startswith("./"):
        return archive_url
    parts = urllib.parse.urlsplit(remote_url)
    base = urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    if base.endswith("/" + INDEX_FILENAME):
        base = base.rpartition("/")[0]
    return base.rstrip("/") + archive_url[1:]


def cache_paths(repo_dir: str, pkg_id: str) -> tuple[str, str]:
    """(final cache archive path bl_pkg looks for, our temp download path)."""
    cache_dir = os.path.join(repo_dir, REPO_PRIVATE_DIR, "cache")
    return (os.path.join(cache_dir, pkg_id + ".zip"),
            os.path.join(cache_dir, f".{pkg_id}.zip.part"))


def load_synced_index(repo_dir: str) -> dict | None:
    """The index Blender just synced into the repo's private dir."""
    try:
        with open(os.path.join(repo_dir, REPO_PRIVATE_DIR, INDEX_FILENAME), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def verify_and_place(tmp_path: str, final_path: str, size: int, digest_hex: str,
                     expected_size: int, expected_hash: str) -> tuple[bool, str]:
    """Move the download into place only if it matches the index entry exactly.
    ``expected_hash`` is the index form, ``sha256:<hex>``."""
    if size != int(expected_size):
        _unlink(tmp_path)
        return False, f"size mismatch: got {size}, index says {expected_size}"
    got = "sha256:" + digest_hex.lower()
    if got != str(expected_hash).lower():
        _unlink(tmp_path)
        return False, "sha256 mismatch"
    os.replace(tmp_path, final_path)
    return True, "verified"


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


class Prefetch:
    """One background download. The main thread only reads its fields."""

    def __init__(self, *, url: str, repo_index: int, pkg_id: str, repo_dir: str,
                 expected_size: int, expected_hash: str, timeout: float,
                 headers: dict | None = None, version: str = "") -> None:
        self.url = url
        self.repo_index = repo_index
        self.pkg_id = pkg_id
        self.version = version
        self.expected_size = int(expected_size)
        self.expected_hash = expected_hash
        self.timeout = timeout
        self.headers = dict(headers or {})
        self.final_path, self.tmp_path = cache_paths(repo_dir, pkg_id)
        self.bytes_done = 0
        self.state = "pending"  # pending, downloading, verified, failed, cancelled
        self.error = ""
        self.started_at = 0.0
        self.finished_at = 0.0
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def fraction(self) -> float:
        if self.expected_size <= 0:
            return 0.0
        return max(0.0, min(1.0, self.bytes_done / self.expected_size))

    @property
    def active(self) -> bool:
        return self.state in ("pending", "downloading")

    def start(self) -> None:
        self.state = "downloading"
        self.started_at = time.monotonic()
        self._thread = threading.Thread(target=self._run, name="blendermcp-update", daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        self._cancel.set()

    def _run(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.tmp_path), exist_ok=True)
            req = urllib.request.Request(self.url, headers=self.headers)
            sha = hashlib.sha256()
            with urllib.request.urlopen(req, timeout=self.timeout) as resp, \
                    open(self.tmp_path, "wb") as out:
                while True:
                    if self._cancel.is_set():
                        break
                    block = resp.read(CHUNK)
                    if not block:
                        break
                    out.write(block)
                    sha.update(block)
                    self.bytes_done += len(block)
            if self._cancel.is_set():
                _unlink(self.tmp_path)
                self.state = "cancelled"
                return
            ok, reason = verify_and_place(self.tmp_path, self.final_path, self.bytes_done,
                                          sha.hexdigest(), self.expected_size, self.expected_hash)
            self.error = "" if ok else reason
            self.state = "verified" if ok else "failed"
        except Exception as e:  # noqa: BLE001 - any failure falls back to Blender's own download
            _unlink(self.tmp_path)
            self.error = str(e) or type(e).__name__
            self.state = "failed"
        finally:
            self.finished_at = time.monotonic()
