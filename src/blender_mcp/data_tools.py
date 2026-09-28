"""Getting data into Blender: mesh-from-data, uploads, and 2D geometry ops.

Covers model-home's gaps-log items 1-3:

- ``create_mesh`` / ``extrude_polygons`` build geometry from data in one
  call (their 286 wall prisms had to go through execute_code with bmesh).
- ``upload`` moves a payload (JSON, text or a binary file) onto the Blender
  host, so data no longer depends on a shared filesystem. The server splits
  it into chunks and dispatches them; each chunk is written at an explicit
  offset, so a retried chunk is harmless.
- ``polygon_ops`` runs shapely server-side (Blender's Python has none) and
  returns rings ``extrude_polygons`` accepts.

Chunk size: dispatch params over 100 KB serialized are stored only as a
summary in bus_job (see storage/job_repo.PARAMS_CAP), so the pull fallback
can't replay them. Chunks are deliberately larger than that (256 KiB raw,
~350 KB base64) to keep bus_job small; delivery is instead guaranteed by the
server retrying each chunk, which the idempotent offset writes make safe.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import secrets
import time
from typing import Any

from fastmcp import Context
from fastmcp.contrib.mcp_mixin import MCPMixin, mcp_tool

from . import geometry_ops
from .bus_tools import _resolve_user_id, resolve_bus
from .client_role import check_role_or_reject
from .dispatch_component import DEFAULT_TIMEOUT_S, TIMEOUT_MEDIUM, _dispatch
from .worker_tools import _parse_result

CHUNK_BYTES = 256 * 1024
CHUNK_CONCURRENCY = 4
CHUNK_RETRIES = 3
CHUNK_TIMEOUT_S = 30.0


def upload_max_bytes() -> int:
    try:
        return int(os.environ.get("BLENDER_MCP_UPLOAD_MAX_BYTES", 50 * 1024 * 1024))
    except ValueError:
        return 50 * 1024 * 1024


def plan_chunks(total: int, chunk: int = CHUNK_BYTES) -> list[tuple[int, int]]:
    """(offset, length) pairs covering ``total`` bytes."""
    if total <= 0:
        return [(0, 0)]
    return [(off, min(chunk, total - off)) for off in range(0, total, chunk)]


def payload_bytes(content_base64: str | None, text: str | None, json_data: Any) -> bytes:
    given = [x is not None for x in (content_base64, text, json_data)]
    if sum(given) != 1:
        raise ValueError("pass exactly one of content_base64, text or json_data")
    if content_base64 is not None:
        try:
            return base64.b64decode(content_base64, validate=True)
        except Exception:
            raise ValueError("content_base64 is not valid base64") from None
    if text is not None:
        return str(text).encode("utf-8")
    return json.dumps(json_data, separators=(",", ":")).encode("utf-8")


def _err(error: str, **extra) -> str:
    return json.dumps({"status": "error", "error": error, **extra})


async def _resolve(ctx, tool: str, bus_id: str | None):
    """Auth + role gate + bus resolution, as BlenderDispatchComponent._call does.
    Returns (bus, bus_id_str, user_id) or (None, error_json, None)."""
    rejection = check_role_or_reject(tool, ctx, "llm-client")
    if rejection:
        return None, rejection, None
    user_id = _resolve_user_id(ctx)
    if not user_id:
        return None, _err("unauthenticated"), None
    resolved = await resolve_bus(user_id, bus_id)
    if not resolved["ok"]:
        return None, json.dumps(resolved), None
    return resolved["bus"], str(resolved["bus_id"]), user_id


class BlenderDataComponent(MCPMixin):
    """create_mesh, extrude_polygons, upload, list_uploads, delete_uploads, polygon_ops."""

    async def _send(self, ctx, command, params, target_uuid, timeout, bus_id):
        bus, bus_id_str, user_id = await _resolve(ctx, f"blender_{command}", bus_id)
        if bus is None:
            return bus_id_str
        return await _dispatch(bus, bus_id_str, command, {**params, "bus_dir": bus_id_str},
                               target_uuid, timeout, caller_sub=user_id)

    @mcp_tool()
    async def create_mesh(
        self,
        name: str = "Mesh",
        vertices: list[list[float]] | None = None,
        faces: list[list[int]] | None = None,
        source: str | None = None,
        collection: str | None = None,
        parent: str | None = None,
        location: list[float] | None = None,
        scale: float = 1.0,
        smooth: bool = False,
        fix_normals: bool = True,
        target_uuid: str | None = None,
        _timeout: float = TIMEOUT_MEDIUM,
        bus_id: str | None = None,
        ctx: Context = None,
    ) -> str:
        """Create a mesh object from vertex coordinates and faces.

        ``vertices``: [[x, y, z], ...]; ``faces``: lists of vertex indices
        (3+ per face). ``scale`` multiplies every coordinate (e.g. 0.3048 for
        feet to metres). With ``parent``, coordinates are in the parent's
        local frame. ``collection`` is created under the scene if missing.
        For large meshes, upload a JSON file ``{"vertices": [...], "faces":
        [...]}`` with blender_upload and pass its name as ``source``:
        dispatch parameters over ~100 KB can't be recovered if Blender's
        event stream drops mid-call. Face winding doesn't matter: normals are
        made consistent and outward (``fix_normals=False`` keeps yours).
        Returns face counts and whether the result is a closed manifold solid.
        """
        if source is None and not vertices:
            return _err("invalid_argument", detail="pass vertices (and faces) or a source file")
        return await self._send(ctx, "create_mesh", {
            "name": name, "vertices": vertices, "faces": faces, "source": source,
            "collection": collection, "parent": parent, "location": location,
            "scale": scale, "smooth": smooth,
            # Only sent when off: add-ons before 2026.927.12 don't take it.
            **({} if fix_normals else {"fix_normals": False}),
        }, target_uuid, _timeout, bus_id)

    @mcp_tool()
    async def extrude_polygons(
        self,
        polygons: list | None = None,
        height: float | None = None,
        base_z: float = 0.0,
        source: str | None = None,
        name: str = "Prisms",
        merge: bool = True,
        collection: str | None = None,
        parent: str | None = None,
        scale: float = 1.0,
        origin: list[float] | None = None,
        location: list[float] | None = None,
        target_uuid: str | None = None,
        _timeout: float = TIMEOUT_MEDIUM,
        bus_id: str | None = None,
        ctx: Context = None,
    ) -> str:
        """Extrude 2D polygons (outer ring plus optional holes) into closed prisms.

        Each polygon: ``{"outer": [[x, y], ...], "holes": [[[x, y], ...]],
        "height"?: h, "base_z"?: z, "name"?: str}``, or a bare ring. Per
        polygon height/base_z override the call's ``height``/``base_z``.
        Rings may be open or closed and any winding. Many polygons in one
        call: ``merge=True`` builds one object (one shell per prism),
        ``merge=False`` one object per prism. ``origin`` [x, y] is subtracted
        before ``scale`` multiplies x, y, heights and base_z (e.g. drawing
        coordinates in feet: origin = drawing centre, scale = 0.3048). With
        ``parent``, coordinates are in the parent's local frame. Prisms
        without holes get single n-gon caps; with holes the caps are
        triangulated. For large sets, upload a JSON file (a polygon list, or
        ``{"polygons": [...], "height": h}``) with blender_upload and pass
        ``source``. Polygon cleanup (unions, closing gaps) is blender_polygon_ops.
        Returns per-object face counts and whether each is closed and manifold.
        """
        if source is None and not polygons:
            return _err("invalid_argument", detail="pass polygons or a source file")
        if scale <= 0:
            return _err("invalid_argument", detail="scale must be positive")
        return await self._send(ctx, "extrude_polygons", {
            "polygons": polygons, "height": height, "base_z": base_z, "source": source,
            "name": name, "merge": merge, "collection": collection, "parent": parent,
            "scale": scale, "origin": origin, "location": location,
        }, target_uuid, _timeout, bus_id)

    @mcp_tool()
    async def upload(
        self,
        name: str,
        content_base64: str | None = None,
        text: str | None = None,
        json_data: Any = None,
        sha256: str | None = None,
        overwrite: bool = True,
        upload_id: str | None = None,
        part_index: int = 0,
        part_count: int = 1,
        target_uuid: str | None = None,
        bus_id: str | None = None,
        ctx: Context = None,
    ) -> str:
        """Put a file on the Blender host: JSON, text, or binary (base64).

        Pass exactly one of ``content_base64``, ``text`` or ``json_data``.
        The file lands in a per-bus upload folder on the Blender machine and
        the result gives its path; tools that read input files
        (``source`` in extrude_polygons / create_mesh) take the bare name.
        Names are sanitised to letters, digits, '.', '-', '_'. The server
        chunks and verifies the transfer (sha256 is computed and checked
        automatically; pass your own ``sha256`` to verify end to end).

        For payloads too big for one call, split them into parts yourself:
        call with ``part_index=0, part_count=N`` (the reply has an
        ``upload_id``), then send parts 1..N-1 in order with that
        ``upload_id``. The file appears when the last part lands. Max total
        size: BLENDER_MCP_UPLOAD_MAX_BYTES (default 50 MB).
        """
        started = time.monotonic()
        try:
            data = payload_bytes(content_base64, text, json_data)
            if part_count < 1 or not (0 <= part_index < part_count):
                raise ValueError("need 0 <= part_index < part_count")
            if part_index > 0 and not upload_id:
                raise ValueError("parts after the first need the upload_id from part 0")
            if part_count == 1 and len(data) > upload_max_bytes():
                raise ValueError(f"payload is {len(data)} bytes; max {upload_max_bytes()}")
        except ValueError as e:
            return _err("invalid_argument", detail=str(e))

        bus, bus_id_str, user_id = await _resolve(ctx, "blender_upload", bus_id)
        if bus is None:
            return bus_id_str
        uid = upload_id or secrets.token_urlsafe(12)

        async def call(command, params, target, timeout=CHUNK_TIMEOUT_S):
            return _parse_result(await _dispatch(
                bus, bus_id_str, command, {**params, "upload_id": uid, "bus_dir": bus_id_str},
                target, timeout, caller_sub=user_id,
            ))

        ok, inner, reply = await call("upload_begin", {"resume": part_index > 0}, target_uuid)
        if not ok:
            return _err("upload_begin_failed", reply=reply)
        target = reply.get("target_uuid") or target_uuid  # pin every chunk to this Blender
        base = int((inner or {}).get("size", 0))
        if part_index > 0 and not (inner or {}).get("resumed"):
            return _err("unknown_upload", detail=f"no partial upload {uid!r} on the Blender host (expired or never started)")
        if base + len(data) > upload_max_bytes():
            await call("upload_abort", {}, target)
            return _err("too_large", detail=f"upload would reach {base + len(data)} bytes; max {upload_max_bytes()}")

        sem = asyncio.Semaphore(CHUNK_CONCURRENCY)

        async def send_chunk(off, length):
            chunk = base64.b64encode(data[off:off + length]).decode("ascii")
            last = None
            for _ in range(CHUNK_RETRIES):
                async with sem:
                    ok_, _inner, rep = await call(
                        "upload_chunk", {"offset": base + off, "data_b64": chunk}, target,
                    )
                if ok_:
                    return None
                last = rep
            return last

        failures = [f for f in await asyncio.gather(
            *(send_chunk(off, n) for off, n in plan_chunks(len(data)) if n or not data)
        ) if f is not None]
        if failures:
            if part_count == 1:
                await call("upload_abort", {}, target)
            return _err("chunk_failed", detail=f"{len(failures)} chunk(s) failed after {CHUNK_RETRIES} tries",
                        sample=failures[0], upload_id=uid)

        if part_index < part_count - 1:
            return json.dumps({
                "status": "ok", "upload_id": uid, "part_index": part_index,
                "received_bytes": base + len(data), "next_part": part_index + 1,
                "target_uuid": target,
            })

        expected_sha = sha256
        if part_count == 1 and not expected_sha:
            expected_sha = hashlib.sha256(data).hexdigest()
        ok, inner, reply = await call("upload_finish", {
            "name": name, "size": base + len(data), "sha256": expected_sha, "overwrite": overwrite,
        }, target)
        if not ok:
            return _err("upload_finish_failed", reply=reply, upload_id=uid)
        return json.dumps({
            "status": "ok", **(inner or {}), "target_uuid": target,
            "chunks": len(plan_chunks(len(data))), "parts": part_count,
            "seconds": round(time.monotonic() - started, 2),
        })

    @mcp_tool()
    async def list_uploads(
        self,
        target_uuid: str | None = None,
        _timeout: float = DEFAULT_TIMEOUT_S,
        bus_id: str | None = None,
        ctx: Context = None,
    ) -> str:
        """Files uploaded to the Blender host for this bus, with sizes and paths."""
        return await self._send(ctx, "list_uploads", {}, target_uuid, _timeout, bus_id)

    @mcp_tool()
    async def delete_uploads(
        self,
        name: str | None = None,
        target_uuid: str | None = None,
        _timeout: float = DEFAULT_TIMEOUT_S,
        bus_id: str | None = None,
        ctx: Context = None,
    ) -> str:
        """Delete one uploaded file by name, or every upload for this bus when
        ``name`` is omitted (partial uploads included)."""
        return await self._send(ctx, "delete_uploads", {"name": name}, target_uuid, _timeout, bus_id)

    @mcp_tool()
    async def polygon_ops(
        self,
        operation: str,
        polygons: list,
        distance: float | None = None,
        tolerance: float | None = None,
        other: list | None = None,
        join_style: str = "mitre",
        mitre_limit: float = 5.0,
        ctx: Context = None,
    ) -> str:
        """2D polygon cleanup on the server (shapely); no Blender needed.

        Polygons use the same shape as blender_extrude_polygons (``{"outer",
        "holes"}`` or bare rings), and the result's ``polygons`` feed it
        directly. Operations: ``union`` (merge overlaps), ``buffer`` (grow by
        ``distance``, negative shrinks), ``closing`` (grow then shrink by
        ``distance``: bridges gaps narrower than 2x distance, e.g. joining
        wall segments into one footprint), ``opening`` (shrink then grow:
        drops slivers), ``simplify`` (``tolerance``), ``difference`` /
        ``intersection`` (with ``other``), ``validate`` (repair
        self-intersections) and ``area`` (areas only). Invalid input is
        repaired first and flagged. ``join_style``: mitre (keeps square
        corners, the default), round or bevel.
        """
        rejection = check_role_or_reject("blender_polygon_ops", ctx, "llm-client")
        if rejection:
            return rejection
        if not _resolve_user_id(ctx):
            return _err("unauthenticated")
        try:
            result = await asyncio.to_thread(
                geometry_ops.run, operation, polygons, distance=distance,
                tolerance=tolerance, join_style=join_style, mitre_limit=mitre_limit,
                other=other,
            )
        except (ValueError, TypeError) as e:
            return _err("invalid_argument", detail=str(e))
        return json.dumps({"status": "ok", **result})
