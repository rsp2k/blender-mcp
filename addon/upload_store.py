"""Pure file handling for payloads uploaded to the Blender host over the bus.

No bpy here, so it can be tested outside Blender. The handler passes in the
base directory (a per-bus folder under Blender's user datafiles). Every path is
built from a sanitised name and checked to stay inside that directory.

A chunked upload writes to a hidden ``.<upload_id>.part`` file at explicit
offsets (so a retried chunk is harmless), and ``finish`` verifies size and
sha256 before atomically renaming it to its final name.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from pathlib import Path

_NAME_OK = re.compile(r"[^A-Za-z0-9._-]+")
_UPLOAD_ID_OK = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
MAX_NAME_LEN = 128
STALE_PART_S = 3600


def sanitize_name(name) -> str:
    """A safe file name: basename only, restricted characters, no leading dots."""
    if not isinstance(name, str) or not name.strip():
        raise ValueError("name must be a non-empty string")
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    base = _NAME_OK.sub("_", base).lstrip(".")
    base = base[:MAX_NAME_LEN]
    if not base or base in {".", ".."}:
        raise ValueError(f"name {name!r} has no usable characters")
    return base


def check_upload_id(upload_id) -> str:
    if not isinstance(upload_id, str) or not _UPLOAD_ID_OK.match(upload_id):
        raise ValueError("upload_id must be 8-64 characters of letters, digits, '-' or '_'")
    return upload_id


def ensure_dir(base: Path) -> Path:
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(base, 0o700)
    except OSError:
        pass
    return base


def inside(base: Path, name: str) -> Path:
    """Resolve ``name`` inside ``base``; raise if it would escape."""
    root = base.resolve()
    p = (root / name).resolve()
    if p.parent != root:
        raise ValueError(f"{name!r} resolves outside the upload directory")
    return p


def part_path(base: Path, upload_id: str) -> Path:
    return inside(base, f".{check_upload_id(upload_id)}.part")


def begin(base: Path, upload_id: str, resume: bool = False) -> dict:
    """Start (or resume) a chunked upload. Returns the bytes already written."""
    ensure_dir(base)
    clean_stale_parts(base)
    part = part_path(base, upload_id)
    if resume and part.exists():
        return {"upload_id": upload_id, "resumed": True, "size": part.stat().st_size}
    fd = os.open(part, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    os.close(fd)
    return {"upload_id": upload_id, "resumed": False, "size": 0}


def write_chunk(base: Path, upload_id: str, offset: int, data: bytes) -> dict:
    """Write ``data`` at ``offset``. Idempotent, so a retried chunk is harmless."""
    if not isinstance(offset, int) or offset < 0:
        raise ValueError("offset must be a non-negative integer")
    part = part_path(base, upload_id)
    if not part.exists():
        raise ValueError(f"no upload in progress with id {upload_id!r} (call begin first)")
    fd = os.open(part, os.O_WRONLY)
    try:
        os.pwrite(fd, data, offset) if hasattr(os, "pwrite") else _seek_write(fd, data, offset)
    finally:
        os.close(fd)
    return {"upload_id": upload_id, "offset": offset, "bytes": len(data)}


def _seek_write(fd: int, data: bytes, offset: int) -> None:
    os.lseek(fd, offset, os.SEEK_SET)
    view = memoryview(data)
    while view:
        n = os.write(fd, view)
        view = view[n:]


def finish(base: Path, upload_id: str, name: str, size: int | None = None,
           sha256: str | None = None, overwrite: bool = True) -> dict:
    """Verify the part file and rename it to its final, sanitised name."""
    part = part_path(base, upload_id)
    if not part.exists():
        raise ValueError(f"no upload in progress with id {upload_id!r}")
    final_name = sanitize_name(name)
    final = inside(base, final_name)
    actual_size = part.stat().st_size
    if size is not None and actual_size != size:
        raise ValueError(f"size mismatch: expected {size} bytes, have {actual_size}")
    digest = file_sha256(part)
    if sha256 and digest != sha256.lower().removeprefix("sha256:"):
        raise ValueError(f"sha256 mismatch: expected {sha256}, got {digest}")
    if final.exists() and not overwrite:
        raise ValueError(f"{final_name!r} already exists (pass overwrite=true to replace it)")
    os.replace(part, final)
    try:
        os.chmod(final, 0o600)
    except OSError:
        pass
    return {"name": final_name, "path": str(final), "size": actual_size, "sha256": digest}


def abort(base: Path, upload_id: str) -> bool:
    part = part_path(base, upload_id)
    if part.exists():
        part.unlink()
        return True
    return False


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def list_files(base: Path) -> dict:
    if not base.exists():
        return {"directory": str(base), "files": [], "total_bytes": 0}
    files = []
    total = 0
    for p in sorted(base.iterdir()):
        if not p.is_file() or p.name.startswith("."):
            continue
        st = p.stat()
        total += st.st_size
        files.append({
            "name": p.name, "path": str(p), "size": st.st_size,
            "modified": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(st.st_mtime)),
        })
    return {"directory": str(base), "files": files, "total_bytes": total}


def delete(base: Path, name: str | None = None) -> dict:
    """Delete one uploaded file, or everything (including part files) when name is None."""
    if not base.exists():
        return {"deleted": []}
    if name is None:
        deleted = []
        for p in base.iterdir():
            if p.is_file():
                p.unlink()
                deleted.append(p.name)
        return {"deleted": sorted(deleted)}
    target = inside(base, sanitize_name(name))
    if not target.exists():
        raise ValueError(f"no uploaded file named {name!r}")
    target.unlink()
    return {"deleted": [target.name]}


def clean_stale_parts(base: Path, max_age_s: float = STALE_PART_S) -> int:
    """Remove abandoned part files older than ``max_age_s``."""
    if not base.exists():
        return 0
    now = time.time()
    n = 0
    for p in base.glob(".*.part"):
        try:
            if now - p.stat().st_mtime > max_age_s:
                p.unlink()
                n += 1
        except OSError:
            pass
    return n


def resolve_source(base: Path, source: str) -> Path:
    """A payload path for tools that read input files: a bare name means a file
    in the upload directory; an absolute path is used as given."""
    if not isinstance(source, str) or not source:
        raise ValueError("source must be a file name or path")
    if os.path.isabs(source):
        p = Path(source)
    else:
        p = inside(base, sanitize_name(source))
    if not p.is_file():
        raise ValueError(f"source file not found: {p}")
    return p
