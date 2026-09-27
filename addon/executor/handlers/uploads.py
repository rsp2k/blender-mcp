"""Receive payloads uploaded over the bus into a per-bus folder on this host.

Model-home's gaps-log item 2: data reached Blender only because it shared a
filesystem with the caller. The server splits an upload into chunks and
dispatches them here; files land under Blender's per-user datafiles folder
(so they survive a relaunch and background workers of the same user can
read them), in one subfolder per bus. The file logic lives in
``addon/upload_store.py`` so it can be tested without Blender.
"""

from __future__ import annotations

import base64
from pathlib import Path

import bpy

from ... import object_store, upload_store
from ..registry import command

_ROOT_NAME = "blender_mcp_uploads"


def _root() -> Path:
    try:
        return Path(bpy.utils.user_resource("DATAFILES", path=_ROOT_NAME, create=True))
    except Exception:
        return Path(bpy.app.tempdir) / _ROOT_NAME


def upload_dir(bus_dir) -> Path:
    sub = upload_store.sanitize_name(bus_dir) if bus_dir else "default"
    return upload_store.ensure_dir(_root() / sub)


class UploadHandlersMixin:

    @command("upload_begin")
    def upload_begin(self, upload_id, resume=False, bus_dir=None):
        """Start or resume a chunked upload; returns bytes already written."""
        return upload_store.begin(upload_dir(bus_dir), upload_id, resume=bool(resume))

    @command("upload_chunk")
    def upload_chunk(self, upload_id, offset, data_b64, bus_dir=None):
        """Write one base64 chunk at a byte offset (idempotent)."""
        try:
            data = base64.b64decode(data_b64, validate=True)
        except Exception:
            raise ValueError("data_b64 is not valid base64") from None
        return upload_store.write_chunk(upload_dir(bus_dir), upload_id, int(offset), data)

    @command("upload_finish")
    def upload_finish(self, upload_id, name, size=None, sha256=None, overwrite=True, bus_dir=None):
        """Verify size/sha256 and move the upload to its final name."""
        return upload_store.finish(upload_dir(bus_dir), upload_id, name,
                                   size=size, sha256=sha256, overwrite=bool(overwrite))

    @command("upload_abort")
    def upload_abort(self, upload_id, bus_dir=None):
        """Discard a partial upload."""
        return {"aborted": upload_store.abort(upload_dir(bus_dir), upload_id)}

    @command("list_uploads")
    def list_uploads(self, bus_dir=None):
        """Files uploaded to this Blender host for this bus."""
        return upload_store.list_files(upload_dir(bus_dir))

    @command("delete_uploads")
    def delete_uploads(self, name=None, bus_dir=None):
        """Delete one uploaded file, or all of them when no name is given."""
        return upload_store.delete(upload_dir(bus_dir), name)

    @command("fetch_object")
    def fetch_object(self, url, name, size=None, overwrite=True, bus_dir=None):
        """Download an object-storage file (presigned GET URL) into the upload folder.

        Small files arrive before this returns; larger ones download on a
        background thread (state "running" plus a transfer_id for
        transfer_status) so Blender's UI keeps responding.
        """
        base = upload_dir(bus_dir)
        size = int(size) if size is not None else None
        return object_store.run_transfer(
            "download", name, size,
            lambda progress: object_store.get_to_dir(url, base, name, expected_size=size,
                                                     overwrite=bool(overwrite),
                                                     progress=progress))

    @command("transfer_status")
    def transfer_status(self, transfer_id):
        """Progress of a background upload or download."""
        item = object_store.TRANSFERS.get(transfer_id)
        if item is None:
            raise ValueError(f"no transfer with id {transfer_id!r} (Blender may have restarted)")
        return item
