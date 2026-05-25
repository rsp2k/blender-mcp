"""Reference LLM client: register on the bus, dispatch a script, await the reply.

Demonstrates the full round-trip an LLM-side client implements:
- Connect via FastMCP Streamable HTTP with Bearer auth
- Subscribe to MCP logs at debug level (the _message_bus channel)
- Register as an ephemeral client
- Send a job_dispatch to a chosen target
- Wait for the matching blender_job_update reply
- Print result/error, then unregister

Usage:
    uv run python examples/llm_client_example.py \
        --server http://localhost:8000/mcp \
        --token "$TOKEN" \
        --target blender-demo01 \
        --script 'import bpy; print(len(bpy.data.objects))'

The --target arg accepts:
    bare UUID          → direct routing (shorthand for uuid:<uuid>)
    uuid:<client-uuid> → direct
    group:<group-id>   → group
    type:<client_type> → type_filter (e.g. type:blender)
    broadcast          → all peers except self
"""
import argparse
import asyncio
import json
import sys
import uuid

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport


def parse_target(spec: str) -> dict:
    """Convert --target value into send_message routing kwargs."""
    if spec == "broadcast":
        return {}
    if ":" in spec:
        kind, _, val = spec.partition(":")
        mapping = {
            "uuid": {"target_uuid": val},
            "group": {"group_id": val},
            "type": {"client_type": val},
        }
        return mapping.get(kind, {"target_uuid": spec})
    return {"target_uuid": spec}  # bare UUID = direct


class JobWaiter:
    """Resolves a future when the matching job_update arrives."""

    def __init__(self):
        self._futures: dict[str, asyncio.Future] = {}

    def expect(self, job_id: str) -> asyncio.Future:
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._futures[job_id] = fut
        return fut

    def resolve(self, job_id: str, payload: dict):
        fut = self._futures.pop(job_id, None)
        if fut and not fut.done():
            fut.set_result(payload)


def _decode_bus_log(message):
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


def make_handler(waiter: JobWaiter):
    async def handler(message):
        try:
            data = _decode_bus_log(message)
            if data is None:
                return
            payload = data.get("payload", {})
            if payload.get("kind") == "job_update":
                waiter.resolve(payload.get("job_id"), payload)
        except Exception as e:
            print(f"[llm] handler error: {e}", file=sys.stderr)
    return handler


async def run(server: str, token: str, target: str, script: str, timeout: float):
    transport = StreamableHttpTransport(
        url=server,
        headers={"Authorization": f"Bearer {token}"},
    )
    waiter = JobWaiter()
    handler = make_handler(waiter)

    client_uuid = f"llm-example-{uuid.uuid4().hex[:8]}"
    job_id = f"job-{uuid.uuid4().hex[:8]}"

    async with Client(transport, message_handler=handler) as client:
        try:
            await client.set_logging_level("debug")
        except Exception:
            pass

        await client.call_tool("blender_register_client", {
            "client_uuid": client_uuid,
            "client_type": "llm",
            "is_persistent": False,
            "capabilities": ["text_generation", "reasoning"],
        })

        routing = parse_target(target)
        send_args = {
            "payload": {
                "message_type": "job_dispatch",
                "job_id": job_id,
                "script": script,
                "description": "llm_client_example dispatch",
            },
            "priority": "info",
            **routing,
        }

        fut = waiter.expect(job_id)
        await client.call_tool("blender_send_message", send_args)
        print(f"[llm] send_message -> job_id={job_id}")

        exit_code = 0
        try:
            reply = await asyncio.wait_for(fut, timeout=timeout)
            print(f"[llm] received job_update status={reply.get('status')}")
            result = (reply.get("result") or "").strip()
            if result:
                print(f"[llm] result: {result}")
            error = (reply.get("error") or "").strip()
            if error:
                print(f"[llm] error: {error}")
                exit_code = 1
        except asyncio.TimeoutError:
            print(f"[llm] timeout waiting for job_update on {job_id}", file=sys.stderr)
            exit_code = 2

        try:
            await client.call_tool("blender_unregister_client", {"client_uuid": client_uuid})
        except Exception:
            pass

        if exit_code:
            sys.exit(exit_code)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--server", default="http://localhost:8000/mcp",
                    help="MCP endpoint URL (default: %(default)s)")
    ap.add_argument("--token", required=True, help="JWT from POST /auth/login")
    ap.add_argument("--target", required=True,
                    help="Bare UUID (direct), uuid:X, group:X, type:X, or 'broadcast'")
    ap.add_argument("--script", required=True, help="Python source to execute in Blender")
    ap.add_argument("--timeout", type=float, default=30.0,
                    help="Seconds to wait for job_update (default: %(default)s)")
    args = ap.parse_args()
    asyncio.run(run(args.server, args.token, args.target, args.script, args.timeout))


if __name__ == "__main__":
    main()
