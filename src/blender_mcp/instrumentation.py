"""Tool-call instrumentation via fastmcp-feedback, switched by ``QA_LOG``.

``QA_LOG``:
    unset / ``off``  nothing is installed. THE DEFAULT.
    ``meta``         every call recorded to ``ffb_tool_calls``: tool, timing,
                     outcome, caller identity, payload sizes, job/target ids.
    ``full``         also redacted arguments and results, except for the
                     tools in ``META_ONLY_TOOLS``, which stay at ``meta``.

``QA_LOG_STDERR`` (truthy) additionally writes each record as a JSON line to
stderr, so it lands in ``docker logs``.

Redaction of secrets (bearer tokens, ``bmcp_`` access tokens, JWTs,
``*_token``/``*_secret`` keys) in arguments, results, errors and hook output
is on by default in the package.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from importlib.metadata import PackageNotFoundError, version
from typing import Any

logger = logging.getLogger(__name__)

FFB_TABLE_PREFIX = "ffb_"

# Never record payloads for these, even at full: their arguments carry
# arbitrary Python (execute_code, and submit which wraps it) or their
# results carry a live secret (access token creation).
META_ONLY_TOOLS = frozenset({
    "blender_execute_code",
    "blender_submit",
    "blender_create_access_token",
    "blender_set_chat_backend",  # carries a provider API key
    "blender_chat",
})

try:
    SERVER_VERSION: str | None = version("blender-mcp")
except PackageNotFoundError:  # running from a source tree without install
    SERVER_VERSION = None

# Only the head of a result is scanned for ids; execute_code results can be
# megabytes and the enricher runs on the call path.
_SCAN_CHARS = 4096
_ID_PATTERNS = {
    "job_id": re.compile(r'"job_id"\s*:\s*"(j-[0-9a-f]+)"'),
    "target_uuid": re.compile(r'"target_uuid"\s*:\s*"([^"]{1,128})"'),
    "bus_id": re.compile(r'"bus_id"\s*:\s*"([0-9a-f-]{36})"'),
}
_ARG_KEYS = ("target_uuid", "bus_id", "job_id", "worker_uuid")


def qa_mode() -> str:
    mode = (os.getenv("QA_LOG") or "off").strip().lower()
    return mode if mode in ("off", "meta", "full") else "off"


def _truthy(name: str) -> bool:
    return (os.getenv(name) or "").strip().lower() in ("1", "true", "yes", "on")


def identify(context: Any) -> dict:
    """identity_resolver: who made this call. Never raises."""
    out: dict[str, Any] = {}
    ctx = getattr(context, "fastmcp_context", None)
    try:
        from .bus_tools import _resolve_user_id

        out["user_sub"] = _resolve_user_id(ctx)
    except Exception:  # noqa: BLE001, S110 - identity lookup must never break a call
        pass
    access_client_id = None
    try:
        from fastmcp.server.dependencies import get_access_token

        access = get_access_token()
        access_client_id = getattr(access, "client_id", None) if access else None
    except Exception:  # noqa: BLE001, S110 - identity lookup must never break a call
        pass
    try:
        from .client_role import current_downstream_client_id

        downstream = current_downstream_client_id.get() or ""
        if downstream.startswith("chat:"):
            # A tool the in-Blender chat ran on the user's behalf (chat/executor.py).
            out["caller_kind"] = "chat"
        elif access_client_id and str(access_client_id).startswith("pat:"):
            out["caller_kind"] = str(access_client_id)
        else:
            from .client_role import get_caller_role

            out["caller_kind"] = get_caller_role(ctx)
    except Exception:  # noqa: BLE001, S110 - identity lookup must never break a call
        pass
    try:
        from .client_role import current_downstream_client_id

        out["client_id"] = current_downstream_client_id.get() or access_client_id
    except Exception:  # noqa: BLE001 - identity lookup must never break a call
        if access_client_id:
            out["client_id"] = access_client_id
    # Registered Blender clients (GUI or worker) carry a bus identity.
    try:
        from .bus_tools import _session_from_ctx
        from .message_bus import bus_manager

        where = bus_manager.lookup_session(_session_from_ctx(ctx)) if ctx else None
        if where:
            bus_id, client_uuid = where[0], where[1]
            out["bus_client_uuid"] = client_uuid
            bus = bus_manager.all_buses().get(bus_id)
            client = bus.get(client_uuid) if bus is not None else None
            if client is not None:
                out["bus_role"] = getattr(client, "role", None)
                out["parent_uuid"] = getattr(client, "parent_uuid", None)
    except Exception:  # noqa: BLE001, S110 - identity lookup must never break a call
        pass
    return {k: v for k, v in out.items() if v is not None}


def enrich(tool: str, args: Any, result: Any, context: Any) -> dict:
    """enricher: cheap ids linking a call to jobs and Blender clients."""
    out: dict[str, Any] = {}
    if SERVER_VERSION:
        out["server_version"] = SERVER_VERSION
    if isinstance(args, dict):
        for key in _ARG_KEYS:
            value = args.get(key)
            if isinstance(value, str) and value:
                out[key] = value[:128]
    head = _result_head(result)
    if head:
        for key, pattern in _ID_PATTERNS.items():
            if key in out:
                continue
            m = pattern.search(head)
            if m:
                out[key] = m.group(1)
    return out


def _result_head(result: Any) -> str:
    """First few KB of a tool result as text, without parsing it."""
    if isinstance(result, str):
        return result[:_SCAN_CHARS]
    if isinstance(result, list):
        for item in result:
            if isinstance(item, str):
                return item[:_SCAN_CHARS]
        return ""
    # Structured content: small dicts only; str() of a huge dict costs as
    # much as parsing, so cap by key count.
    if isinstance(result, dict) and len(result) <= 50:
        return str(result)[:_SCAN_CHARS]
    return ""


def install(server: Any) -> Any:
    """Add the instrumentation middleware to ``server`` if QA_LOG enables it.

    Must be called before any other ``add_middleware`` so it is outermost
    and records calls other middleware reject. Returns the middleware, or
    None when disabled (then nothing is installed and nothing is written).
    """
    mode = qa_mode()
    if mode == "off":
        return None
    from fastmcp_feedback.instrumentation import DatabaseSink, JsonLinesSink, instrument

    from .storage import get_engine

    sinks = [DatabaseSink(get_engine(), prefix=FFB_TABLE_PREFIX)]
    if _truthy("QA_LOG_STDERR"):
        sinks.append(JsonLinesSink(sys.stderr))
    mw = instrument(
        server,
        sinks,
        mode=mode,
        meta_only_tools=META_ONLY_TOOLS,
        identity_resolver=identify,
        enricher=enrich,
        server_version=SERVER_VERSION,
    )
    logger.info("Tool-call instrumentation on (QA_LOG=%s, table %stool_calls)", mode, FFB_TABLE_PREFIX)
    return mw
