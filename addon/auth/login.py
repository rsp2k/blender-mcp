"""OAuth `/auth/login` + `/auth/refresh` HTTP client.

Pure-requests, no bpy imports — runnable in any Python context, which
is what makes Gate G testable without Blender. The caller (the Login
operator) is responsible for marshaling the result back onto Blender's
main thread.

Server endpoint contract (verified in this session against
`blender-mcp` running on :8765):

    POST /auth/login {"username": str, "password": str}
        -> 200 {access_token, refresh_token, token_type, expires_in, user}
        -> 401 {detail: "Invalid credentials"}
        -> 400 {detail: "username and password required"}

    POST /auth/refresh {"refresh_token": str}
        -> 200 {access_token, refresh_token, token_type, expires_in}
        -> 401 invalid/expired
"""

from __future__ import annotations

from typing import Any

import requests


class LoginError(Exception):
    """Raised when /auth/login or /auth/refresh returns a non-2xx response.

    Carries the HTTP status code and the server's `detail` (or response
    text) so callers can present an actionable error message in the UI.
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def _auth_base(server_url: str) -> str:
    """Strip the trailing `/mcp` from the panel's server URL.

    The panel stores the *MCP endpoint* (e.g. http://localhost:8000/mcp),
    but /auth/* lives at the FastAPI root. Both `/mcp` and `/mcp/` are
    stripped; URLs without `/mcp` pass through unchanged.
    """
    base = server_url.rstrip("/")
    if base.endswith("/mcp"):
        base = base[:-4]
    return base


def login(
    server_url: str,
    username: str,
    password: str,
    *,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """POST credentials, return the parsed JSON response.

    Raises:
        LoginError on non-2xx (carries status_code + detail).
        requests.exceptions.RequestException on network errors (timeout,
            connection refused, DNS, etc.). Callers should usually catch
            both — the Login operator wraps everything for user display.
    """
    url = f"{_auth_base(server_url)}/auth/login"
    resp = requests.post(
        url,
        json={"username": username, "password": password},
        timeout=timeout,
    )
    if resp.status_code != 200:
        detail = _detail(resp)
        raise LoginError(detail, status_code=resp.status_code)
    return resp.json()


def refresh_token(
    server_url: str,
    refresh: str,
    *,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """Exchange a refresh token for a new access token."""
    url = f"{_auth_base(server_url)}/auth/refresh"
    resp = requests.post(
        url,
        json={"refresh_token": refresh},
        timeout=timeout,
    )
    if resp.status_code != 200:
        detail = _detail(resp)
        raise LoginError(detail, status_code=resp.status_code)
    return resp.json()


def _detail(resp: requests.Response) -> str:
    """Best-effort extraction of a human-readable error message."""
    try:
        body = resp.json()
        if isinstance(body, dict) and "detail" in body:
            return str(body["detail"])
        return str(body)
    except ValueError:
        return resp.text or f"HTTP {resp.status_code}"
