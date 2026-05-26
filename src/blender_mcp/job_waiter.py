"""Server-side asyncio.Future registry keyed by (user_id, job_id).

Lets new dispatch tools in :mod:`dispatch_component` send a message to
a blender client and ``await`` the reply transparently, instead of
forcing the MCP caller to listen for ``notifications/message`` on
``_message_bus`` and grep their own session for the matching ``job_id``.

The flow:

1. Dispatch tool calls :meth:`JobWaiter.register(user_id, job_id)` →
   gets back an ``asyncio.Future`` already inserted in the registry.
2. Tool calls :func:`bus_tools.send_message` to push the job onto the
   bus (which carries the ``job_id`` to the addon).
3. Tool calls ``await asyncio.wait_for(future, timeout=...)``.
4. Addon executes the script/command, sends ``blender_job_update``.
5. :meth:`JobWaiter.deliver` is called from inside
   :func:`bus_tools.job_update` after the existing routing — it
   resolves the Future with a result dict, waking the tool.
6. Tool returns the result; the registry entry is auto-cleaned in a
   ``finally``.

Crucially, ``deliver`` is a *no-op* when no Future is registered — so
the JobWaiter is fully additive on top of the pre-existing
``_pending_jobs`` routing in ``bus_tools.py``. Clients that never call
:meth:`register` still get the broadcast/log-notification behavior they
had before this module existed.

Keying on ``(user_id, job_id)`` — not just ``job_id`` — prevents
cross-user job_id collisions from waking the wrong tool call. Two
sessions in different OAuth-isolated buses can both pick ``job_id="x"``
without interfering.
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional


# Result tuple shape stored in the Future (and returned to the caller of
# ``await future``):
#
#     {"status": str, "result": str, "error": str}
#
# Where ``status`` is whatever the addon reported in ``blender_job_update``
# (typically "completed" or "failed"). The Future is *resolved*, never
# *rejected* — error conditions ride in the dict so MCP tool callers can
# return JSON instead of raising (matching the bus_tools.py convention).


class JobWaiter:
    """Singleton-style registry of pending dispatch awaiters.

    Thread-safe by virtue of asyncio: every call should happen from
    code running on the FastAPI/uvicorn event loop. The internal dicts
    are never touched from threads, so no lock is needed.
    """

    def __init__(self) -> None:
        self._futures: dict[tuple[str, str], asyncio.Future] = {}

    # --- registration / cancellation ---------------------------------

    def register(self, user_id: str, job_id: str) -> asyncio.Future:
        """Create + register a Future for the given (user, job).

        The Future is created on the *running* loop, which means callers
        must register from inside an async context (any MCP tool body
        qualifies). Re-registering an existing key overwrites the old
        Future and silently drops anyone awaiting it — generally
        indicates a bug in the caller (don't reuse job_ids within a user).
        """
        key = (user_id, job_id)
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._futures[key] = future
        return future

    def cancel(self, user_id: str, job_id: str) -> None:
        """Drop the registry entry without resolving its Future.

        Used by dispatch tools in their ``finally`` block after
        ``asyncio.wait_for`` raises ``TimeoutError`` — keeps the dict
        from leaking entries when the addon never replies. The Future
        itself stays cancelled; any latecomer ``deliver`` for the same
        key becomes a no-op (the entry is gone).
        """
        fut = self._futures.pop((user_id, job_id), None)
        if fut is not None and not fut.done():
            fut.cancel()

    # --- delivery ----------------------------------------------------

    def deliver(
        self,
        user_id: str,
        job_id: str,
        status: str,
        result: Any = "",
        error: str = "",
    ) -> bool:
        """Resolve the Future for ``(user_id, job_id)`` if one is registered.

        Returns ``True`` if a Future was waked, ``False`` if no awaiter
        was registered (caller didn't go through the dispatch tool
        layer). The False case is normal — it just means we're seeing a
        ``blender_job_update`` from a job that was sent via the old
        ``send_message`` + listen-for-notification pattern.

        Called from inside :func:`bus_tools.job_update`, *after* the
        existing routing happens. Order matters: routing first preserves
        the old behavior for any non-dispatch-tool subscribers; deliver
        second wakes the new dispatch-tool awaiter.
        """
        fut = self._futures.pop((user_id, job_id), None)
        if fut is None or fut.done():
            return False
        fut.set_result({
            "status": status,
            "result": result,
            "error": error,
        })
        return True

    # --- introspection (handy for tests + /health-style checks) ------

    def pending_count(self, user_id: Optional[str] = None) -> int:
        """Number of awaiting Futures; optionally filtered to one user."""
        if user_id is None:
            return len(self._futures)
        return sum(1 for (uid, _) in self._futures if uid == user_id)

    def pending_keys(self) -> list[tuple[str, str]]:
        """All currently-pending (user_id, job_id) keys. Snapshot copy."""
        return list(self._futures.keys())


# Module-level singleton. Importers grab the same instance; tests can
# reach in via ``import job_waiter as jw; jw.job_waiter = JobWaiter()``
# to reset state between cases.
job_waiter = JobWaiter()
