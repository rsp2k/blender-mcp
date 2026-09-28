"""Approval prompts: MCP elicitations that start with "BlenderMCP approval:".

The server asks while blender_chat is running, with an empty schema, so
the answer is accept (Allow) or decline (Deny). Anything else is declined.

mcp's ClientSession awaits the elicitation callback inside its receive
loop (mcp/shared/session.py _receive_loop -> client/session.py
_received_request), so a callback that waits two minutes for a click
would hold up every other incoming message: ping replies, dispatch pulls,
bus notifications. The heartbeat would time out and reconnect, killing
the chat turn. install_nonblocking_elicitation moves elicitations onto
the session's task group so the receive loop keeps running.
"""

from __future__ import annotations

import asyncio
from typing import Any

from . import state as _state
from .state import chat_state, is_approval_request


def _redraw() -> None:
    try:
        from ..connection import request_ui_redraw
        request_ui_redraw()
    except Exception:  # noqa: BLE001, S110
        pass


def _flush_log() -> None:
    try:
        from .client import schedule_log_flush
        schedule_log_flush()
    except Exception:  # noqa: BLE001, S110
        pass


def _decline():
    from fastmcp.client.elicitation import ElicitResult
    return ElicitResult(action="decline")


def _accept():
    from fastmcp.client.elicitation import ElicitResult
    return ElicitResult(action="accept", content={})


async def handle_elicitation(message: str, response_type: Any, params: Any, context: Any) -> Any:
    """fastmcp elicitation_handler. Runs on the client loop."""
    if not is_approval_request(message):
        print(f"[BlenderMCP] Declined an unrecognised elicitation: {str(message)[:80]!r}")
        return _decline()

    loop = asyncio.get_running_loop()
    future = loop.create_future()
    previous = chat_state.set_pending_approval(message, future, loop)
    if previous and not previous["future"].done():
        previous["future"].set_result(False)
    chat_state.log_raw(message)
    _flush_log()
    _redraw()
    try:
        allowed = await asyncio.wait_for(future, _state.APPROVAL_TIMEOUT_S)
    except TimeoutError:
        allowed = False
        chat_state.add_message("status", "No answer within two minutes, so it was declined.")
    finally:
        chat_state.clear_pending_approval(future)
        _flush_log()
        _redraw()
    chat_state.add_message("status", "Allowed." if allowed else "Declined.")
    return _accept() if allowed else _decline()


def _set_result(future: Any, allowed: bool) -> None:
    if not future.done():
        future.set_result(allowed)


def resolve_approval(allowed: bool) -> bool:
    """Answer the pending approval from any thread (Allow/Deny operators)."""
    with chat_state.lock:
        pending = chat_state.pending_approval
    if not pending:
        return False
    try:
        pending["loop"].call_soon_threadsafe(_set_result, pending["future"], bool(allowed))
    except RuntimeError:
        # Loop already closed: the session is gone and the server gave up.
        chat_state.clear_pending_approval(pending["future"])
        return False
    return True


def install_nonblocking_elicitation(session: Any) -> bool:
    """Run elicitation requests in the session's task group instead of inline."""
    original = getattr(session, "_received_request", None)
    group = getattr(session, "_task_group", None)
    if original is None or group is None:
        print("[BlenderMCP] Couldn't make approvals non-blocking; mcp internals changed")
        return False
    if getattr(original, "_blendermcp_nonblocking", False):
        return True
    try:
        from mcp.types import ElicitRequest
    except ImportError:
        return False

    async def _answer(responder: Any) -> None:
        # An exception escaping a task-group child would tear the whole
        # session down; inline, the receive loop would have caught it.
        try:
            await original(responder)
        except Exception as e:  # noqa: BLE001
            print(f"[BlenderMCP] Elicitation handling failed: {e!r}")

    async def _received_request(responder: Any) -> None:
        root = getattr(responder.request, "root", None)
        if isinstance(root, ElicitRequest) and getattr(root.params, "task", None) is None:
            # The receive loop then hands the not-yet-answered responder to
            # the message handler, which ignores anything without a method.
            group.start_soon(_answer, responder)
            return
        await original(responder)

    _received_request._blendermcp_nonblocking = True
    session._received_request = _received_request
    return True
