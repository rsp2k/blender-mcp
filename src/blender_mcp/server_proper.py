"""FastMCP server bootstrap.

Two builders, one per transport — because the bus tools rely on a per-request
`ContextVar` that the FastAPI middleware sets from a JWT. Over stdio there's
no way to propagate that identity, so registering the bus tools there just
gives every caller `unauthenticated` and pollutes tool discovery.

- `build_stdio_mcp()` → diagnostics + install only (open access)
- `build_http_mcp()`  → diagnostics + bus tools (the bus is OAuth-gated)

The HTTP build's auth backend is selected by the ``AUTH_BACKEND`` env var:

- ``AUTH_BACKEND=authentik`` (default): OIDC proxy to Authentik. Requires
  ``AUTHENTIK_CONFIG_URL``, ``AUTHENTIK_CLIENT_ID``, ``AUTHENTIK_CLIENT_SECRET``,
  ``PUBLIC_BASE_URL``. Production path.
- ``AUTH_BACKEND=inmemory``: ``BlenderMCPOAuthProvider`` against a local
  USERS dict. Local-dev fallback for working on the server when Authentik
  isn't reachable. Requires ``ADMIN_PASSWORD`` like the pre-Phase-G setup.

`oauth_server` imports the module-level `mcp` (the HTTP build); the stdio
entry point in `main()` constructs the stdio build on demand.
"""

import logging
import os

from fastmcp import FastMCP
from fastmcp.server.auth.auth import AuthProvider

from .bus_tools import BlenderBusComponent
from .diagnostics_component import BlenderDiagnosticsComponent
from .dispatch_component import BlenderDispatchComponent
from .prompts_component import BlenderPromptsComponent

logger = logging.getLogger(__name__)


def _build_oauth_storage():
    """Build a Postgres-backed AsyncKeyValue store for OIDCProxy state.

    Replaces FastMCP's default encrypted FileTreeStore so that all of
    OIDCProxy's internal state — client registrations, transactions,
    auth codes, JTI→upstream mappings, refresh tokens, upstream tokens
    — survives server restarts. Before this, every redeploy invalidated
    every issued JWT (JTI mapping wipe), forcing every user to re-Login.

    Returns None if DATABASE_URL is unset — fall back to FastMCP's default
    file store, which is fine for stdio / local-dev / inmemory backends.

    Lazy setup: the store creates its kv_store table on first read/write
    via the BaseStore._setup hook, so we can construct it here without an
    event loop. Auto-create coexists with our Alembic-managed schema by
    living in a different table name; Alembic should be configured to
    ignore kv_store via include_object in env.py.
    """
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        logger.info("Auth storage: no DATABASE_URL — falling back to encrypted file store")
        return None

    # PostgreSQLStore uses asyncpg directly; strip any sqlalchemy driver
    # suffix that bus_repo's engine config might have added.
    pg_url = database_url.replace("postgresql+asyncpg://", "postgresql://")
    pg_url = pg_url.replace("postgresql+psycopg://", "postgresql://")

    from key_value.aio.stores.postgresql import PostgreSQLStore

    logger.info("Auth storage: PostgreSQLStore (kv_store table)")
    return PostgreSQLStore(url=pg_url, table_name="oauth_kv_store", auto_create=True)


def _build_auth_provider() -> AuthProvider | None:
    """Pick + construct the auth provider based on AUTH_BACKEND.

    Returns None if auth is intentionally disabled (e.g. for stdio).
    """
    backend = os.getenv("AUTH_BACKEND", "authentik").lower()

    if backend == "authentik":
        from fastmcp.server.auth.oidc_proxy import OIDCProxy

        class _OIDCProxyWithUserClaims(OIDCProxy):
            """OIDCProxy that embeds OIDC id_token claims in the FastMCP JWT.

            The MCP SDK's OAuthToken response model has no id_token field, so
            the upstream Authentik id_token gets stripped before reaching
            downstream clients (verified by reading mcp/shared/auth.py:6).
            Without an id_token, the addon can't display the user's name in
            its sidebar — it sees only an opaque access_token.

            FastMCP's _extract_upstream_claims hook is the documented escape:
            return a dict; FastMCP embeds it under `upstream_claims` in the
            FastMCP-issued JWT payload. The addon decodes its own JWT (no
            signature verification needed — same trust chain as the id_token
            decode) and reads upstream_claims to populate the sidebar.

            Decode is signature-unverified intentionally: the id_token came
            from inside ``_handle_idp_callback`` where FastMCP already
            validated it against upstream JWKS; we're only extracting display
            fields, not granting any privilege based on them.
            """

            async def _extract_upstream_claims(self, idp_tokens):
                id_token = idp_tokens.get("id_token")
                if not id_token:
                    return None
                try:
                    import base64
                    import json as _json
                    parts = id_token.split(".")
                    if len(parts) != 3:
                        return None
                    payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
                    payload = _json.loads(base64.urlsafe_b64decode(payload_b64))
                    # Pick the standard OIDC identity claims; drop bulky
                    # internals (aud, iss, exp, etc.) to keep the FastMCP
                    # JWT slim.
                    return {
                        k: payload[k]
                        for k in ("sub", "preferred_username", "email", "name", "given_name", "family_name")
                        if k in payload
                    }
                except Exception as e:
                    logger.warning("id_token decode for upstream_claims failed: %s", e)
                    return None

        config_url = os.environ["AUTHENTIK_CONFIG_URL"]
        client_id = os.environ["AUTHENTIK_CLIENT_ID"]
        client_secret = os.environ["AUTHENTIK_CLIENT_SECRET"]
        # ``base_url`` is the public URL where OAuth endpoints live.
        # MCP is mounted at root (the mcp.* hostname carries the semantic —
        # no need for a redundant /mcp path prefix), so OAuth endpoints land
        # at /register, /authorize, /token, /auth/callback directly.
        base_url = os.environ["PUBLIC_BASE_URL"].rstrip("/")

        logger.info("Auth: OIDCProxy → Authentik (%s)", config_url)
        provider = _OIDCProxyWithUserClaims(
            config_url=config_url,
            client_id=client_id,
            client_secret=client_secret,
            base_url=base_url,
            # Authentik already has its own consent screen — skip FastMCP's
            # second consent UI to avoid double-prompting users.
            require_authorization_consent="external",
            # Per-app issuer in Authentik
            audience=client_id,
            # Persist OIDCProxy state (JTI mapping, refresh tokens, DCR
            # client registrations, etc.) to Postgres so server restarts
            # don't invalidate every issued JWT. Falls back to the default
            # encrypted file store if DATABASE_URL isn't set (stdio/local).
            client_storage=_build_oauth_storage(),
            # IMPORTANT: do NOT pass required_scopes. Authentik's access tokens
            # don't carry a `scope` or `scp` claim (those live on the ID token
            # per OIDC spec, not on the OAuth2 access token). If we set
            # required_scopes=["openid"], JWTVerifier extracts scopes=[] from
            # the access JWT and 401s every request. We're single-tenant +
            # single-app — scope-gated authorization isn't load-bearing.
        )
        # Post-init scope allowlist for DCR. Without this, OAuthProxy's
        # constructor derives valid_scopes from
        # token_verifier.required_scopes which defaults to [], and the
        # MCP SDK's register handler then rejects ANY non-empty `scope`
        # in the DCR body as "not in the valid list" — even standard
        # OIDC scopes Authentik supports natively. Setting via the
        # public update_default_scopes() API (OIDCProxy.__init__
        # doesn't accept valid_scopes directly; only required_scopes,
        # which would re-enable the JWT-scope-401 issue above).
        # Verified via Authentik's discovery doc: scopes_supported is
        # ['openid', 'email', 'profile']. Required by addon 1.5.12+ so
        # the id_token comes back with user_display_name/email claims.
        provider.update_default_scopes(["openid", "email", "profile"])
        return provider

    if backend == "inmemory":
        # Build USERS dict locally to avoid circular import with oauth_server
        # (oauth_server imports `mcp` from this module, so we can't import
        # USERS from oauth_server during this module's load).
        import bcrypt

        def _hash(password: str) -> str:
            return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

        def _verify(password: str, hashed: str) -> bool:
            try:
                return bcrypt.checkpw(password.encode(), hashed.encode())
            except Exception:
                return False

        admin_pw = os.environ.get("ADMIN_PASSWORD")
        if not admin_pw:
            raise RuntimeError(
                "AUTH_BACKEND=inmemory requires ADMIN_PASSWORD env var"
            )

        users = {
            "admin": {
                "user_id": "admin",
                "username": "admin",
                "password_hash": _hash(admin_pw),
                "roles": ["admin", "user"],
                "scopes": ["*"],
            },
        }
        demo_pw = os.environ.get("DEMO_PASSWORD")
        if demo_pw:
            users["demo"] = {
                "user_id": "demo",
                "username": "demo",
                "password_hash": _hash(demo_pw),
                "roles": ["user"],
                "scopes": ["read", "write"],
            }

        from .mcp_oauth_provider import BlenderMCPOAuthProvider

        logger.warning(
            "Auth: BlenderMCPOAuthProvider (in-memory, local-dev only). "
            "Set AUTH_BACKEND=authentik for production."
        )
        return BlenderMCPOAuthProvider(
            users=users,
            verify_password=_verify,
            # MCP mounted at root — see the authentik branch above for rationale.
            base_url=os.getenv("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/"),
        )

    raise ValueError(
        f"Unknown AUTH_BACKEND={backend!r}. Use 'authentik' or 'inmemory'."
    )


def build_stdio_mcp() -> FastMCP:
    """Build the stdio MCP server.

    Diagnostics + install only. Bus tools are omitted because the stdio
    transport can't carry user identity — registering them would mean every
    call returns `unauthenticated`, which is worse than not seeing the tool
    at all (it pollutes tool listings and misleads clients).
    """
    server = FastMCP("BlenderMCP")
    BlenderDiagnosticsComponent().register_all(mcp_server=server, prefix="blender")
    # Prompts are pure templates — no bus, no user_id, no Blender peer needed.
    # Safe + useful in stdio (prototyping scripts before connecting Blender).
    BlenderPromptsComponent().register_prompts(mcp_server=server, prefix="blender")
    logger.info("FastMCP server built (stdio): diagnostics + prompts")
    return server


def build_http_mcp() -> FastMCP:
    """Build the HTTP MCP server.

    Diagnostics + bus tools + dispatch tools. The bus tools and dispatch
    tools both resolve user identity from a ContextVar set by the FastAPI
    middleware in `oauth_server.py` after JWT verification.

    - Diagnostics: open-access helpers (Blender install probes)
    - Bus tools: low-level register/send/list (used by addon + advanced clients)
    - Dispatch tools: flat round-trip tools for the 24 addon commands;
      shield MCP clients from job_id correlation and notification-listening
    """
    auth_provider = _build_auth_provider()
    server = FastMCP("BlenderMCP", auth=auth_provider)
    BlenderDiagnosticsComponent().register_all(mcp_server=server, prefix="blender")
    # Bus + dispatch: tools and prompts get the ``blender_`` prefix so they
    # don't collide with anything else in tool listings, but resources are
    # registered WITHOUT a prefix — otherwise the MCPMixin double-prefixes the
    # URI to ``blender+blender://...`` which (a) reads awkwardly to MCP clients
    # and (b) breaks URI-template detection: pydantic's AnyUrl validation
    # percent-encodes the ``{...}`` placeholders in the mangled URI, so
    # ``FunctionResource.from_function`` no longer recognizes it as a template
    # and registers it as a static URI literally named ``…/%7Blevel%7D``.
    # Registering resources without prefix keeps URIs as the natural
    # ``blender://...`` form and preserves template handling.
    bus = BlenderBusComponent()
    bus.register_tools(mcp_server=server, prefix="blender")
    bus.register_resources(mcp_server=server)
    bus.register_prompts(mcp_server=server, prefix="blender")

    dispatch = BlenderDispatchComponent()
    dispatch.register_tools(mcp_server=server, prefix="blender")
    dispatch.register_resources(mcp_server=server)
    dispatch.register_prompts(mcp_server=server, prefix="blender")
    # Templated URI resources (e.g. blender://console/{level}) go through a
    # different code path that supports template detection — see the block
    # comment in dispatch_component.py above ``console_resource``.
    dispatch.register_templated_resources(mcp_server=server)

    # Skeletal prompts — same registration as stdio (templates only,
    # no per-request state).
    BlenderPromptsComponent().register_prompts(mcp_server=server, prefix="blender")

    logger.info("FastMCP server built (HTTP): diagnostics + bus + dispatch + prompts")
    return server


# Module-level instance for `from .server_proper import mcp` (oauth_server).
mcp = build_http_mcp()


def main():
    """Standalone stdio entrypoint (for non-HTTP usage).

    Builds its own stdio-flavored server rather than reusing the module-level
    HTTP `mcp`, so stdio callers don't see bus tools they can never use.
    """
    try:
        from importlib.metadata import version
        v = version("blender-mcp")
    except Exception:
        v = "0.0.0"
    print(f"BlenderMCP v{v} — stdio mode (diagnostics only)")
    build_stdio_mcp().run()


if __name__ == "__main__":
    main()
