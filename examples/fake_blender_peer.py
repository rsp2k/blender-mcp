"""Minimal fake-Blender peer for integration testing.

Masquerades as a persistent Blender client on the bus. When it receives a
job_dispatch via the _message_bus log channel, it doesn't actually execute
the script — it replies immediately with blender_job_update(status='completed')
and the script source echoed back as the result. Use this whenever you want
to exercise the round-trip without a real Blender install.

Usage:
    uv run python examples/fake_blender_peer.py \
        --server http://localhost:8000/mcp \
        --token "$TOKEN" \
        --uuid blender-demo01
"""
import argparse
import asyncio
import json
import sys

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport


def _decode_bus_log(message):
    """Extract a _message_bus log record's data dict or return None."""
    inner = getattr(message, "root", message)
    if getattr(inner, "method", None) != "notifications/message":
        return None
    params = getattr(inner, "params", None)
    if params is None:
        return None
    logger_name = getattr(params, "logger", None) or (
        params.get("logger") if isinstance(params, dict) else None
    )
    if logger_name != "_message_bus":
        return None
    data = getattr(params, "data", None) or (
        params.get("data") if isinstance(params, dict) else None
    )
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            return None
    return data if isinstance(data, dict) else None


def make_handler(holder: dict, peer_uuid: str):
    async def handler(message):
        try:
            data = _decode_bus_log(message)
            if data is None:
                return

            target = data.get("target_uuid")
            if target and target != peer_uuid:
                return  # not addressed to us

            payload = data.get("payload", {})
            if payload.get("kind") == "job_update":
                return  # don't loop on our own reply

            if payload.get("message_type") != "job_dispatch":
                return

            job_id = payload.get("job_id")
            script = payload.get("script", "")
            print(f"[peer] job_dispatch job_id={job_id} script_len={len(script)}")

            client = holder.get("client")
            if client is None:
                return

            await client.call_tool("blender_job_update", {
                "job_id": job_id,
                "status": "completed",
                "result": f"(fake-peer) executed:\n{script}",
                "error": "",
            })
            print(f"[peer] job_update sent for {job_id}")
        except Exception as e:
            print(f"[peer] handler error: {e}", file=sys.stderr)
    return handler


async def run(server: str, token: str, peer_uuid: str, duration: float):
    transport = StreamableHttpTransport(
        url=server,
        headers={"Authorization": f"Bearer {token}"},
    )
    holder = {}
    handler = make_handler(holder, peer_uuid)

    async with Client(transport, message_handler=handler) as client:
        holder["client"] = client
        print(f"[peer] Connected as {peer_uuid}")

        try:
            await client.set_logging_level("debug")
            print("[peer] set_logging_level=debug")
        except Exception as e:
            print(f"[peer] set_logging_level failed (non-fatal): {e}")

        await client.call_tool("blender_register_client", {
            "client_uuid": peer_uuid,
            "client_type": "blender",
            "is_persistent": True,
            "capabilities": [
                "python_execution", "modeling", "rendering",
                "scene_management", "asset_processing",
            ],
        })
        print("[peer] register_client ok")
        print("[peer] waiting for jobs...")

        try:
            await asyncio.sleep(duration)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass

        try:
            await client.call_tool("blender_unregister_client", {"client_uuid": peer_uuid})
        except Exception:
            pass
        print("[peer] unregistered, exiting")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--server", default="http://localhost:8000/mcp",
                    help="MCP endpoint URL (default: %(default)s)")
    ap.add_argument("--token", required=True, help="JWT from POST /auth/login")
    ap.add_argument("--uuid", default="blender-demo01",
                    help="Sticky client UUID (default: %(default)s)")
    ap.add_argument("--duration", type=float, default=600.0,
                    help="Seconds to stay subscribed before exit (default: %(default)s)")
    args = ap.parse_args()
    asyncio.run(run(args.server, args.token, args.uuid, args.duration))


if __name__ == "__main__":
    main()
