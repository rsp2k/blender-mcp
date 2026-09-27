"""Object storage tools: presigned upload/download URLs, scoped to a bus.

The bytes never pass through MCP. The caller PUTs to (or GETs from) a
presigned URL directly, and blender_fetch_object_to_blender hands Blender
a GET URL so the addon pulls the file itself. See object_storage.py for the
storage side and the docs how-to "Large files: uploads and downloads".

Every key is ``<bus_id>/<hex>/<filename>``; a tool only accepts keys under a
bus the caller is a member of (resolve_bus enforces membership).
"""

from __future__ import annotations

import asyncio
import json
import time

from fastmcp import Context
from fastmcp.contrib.mcp_mixin import MCPMixin, mcp_tool

from .data_tools import _resolve
from .dispatch_component import DEFAULT_TIMEOUT_S, TIMEOUT_LONG, _dispatch
from .object_storage import (
    NOT_CONFIGURED_DETAIL,
    StorageError,
    check_key,
    get_store,
    make_key,
    parse_key,
)
from .worker_tools import _parse_result

TRANSFER_POLL_S = 1.0


def _err(error: str, **extra) -> str:
    return json.dumps({"status": "error", "error": error, **extra})


def _not_configured() -> str:
    return _err("storage_not_configured", detail=NOT_CONFIGURED_DETAIL)


def _key_error(e: Exception) -> str:
    if isinstance(e, PermissionError):
        return _err("forbidden", detail=str(e))
    return _err("invalid_argument", detail=str(e))


class BlenderObjectStorageComponent(MCPMixin):
    """create_upload_url, create_download_url, list_objects, delete_object,
    fetch_object_to_blender, object_transfer_status."""

    async def _prepare(self, ctx, tool: str, bus_id: str | None):
        """(store, bus, bus_id_str, user_id), or (None, None, error_json, None)."""
        store = get_store()
        if store is None:
            return None, None, _not_configured(), None
        bus, bus_id_str, user_id = await _resolve(ctx, tool, bus_id)
        if bus is None:
            return None, None, bus_id_str, None
        try:
            await store.ensure_ready()
        except StorageError as e:
            return None, None, _err(e.code, detail=e.detail), None
        return store, bus, bus_id_str, user_id

    @mcp_tool()
    async def create_upload_url(
        self,
        filename: str,
        content_type: str | None = None,
        size: int | None = None,
        bus_id: str | None = None,
        ctx: Context = None,
    ) -> str:
        """Get a presigned URL to upload a large file straight to object storage.

        For payloads too big for a tool argument (anything over a few MB,
        up to 5 GB). PUT the raw bytes to ``upload_url`` before it expires
        (15 minutes by default), e.g. ``curl -T model.glb '<upload_url>'``,
        sending the ``headers`` given. Then pass ``object_key`` to
        blender_fetch_object_to_blender to put the file on the Blender host,
        or share it with blender_create_download_url. ``size`` (bytes) is
        checked against the server's limit. Objects expire after the
        retention period (see ``retention_days``). Without object storage
        on the server, use blender_upload (up to 50 MB, chunked over the bus).
        """
        store, _, bus_id_str, _ = await self._prepare(ctx, "blender_create_upload_url", bus_id)
        if store is None:
            return bus_id_str
        cfg = store.config
        if size is not None and (not isinstance(size, int) or size < 0):
            return _err("invalid_argument", detail="size must be a non-negative integer (bytes)")
        if size is not None and size > cfg.max_upload_bytes:
            return _err("too_large", detail=f"{size} bytes exceeds the {cfg.max_upload_bytes} "
                                            "byte limit for one upload")
        try:
            key = make_key(bus_id_str, filename)
        except ValueError as e:
            return _err("invalid_argument", detail=str(e))
        headers = {"Content-Type": content_type} if content_type else {}
        return json.dumps({
            "status": "ok",
            "object_key": key,
            "upload_url": store.presign_put(key),
            "method": "PUT",
            "headers": headers,
            "expires_in": cfg.url_expiry_s,
            "expires_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                        time.gmtime(time.time() + cfg.url_expiry_s)),
            "retention_days": cfg.retention_days,
            "max_bytes": cfg.max_upload_bytes,
            "next": "PUT the file, then blender_fetch_object_to_blender(object_key=...)",
        })

    @mcp_tool()
    async def create_download_url(
        self,
        object_key: str,
        bus_id: str | None = None,
        ctx: Context = None,
    ) -> str:
        """Presigned GET URL for a stored object (a stored render, an upload).

        Hand the URL to whoever needs the bytes (a browser, curl, another
        tool); it expires after 15 minutes by default, and calling this
        again gives a fresh one. ``bus_id`` must be the bus the object was
        stored under (default: your personal bus).
        """
        store, _, bus_id_str, _ = await self._prepare(ctx, "blender_create_download_url", bus_id)
        if store is None:
            return bus_id_str
        try:
            check_key(bus_id_str, object_key)
            info = await store.stat(object_key)
        except (ValueError, PermissionError) as e:
            return _key_error(e)
        except StorageError as e:
            return _err(e.code, detail=e.detail, object_key=object_key)
        return json.dumps({"status": "ok", **info,
                           "download_url": store.presign_get(object_key),
                           "expires_in": store.config.url_expiry_s})

    @mcp_tool()
    async def list_objects(
        self,
        bus_id: str | None = None,
        ctx: Context = None,
    ) -> str:
        """Objects stored for a bus (default: your personal bus), newest first,
        with size and the earliest time each expires."""
        store, _, bus_id_str, _ = await self._prepare(ctx, "blender_list_objects", bus_id)
        if store is None:
            return bus_id_str
        try:
            objects = await store.list(bus_id_str)
        except StorageError as e:
            return _err(e.code, detail=e.detail)
        objects.sort(key=lambda o: o.get("last_modified", ""), reverse=True)
        return json.dumps({"status": "ok", "bus_id": bus_id_str, "objects": objects,
                           "total_bytes": sum(o["size"] or 0 for o in objects),
                           "retention_days": store.config.retention_days})

    @mcp_tool()
    async def delete_object(
        self,
        object_key: str,
        bus_id: str | None = None,
        ctx: Context = None,
    ) -> str:
        """Delete a stored object now instead of waiting for it to expire."""
        store, _, bus_id_str, _ = await self._prepare(ctx, "blender_delete_object", bus_id)
        if store is None:
            return bus_id_str
        try:
            check_key(bus_id_str, object_key)
            await store.delete(object_key)
        except (ValueError, PermissionError) as e:
            return _key_error(e)
        except StorageError as e:
            return _err(e.code, detail=e.detail, object_key=object_key)
        return json.dumps({"status": "ok", "deleted": object_key})

    @mcp_tool()
    async def fetch_object_to_blender(
        self,
        object_key: str,
        name: str | None = None,
        overwrite: bool = True,
        wait: bool = True,
        target_uuid: str | None = None,
        _timeout: float = TIMEOUT_LONG,
        bus_id: str | None = None,
        ctx: Context = None,
    ) -> str:
        """Download a stored object onto the Blender host, into the same
        per-bus folder blender_upload uses; returns the local ``path``.

        ``name``: file name there (default: the object's own name). Any tool
        that takes an uploaded file (e.g. blender_create_mesh ``source``)
        accepts that name. Blender pulls the file itself from a presigned
        URL, so size isn't limited by MCP or the bus. Files over 16 MB
        download in the background; with ``wait`` (default) this call polls
        until done or ``_timeout``, otherwise it returns a ``transfer_id``
        for blender_object_transfer_status.
        """
        store, bus, bus_id_str, user_id = await self._prepare(
            ctx, "blender_fetch_object_to_blender", bus_id)
        if store is None:
            return bus_id_str
        try:
            check_key(bus_id_str, object_key)
            _, _, own_name = parse_key(object_key)
            info = await store.stat(object_key)
        except (ValueError, PermissionError) as e:
            return _key_error(e)
        except StorageError as e:
            return _err(e.code, detail=e.detail, object_key=object_key)
        deadline = time.monotonic() + _timeout
        # The download URL must outlive a slow background transfer's start.
        url = store.presign_get(object_key, max(store.config.url_expiry_s, int(_timeout) + 60))
        ok, inner, reply = _parse_result(await _dispatch(
            bus, bus_id_str, "fetch_object",
            {"url": url, "name": name or own_name, "size": info["size"],
             "overwrite": overwrite, "bus_dir": bus_id_str},
            target_uuid, min(_timeout, DEFAULT_TIMEOUT_S * 4), caller_sub=user_id,
        ))
        if not ok:
            return json.dumps(reply)
        inner = inner or {}
        target = reply.get("target_uuid") or target_uuid
        if inner.get("state") == "running" and wait:
            inner = await self._wait_transfer(bus, user_id, bus_id_str, inner, target, deadline)
        if inner.get("state") == "done" and "result" in inner:
            inner = {**inner.pop("result"), **inner}
        return json.dumps({"status": "ok" if inner.get("state") != "failed" else "error",
                           "object_key": object_key, **inner, "target_uuid": target})

    async def _wait_transfer(self, bus, user_id, bus_id_str, running, target, deadline):
        status = running
        while time.monotonic() < deadline:
            await asyncio.sleep(TRANSFER_POLL_S)
            ok, inner, reply = _parse_result(await _dispatch(
                bus, bus_id_str, "transfer_status", {"transfer_id": running["transfer_id"]},
                target, DEFAULT_TIMEOUT_S, caller_sub=user_id))
            if not ok:
                return {**running, "poll_error": reply}
            status = inner or {}
            if status.get("state") != "running":
                return status
        return {**status, "note": "still downloading; check blender_object_transfer_status"}

    @mcp_tool()
    async def object_transfer_status(
        self,
        transfer_id: str,
        target_uuid: str | None = None,
        _timeout: float = DEFAULT_TIMEOUT_S,
        bus_id: str | None = None,
        ctx: Context = None,
    ) -> str:
        """Progress of a background transfer between Blender and object storage
        (a large fetch_object_to_blender, or a large output stored with
        store=true): state running/done/failed, bytes moved so far."""
        bus, bus_id_str, user_id = await _resolve(ctx, "blender_object_transfer_status", bus_id)
        if bus is None:
            return bus_id_str
        return await _dispatch(bus, bus_id_str, "transfer_status", {"transfer_id": transfer_id},
                               target_uuid, _timeout, caller_sub=user_id)

