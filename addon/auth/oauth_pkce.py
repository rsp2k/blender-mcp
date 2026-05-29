"""RFC 8252 OAuth 2.1 Authorization Code + PKCE flow with loopback callback.

For Blender addon login against the MCP-spec OAuth endpoints (`/register`,
`/authorize`, `/token`) which our server exposes via FastMCP's
OAuthProxy → Authentik bridge.

The flow:

1. **DCR**: POST /register to dynamically register this addon instance as
   an OAuth public client (no client_secret, PKCE only).
2. **PKCE**: generate a 256-bit verifier + SHA-256 S256 challenge + random
   ``state`` for CSRF protection.
3. **Loopback server**: bind a localhost socket to an OS-assigned random port.
   Multiple Blender instances on the same machine don't collide. Server is
   single-shot — handles ONE request then exits.
4. **Browser**: open the user's default browser to /authorize?... The
   user authenticates against Authentik in their browser.
5. **Callback**: Authentik → our server (`/auth/callback`) → MCP proxy
   issues its own auth code → browser redirected to localhost:PORT/callback
   with `code` query param. Loopback server captures it.
6. **Token exchange**: POST /token with grant_type=authorization_code +
   verifier. Receive access_token + refresh_token.

All HTTP calls use the stdlib (urllib.request) + ``requests`` (already a
runtime dep for the addon). No new dependencies, no Blender ``pip install``
required.

Usage from the Blender operator:

    payload = oauth_login("https://mcp.blender.bet/", timeout=300)
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
import json
import secrets
import socketserver
import sys
import threading
import webbrowser
from typing import Any


def _addon_version() -> str:
    """Resolve the addon version string for DCR's software_version field.

    Robust against weird import contexts (Blender's _fake_module
    introspection, sys.modules dance during addon reload, etc.):
    falls back to a sentinel rather than raising. The version is
    informational — DCR succeeds either way.
    """
    try:
        from .._version import __version__  # type: ignore[no-redef]
        return __version__
    except Exception:
        return "unknown"
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

_SUCCESS_STYLE = """
body{font-family:system-ui,sans-serif;background:#0e1116;color:#d4d4d4;
  margin:0;min-height:100vh;display:grid;place-items:center;padding:2em}
.card{background:#161b22;border:1px solid #2a313a;border-radius:10px;
  padding:2.5em 3em;max-width:560px;width:100%;
  box-shadow:0 10px 40px rgba(0,0,0,.4)}
h1{color:#5fb878;margin:0 0 .2em;font-size:1.6em;font-weight:600}
.sub{color:#8b949e;margin:0 0 1.6em;font-size:.95em}
.next{margin:0 0 1.8em;line-height:1.45}
table{width:100%;border-collapse:collapse;font-size:.9em;
  font-family:'JetBrains Mono','SF Mono',Menlo,Consolas,monospace}
th{text-align:left;color:#8b949e;font-weight:400;padding:.35em .7em .35em 0;
  white-space:nowrap;vertical-align:top}
td{color:#d4d4d4;padding:.35em 0;word-break:break-all}
.foot{margin-top:1.8em;padding-top:1.2em;border-top:1px solid #2a313a;
  font-size:.78em;color:#6e7681;text-align:center;line-height:1.5}
.foot code{color:#8b949e;background:#0e1116;padding:1px 5px;border-radius:3px}
"""

_SUCCESS_HTML_TEMPLATE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>BlenderMCP Authorized</title>
<meta http-equiv="refresh" content="3;url={redirect_to}">
<style>{style}</style></head>
<body><div class="card">
<h1>{ok} BlenderMCP login complete</h1>
<p class="sub">You can close this tab and return to Blender.</p>
<p class="next">Redirecting to <a href="{redirect_to}">blender.bet</a> in
3 seconds for the full welcome page. The addon is finishing the token
exchange in the background — status will appear in Blender's panel
within a second or two.</p>
<table>
<tr><th>Server</th><td>{server_url}</td></tr>
<tr><th>Addon</th><td>BlenderMCP {addon_version}</td></tr>
<tr><th>Client ID</th><td>{client_id}</td></tr>
<tr><th>Issued at</th><td>{timestamp}</td></tr>
<tr><th>Callback</th><td>{redirect_uri}</td></tr>
</table>
<div class="foot">
MCP-spec OAuth 2.1 + PKCE &middot; RFC 8252 native-app flow<br>
Powered by Authentik via FastMCP <code>OIDCProxy</code>
</div>
</div></body></html>"""

# Where to send the user after the loopback page renders. Hosted at the
# docs site so we can iterate the welcome UX without addon redeploys.
# Derived from server_url's host: mcp.blender.bet → blender.bet (the
# apex), localhost → http://localhost:4321 (Astro dev server) if you're
# running the docs site locally. None of the params are secrets.
_DEFAULT_LANDING_HOST = "https://blender.bet"


def _build_landing_url(ctx: dict) -> str:
    """Construct the post-OAuth landing URL on the docs site.

    Maps the addon's MCP server hostname to the docs site:
      https://mcp.blender.bet → https://blender.bet
      http://localhost:8000   → http://localhost:4321 (Astro dev port)
      anything else           → https://blender.bet (sane default)
    """
    from urllib.parse import urlparse as _urlparse, urlencode as _urlencode

    server_host = _urlparse(ctx.get("server_url", "")).hostname or ""
    if server_host.startswith("localhost") or server_host.startswith("127."):
        base = "http://localhost:4321"
    else:
        base = _DEFAULT_LANDING_HOST

    params = {
        "server": ctx.get("server_url", ""),
        "client_id": ctx.get("client_id", ""),
        "ver": ctx.get("addon_version", ""),
        "ts": ctx.get("timestamp", ""),
    }
    # Trailing slash matches Astro's default route shape; without it the
    # docs site 308-redirects, adding a round-trip the user doesn't need.
    return f"{base}/login-complete/?{_urlencode(params)}"

_ERROR_HTML_TEMPLATE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>BlenderMCP Authorization Failed</title>
<style>{style}h1{{color:#f97583}}pre{{background:#0e1116;border:1px solid #2a313a;
border-radius:6px;padding:1em;text-align:left;overflow:auto;
font-family:'JetBrains Mono','SF Mono',Menlo,monospace;font-size:.85em;
color:#d4d4d4}}</style></head>
<body><div class="card">
<h1>Authorization failed</h1>
<p class="sub">The OAuth flow couldn't complete. The addon stopped before
exchanging the code.</p>
<pre>{detail}</pre>
<p class="next">Close this tab and retry from Blender's BlenderMCP panel.
If this keeps happening, the most useful diagnostic is the server log:
<br><code>docker logs blender-mcp-server-prod | grep -i 'authoriz\\|invalid'</code>
</p>
</div></body></html>"""


def _render_success(ctx: dict) -> bytes:
    """Render the success page from the loopback-handler context dict.

    ``ctx`` carries non-secret values gathered before/during the OAuth
    flow: server_url, addon_version, client_id (DCR-issued), timestamp,
    redirect_uri. Tokens and codes are NEVER passed in — they exist only
    long enough for the next-step token exchange and don't belong in a
    tab the user might leave open.

    The rendered page includes a 3-second meta-refresh to the docs site's
    /login-complete landing (hosted at blender.bet). If that page is
    down/unreachable, the inline content already conveyed the essentials.
    """
    return _SUCCESS_HTML_TEMPLATE.format(
        style=_SUCCESS_STYLE,
        ok="✓",  # heavy check mark — UTF-8, no extra font deps
        redirect_to=_build_landing_url(ctx),
        **ctx,
    ).encode("utf-8")


def _render_error(detail: str) -> bytes:
    return _ERROR_HTML_TEMPLATE.format(
        style=_SUCCESS_STYLE,
        detail=detail,
    ).encode("utf-8")


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Single-shot HTTP handler that captures the OAuth callback.

    Class-level state slots (set by the handler when a callback arrives,
    read by oauth_login after server.handle_request() returns):

    - ``received_code`` / ``received_state`` / ``received_error`` — what the
      OAuth server sent back as query params.
    - ``page_context`` — populated by oauth_login BEFORE starting the
      server; rendered into the success page so the user sees server URL,
      addon version, DCR client_id, timestamp, callback URL. Non-secret;
      no codes or tokens.
    """

    received_code: str | None = None
    received_state: str | None = None
    received_error: str | None = None
    page_context: dict | None = None  # populated by oauth_login before .serve()

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
            self.wfile.write(_render_error(_CallbackHandler.received_error))
            return

        if "code" in params:
            _CallbackHandler.received_code = params["code"][0]
            _CallbackHandler.received_state = params.get("state", [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            ctx = _CallbackHandler.page_context or {}
            self.wfile.write(_render_success(ctx))
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
    """Strip trailing slashes and a trailing ``/mcp`` suffix.

    The server now serves MCP + OAuth at root: the hostname (mcp.*) carries
    the semantic, no path prefix needed. So the OAuth base URL is the
    same as the MCP base URL with no path component.

    Stripping a stored ``/mcp`` suffix is backwards-compat plumbing: users
    who configured ``https://mcp.blender.bet/mcp/`` against the old server
    layout get auto-normalized to ``https://mcp.blender.bet``, and the
    construction ``<base>/register`` lands on the new endpoints correctly.
    """
    server_url = server_url.rstrip("/")
    if server_url.endswith("/mcp"):
        server_url = server_url[: -len("/mcp")]
    return server_url


def _register_client(server_url: str, redirect_uri: str, timeout: float = 10.0) -> dict:
    """Dynamic Client Registration. Returns the full registration response.

    Declares ``openid email profile`` scope so the subsequent /authorize
    request can ask for them (Authentik enforces "client was not
    registered with scope X" if /authorize requests a scope the DCR
    didn't declare — verified empirically 2026-05-28). The OIDC scopes
    give us an ``id_token`` in the /token response, which the addon
    decodes locally to populate user_display_name / user_email for the
    "Logged in as <name>" sidebar label.

    The access_token's role for bus dispatch is unaffected — sub still
    identifies the user, scope-gated authorization isn't load-bearing
    in our single-tenant model.
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
            "scope": "openid email profile",
            # RFC 7591 software identity (phase H — role attribution). The
            # server records (client_id → role="addon") at DCR time and
            # uses it to gate addon-only tools (bus_register_client etc.)
            # and reject LLM-client tools the addon shouldn't call. Static
            # software_id is shared across all addon installs — server
            # only cares about WHICH KIND of client this is, not which
            # installation. Version pulled from _version.py so telemetry
            # reflects the actual running build.
            "software_id": "blender-mcp-addon",
            "software_version": _addon_version(),
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
            ``https://mcp.blender.bet/``. A trailing ``/mcp`` segment is
            stripped automatically (backwards compat for stored prefs).
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
    _CallbackHandler.page_context = None

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

    # 4. Populate context for the success page (non-secret only — see
    #    _render_success docstring). Set BEFORE starting the server so
    #    the handler thread sees it even on a fast redirect.
    import datetime as _dt
    _CallbackHandler.page_context = {
        "server_url": server_url,
        "addon_version": _addon_version(),
        "client_id": client_id,
        "timestamp": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S %Z").strip(),
        "redirect_uri": redirect_uri,
    }

    # 5. Spawn the callback server thread BEFORE opening the browser
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
                # Request OIDC scopes so the /token response includes an
                # id_token with human-readable user info (preferred_username,
                # email, name). Used by the addon's sidebar to display
                # "Logged in as <name>" instead of "Logged in via OAuth".
                # The access_token's role for bus dispatch is unaffected —
                # it still carries sub for user identity.
                "scope": "openid email profile",
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

    # Decode the id_token (if Authentik returned one) for human-readable
    # user info. Safe to skip verification — the token was minted server-
    # side by mcp.blender.bet, which already validated upstream identity
    # before issuing it, AND we only use the claims for display purposes
    # (no privilege is granted based on them). Failure here is non-fatal:
    # caller renders without user info if the dict keys are missing.
    id_token = token.get("id_token")
    if id_token:
        try:
            parts = id_token.split(".")
            if len(parts) == 3:
                payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
                claims = json.loads(base64.urlsafe_b64decode(payload_b64))
                # preferred_username is usually the short login; name is the
                # display name; email is self-explanatory. Pick the best
                # available per the OIDC spec's conventions.
                token["user_display_name"] = (
                    claims.get("name")
                    or claims.get("preferred_username")
                    or claims.get("email")
                    or ""
                )
                token["user_email"] = claims.get("email") or ""
                token["user_preferred_username"] = claims.get("preferred_username") or ""
        except Exception as e:
            print(f"[oauth_pkce] id_token decode failed (non-fatal): {e}")
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
    ``/auth/refresh`` endpoint to the OAuth ``/token`` endpoint.

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
