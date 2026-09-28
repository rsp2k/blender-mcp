"""Send chat turns and backend settings over the bus client's own loop.

Everything here is scheduled with run_coroutine_threadsafe and finishes in
a done callback; nothing waits on the loop. That matters because the tools
the model picks come back to this same Blender as ordinary dispatches
(pending_dispatches + the drain timer) while blender_chat is still running.

send/stop/set_backend are called on Blender's main thread. Progress
handlers and done callbacks run on the loop thread and only touch
chat_state; the text log and redraws hop to the main thread via timers.
"""

from __future__ import annotations

import asyncio
from typing import Any

from .state import (
    chat_state,
    decode_tool_result,
    parse_progress_event,
    result_message,
    tool_error_text,
)

CHAT_TOOL = "blender_chat"
SET_BACKEND_TOOL = "blender_set_chat_backend"
GET_BACKEND_TOOL = "blender_get_chat_backend"
# The server stops a turn at its own limit (5 min by default) and approvals
# count toward it; this only catches a server that never answers.
CALL_TIMEOUT_S = 600.0
BACKEND_TIMEOUT_S = 30.0
CANCEL_TIMEOUT_S = 5.0


def request_redraw() -> None:
    try:
        from ..connection import request_ui_redraw
        request_ui_redraw()
    except Exception:  # noqa: BLE001, S110
        pass


def schedule_log_flush() -> None:
    try:
        import bpy

        from .log import flush_pending
        if not bpy.app.timers.is_registered(flush_pending):
            bpy.app.timers.register(flush_pending, first_interval=0.0)
    except Exception:  # noqa: BLE001, S110
        pass


def chats_dir() -> str | None:
    """Where saved conversations live: Blender's config folder, which persists
    across restarts (and container recreates, on the config volume)."""
    try:
        import bpy
        return bpy.utils.user_resource('CONFIG', path="blender_mcp/chats", create=True)
    except Exception:  # noqa: BLE001 - chat still works, it just isn't saved
        return None


def persist_current() -> None:
    """Save the conversation on screen. Main thread (timer or operator)."""
    from . import history
    folder = chats_dir()
    cid, messages = chat_state.export()
    if folder and cid and messages:
        try:
            history.save(folder, cid, messages)
        except OSError as e:
            print(f"[BlenderMCP] couldn't save chat: {e}")


def schedule_persist() -> None:
    try:
        import bpy
        if not bpy.app.timers.is_registered(persist_current):
            bpy.app.timers.register(persist_current, first_interval=0.0)
    except Exception:  # noqa: BLE001, S110
        pass


def open_conversation(cid: str) -> bool:
    """Switch the panel to a saved conversation (saving the current one first)."""
    from . import history
    folder = chats_dir()
    if not folder or chat_state.busy:
        return False
    persist_current()
    ok = chat_state.load_conversation(cid, history.load(folder, cid))
    request_redraw()
    return ok


def new_conversation() -> bool:
    if chat_state.busy:
        return False
    persist_current()
    ok = chat_state.load_conversation(None, [])
    request_redraw()
    return ok


def restore_latest() -> None:
    """At startup, bring back the most recent conversation. Main thread."""
    from . import history
    folder = chats_dir()
    if not folder or chat_state.has_messages():
        return
    latest = history.listing(folder)[:1]
    if latest:
        chat_state.load_conversation(latest[0]["id"], history.load(folder, latest[0]["id"]))
        request_redraw()


def _live_client() -> Any | None:
    from .. import connection, state
    client = state._client
    if not connection.client_alive(client):
        return None
    if not (client.connected and client.client and client.loop and client.loop.is_running()):
        return None
    return client


def send_problem() -> str | None:
    """Why a message can't be sent right now, or None. Main thread."""
    from ..preferences import get_prefs
    try:
        if not get_prefs().jwt_token:
            return "Log in from the BlenderMCP tab to chat."
    except Exception:  # noqa: BLE001, S110
        pass
    if _live_client() is None:
        return "Not connected. Turn on Connect in the BlenderMCP tab."
    if chat_state.available is False:
        return "This server doesn't offer chat."
    if chat_state.available is None:
        return "Still checking whether this server offers chat."
    if chat_state.busy:
        return "A reply is still coming. Press Stop to cancel it."
    return None


def _make_progress_handler(turn: int):
    async def _on_progress(progress: float, total: float | None, message: str | None) -> None:
        event = parse_progress_event(message)
        if event is None or not chat_state.turn_open(turn):
            return
        chat_state.apply_event(event)
        schedule_log_flush()
        request_redraw()
    return _on_progress


async def _call_chat(mcp_client: Any, text: str, history: list, handler: Any) -> Any:
    return await mcp_client.call_tool(
        CHAT_TOOL,
        {"message": text, "history": history or None},
        progress_handler=handler,
        timeout=CALL_TIMEOUT_S,
        raise_on_error=False,
    )


def _finish_from_future(fut: Any, turn: int) -> None:
    """Done callback for a chat call (loop thread)."""
    if fut.cancelled():
        chat_state.finish_turn(turn, "status", "Stopped.")
    else:
        try:
            result = fut.result()
        except Exception as e:  # noqa: BLE001
            chat_state.finish_turn(turn, "error", f"Chat call failed: {str(e) or type(e).__name__}")
        else:
            if getattr(result, "is_error", False) or getattr(result, "isError", False):
                chat_state.finish_turn(turn, "error", f"Chat call failed: {tool_error_text(result)}")
            else:
                payload = decode_tool_result(result)
                role, text = result_message(payload)
                chat_state.finish_turn(turn, role, text, payload)
    schedule_log_flush()
    schedule_persist()
    request_redraw()


def send(text: str) -> tuple[bool, str | None]:
    """Start a chat turn. Returns (started, problem). Main thread."""
    text = (text or "").strip()
    if not text:
        return False, None
    problem = send_problem()
    if problem:
        chat_state.last_error = problem
        request_redraw()
        return False, problem
    client = _live_client()
    if chat_state.conversation_id is None:
        from .history import new_id
        chat_state.conversation_id = new_id()
    history = chat_state.begin_turn(text)
    schedule_persist()
    turn = chat_state.turn
    handler = _make_progress_handler(turn)
    try:
        fut = asyncio.run_coroutine_threadsafe(
            _call_chat(client.client, text, history, handler), client.loop,
        )
    except Exception as e:  # noqa: BLE001
        chat_state.finish_turn(turn, "error", f"Couldn't start the chat call: {e}")
        schedule_log_flush()
        request_redraw()
        return False, str(e)
    with chat_state.lock:
        if chat_state.turn_open(turn):
            chat_state.inflight = (fut, handler, client)
    fut.add_done_callback(lambda f: _finish_from_future(f, turn))
    schedule_log_flush()
    request_redraw()
    return True, None


def _request_id_for(mcp_client: Any, handler: Any) -> Any | None:
    """The JSON-RPC id of our blender_chat request.

    mcp's send_request stores the progress callback under the request id,
    so the handler object we passed identifies it exactly.
    """
    try:
        callbacks = mcp_client.session._progress_callbacks
    except Exception:  # noqa: BLE001
        return None
    for rid, cb in list(callbacks.items()):
        if cb is handler:
            return rid
    return None


async def _cancel_remote(mcp_client: Any, handler: Any, fut: Any) -> None:
    try:
        rid = _request_id_for(mcp_client, handler)
        if rid is not None:
            await asyncio.wait_for(
                mcp_client.cancel(rid, "stopped from the Blender chat panel"), CANCEL_TIMEOUT_S,
            )
    except Exception as e:  # noqa: BLE001
        print(f"[BlenderMCP] Chat cancel notification failed: {e!r}")
    finally:
        fut.cancel()


def stop() -> bool:
    """Stop the turn in flight: MCP cancel to the server, then drop the call. Main thread."""
    with chat_state.lock:
        inflight = chat_state.inflight
        turn = chat_state.turn
        was_busy = chat_state.busy
    if not was_busy:
        return False
    from .elicitation import resolve_approval
    resolve_approval(False)
    if inflight is not None:
        fut, handler, client = inflight
        loop, mcp_client = client.loop, client.client
        if loop is not None and mcp_client is not None and loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(_cancel_remote(mcp_client, handler, fut), loop)
            except Exception:  # noqa: BLE001
                fut.cancel()
        else:
            fut.cancel()
    chat_state.finish_turn(turn, "status", "Stopped.")
    schedule_log_flush()
    request_redraw()
    return True


# --- backend settings -------------------------------------------------------

def _on_backend_result(fut: Any) -> None:
    try:
        result = fut.result()
    except Exception as e:  # noqa: BLE001
        chat_state.backend_error = f"{str(e) or type(e).__name__}"
    else:
        if getattr(result, "is_error", False) or getattr(result, "isError", False):
            chat_state.backend_error = tool_error_text(result)
        else:
            payload = decode_tool_result(result) or {}
            if payload.get("status") == "ok" and isinstance(payload.get("backend"), dict):
                chat_state.backend = payload["backend"]
                chat_state.backend_error = None
            else:
                chat_state.backend_error = str(
                    payload.get("detail") or payload.get("message")
                    or f"status {payload.get('status')!r}"
                )
    request_redraw()


def _call_backend_tool(name: str, args: dict) -> str | None:
    client = _live_client()
    if client is None:
        return "Not connected."
    try:
        fut = asyncio.run_coroutine_threadsafe(
            client.client.call_tool(name, args, timeout=BACKEND_TIMEOUT_S, raise_on_error=False),
            client.loop,
        )
    except Exception as e:  # noqa: BLE001
        return str(e)
    fut.add_done_callback(_on_backend_result)
    return None


def refresh_backend() -> str | None:
    """Ask the server for the current backend. Any thread; never waits."""
    return _call_backend_tool(GET_BACKEND_TOOL, {})


def set_backend(provider: str, model: str = "", base_url: str = "",
                api_key: str = "") -> str | None:
    args: dict = {"provider": provider}
    if model.strip():
        args["model"] = model.strip()
    if base_url.strip():
        args["base_url"] = base_url.strip()
    if api_key:
        args["api_key"] = api_key
    return _call_backend_tool(SET_BACKEND_TOOL, args)


def clear_backend(provider: str) -> str | None:
    return _call_backend_tool(SET_BACKEND_TOOL, {"provider": provider, "clear": True})


def on_registered(payload: dict | None) -> None:
    """Registration answered (loop thread): read features, fetch the backend."""
    features = payload.get("features") if isinstance(payload, dict) else None
    chat_state.set_features(features)
    if chat_state.available:
        refresh_backend()
    request_redraw()
