"""MCP-spec OAuth 2.1 provider backed by the existing USERS dict.

Subclasses FastMCP's ``InMemoryOAuthProvider`` (a complete MCP-spec OAuth
implementation with Dynamic Client Registration, PKCE, code+token rotation)
and adds:

1. **User authentication via the USERS dict** — ``authenticate_user`` calls
   the same ``_verify_password`` the addon's ``/auth/login`` path uses, so
   both flows share auth logic.

2. **User identity propagation** — `mcp.server.auth.provider.AccessToken`
   only carries ``client_id`` and ``scopes``, not user identity. We
   maintain a parallel ``_token_to_user`` mapping so the JWT-middleware
   replacement can resolve the ``current_user_id`` ContextVar from a
   bearer token alone (which is what ``bus_tools`` expects).

3. **Issue-tokens-for-user** — a high-level helper used by ``/auth/login``
   to mint tokens directly without going through the full OAuth dance. The
   addon doesn't speak OAuth (it speaks password→token), so this gives us
   one token-issuing surface that both paths feed into.

Phase G1: provider subclass only. No FastMCP wiring yet — that's G2.
"""

from __future__ import annotations

import secrets
import time
from typing import Any

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    RefreshToken,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from fastmcp.server.auth.providers.in_memory import (
    DEFAULT_ACCESS_TOKEN_EXPIRY_SECONDS,
    DEFAULT_REFRESH_TOKEN_EXPIRY_SECONDS,
    InMemoryOAuthProvider,
)


class BlenderMCPOAuthProvider(InMemoryOAuthProvider):
    """OAuth 2.1 provider that authenticates against the BlenderMCP USERS dict.

    Inherits the full MCP-spec OAuth implementation (codes, tokens, PKCE,
    refresh rotation, revocation) from ``InMemoryOAuthProvider`` and adds
    user-identity tracking + a direct token-issuance path for the addon's
    ``/auth/login`` flow.
    """

    def __init__(
        self,
        users: dict[str, dict[str, Any]],
        verify_password: Any,  # callable[[str, str], bool] — passed in to avoid import cycle
        **kwargs: Any,
    ) -> None:
        """Construct the provider.

        Args:
            users: The USERS dict from oauth_server.py
                (``{username: {user_id, username, password_hash, roles, scopes}}``)
            verify_password: ``_verify_password(plain, hashed) -> bool``,
                passed by reference to avoid a circular import.
            **kwargs: Forwarded to ``InMemoryOAuthProvider`` (base_url,
                client_registration_options, required_scopes, etc.)
        """
        super().__init__(**kwargs)
        self._users = users
        self._verify_password = verify_password
        # token_string -> user_id. Populated whenever we mint an access or
        # refresh token. Cleaned up by _revoke_internal via our override.
        self._token_to_user: dict[str, str] = {}
        # authorization_code -> user_id. Carries identity from the consent
        # screen through code-exchange into the issued access token.
        self._auth_code_to_user: dict[str, str] = {}

    # ---- User auth (used by consent/login form + /auth/login) ----

    def authenticate_user(self, username: str, password: str) -> str | None:
        """Verify credentials. Returns user_id on success, None on failure.

        Same auth logic as the existing addon flow — no second password
        hash to rotate, no divergent timing characteristics. Constant-time
        dummy-hash for unknown users (delegated to the verify_password
        callable, which already does this).
        """
        user = self._users.get(username)
        if not user:
            # Constant-time path: still verify against a dummy hash
            self._verify_password(password, "$2b$12$" + "x" * 53)
            return None
        if not self._verify_password(password, user["password_hash"]):
            return None
        return user["user_id"]

    # ---- Issue tokens directly (used by /auth/login for the addon) ----

    def issue_tokens_for_user(
        self,
        user_id: str,
        scopes: list[str] | None = None,
        client_id: str = "blendermcp-addon-direct",
    ) -> OAuthToken:
        """Mint an access+refresh token pair for ``user_id`` without going
        through the full OAuth dance.

        Used by ``/auth/login`` so the addon's password→token flow produces
        tokens this provider can verify. The synthetic ``client_id`` marks
        these as direct-issued (not from a registered DCR client) so they
        can be distinguished in logs/auditing if needed.

        Args:
            user_id: The authenticated user's identifier (from
                ``authenticate_user``).
            scopes: Token scopes. Defaults to the user's scopes from USERS,
                or ``["mcp.read", "mcp.write"]`` if not configured.
            client_id: Synthetic client_id for direct-issued tokens.
                Defaults to ``"blendermcp-addon-direct"``.

        Returns:
            ``OAuthToken`` with access_token, refresh_token, expires_in,
            token_type, and scope. Caller can serialize the relevant
            fields back to the addon as a JSON response.
        """
        if scopes is None:
            user = next(
                (u for u in self._users.values() if u["user_id"] == user_id), None
            )
            scopes = (user or {}).get("scopes") or ["mcp.read", "mcp.write"]

        access_token_value = f"bmcp_at_{secrets.token_hex(32)}"
        refresh_token_value = f"bmcp_rt_{secrets.token_hex(32)}"

        access_expires_at = int(time.time() + DEFAULT_ACCESS_TOKEN_EXPIRY_SECONDS)
        refresh_expires_at = None
        if DEFAULT_REFRESH_TOKEN_EXPIRY_SECONDS is not None:
            refresh_expires_at = int(
                time.time() + DEFAULT_REFRESH_TOKEN_EXPIRY_SECONDS
            )

        self.access_tokens[access_token_value] = AccessToken(
            token=access_token_value,
            client_id=client_id,
            scopes=scopes,
            expires_at=access_expires_at,
        )
        self.refresh_tokens[refresh_token_value] = RefreshToken(
            token=refresh_token_value,
            client_id=client_id,
            scopes=scopes,
            expires_at=refresh_expires_at,
        )
        self._access_to_refresh_map[access_token_value] = refresh_token_value
        self._refresh_to_access_map[refresh_token_value] = access_token_value
        # Crucial: tag BOTH tokens with the user so verify_token + refresh
        # both resolve to the right user.
        self._token_to_user[access_token_value] = user_id
        self._token_to_user[refresh_token_value] = user_id

        return OAuthToken(
            access_token=access_token_value,
            token_type="Bearer",
            expires_in=DEFAULT_ACCESS_TOKEN_EXPIRY_SECONDS,
            refresh_token=refresh_token_value,
            scope=" ".join(scopes),
        )

    # ---- User-identity lookup (used by middleware replacement) ----

    def get_user_for_token(self, token: str) -> str | None:
        """Resolve a bearer token to its authorized user_id.

        Returns None if the token is unknown, expired, or wasn't tagged
        with a user (e.g. tokens minted by the bare ``InMemoryOAuthProvider``
        codepath before our overrides took effect — shouldn't happen in
        practice but defended).
        """
        # Verify the token is still valid first (handles expiry/revocation)
        access = self.access_tokens.get(token)
        if access is None or (
            access.expires_at is not None and access.expires_at < time.time()
        ):
            return None
        return self._token_to_user.get(token)

    # ---- Overrides: tag tokens with user_id during OAuth flow ----

    async def authorize(
        self,
        client: OAuthClientInformationFull,
        params: Any,
        user_id: str | None = None,
    ) -> str:
        """Override base ``authorize`` to associate the auth code with
        an authenticated user_id.

        The base impl creates an AuthorizationCode and returns the redirect
        URI. We do the same, but additionally record which user_id the
        code maps to, so subsequent ``exchange_authorization_code`` can
        propagate that into the access token's user mapping.

        ``user_id`` is passed by our /authorize HTTP handler after it
        verifies the consent-form login.
        """
        redirect_uri = await super().authorize(client, params)
        if user_id is not None:
            # Pull the auth code out of the redirect URI we got back
            # (format: ``...?code=<value>&state=<state>``). We could also
            # snapshot ``self.auth_codes`` to find the new one, but
            # parsing the redirect is unambiguous and matches the base
            # impl's return contract.
            from urllib.parse import parse_qs, urlparse

            code = parse_qs(urlparse(redirect_uri).query).get("code", [None])[0]
            if code:
                self._auth_code_to_user[code] = user_id
        return redirect_uri

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: AuthorizationCode,
    ) -> OAuthToken:
        """Override to propagate user_id from auth-code mapping into the
        access+refresh token mappings."""
        user_id = self._auth_code_to_user.pop(authorization_code.code, None)
        token = await super().exchange_authorization_code(client, authorization_code)
        if user_id is not None:
            self._token_to_user[token.access_token] = user_id
            if token.refresh_token:
                self._token_to_user[token.refresh_token] = user_id
        return token

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        """Override to carry user_id forward when rotating refresh tokens."""
        # Pull the user_id off the OLD refresh token BEFORE the base impl
        # revokes it via _revoke_internal.
        user_id = self._token_to_user.get(refresh_token.token)
        token = await super().exchange_refresh_token(client, refresh_token, scopes)
        if user_id is not None:
            self._token_to_user[token.access_token] = user_id
            if token.refresh_token:
                self._token_to_user[token.refresh_token] = user_id
        return token

    def _revoke_internal(
        self,
        access_token_str: str | None = None,
        refresh_token_str: str | None = None,
    ) -> None:
        """Override to clean up _token_to_user mapping when tokens get revoked
        (either by explicit revocation, rotation, or expiry sweep)."""
        # Capture associations BEFORE base impl mutates them
        also_drop: list[str] = []
        if access_token_str:
            also_drop.append(access_token_str)
            paired = self._access_to_refresh_map.get(access_token_str)
            if paired:
                also_drop.append(paired)
        if refresh_token_str:
            also_drop.append(refresh_token_str)
            paired = self._refresh_to_access_map.get(refresh_token_str)
            if paired:
                also_drop.append(paired)

        super()._revoke_internal(
            access_token_str=access_token_str,
            refresh_token_str=refresh_token_str,
        )

        for tok in also_drop:
            self._token_to_user.pop(tok, None)
