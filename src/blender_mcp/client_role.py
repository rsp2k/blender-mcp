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

import contextvars
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

# Per-request DOWNSTREAM client_id, snooped from the raw FastMCP JWT by the
# middleware in ``oauth_server.py`` BEFORE FastMCP unwraps the bearer.
#
# Reason this ContextVar exists: by the time a tool runs, FastMCP's
# ``get_access_token()`` returns the UPSTREAM Authentik AccessToken — its
# ``client_id`` is the OIDCProxy's own Authentik app id, which is the same
# value for every installation in the world. The DOWNSTREAM DCR-issued
# client_id we need for role lookup lives only in the raw FastMCP JWT
# payload's ``client_id`` claim. Snooping it at the middleware layer and
# stashing it here is the only path to keep it in scope by the time a
# tool's ``@require_role`` decorator runs.
current_downstream_client_id: contextvars.ContextVar[Optional[str]] = (
    contextvars.ContextVar("current_downstream_client_id", default=None)
)

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

    Writes through to Postgres (fire-and-forget) so attribution survives
    server restarts. The in-memory dict is the authoritative fast path
    during the running process; DB persistence is just for restart-
    survival. DB write failures are logged and swallowed.
    """
    role = _SOFTWARE_ID_TO_ROLE.get(software_id or "", "llm-client")
    _role_by_client_id[client_id] = role
    logger.info(
        "DCR: client_id=%s registered as role=%s (software_id=%r)",
        client_id, role, software_id,
    )
    _schedule_db_persist(client_id, role, software_id)
    return role


# ----- Postgres persistence (restart-survival) --------------------------


def _schedule_db_persist(client_id: str, role: str, software_id: Optional[str]) -> None:
    """Fire-and-forget upsert into oauth_client_role.

    Imports inside the function to avoid pulling storage at module-load
    time (this module is imported during app build, before the storage
    engine is necessarily initialized). DB unavailable → log + swallow;
    the in-memory dict remains authoritative for the current process.
    """
    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # Not inside an event loop (synchronous test path). Skip silently.
        return
    loop.create_task(_db_persist(client_id, role, software_id))


async def _db_persist(client_id: str, role: str, software_id: Optional[str]) -> None:
    try:
        from sqlalchemy.dialects.postgresql import insert

        from .storage import OAuthClientRole, get_session

        stmt = insert(OAuthClientRole).values(
            client_id=client_id, role=role, software_id=software_id,
        ).on_conflict_do_update(
            index_elements=["client_id"],
            set_={"role": role, "software_id": software_id},
        )
        async with get_session() as s:
            await s.execute(stmt)
    except Exception as e:
        logger.warning(
            "Could not persist client role to DB (client_id=%s): %s",
            client_id, e,
        )


async def rehydrate_from_db() -> int:
    """Load all (client_id, role) rows from Postgres into the cache.

    Called once at FastAPI lifespan startup. Returns the number of entries
    loaded (for logging). Safe to call multiple times — overwrites the
    cache idempotently.
    """
    try:
        from sqlalchemy import select

        from .storage import OAuthClientRole, get_session

        async with get_session() as s:
            rows = (await s.execute(select(OAuthClientRole))).scalars().all()
        for row in rows:
            _role_by_client_id[row.client_id] = row.role
        logger.info("Role registry rehydrated from DB: %d entries", len(rows))
        return len(rows)
    except Exception as e:
        logger.warning("Role registry rehydration skipped: %s", e)
        return 0


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
    # Preferred: the JWT-snoop middleware captured the downstream client_id
    # from the raw bearer BEFORE FastMCP unwrapped it. This is the only
    # path that yields the per-installation DCR client_id under OIDCProxy.
    snooped = current_downstream_client_id.get()
    if snooped:
        return _role_by_client_id.get(snooped, "llm-client")

    # Fallback for code paths where the middleware didn't run (tests,
    # the inmemory BlenderMCPOAuthProvider where there's no upstream and
    # the access token's client_id IS the downstream DCR id).
    try:
        token = get_access_token()
    except Exception:
        return "llm-client"
    if token is None:
        return "llm-client"
    client_id = getattr(token, "client_id", None)
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
