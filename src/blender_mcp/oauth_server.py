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
import logging
import os
import secrets
from datetime import datetime, timedelta
from typing import Any, Optional

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt

from .bus_tools import current_user_id
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


USERS: dict[str, dict[str, Any]] = {
    "admin": {
        "user_id": "admin",
        "username": "admin",
        "password_hash": _hash_password(os.getenv("ADMIN_PASSWORD", "SecureAdmin123!")),
        "roles": ["admin", "user"],
        "scopes": ["*"],
    },
    "demo": {
        "user_id": "demo",
        "username": "demo",
        "password_hash": _hash_password(os.getenv("DEMO_PASSWORD", "DemoUser456!")),
        "roles": ["user"],
        "scopes": ["read", "write"],
    },
}

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

    # ---- JWT middleware ----
    @app.middleware("http")
    async def jwt_middleware(request: Request, call_next):
        # Auth routes pass through; everything else under /mcp needs JWT.
        path = request.url.path
        if path.startswith("/auth/") or path in ("/health", "/docs", "/openapi.json", "/redoc"):
            return await call_next(request)

        if path.startswith("/mcp"):
            auth = request.headers.get("authorization", "")
            if not auth.lower().startswith("bearer "):
                from starlette.responses import JSONResponse
                return JSONResponse({"detail": "Bearer token required"}, status_code=401)
            token = auth.split(None, 1)[1].strip()
            try:
                payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
            except JWTError as e:
                from starlette.responses import JSONResponse
                return JSONResponse({"detail": f"Invalid token: {e}"}, status_code=401)
            user_id = payload.get("sub")
            if not user_id:
                from starlette.responses import JSONResponse
                return JSONResponse({"detail": "Token missing sub"}, status_code=401)

            request.state.user_id = user_id
            token_cv = current_user_id.set(user_id)
            try:
                return await call_next(request)
            finally:
                current_user_id.reset(token_cv)

        return await call_next(request)

    # ---- auth endpoints ----

    @app.post("/auth/login")
    async def login(credentials: dict):
        username = credentials.get("username")
        password = credentials.get("password")
        if not username or not password:
            raise HTTPException(400, "username and password required")

        user = USERS.get(username)
        if not user or not _verify_password(password, user["password_hash"]):
            # Constant-time-ish: still hash even on missing user.
            if not user:
                _verify_password(password, "$2b$12$" + "x" * 53)
            raise HTTPException(401, "Invalid credentials",
                                headers={"WWW-Authenticate": "Bearer"})

        access = _create_access_token({
            "sub": user["user_id"],
            "username": user["username"],
            "roles": user["roles"],
            "scopes": user["scopes"],
        })
        refresh = _create_refresh_token({"sub": user["user_id"]})
        REFRESH_TOKENS[refresh] = {
            "user_id": user["user_id"],
            "created_at": datetime.utcnow().isoformat(),
        }

        return {
            "access_token": access,
            "refresh_token": refresh,
            "token_type": "bearer",
            "expires_in": ACCESS_TOKEN_EXPIRE_MINUTES * 60,
            "user": {
                "user_id": user["user_id"],
                "username": user["username"],
                "roles": user["roles"],
                "scopes": user["scopes"],
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
            "clients_per_bus": {uid: len(b.all_clients()) for uid, b in buses.items()},
        }

    # Mount FastMCP last so middleware ordering is correct.
    app.mount("/mcp", mcp_asgi)

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
