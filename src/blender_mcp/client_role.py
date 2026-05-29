"""Per-OAuth-client role tracking (phase H — addon vs. LLM-client separation).

Two roles exist:

- ``addon``        — the BlenderMCP Blender addon (declares ``software_id =
                     "blender-mcp-addon"`` during DCR). It RECEIVES dispatched
                     work from LLM clients via the bus.
- ``llm-client``   — anything else (Claude Code, Desktop, mcp-inspector,
                     scripts, etc.). It SENDS dispatched work to the addon.

A third value, ``unspecified``, is recorded for clients we have no record
of (e.g. tokens issued before the DCR-capture middleware existed, or by an
in-memory backend during testing). Policy: ``unspecified`` is treated as
``llm-client`` by :func:`get_caller_role` so existing LLM clients keep
working without changes — only the addon must opt in.

Roles are populated at DCR time by the FastAPI middleware in
``oauth_server.py`` and consulted at tool-call time via
:func:`get_caller_role`.
"""

from __future__ import annotations

import functools
import json
import logging
from typing import Any, Awaitable, Callable, Optional

from fastmcp.server.dependencies import get_access_token

logger = logging.getLogger(__name__)


# ----- registry ---------------------------------------------------------

# DCR-issued client_id → role. Populated by ``record_client_role`` from the
# DCR-capture middleware. In-memory only; rebuilds on server restart along
# with FastMCP's own client storage.
_role_by_client_id: dict[str, str] = {}

# Recognized software_id values. Anything not in this map gets logged as
# "unknown" and falls through to ``llm-client``.
_SOFTWARE_ID_TO_ROLE = {
    "blender-mcp-addon": "addon",
}


def record_client_role(client_id: str, software_id: Optional[str]) -> str:
    """Record the role for a freshly-registered DCR client.

    Returns the role that was recorded (for logging convenience). Unknown
    software_id values get recorded as ``llm-client`` so the registry is
    always consulted-once-and-done.
    """
    role = _SOFTWARE_ID_TO_ROLE.get(software_id or "", "llm-client")
    _role_by_client_id[client_id] = role
    logger.info(
        "DCR: client_id=%s registered as role=%s (software_id=%r)",
        client_id, role, software_id,
    )
    return role


def get_caller_role(ctx: Any = None) -> str:
    """Return the role for the current request's OAuth client.

    Falls back to ``llm-client`` if there's no access token in scope, or if
    the token's client_id isn't in the registry (e.g. issued before
    middleware was installed). This keeps existing LLM clients working
    unchanged — only ``addon`` requires explicit opt-in via DCR software_id.

    The ``ctx`` parameter is accepted for symmetry with bus_tools.
    ``_resolve_user_id`` but isn't used here — FastMCP's
    ``get_access_token`` reads the current request's auth state from a
    ContextVar that's set by the auth middleware.
    """
    try:
        token = get_access_token()
    except Exception:
        return "llm-client"
    if token is None:
        return "llm-client"

    # IMPORTANT: with OIDCProxy/OAuthProxy, ``token.client_id`` is the
    # UPSTREAM Authentik app's client_id (the OIDCProxy's own client
    # to Authentik), NOT the per-installation DOWNSTREAM DCR client_id.
    # Every addon in the world shares the same upstream client_id, so
    # role attribution by ``token.client_id`` is impossible — every
    # lookup would miss the registry and default to llm-client.
    #
    # The raw JWT payload preserves the downstream client_id (it was
    # written there at token-issue time by FastMCP's jwt_issuer with
    # the value of transaction["client_id"] from the DCR record).
    # Read it from ``token.claims["client_id"]`` first; fall back to
    # ``token.client_id`` for non-proxy issuers like the inmemory
    # BlenderMCPOAuthProvider where the two are the same value.
    claims = getattr(token, "claims", None) or {}
    client_id = claims.get("client_id") or getattr(token, "client_id", None)
    if not client_id:
        return "llm-client"
    return _role_by_client_id.get(client_id, "llm-client")


# ----- gating decorator -------------------------------------------------


def _format_rejection(tool_name: str, your_role: str, allowed: tuple[str, ...]) -> str:
    """Build the structured-JSON rejection payload."""
    hint_map = {
        "addon": (
            "This tool is for the Blender addon (which receives dispatched "
            "work). If you are an LLM client, you probably wanted a "
            "``blender_*`` dispatch tool instead."
        ),
        "llm-client": (
            "This tool is for LLM clients (which dispatch work to the addon). "
            "The addon itself shouldn't call dispatch tools — that would "
            "create a dispatch loop."
        ),
    }
    # Pick the most specific hint based on what's allowed.
    if len(allowed) == 1:
        hint = hint_map.get(allowed[0], "Wrong role for this tool.")
    else:
        hint = f"This tool requires role in {list(allowed)}."
    return json.dumps({
        "status": "wrong_role",
        "tool": tool_name,
        "your_role": your_role,
        "tool_requires": list(allowed),
        "hint": hint,
    })


def require_role(*allowed_roles: str) -> Callable:
    """Decorator: only let callers with one of ``allowed_roles`` invoke the tool.

    On mismatch, the tool returns structured JSON instead of raising — MCP
    clients display this naturally as a tool result rather than choking on
    an exception serialized over the wire.

    Wraps async methods on MCPMixin components. The wrapped function still
    receives ``ctx`` so it can use it normally; this decorator just adds a
    pre-check.
    """
    allowed = tuple(allowed_roles)

    def decorator(func: Callable[..., Awaitable[str]]) -> Callable[..., Awaitable[str]]:
        @functools.wraps(func)
        async def wrapper(self, *args, **kwargs):
            role = get_caller_role(kwargs.get("ctx"))
            if role not in allowed:
                logger.info(
                    "Role gate: %s rejected for role=%s (allowed=%s)",
                    func.__name__, role, allowed,
                )
                return _format_rejection(func.__name__, role, allowed)
            return await func(self, *args, **kwargs)
        return wrapper
    return decorator


def check_role_or_reject(tool_name: str, ctx: Any, *allowed_roles: str) -> Optional[str]:
    """Imperative variant of ``require_role`` for funnels like ``_dispatch``.

    Returns ``None`` if the caller is allowed; returns a JSON rejection
    string otherwise. Lets callers include the actual tool name (vs. the
    funnel's name) in the rejection payload.
    """
    role = get_caller_role(ctx)
    if role not in allowed_roles:
        logger.info(
            "Role gate: %s rejected for role=%s (allowed=%s)",
            tool_name, role, allowed_roles,
        )
        return _format_rejection(tool_name, role, allowed_roles)
    return None
