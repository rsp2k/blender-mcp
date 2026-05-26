"""RFC 8252 OAuth 2.1 Authorization Code + PKCE flow with loopback callback.

For Blender addon login against the MCP-spec OAuth endpoints (`/mcp/register`,
`/mcp/authorize`, `/mcp/token`) which our server exposes via FastMCP's
OAuthProxy → Authentik bridge.

The flow:

1. **DCR**: POST /mcp/register to dynamically register this addon instance as
   an OAuth public client (no client_secret, PKCE only).
2. **PKCE**: generate a 256-bit verifier + SHA-256 S256 challenge + random
   ``state`` for CSRF protection.
3. **Loopback server**: bind a localhost socket to an OS-assigned random port.
   Multiple Blender instances on the same machine don't collide. Server is
   single-shot — handles ONE request then exits.
4. **Browser**: open the user's default browser to /mcp/authorize?... The
   user authenticates against Authentik in their browser.
5. **Callback**: Authentik → our server (`/mcp/auth/callback`) → MCP proxy
   issues its own auth code → browser redirected to localhost:PORT/callback
   with `code` query param. Loopback server captures it.
6. **Token exchange**: POST /mcp/token with grant_type=authorization_code +
   verifier. Receive access_token + refresh_token.

All HTTP calls use the stdlib (urllib.request) + ``requests`` (already a
runtime dep for the addon). No new dependencies, no Blender ``pip install``
required.

Usage from the Blender operator:

    payload = oauth_login("https://mcp.l.warehack.ing/mcp/", timeout=300)
    # payload contains access_token, refresh_token, expires_in, client_id
    prefs.jwt_token = payload["access_token"]
    prefs.refresh_token = payload["refresh_token"]
    prefs.oauth_client_id = payload["client_id"]
    prefs.jwt_expires_at = str(int(time.time()) + payload["expires_in"])

The operator MUST run this on a worker thread — it blocks for up to
``timeout`` seconds waiting for the browser callback.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import secrets
import socketserver
import sys
import threading
import webbrowser
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import requests


# ---- PKCE helpers ----------------------------------------------------------


def _b64url(data: bytes) -> str:
    """RFC 4648 §5 base64url encoding without padding."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _gen_pkce() -> tuple[str, str]:
    """Return (verifier, challenge) using PKCE S256."""
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


# ---- Loopback callback server ---------------------------------------------

_SUCCESS_HTML = """<!DOCTYPE html>
<html><head><title>BlenderMCP Authorized</title>
<style>body{font-family:system-ui;text-align:center;padding:4em;
background:#1a1a1a;color:#e0e0e0}h1{color:#5fa}</style></head>
<body><h1>BlenderMCP login complete</h1>
<p>You can close this tab and return to Blender.</p></body></html>""".encode("utf-8")

_ERROR_HTML_TEMPLATE = """<!DOCTYPE html>
<html><head><title>BlenderMCP Authorization Failed</title>
<style>body{font-family:system-ui;text-align:center;padding:4em;
background:#1a1a1a;color:#e0e0e0}h1{color:#f55}pre{background:#000;
padding:1em;text-align:left;display:inline-block}</style></head>
<body><h1>Authorization failed</h1>
<pre>{}</pre>
<p>Close this tab; you can retry from Blender's panel.</p></body></html>"""


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Single-shot HTTP handler that captures the OAuth callback.

    Sets class-level vars ``received_code`` / ``received_state`` / ``received_error``
    when a callback arrives. The caller reads these after server.handle_request()
    returns.
    """

    received_code: str | None = None
    received_state: str | None = None
    received_error: str | None = None

    def do_GET(self) -> None:  # noqa: N802 — http.server interface
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        if "error" in params:
            err = params["error"][0]
            desc = params.get("error_description", [""])[0]
            _CallbackHandler.received_error = f"{err}: {desc}"
            self.send_response(400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            html = _ERROR_HTML_TEMPLATE.format(_CallbackHandler.received_error)
            self.wfile.write(html.encode("utf-8"))
            return

        if "code" in params:
            _CallbackHandler.received_code = params["code"][0]
            _CallbackHandler.received_state = params.get("state", [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(_SUCCESS_HTML)
            return

        self.send_response(400)
        self.end_headers()
        self.wfile.write(b"missing code or error")

    def log_message(self, *_args: Any) -> None:
        """Silence access logs (default goes to stderr)."""


# ---- Public API ------------------------------------------------------------


class OAuthError(Exception):
    """Raised on any failure in the OAuth flow."""


def _normalize_server_url(server_url: str) -> str:
    """Strip trailing /mcp or /mcp/ — the OAuth endpoints live ABOVE /mcp/.

    Our server is at https://mcp.l.warehack.ing/mcp/ but the OAuth surface
    is at https://mcp.l.warehack.ing/mcp/{register,authorize,token,...}.
    So the OAuth base URL is the same as the MCP base URL in our case.
    """
    if server_url.endswith("/"):
        server_url = server_url.rstrip("/")
    return server_url


def _register_client(server_url: str, redirect_uri: str, timeout: float = 10.0) -> dict:
    """Dynamic Client Registration. Returns the full registration response.

    Note: we intentionally don't request specific scopes here. The MCP server
    is single-tenant + single-app (one Authentik OAuth app, all clients
    proxy through it), so scope-gated authorization isn't doing useful
    work. Requesting scopes would force them into Authentik's
    ``scopes_supported`` config which adds friction without benefit.
    """
    url = f"{server_url}/register"
    resp = requests.post(
        url,
        json={
            "client_name": "BlenderMCP Addon",
            "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
        timeout=timeout,
    )
    if resp.status_code >= 400:
        raise OAuthError(f"DCR failed: HTTP {resp.status_code} — {resp.text[:200]}")
    body = resp.json()
    if "client_id" not in body:
        raise OAuthError(f"DCR response missing client_id: {body}")
    return body


def oauth_login(
    server_url: str,
    *,
    timeout: float = 300.0,
    open_browser: bool = True,
) -> dict:
    """Run the full Authorization Code + PKCE flow.

    Blocking — must run on a worker thread, not Blender's main UI thread.

    Args:
        server_url: Base URL of the MCP server, e.g.
            ``https://mcp.l.warehack.ing/mcp/``.
        timeout: Max seconds to wait for the browser callback. Default 5min.
        open_browser: If True (default), opens the user's system browser to
            the authorize URL. Set False to print the URL and let the
            caller open it manually (useful for headless testing).

    Returns:
        Dict with keys:
            access_token, refresh_token, expires_in, token_type, scope,
            client_id (the DCR-issued ID this addon instance got).

    Raises:
        OAuthError on any flow failure (DCR, browser timeout, token exchange).
    """
    server_url = _normalize_server_url(server_url)

    # Reset class-level handler state in case oauth_login was called before.
    _CallbackHandler.received_code = None
    _CallbackHandler.received_state = None
    _CallbackHandler.received_error = None

    # 1. Bind callback server first so we know the port for DCR / authorize URL.
    httpd = socketserver.TCPServer(("127.0.0.1", 0), _CallbackHandler)
    port = httpd.server_address[1]
    redirect_uri = f"http://127.0.0.1:{port}/callback"

    # 2. Register this addon instance as an OAuth client.
    registration = _register_client(server_url, redirect_uri)
    client_id = registration["client_id"]

    # 3. PKCE + state
    verifier, challenge = _gen_pkce()
    state = secrets.token_urlsafe(16)

    # 4. Spawn the callback server thread BEFORE opening the browser
    #    (otherwise browser could redirect before server is listening).
    server_thread = threading.Thread(
        target=httpd.handle_request, daemon=True, name="oauth-callback"
    )
    server_thread.start()

    # 5. Construct authorize URL + open browser
    auth_url = (
        f"{server_url}/authorize?"
        + urlencode(
            {
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": state,
                # No scope requested — see _register_client docstring for why.
                # Authentik still issues a JWT access_token with sub claim
                # that our bus uses for user identity.
            }
        )
    )
    if open_browser:
        webbrowser.open(auth_url, new=2)
    else:
        print(f"[oauth_pkce] open this URL: {auth_url}", file=sys.stderr)

    # 6. Wait for callback (up to timeout)
    server_thread.join(timeout=timeout)
    httpd.server_close()

    if _CallbackHandler.received_error:
        raise OAuthError(f"Authorization failed: {_CallbackHandler.received_error}")
    if _CallbackHandler.received_code is None:
        raise OAuthError(
            f"No callback received within {timeout}s. Did the browser open?"
        )
    if _CallbackHandler.received_state != state:
        raise OAuthError(
            "State mismatch — possible CSRF. Aborting."
        )

    # 7. Exchange code for tokens
    code = _CallbackHandler.received_code
    token_resp = requests.post(
        f"{server_url}/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "code_verifier": verifier,
        },
        timeout=15.0,
    )
    if token_resp.status_code >= 400:
        raise OAuthError(
            f"Token exchange failed: HTTP {token_resp.status_code} — {token_resp.text[:200]}"
        )
    token = token_resp.json()
    if "access_token" not in token:
        raise OAuthError(f"Token response missing access_token: {token}")

    # Stuff client_id into the response so the caller can persist it
    # (needed for the refresh flow).
    token["client_id"] = client_id
    return token


def refresh_oauth_token(
    server_url: str,
    refresh_token: str,
    client_id: str,
    *,
    timeout: float = 15.0,
) -> dict:
    """Rotate the access token via the refresh grant.

    Used by the bus client's existing ``_refresh_watcher`` /
    ``_do_refresh_once`` once it's been migrated from the legacy
    ``/auth/refresh`` endpoint to the OAuth ``/mcp/token`` endpoint.

    Args:
        server_url: Base URL of the MCP server (same as oauth_login).
        refresh_token: The refresh token from a prior oauth_login call.
        client_id: The DCR-issued client_id this addon instance received.
        timeout: HTTP timeout in seconds.

    Returns:
        Same shape as oauth_login (access_token, refresh_token, expires_in,
        token_type, scope) — refresh_token may be rotated, save the new one.

    Raises:
        OAuthError on any failure.
    """
    server_url = _normalize_server_url(server_url)
    resp = requests.post(
        f"{server_url}/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
        },
        timeout=timeout,
    )
    if resp.status_code >= 400:
        raise OAuthError(
            f"Refresh failed: HTTP {resp.status_code} — {resp.text[:200]}"
        )
    body = resp.json()
    if "access_token" not in body:
        raise OAuthError(f"Refresh response missing access_token: {body}")
    return body


def revoke_oauth_token(
    server_url: str,
    token: str,
    client_id: str,
    *,
    timeout: float = 5.0,
) -> None:
    """Best-effort token revocation. Failures are swallowed (the token will
    expire on its own anyway)."""
    server_url = _normalize_server_url(server_url)
    try:
        requests.post(
            f"{server_url}/revoke",
            data={"token": token, "client_id": client_id},
            timeout=timeout,
        )
    except Exception:
        pass
