"""OAuth2 + FastMCP combined server.

FastAPI handles /auth/* endpoints (login/refresh/logout/profile).
FastMCP is mounted at /mcp via its streamable-HTTP ASGI app.
A JWT middleware decodes the Bearer token before /mcp requests and
populates the bus_tools.current_user_id ContextVar so tools can
identify the caller's user bus.

A logging Handler on `_message_bus` forwards each bus record as an MCP
log notification (notifications/message) addressed to the target client's
MCP session — that's the wire transport for the bus.
"""

import asyncio
import contextlib
import json
import logging
import os
import secrets
from datetime import datetime, timedelta
from typing import Any, Optional

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt

from .bus_tools import current_user_id
from .client_role import record_client_role
from .message_bus import bus_manager
from .message_router import _message_bus, PRIORITY_TO_MCP_LEVEL, Priority
from .server_proper import mcp


# ---- config ----

logger = logging.getLogger("blender_mcp.oauth_server")

SECRET_KEY = os.getenv("OAUTH_SECRET_KEY")
if not SECRET_KEY:
    SECRET_KEY = secrets.token_urlsafe(32)
    logger.warning("OAUTH_SECRET_KEY not set; generated ephemeral key (won't survive restart)")

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "30"))
REFRESH_TOKEN_EXPIRE_DAYS = int(os.getenv("REFRESH_TOKEN_EXPIRE_DAYS", "7"))

HOST = os.getenv("BLENDER_MCP_HOST", "0.0.0.0")
PORT = int(os.getenv("BLENDER_MCP_PORT", "8000"))

security = HTTPBearer(auto_error=False)


# ---- bus -> MCP log forwarding handler ----

class BusForwardingHandler(logging.Handler):
    """Turn each _message_bus log record into an MCP log notification
    addressed to the target client's MCP session."""

    def emit(self, record: logging.LogRecord) -> None:
        bus_data = getattr(record, "bus", None)
        if not bus_data:
            return
        session = bus_data.get("target_session")
        if session is None:
            return

        priority = bus_data.get("priority", int(Priority.INFO))
        try:
            mcp_level = PRIORITY_TO_MCP_LEVEL[Priority(priority)]
        except (ValueError, KeyError):
            mcp_level = "info"

        # Strip session before sending (not JSON-serializable).
        wire = {k: v for k, v in bus_data.items() if k != "target_session"}

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # No loop — drop the record; handler is no-op in sync context.

        loop.create_task(self._send(session, mcp_level, wire))

    @staticmethod
    async def _send(session: Any, level: str, data: dict) -> None:
        try:
            await session.send_log_message(level=level, data=data, logger="_message_bus")
        except Exception as e:
            logger.warning("Bus forward failed: %s", e)


def _install_bus_forwarder() -> None:
    if not any(isinstance(h, BusForwardingHandler) for h in _message_bus.handlers):
        _message_bus.addHandler(BusForwardingHandler())


# ---- JWT helpers ----

def _create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    payload = data.copy()
    payload["exp"] = datetime.utcnow() + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def _create_refresh_token(data: dict) -> str:
    payload = data.copy()
    payload["token_type"] = "refresh"
    payload["exp"] = datetime.utcnow() + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def _decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"Invalid token: {e}",
                            headers={"WWW-Authenticate": "Bearer"})


# ---- user store (demo; replace with real backend) ----

def _hash_password(password: str) -> str:
    import bcrypt
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def _verify_password(password: str, hashed: str) -> bool:
    import bcrypt
    try:
        return bcrypt.checkpw(password.encode(), hashed.encode())
    except Exception:
        return False


def _build_users() -> dict[str, dict[str, Any]]:
    """Construct the in-memory user store from environment variables.

    Hard-fail at module-load time if ``ADMIN_PASSWORD`` isn't set — shipping
    a weak default ("SecureAdmin123!") that the world has seen in this repo
    is a credential leak waiting to happen. The compose layer ALSO enforces
    this via ``${ADMIN_PASSWORD:?}``; this is the defense-in-depth for
    direct ``uvicorn`` / ``blender-mcp`` invocations that bypass compose.

    Demo user is opt-in: only created when ``DEMO_PASSWORD`` is explicitly
    set. Production deployments shouldn't have a demo account by default.
    """
    admin_password = os.getenv("ADMIN_PASSWORD")
    if not admin_password:
        raise RuntimeError(
            "ADMIN_PASSWORD environment variable is required (no default is "
            "provided — set it in .env, your shell, or compose). The previous "
            "default 'SecureAdmin123!' was removed for security."
        )

    users: dict[str, dict[str, Any]] = {
        "admin": {
            "user_id": "admin",
            "username": "admin",
            "password_hash": _hash_password(admin_password),
            "roles": ["admin", "user"],
            "scopes": ["*"],
        },
    }

    demo_password = os.getenv("DEMO_PASSWORD")
    if demo_password:
        users["demo"] = {
            "user_id": "demo",
            "username": "demo",
            "password_hash": _hash_password(demo_password),
            "roles": ["user"],
            "scopes": ["read", "write"],
        }

    return users


USERS: dict[str, dict[str, Any]] = _build_users()

REFRESH_TOKENS: dict[str, dict[str, Any]] = {}


# ---- app factory ----

def build_app() -> FastAPI:
    _install_bus_forwarder()

    # path="/" → FastMCP serves at the ASGI root; FastAPI mount at "/mcp" makes
    # the effective endpoint /mcp/. Using path="/mcp" here doubles the prefix.
    mcp_asgi = mcp.http_app(path="/", transport="http")

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        # Chain to FastMCP's own lifespan (sets up its session manager).
        async with mcp_asgi.lifespan(app):
            yield

    app = FastAPI(
        title="BlenderMCP OAuth + Bus",
        version="1.2",
        lifespan=lifespan,
    )

    # ---- JWT middleware (LEGACY) ----
    # Phase G3 removed the JWT validation logic; FastMCP's auth pipeline now
    # handles token verification for /mcp/* via the configured provider
    # (OIDCProxy or BlenderMCPOAuthProvider). The middleware function stays
    # as a no-op pass-through for now in case future debugging needs an
    # interception point; can be deleted entirely once the cutover is
    # proven stable in production.
    @app.middleware("http")
    async def jwt_middleware(request: Request, call_next):
        return await call_next(request)

    # ---- DCR-capture middleware (phase H — role attribution) ----
    # Intercept POST /register to record (client_id → role) from the
    # client's declared ``software_id``. We DON'T modify the request or the
    # response — just snoop the body in flight.
    #
    # Pattern: read+restore the request body BEFORE passing through (so
    # OAuthProxy sees the original), then buffer the response body to
    # extract the issued client_id (and rebuild a fresh Response so the
    # client still gets the JSON it expects).
    @app.middleware("http")
    async def dcr_role_middleware(request: Request, call_next):
        if not (request.url.path == "/register" and request.method == "POST"):
            return await call_next(request)

        # 1. Snoop the request body, then re-attach for downstream consumers.
        body_bytes = await request.body()
        try:
            req_data = json.loads(body_bytes) if body_bytes else {}
            software_id = req_data.get("software_id")
        except json.JSONDecodeError:
            software_id = None

        async def _replay_receive():
            return {"type": "http.request", "body": body_bytes, "more_body": False}
        request._receive = _replay_receive  # type: ignore[attr-defined]

        # 2. Pass through; buffer the (streaming) response so we can read it.
        response = await call_next(request)
        if not (200 <= response.status_code < 300):
            return response

        buffered = b""
        async for chunk in response.body_iterator:  # type: ignore[attr-defined]
            buffered += chunk

        # 3. Extract client_id from the response body. RFC 7591 guarantees
        # `client_id` in a successful response. If we can't parse it, log
        # and pass the response through unmodified — gating just won't apply
        # to this client (defaults to llm-client).
        try:
            resp_data = json.loads(buffered)
            client_id = resp_data.get("client_id")
        except json.JSONDecodeError:
            client_id = None

        if client_id:
            record_client_role(client_id, software_id)
        else:
            logger.warning(
                "DCR response missing client_id; can't attribute role "
                "(software_id=%r). Body preview: %r",
                software_id, buffered[:200],
            )

        return Response(
            content=buffered,
            status_code=response.status_code,
            headers=dict(response.headers),
            media_type=response.media_type,
        )

    # ---- auth endpoints ----

    @app.post("/auth/login")
    async def login(credentials: dict):
        """Password-based token issuance for the addon flow.

        Phase G: when AUTH_BACKEND=inmemory (Gate H + local dev), this mints
        tokens via the BlenderMCPOAuthProvider's issue_tokens_for_user so the
        resulting tokens are valid against FastMCP's auth pipeline.

        When AUTH_BACKEND=authentik, the addon should use the OAuth flow
        instead (G4); this endpoint returns 410 with a hint.
        """
        from .mcp_oauth_provider import BlenderMCPOAuthProvider

        username = credentials.get("username")
        password = credentials.get("password")
        if not username or not password:
            raise HTTPException(400, "username and password required")

        provider = mcp.auth
        if not isinstance(provider, BlenderMCPOAuthProvider):
            raise HTTPException(
                410,
                "Password login disabled. Use the OAuth flow at /mcp/authorize "
                "(or set AUTH_BACKEND=inmemory for local dev).",
            )

        user_id = provider.authenticate_user(username, password)
        if not user_id:
            raise HTTPException(
                401, "Invalid credentials",
                headers={"WWW-Authenticate": "Bearer"},
            )

        # Find user record for the response (roles/scopes/username).
        user = next(
            (u for u in USERS.values() if u["user_id"] == user_id), None
        )
        oauth_tok = provider.issue_tokens_for_user(user_id)

        return {
            "access_token": oauth_tok.access_token,
            "refresh_token": oauth_tok.refresh_token,
            "token_type": "bearer",
            "expires_in": oauth_tok.expires_in,
            "user": {
                "user_id": user_id,
                "username": (user or {}).get("username", user_id),
                "roles": (user or {}).get("roles", []),
                "scopes": (user or {}).get("scopes", []),
            },
        }

    @app.post("/auth/refresh")
    async def refresh_endpoint(body: dict):
        refresh = body.get("refresh_token")
        if not refresh or refresh not in REFRESH_TOKENS:
            raise HTTPException(401, "Invalid refresh token")
        payload = _decode_token(refresh)
        if payload.get("token_type") != "refresh":
            raise HTTPException(401, "Not a refresh token")
        user_id = payload.get("sub")
        user = next((u for u in USERS.values() if u["user_id"] == user_id), None)
        if not user:
            raise HTTPException(401, "User not found")
        access = _create_access_token({
            "sub": user["user_id"],
            "username": user["username"],
            "roles": user["roles"],
            "scopes": user["scopes"],
        })
        return {
            "access_token": access,
            "token_type": "bearer",
            "expires_in": ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        }

    @app.post("/auth/logout")
    async def logout(credentials: HTTPAuthorizationCredentials = Depends(security)):
        if credentials is None:
            raise HTTPException(401, "Bearer token required")
        try:
            payload = _decode_token(credentials.credentials)
        except HTTPException:
            return {"message": "Logged out"}
        user_id = payload.get("sub")
        # Drop any refresh tokens for this user.
        for tok, meta in list(REFRESH_TOKENS.items()):
            if meta.get("user_id") == user_id:
                REFRESH_TOKENS.pop(tok, None)
        return {"message": "Logged out"}

    @app.get("/auth/profile")
    async def profile(credentials: HTTPAuthorizationCredentials = Depends(security)):
        if credentials is None:
            raise HTTPException(401, "Bearer token required")
        payload = _decode_token(credentials.credentials)
        return {
            "user_id": payload.get("sub"),
            "username": payload.get("username"),
            "roles": payload.get("roles", []),
            "scopes": payload.get("scopes", []),
        }

    @app.get("/health")
    async def health():
        buses = bus_manager.all_buses()
        return {
            "status": "healthy",
            "buses": len(buses),
            # Phase I5: dict is keyed by bus_id (UUID); stringify for JSON.
            "clients_per_bus": {str(bid): len(b.all_clients()) for bid, b in buses.items()},
        }

    # ---- Phase I7/I8: REST API for bus management ----
    # Wraps the same bus tools that MCP clients see, but as plain HTTP +
    # Bearer auth so the Blender addon (and any web UI) can call them
    # without the JSON-RPC initialize/handshake dance MCP requires.
    # All routes validate the JWT through the SAME provider FastMCP uses
    # (OIDCProxy → Authentik or BlenderMCPOAuthProvider) — so a token
    # that works for MCP works for /api/* and vice versa.

    async def _api_user_id(authorization: Optional[str]) -> Optional[str]:
        """Resolve a Bearer-Authorization header to a user_id via the
        configured auth provider. Returns None on missing/invalid token."""
        if not authorization or not authorization.lower().startswith("bearer "):
            return None
        token = authorization.split(None, 1)[1].strip()
        # Try the configured provider's verify path. For OIDCProxy this
        # validates against Authentik's JWKS + audience. For
        # BlenderMCPOAuthProvider it does a JTI-store lookup.
        try:
            from .mcp_oauth_provider import BlenderMCPOAuthProvider
            provider = mcp.auth
            if isinstance(provider, BlenderMCPOAuthProvider):
                uid = provider.get_user_for_token(token)
                return uid
            # OIDCProxy / JWT path — decode without verifying signature since
            # the provider already enforces validation via verify_token. For
            # safety in non-MCP paths we DO want signature validation —
            # ``token_validator`` exposes verify_token (async).
            tv = getattr(provider, "_token_validator", None)
            if tv is not None:
                result = await tv.verify_token(token)
                if result is None:
                    return None
                claims = getattr(result, "claims", {}) or {}
                return claims.get("sub") or claims.get("preferred_username")
        except Exception as e:
            logger.warning("API auth lookup failed: %s", e)
            return None
        return None

    @app.get("/api/buses")
    async def api_list_buses(request: Request):
        """List all buses the bearer's user is a member of."""
        import uuid as _u
        from .storage import bus_repo, get_session

        uid = await _api_user_id(request.headers.get("authorization"))
        if not uid:
            raise HTTPException(401, "Invalid or missing bearer token")

        async with get_session() as s:
            await bus_repo.ensure_personal_bus(s, uid)
        async with get_session() as s:
            rows = await bus_repo.list_buses_for_user(s, uid)

        return {
            "user_id": uid,
            "buses": [
                {
                    "bus_id": str(bus.bus_id),
                    "name": bus.name,
                    "description": bus.description,
                    "role": role.value,
                    "is_personal": bus.is_personal,
                    "owner_user_id": bus.owner_user_id,
                    "is_owned_by_me": bus.owner_user_id == uid,
                    "created_at": bus.created_at.isoformat(),
                }
                for bus, role in rows
            ],
        }

    @app.post("/api/buses")
    async def api_create_bus(request: Request, body: dict):
        """Create a shared bus. Body: {name, description?}. Caller becomes owner."""
        from .storage import bus_repo, get_session

        uid = await _api_user_id(request.headers.get("authorization"))
        if not uid:
            raise HTTPException(401, "Invalid or missing bearer token")
        name = (body.get("name") or "").strip()
        if not name:
            raise HTTPException(400, "name required")
        if len(name) > 128:
            raise HTTPException(400, "name too long (max 128)")
        async with get_session() as s:
            bus = await bus_repo.create_shared_bus(
                s, owner_user_id=uid, name=name,
                description=body.get("description", ""),
            )
        return {
            "bus_id": str(bus.bus_id),
            "name": bus.name,
            "description": bus.description,
        }

    @app.post("/api/buses/{bus_id}/invite")
    async def api_invite_user(request: Request, bus_id: str, body: dict):
        """Owner/member issues an invitation code. Body: {role?: member|guest}."""
        import uuid as _u
        from .storage import BusRole, bus_repo, get_session

        uid = await _api_user_id(request.headers.get("authorization"))
        if not uid:
            raise HTTPException(401, "Invalid or missing bearer token")
        try:
            bus_uuid = _u.UUID(bus_id)
        except ValueError:
            raise HTTPException(400, "invalid bus_id")
        role = body.get("role", "member")
        if role not in ("member", "guest"):
            raise HTTPException(400, "role must be 'member' or 'guest'")
        async with get_session() as s:
            bus = await bus_repo.get_bus(s, bus_uuid)
            if bus is None:
                raise HTTPException(404, "bus not found")
            if bus.is_personal:
                raise HTTPException(409, "cannot invite to personal bus")
            if await bus_repo.get_membership(s, bus_uuid, uid) is None:
                raise HTTPException(403, "not a member")
            inv = await bus_repo.create_invitation(
                s, bus_id=bus_uuid, invited_by=uid, role=BusRole(role),
            )
        return {
            "code": inv.code,
            "bus_id": str(inv.bus_id),
            "role": inv.role.value,
            "expires_at": inv.expires_at.isoformat(),
        }

    @app.post("/api/buses/join")
    async def api_join_bus(request: Request, body: dict):
        """Consume an invitation code. Body: {code}."""
        from .storage import bus_repo, get_session

        uid = await _api_user_id(request.headers.get("authorization"))
        if not uid:
            raise HTTPException(401, "Invalid or missing bearer token")
        code = (body.get("code") or "").strip().upper()
        if not code:
            raise HTTPException(400, "code required")
        async with get_session() as s:
            bus, status = await bus_repo.consume_invitation(
                s, code=code, joining_user_id=uid,
            )
        if bus is None:
            raise HTTPException(404, status)  # not_found, expired, already_consumed, wrong_invitee
        return {
            "status": status,
            "bus_id": str(bus.bus_id),
            "name": bus.name,
            "description": bus.description,
        }

    @app.post("/api/buses/{bus_id}/leave")
    async def api_leave_bus(request: Request, bus_id: str):
        """Leave a bus you're a member of. Personal + owner buses refused."""
        import uuid as _u
        from .storage import bus_repo, get_session

        uid = await _api_user_id(request.headers.get("authorization"))
        if not uid:
            raise HTTPException(401, "Invalid or missing bearer token")
        try:
            bus_uuid = _u.UUID(bus_id)
        except ValueError:
            raise HTTPException(400, "invalid bus_id")
        async with get_session() as s:
            bus = await bus_repo.get_bus(s, bus_uuid)
            if bus is None:
                raise HTTPException(404, "bus not found")
            if bus.is_personal:
                raise HTTPException(409, "cannot leave personal bus")
            if bus.owner_user_id == uid:
                raise HTTPException(409, "owner cannot leave own bus")
            ok = await bus_repo.revoke_member(s, bus_id=bus_uuid, user_id=uid)
        if not ok:
            raise HTTPException(404, "not a member")
        return {"status": "ok"}

    @app.post("/api/buses/{bus_id}/revoke")
    async def api_revoke_member(request: Request, bus_id: str, body: dict):
        """Owner kicks a member. Body: {user_id}."""
        import uuid as _u
        from .storage import bus_repo, get_session

        uid = await _api_user_id(request.headers.get("authorization"))
        if not uid:
            raise HTTPException(401, "Invalid or missing bearer token")
        target = body.get("user_id")
        if not target:
            raise HTTPException(400, "user_id required")
        if target == uid:
            raise HTTPException(409, "cannot revoke owner")
        try:
            bus_uuid = _u.UUID(bus_id)
        except ValueError:
            raise HTTPException(400, "invalid bus_id")
        async with get_session() as s:
            bus = await bus_repo.get_bus(s, bus_uuid)
            if bus is None:
                raise HTTPException(404, "bus not found")
            if bus.owner_user_id != uid:
                raise HTTPException(403, "not owner")
            ok = await bus_repo.revoke_member(s, bus_id=bus_uuid, user_id=target)
        if not ok:
            raise HTTPException(404, "target not a member")
        return {"status": "ok"}

    # OAuth discovery (RFC 8414 + RFC 9728) — FastMCP's auth provider knows
    # the right path-aware shapes for its mounted MCP resource. Mount those
    # routes at the FastAPI root so a fresh MCP client can follow the 401 →
    # resource_metadata → authorization-server chain. Without this, the
    # `WWW-Authenticate: ... resource_metadata="…/oauth-protected-resource/mcp/"`
    # header advertises a URL that 404s, and DCR-capable clients (Claude Code,
    # etc.) can't bootstrap. The addon's hard-coded /mcp/register flow works
    # either way; this fixes the spec-compliant discovery path.
    # `mcp_path="/"` matches what FastMCP advertises in its own
    # WWW-Authenticate header. With MCP mounted at root, this produces the
    # discovery URL `/.well-known/oauth-protected-resource/mcp/` (the "mcp"
    # segment is FastMCP's internal resource-name, not the mount path) and
    # the discovery body advertises `resource: https://<host>/mcp/`.
    auth_provider = getattr(mcp, "auth", None)
    if auth_provider is not None and hasattr(auth_provider, "get_well_known_routes"):
        for route in auth_provider.get_well_known_routes(mcp_path="/"):
            app.router.routes.append(route)

    # Mount FastMCP at root. FastAPI's registered routes (/auth/login,
    # /health, /openapi.json, the .well-known/* routes added above) match
    # FIRST in registration order — only paths NOT claimed by FastAPI fall
    # through to the mount. So /register, /authorize, /token, /auth/callback
    # (FastMCP's own paths) land at FastMCP correctly, while /auth/login etc.
    # stay with FastAPI.
    app.mount("/", mcp_asgi)

    return app


# Module-level app for `uvicorn blender_mcp.oauth_server:app`.
app = build_app()


def main():
    try:
        from importlib.metadata import version
        v = version("blender-mcp")
    except Exception:
        v = "0.0.0"
    print(f"BlenderMCP OAuth + Bus v{v}")
    print(f"Listening on http://{HOST}:{PORT}")
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")


if __name__ == "__main__":
    main()
