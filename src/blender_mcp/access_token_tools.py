"""MCP tools for minting and managing personal access tokens.

Tokens let CI jobs and scripts call the server with a static
``Authorization: Bearer bmcp_...`` header instead of the OAuth browser
flow. Minting requires an OAuth-authenticated session: a caller that is
itself using a personal access token is refused, so a leaked token can't
be used to mint replacements. Each user sees and revokes only their own.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from fastmcp import Context
from fastmcp.contrib.mcp_mixin import MCPMixin, mcp_tool

from . import access_tokens as pat
from .bus_tools import _resolve_user_id
from .storage import get_session

logger = logging.getLogger(__name__)


def _refuse_pat() -> Optional[str]:
    if pat.caller_is_pat():
        return json.dumps({
            "status": "error",
            "error": "oauth_required",
            "message": "Token management needs an OAuth sign-in; a personal access "
                       "token can't create, list or revoke tokens.",
        })
    return None


class BlenderAccessTokenComponent(MCPMixin):
    """create / list / revoke personal access tokens for the calling user."""

    @mcp_tool()
    async def create_access_token(
        self,
        name: str,
        expires_days: int = pat.DEFAULT_EXPIRES_DAYS,
        ctx: Context = None,
    ) -> str:
        """Create a personal access token for CI or scripted MCP clients.

        The token is returned ONCE in ``token``; store it in a secret
        (e.g. the BLENDER_MCP_TOKEN environment variable) and send it as
        ``Authorization: Bearer <token>``. It acts as your LLM client on
        your buses. ``expires_days`` is 1-366 (default 90). Requires an
        OAuth sign-in; can't be called with a personal access token.
        """
        if (refusal := _refuse_pat()) is not None:
            return refusal
        user_sub = _resolve_user_id(ctx)
        if not user_sub:
            return json.dumps({"status": "error", "error": "unauthenticated"})
        if not name or not name.strip():
            return json.dumps({"status": "error", "error": "name_required"})
        if not (1 <= int(expires_days) <= pat.MAX_EXPIRES_DAYS):
            return json.dumps({
                "status": "error",
                "error": "bad_expiry",
                "message": f"expires_days must be between 1 and {pat.MAX_EXPIRES_DAYS}",
            })
        async with get_session() as s:
            token, row = await pat.create_token(s, user_sub, name, int(expires_days))
        logger.info("PAT created: %s (%s) for %s", row.token_prefix, row.name, user_sub[:12])
        return json.dumps({
            "status": "ok",
            "token": token,
            "note": "Shown once. Store it now; only its prefix is kept for display.",
            **pat.to_dict(row),
        })

    @mcp_tool()
    async def list_access_tokens(self, ctx: Context = None) -> str:
        """List your personal access tokens (prefix, name, status, dates; never the secret)."""
        if (refusal := _refuse_pat()) is not None:
            return refusal
        user_sub = _resolve_user_id(ctx)
        if not user_sub:
            return json.dumps({"status": "error", "error": "unauthenticated"})
        async with get_session() as s:
            rows = await pat.list_tokens(s, user_sub)
        return json.dumps({"status": "ok", "tokens": [pat.to_dict(r) for r in rows]})

    @mcp_tool()
    async def revoke_access_token(self, token_id_or_prefix: str, ctx: Context = None) -> str:
        """Revoke one of your personal access tokens by id or prefix (e.g. "bmcp_Ab3xYz9Q").

        Takes effect within about 30 seconds.
        """
        if (refusal := _refuse_pat()) is not None:
            return refusal
        user_sub = _resolve_user_id(ctx)
        if not user_sub:
            return json.dumps({"status": "error", "error": "unauthenticated"})
        async with get_session() as s:
            row = await pat.revoke_token(s, token_id_or_prefix, user_sub)
        if row is None:
            return json.dumps({
                "status": "error",
                "error": "not_found",
                "message": "No single token of yours matches that id or prefix.",
            })
        return json.dumps({"status": "ok", **pat.to_dict(row)})
