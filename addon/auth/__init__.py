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

from .login import LoginError, login, refresh_token

__all__ = ["login", "refresh_token", "LoginError"]
