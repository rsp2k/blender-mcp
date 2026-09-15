"""Reply path — sends `blender_job_update` back to the bus.

Called from Blender's main thread (in `drainer.execute_script`); marshals
the FastMCP `call_tool` coroutine onto the asyncio loop that lives on the
worker thread. Non-blocking: errors are logged when the future resolves.

Server derives the caller identity from the JWT in the bearer header on
the worker's persistent MCP session — the addon only sends four fields.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .bus_client import BlenderMCPClient


def submit_job_update(
    client: "BlenderMCPClient",
    job_id: str,
    status: str,
    result: str = "",
    error: str = "",
) -> None:
    """Schedule a `blender_job_update` tool call on the worker loop."""
    if not (client.loop and client.client and client.loop.is_running()):
        print(f"[BlenderMCP] Cannot report job {job_id}: client not connected")
        return

    coro = client.client.call_tool("blender_job_update", {
        "job_id": job_id,
        "status": status,
        "result": result,
        "error": error,
    })
    future = asyncio.run_coroutine_threadsafe(coro, client.loop)

    def _log_err(fut):
        try:
            fut.result(timeout=0)
        except Exception as e:
            print(f"[BlenderMCP] job_update for {job_id} failed: {e}")

    future.add_done_callback(_log_err)


def submit_force_release_control(client: "BlenderMCPClient", target_uuid: str) -> None:
    """Fire-and-forget ``blender_force_release_control`` call from the addon.

    Used by the "Take back" button so LLMs polling get_control_state
    see the release immediately instead of waiting for the natural
    expiry. Same asyncio-marshal pattern as ``submit_job_update``.
    """
    if not (client.loop and client.client and client.loop.is_running()):
        print(f"[BlenderMCP] Cannot force-release: client not connected")
        return

    coro = client.client.call_tool("blender_force_release_control", {
        "target_uuid": target_uuid,
    })
    future = asyncio.run_coroutine_threadsafe(coro, client.loop)

    def _log_err(fut):
        try:
            fut.result(timeout=0)
        except Exception as e:
            print(f"[BlenderMCP] force_release_control failed: {e}")

    future.add_done_callback(_log_err)
