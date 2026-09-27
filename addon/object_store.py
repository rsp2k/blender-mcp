"""Move files between this Blender host and object storage over presigned URLs.

No bpy here, so it can be tested outside Blender. The server signs every URL;
this side only does plain HTTP PUT/GET with the bundled ``requests`` and a
timeout, so no storage credentials ever reach the addon.

Commands run on Blender's main thread, so a big transfer must not happen
inline: files up to ``SYNC_LIMIT`` transfer before the command returns, and
anything larger runs on a daemon thread that reports progress into
``TRANSFERS`` (readable through the ``transfer_status`` command).
"""

from __future__ import annotations

import hashlib
import os
import secrets
import threading
import time
from pathlib import Path

from . import upload_store

SYNC_LIMIT = 16 * 1024 * 1024
CONNECT_TIMEOUT_S = 10
READ_TIMEOUT_S = 120  # per socket read, not the whole transfer
BLOCK = 1 << 20
OUTPUT_KEYS = ("filepath", "path", "exported")
MAX_TRACKED = 200


class Transfers:
    """Thread-safe progress records for background transfers."""

    def __init__(self):
        self._lock = threading.Lock()
        self._items: dict[str, dict] = {}

    def start(self, kind: str, name: str, size: int | None) -> str:
        tid = secrets.token_hex(8)
        with self._lock:
            if len(self._items) >= MAX_TRACKED:
                done = [k for k, v in self._items.items() if v["state"] != "running"]
                for k in done[: len(self._items) - MAX_TRACKED + 1]:
                    del self._items[k]
            self._items[tid] = {"transfer_id": tid, "kind": kind, "name": name, "size": size,
                                "bytes": 0, "state": "running", "started": time.time()}
        return tid

    def update(self, tid: str, **fields) -> None:
        with self._lock:
            if tid in self._items:
                self._items[tid].update(fields)

    def get(self, tid: str) -> dict | None:
        with self._lock:
            item = self._items.get(tid)
            return dict(item) if item else None


TRANSFERS = Transfers()


def _session():
    import requests

    return requests


def put_file(url: str, path: Path, content_type: str | None = None,
             progress=None) -> dict:
    """Stream ``path`` to a presigned PUT URL. Returns size and sha256."""
    size = path.stat().st_size
    headers = {"Content-Type": content_type} if content_type else {}
    with open(path, "rb") as fh:
        body = _Reader(fh, size, progress)
        r = _session().put(url, data=body, headers=headers,
                           timeout=(CONNECT_TIMEOUT_S, READ_TIMEOUT_S))
    if r.status_code >= 300:
        raise RuntimeError(f"upload failed: HTTP {r.status_code} {r.text[:300]}")
    if body.sent != size:
        raise RuntimeError(f"file changed during upload: sent {body.sent} of {size} bytes")
    return {"size": size, "sha256": body.sha256.hexdigest()}


class _Reader:
    """File wrapper with a length, so requests sends Content-Length and streams
    it (a generator body would go out chunked, which a presigned S3 PUT refuses),
    hashing and reporting progress on the way."""

    def __init__(self, fh, size: int, progress=None):
        self._fh = fh
        self._size = size
        self._progress = progress
        self.sent = 0
        self.sha256 = hashlib.sha256()

    def __len__(self):
        return self._size

    def read(self, n: int = -1) -> bytes:
        block = self._fh.read(BLOCK if n is None or n < 0 else n)
        if block:
            self.sha256.update(block)
            self.sent += len(block)
            if self._progress:
                self._progress(self.sent)
        return block


def get_to_dir(url: str, base: Path, name: str, expected_size: int | None = None,
               overwrite: bool = True, progress=None) -> dict:
    """Download a presigned GET URL into ``base/<name>`` (sanitised, atomic)."""
    upload_store.ensure_dir(base)
    final_name = upload_store.sanitize_name(name)
    final = upload_store.inside(base, final_name)
    if final.exists() and not overwrite:
        raise ValueError(f"{final_name!r} already exists (pass overwrite=true to replace it)")
    part = upload_store.part_path(base, secrets.token_hex(8))
    h = hashlib.sha256()
    got = 0
    try:
        with _session().get(url, stream=True, timeout=(CONNECT_TIMEOUT_S, READ_TIMEOUT_S)) as r:
            if r.status_code >= 300:
                raise RuntimeError(f"download failed: HTTP {r.status_code} {r.text[:300]}")
            fd = os.open(part, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "wb") as out:
                for block in r.iter_content(BLOCK):
                    out.write(block)
                    h.update(block)
                    got += len(block)
                    if progress:
                        progress(got)
        if expected_size is not None and got != expected_size:
            raise RuntimeError(f"size mismatch: expected {expected_size} bytes, got {got}")
        os.replace(part, final)
    finally:
        if part.exists():
            part.unlink()
    return {"name": final_name, "path": str(final), "size": got, "sha256": h.hexdigest()}


def run_transfer(kind: str, name: str, size: int | None, fn) -> dict:
    """Run ``fn(progress)`` inline when small, else on a daemon thread.

    Inline: returns fn's result with ``state: done``. Background: returns at
    once with ``state: running`` and a transfer_id; the record in TRANSFERS
    ends as ``done`` (with fn's result) or ``failed`` (with the error).
    """
    if size is not None and size <= SYNC_LIMIT:
        return {**fn(None), "state": "done"}
    tid = TRANSFERS.start(kind, name, size)

    def progress(n):
        TRANSFERS.update(tid, bytes=n)

    def work():
        try:
            TRANSFERS.update(tid, state="done", result=fn(progress), finished=time.time())
        except Exception as e:  # noqa: BLE001 - reported in the record
            TRANSFERS.update(tid, state="failed", error=str(e), finished=time.time())

    threading.Thread(target=work, name=f"blendermcp-{kind}-{tid}", daemon=True).start()
    return {"state": "running", "transfer_id": tid, "name": name, "size": size}


def output_path(result: dict) -> Path | None:
    for k in OUTPUT_KEYS:
        v = result.get(k)
        if isinstance(v, str) and v:
            return Path(v)
    return None


def attach_stored(result: dict, store: dict) -> dict:
    """Upload the file a handler reported and add ``stored`` to its result.

    Never raises: a storage problem must not turn a finished render into an
    error, so failures are reported inside ``stored`` instead.
    """
    key = store.get("object_key") if isinstance(store, dict) else None
    try:
        url = store["url"]
        path = output_path(result)
        if path is None:
            raise ValueError("the command reported no output file")
        if not path.is_file():
            raise ValueError(f"output file not found: {path}")
        size = path.stat().st_size
        ctype = store.get("content_type") or _guess_type(path)
        t = run_transfer("upload", path.name, size,
                         lambda progress: put_file(url, path, ctype, progress))
        stored = {"object_key": key, "size": size,
                  "state": "uploaded" if t["state"] == "done" else "uploading"}
        if "sha256" in t:
            stored["sha256"] = t["sha256"]
        if "transfer_id" in t:
            stored["transfer_id"] = t["transfer_id"]
    except Exception as e:  # noqa: BLE001 - must not fail the command
        stored = {"object_key": key, "state": "failed", "error": str(e)}
    return {**result, "stored": stored}


_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
          ".exr": "image/x-exr", ".glb": "model/gltf-binary", ".gltf": "model/gltf+json",
          ".blend": "application/x-blender", ".json": "application/json"}


def _guess_type(path: Path) -> str:
    return _TYPES.get(path.suffix.lower(), "application/octet-stream")
