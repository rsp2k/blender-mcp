"""OAuth login + token storage helpers.

Phase 7 introduces a real /auth/login call so users no longer have to
paste raw JWTs into the panel. Phase 8 will move token storage from
Scene properties to AddonPreferences (Scene props leak secrets into
every .blend file).

Public API:

    from addon.auth import login, refresh_token, LoginError

    payload = login("http://localhost:8000/mcp", "alice", "hunter2")
    # payload: {access_token, refresh_token, token_type, expires_in, user}
"""

from __future__ import annotations

from .login import LoginError, login, logout, refresh_token
from .oauth_pkce import (
    OAuthError,
    oauth_login,
    refresh_oauth_token,
    revoke_oauth_token,
)

__all__ = [
    # Legacy password-based flow (kept for AUTH_BACKEND=inmemory dev servers)
    "login",
    "logout",
    "refresh_token",
    "LoginError",
    # MCP-spec OAuth PKCE flow (production path, against AUTH_BACKEND=authentik)
    "oauth_login",
    "refresh_oauth_token",
    "revoke_oauth_token",
    "OAuthError",
]
