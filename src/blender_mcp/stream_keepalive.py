"""Periodic keepalive notifications to every connected Blender client.

Dispatches reach the addon as MCP notifications on the session's standalone
SSE (GET) stream, while the addon's heartbeat is a POST ping. The ping can
keep succeeding after that stream has died: the MCP client stops
reconnecting it after two errors, and without an event store anything sent
while it's detached is dropped. A small notification every few seconds
gives the addon something to expect on the stream, so it can tell "idle"
from "dead" and reconnect. It also keeps proxies from treating the stream
as idle.

Sent at MCP level ``debug`` on the ``_message_bus`` logger: the addon
recognizes ``message_type == "bus_keepalive"`` and never queues it, and
older addons drop it because it carries no job_id.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

from .message_bus import bus_manager

logger = logging.getLogger(__name__)


def _interval() -> float:
    try:
        return max(5.0, float(os.environ.get("BLENDER_MCP_STREAM_KEEPALIVE", "20")))
    except ValueError:
        return 20.0


KEEPALIVE_INTERVAL_S = _interval()


def keepalive_targets() -> list[tuple[str, str, object]]:
    """(bus_id, client_uuid, session) for every registered Blender client."""
    out = []
    for bus_id, bus in bus_manager.all_buses().items():
        for c in bus.all_clients():
            if c.client_type == "blender" and c.session is not None:
                out.append((str(bus_id), c.uuid, c.session))
    return out


async def send_keepalives() -> int:
    sent = 0
    now = time.time()
    for bus_id, uuid, session in keepalive_targets():
        data = {
            "bus_id": bus_id,
            "target_uuid": uuid,
            "payload": {"message_type": "bus_keepalive", "sent_at": now},
            "timestamp": now,
        }
        try:
            await asyncio.wait_for(
                session.send_log_message(level="debug", data=data, logger="_message_bus"),
                timeout=5.0,
            )
            sent += 1
        except Exception as e:  # noqa: BLE001 - a dead session is the reason we're here
            logger.debug("keepalive to %s failed: %s", uuid, e)
    return sent


async def run_keepalive(interval: float | None = None) -> None:
    interval = interval or KEEPALIVE_INTERVAL_S
    while True:
        await asyncio.sleep(interval)
        try:
            await send_keepalives()
        except Exception as e:  # noqa: BLE001 - never let the loop die
            logger.warning("keepalive round failed: %s", e)
