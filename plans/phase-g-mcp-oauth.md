# Phase G: Authentik OIDC + MCP-spec OAuth via FastMCP OIDCProxy

## Context

Phases A-F shipped the dispatch surface, deployed it behind caddy-docker-proxy
at `https://mcp.l.warehack.ing/mcp/`, and got the addon connecting via a
custom JWT-by-password flow at `/auth/login`. That flow is **not MCP-spec
OAuth** — it's a homebrew bearer-token scheme that works for our addon
(which we control) but doesn't comply with the MCP authorization spec, AND
it duplicates user management we already have elsewhere (Authentik).

**Why it matters**: any MCP-spec-compliant client (Claude Code, Claude
Desktop's MCP connector, Cursor, MCP Inspector, future unknown clients)
expects auth via `/.well-known/oauth-authorization-server` + Dynamic Client
Registration + Authorization Code flow + PKCE. Without that, those clients
can't auto-discover our server's auth config and have to be fed a static
Bearer token (which expires, which leaks into config files). Implementing
proper MCP OAuth — and backing it with Authentik as the actual identity
provider — makes our server a first-class MCP citizen AND consolidates
user management into the IDP that already runs everything else here.

**Architecture decisions (locked):**

- **Authentik is the primary IDP** (dev: `auth.new.l.supported.systems`;
  prod: `auth.supported.systems`). Users authenticate against Authentik
  for both Claude Code and the Blender addon.
- **`OIDCProxy` is the FastMCP class we use** — Authentik is OIDC-compliant,
  and `OIDCProxy` is purpose-built for OIDC IDPs that don't natively
  speak MCP DCR. It registers itself as a DCR-capable surface for MCP
  clients while tunneling all real auth to Authentik via the standard
  Authorization Code + PKCE flow.
- **`BlenderMCPOAuthProvider` (from Phase G1) stays as a local-dev
  fallback**, gated by an env var. Useful when working on the server
  offline (Authentik unreachable, airplane mode, etc). Default backend
  in prod is Authentik.
- **`/auth/login` + USERS dict + bcrypt are deleted**. Authentik manages
  every user account.
- **Addon's Login button becomes a real OAuth client**: opens the user's
  system browser to Authentik, captures the redirect at a loopback HTTP
  server, exchanges the code for tokens via PKCE. Standard RFC 8252
  flow for native/desktop apps.

## Approach

### 1. (already done in G1) `BlenderMCPOAuthProvider` — local-dev fallback

Subclass of `InMemoryOAuthProvider` already written. Gated behind env var
`AUTH_BACKEND=inmemory` (default `authentik`). Useful for offline server
hacking; not the prod path.

### 2. Provision Authentik OAuth app (done by user)

In `https://auth.new.l.supported.systems/if/admin/`:

- Create **OAuth2/OpenID Provider** named `BlenderMCP OAuth`
  - Client type: Confidential
  - Redirect URIs:
    - `https://mcp.l.warehack.ing/auth/callback`
    - `http://localhost:8000/auth/callback` (local dev)
  - Subject mode: Based on User's hashed ID
- Create **Application** named `BlenderMCP`, slug `blender-mcp`,
  bound to the provider above
- Result: a `client_id` + `client_secret`, and the OIDC config endpoint at
  `https://auth.new.l.supported.systems/application/o/blender-mcp/.well-known/openid-configuration`

These values get stored as env vars in the server's `.env`:
```
AUTH_BACKEND=authentik
AUTHENTIK_CONFIG_URL=https://auth.new.l.supported.systems/application/o/blender-mcp/.well-known/openid-configuration
AUTHENTIK_CLIENT_ID=<from authentik>
AUTHENTIK_CLIENT_SECRET=<from authentik>
PUBLIC_BASE_URL=https://mcp.l.warehack.ing
```

### 3. Wire `OIDCProxy` into `build_http_mcp()` in `server_proper.py`

```python
from fastmcp.server.auth.oidc_proxy import OIDCProxy

def _build_authentik_proxy() -> OIDCProxy:
    return OIDCProxy(
        config_url=os.environ["AUTHENTIK_CONFIG_URL"],
        client_id=os.environ["AUTHENTIK_CLIENT_ID"],
        client_secret=os.environ["AUTHENTIK_CLIENT_SECRET"],
        base_url=os.environ["PUBLIC_BASE_URL"],
        required_scopes=["openid", "profile"],
    )

def _build_inmemory_provider() -> BlenderMCPOAuthProvider:
    from .mcp_oauth_provider import BlenderMCPOAuthProvider
    from .oauth_server import _DEV_USERS, _verify_password  # tiny dev-only dict
    return BlenderMCPOAuthProvider(
        users=_DEV_USERS,
        verify_password=_verify_password,
        base_url=os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000"),
    )

def build_http_mcp() -> FastMCP:
    backend = os.environ.get("AUTH_BACKEND", "authentik")
    auth = _build_authentik_proxy() if backend == "authentik" else _build_inmemory_provider()
    server = FastMCP("BlenderMCP", auth=auth)
    # ... existing component registration ...
    return server
```

`OIDCProxy` automatically exposes `/.well-known/oauth-authorization-server`,
`/.well-known/oauth-protected-resource`, `/oauth/register` (DCR), `/oauth/authorize`,
`/oauth/token`, `/oauth/callback`. No further work for MCP-spec endpoints.

### 4. Delete `/auth/login`, USERS dict, bcrypt, jwt_middleware

The whole homebrew auth scheme in `oauth_server.py` goes away. What remains:

- The FastAPI app that mounts `mcp_asgi`
- The bus-forwarding handler (`BusForwardingHandler` — unchanged)
- `/health` endpoint (unchanged)
- A small `current_user_id` ContextVar setup that reads from the OIDCProxy's
  verified token data instead of decoding JWTs manually

`addon/auth/login.py` (with `login`, `refresh_token`, `logout` helpers)
gets refactored into `addon/auth/oauth_pkce.py` — see G4 below.

### 5. Addon OAuth client (PKCE with system browser, RFC 8252)

New module `addon/auth/oauth_pkce.py`:

```python
import http.server, socketserver, threading, webbrowser
import secrets, hashlib, base64
from urllib.parse import urlencode, urlparse, parse_qs

class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    received_code: str | None = None
    received_state: str | None = None
    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        if 'code' in params:
            _CallbackHandler.received_code = params['code'][0]
            _CallbackHandler.received_state = params.get('state', [None])[0]
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            self.wfile.write(b"<html><body><h1>Logged in!</h1>"
                             b"<p>You can close this tab.</p></body></html>")
        else:
            self.send_response(400); self.end_headers()
    def log_message(self, *_): pass  # suppress

def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b'=').decode()

def oauth_login(server_url: str) -> dict:
    """Run the full Authorization Code + PKCE flow against server_url's
    OAuth endpoints. Returns the token response dict on success.

    Blocking call — must run on a worker thread, NOT Blender's main thread.
    """
    # 1. DCR: register this addon as a public client
    reg = requests.post(f"{server_url}/oauth/register", json={
        "client_name": "BlenderMCP Addon",
        "redirect_uris": ["http://localhost"],  # exact port set later
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }, timeout=10).json()
    client_id = reg["client_id"]

    # 2. PKCE
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    state = secrets.token_urlsafe(16)

    # 3. Local callback server on random port
    httpd = socketserver.TCPServer(('localhost', 0), _CallbackHandler)
    port = httpd.server_address[1]
    redirect_uri = f"http://localhost:{port}/callback"
    server_thread = threading.Thread(target=httpd.handle_request, daemon=True)
    server_thread.start()

    # 4. Open browser
    auth_url = f"{server_url}/oauth/authorize?" + urlencode({
        'response_type': 'code',
        'client_id': client_id,
        'redirect_uri': redirect_uri,
        'code_challenge': challenge,
        'code_challenge_method': 'S256',
        'state': state,
        'scope': 'openid profile',
    })
    webbrowser.open(auth_url)

    # 5. Wait for callback (5min timeout)
    server_thread.join(timeout=300)
    if _CallbackHandler.received_code is None:
        raise TimeoutError("OAuth login: no callback received within 5min")
    if _CallbackHandler.received_state != state:
        raise ValueError("OAuth login: state mismatch (possible CSRF)")

    # 6. Exchange code for tokens
    return requests.post(f"{server_url}/oauth/token", data={
        'grant_type': 'authorization_code',
        'code': _CallbackHandler.received_code,
        'redirect_uri': redirect_uri,
        'client_id': client_id,
        'code_verifier': verifier,
    }, timeout=10).json()


def refresh_token(server_url: str, refresh_tok: str, client_id: str) -> dict:
    """Rotate access token. Used by the bus client's existing
    _refresh_watcher / _do_refresh_once."""
    return requests.post(f"{server_url}/oauth/token", data={
        'grant_type': 'refresh_token',
        'refresh_token': refresh_tok,
        'client_id': client_id,
    }, timeout=10).json()
```

New operator `BLENDERMCP_OT_OAuthLogin` replaces `BLENDERMCP_OT_Login`:

- `execute()` spawns a worker thread that runs `oauth_login()`
- Reports "Browser opened — waiting for login" via `self.report({'INFO'}, ...)`
- A bpy.app.timer polls for thread completion; on success, populates
  `prefs.jwt_token` + `prefs.refresh_token` + `prefs.jwt_expires_at` AND
  `prefs.oauth_client_id` (new field — needed for refresh)
- The existing `BLENDERMCP_OT_Logout` operator and bus-client refresh flow
  keep working (token field names unchanged)

The username + password fields in the prefs panel get DELETED — Authentik
collects credentials, not us.

## Phases (incremental commits on `feat/mcp-oauth` branch)

| # | Phase | Outcome | Verification gate |
|---|---|---|---|
| 0 | Baseline | `git tag pre-mcp-oauth`; branch `feat/mcp-oauth` | tag + branch exist |
| G1 | InMemory provider | `mcp_oauth_provider.py` with BlenderMCPOAuthProvider. Importable. | `uv run python -c 'from blender_mcp.mcp_oauth_provider import BlenderMCPOAuthProvider; print("ok")'` |
| G2 | Authentik app + OIDCProxy wiring | Auth app provisioned (user-side). server_proper.py picks `OIDCProxy` when `AUTH_BACKEND=authentik`. AS metadata endpoint live. | `curl https://mcp.l.warehack.ing/.well-known/oauth-authorization-server` returns valid JSON; `curl -X POST .../oauth/register -d '{...}'` returns 200 |
| G3 | Cutover: delete /auth/login + USERS + jwt_middleware | Old auth scheme entirely removed. ContextVar resolution moves to OIDCProxy's verified token data. | Gate H still PASS (5/5); MCP Inspector can connect via OAuth flow + dispatch a tool |
| G4 | Addon OAuth client | `addon/auth/oauth_pkce.py`. New `BLENDERMCP_OT_OAuthLogin` operator. Username/password prefs removed. | Real-Blender e2e: click Login → browser opens → Authentik login → token in prefs → Connect succeeds → dispatch works |
| G5 | Documentation + cleanup | README updated. Addon install guide updated. Old `addon/auth/login.py` deleted (replaced by oauth_pkce.py). | Fresh-install walkthrough passes for a user who's never seen this before |

## Critical files

| File | Action |
|---|---|
| `src/blender_mcp/mcp_oauth_provider.py` | EXISTS (G1) — keep as local-dev fallback |
| `src/blender_mcp/server_proper.py` | EDIT (G2) — env-gated provider selection |
| `src/blender_mcp/oauth_server.py` | EDIT (G3) — delete /auth/login, USERS, bcrypt, jwt_middleware. ~150 lines removed. |
| `src/blender_mcp/bus_tools.py` | EDIT (G3) — `_resolve_user_id` reads from OIDCProxy's request state instead of ContextVar set by old middleware |
| `addon/auth/oauth_pkce.py` | NEW (G4) — ~120 lines RFC 8252 PKCE client |
| `addon/auth/__init__.py` | EDIT (G4) — re-export from oauth_pkce instead of login |
| `addon/auth/login.py` | DELETE (G4) — replaced by oauth_pkce.py |
| `addon/ui/operators.py` | EDIT (G4) — `BLENDERMCP_OT_OAuthLogin` replaces `BLENDERMCP_OT_Login` |
| `addon/preferences.py` | EDIT (G4) — delete username/password fields; add `oauth_client_id` field |
| `addon/ui/panel.py` | EDIT (G4) — simpler login UI (just a button, no creds fields) |
| `addon/client/bus_client.py` | EDIT (G4) — refresh flow points at `/oauth/token` instead of `/auth/refresh` |
| `.env.example` | EDIT (G2) — document AUTH_BACKEND + AUTHENTIK_* env vars |
| `docker-compose.yml` | EDIT (G2) — pass new env vars through to container |
| `README.md` | EDIT (G5) — new auth setup section |

## Reuse

- **`fastmcp.server.auth.oidc_proxy.OIDCProxy`** — drop-in OIDC → MCP-spec
  bridge. Fetches OIDC discovery, handles DCR, proxies authn to Authentik.
- **`mcp_oauth_provider.BlenderMCPOAuthProvider`** (from G1) — kept as
  fallback for `AUTH_BACKEND=inmemory`.
- **Addon's existing `prefs.jwt_token`, `prefs.refresh_token`,
  `prefs.jwt_expires_at`** — token storage fields stay; OAuth flow populates
  them just like the old login flow did.
- **Bus client's `_refresh_watcher` + `_do_refresh_once`** — refresh
  lifecycle stays. Only the HTTP endpoint changes (`/auth/refresh` →
  `/oauth/token`).
- **Existing addon `prefs.username` / `prefs.jwt_token`** scene migration
  code stays as a one-shot upgrader to clear stale fields on first load
  of the post-G4 addon version.

## Verification

### Per-phase gates

```bash
# G1: import smoke (already passing)
uv run python -c "from blender_mcp.mcp_oauth_provider import BlenderMCPOAuthProvider; print('ok')"

# G2: AS metadata + DCR + Authentik passthrough
curl -fsS https://mcp.l.warehack.ing/.well-known/oauth-authorization-server | jq .
# Should include issuer, authorization_endpoint, token_endpoint, registration_endpoint
curl -fsS -X POST https://mcp.l.warehack.ing/oauth/register \
    -H "Content-Type: application/json" \
    -d '{"redirect_uris":["http://localhost:9999/callback"],
         "token_endpoint_auth_method":"none",
         "grant_types":["authorization_code","refresh_token"],
         "response_types":["code"]}' | jq .

# Browser flow (manual):
# 1. Open https://mcp.l.warehack.ing/oauth/authorize?client_id=<from above>&
#    response_type=code&redirect_uri=http://localhost:9999/callback&
#    code_challenge=<S256>&code_challenge_method=S256&state=test&
#    scope=openid+profile
# 2. Should redirect to Authentik login
# 3. After login, redirected back to http://localhost:9999/callback?code=...&state=test

# G3: Gate H + MCP Inspector
uv run python scripts/gate_h_dispatch.py
# (5/5 — but token-acquisition path now uses OAuth or the in-memory fallback)
npx @modelcontextprotocol/inspector https://mcp.l.warehack.ing/mcp/
# (Inspector should auto-discover OAuth, open browser for Authentik login,
#  then list tools)

# G4: Addon real-Blender e2e
# Manual checklist:
# 1. Restart Blender (so new addon code loads)
# 2. Prefs panel: confirm no username/password fields visible
# 3. Click "Login with Authentik" button
# 4. System browser opens to Authentik consent screen
# 5. Login → redirect back → addon shows "Logged in as <user>"
# 6. Click Connect → status: Connected
# 7. From CLI: claude dispatch get_scene_info → returns scene state
```

### End-to-end after G5

Fresh user, never seen this server before:
1. Get blender-mcp addon zip
2. Install in Blender (Edit > Preferences > Add-ons > Install)
3. Enable, open BlenderMCP panel
4. Server URL pre-filled with `https://mcp.l.warehack.ing/mcp/`
5. Click "Login with Authentik" → browser flow → done
6. Click Connect → working

Zero credential setup outside of the browser-based Authentik flow.

## Risk register

| # | Risk | Mitigation |
|---|---|---|
| 1 | OIDCProxy beta — could behave differently in newer fastmcp versions | Pin fastmcp version in pyproject.toml. Watch upstream; test on upgrade. |
| 2 | Authentik OIDC config URL changes if user renames the app slug | Pinned via env var. Document the dependency in CLAUDE.md. |
| 3 | Loopback callback server fails (port already in use, firewall) | OS-assigned port via TCPServer(('localhost', 0)); IPv4 loopback explicitly. Document fallback: paste-callback-url flow if browser can't reach loopback. |
| 4 | Blender freezes during OAuth login (main thread blocked) | Worker thread + bpy.app.timer polling. Operator returns immediately with "Waiting for browser" status. |
| 5 | User closes browser tab before completing login | 5-minute timeout. Cancel button re-enables Login. |
| 6 | Multiple Blender instances open simultaneously | Random port per instance. Each instance's flow is independent. |
| 7 | Authentik down during login | Bus client surfaces the error in the panel's "Last error" line. User retries when Authentik is back. |
| 8 | OIDCProxy doesn't pass `sub` claim through into AccessToken — bus needs user_id | Verify in G3 that the bus's `current_user_id` resolution works against OIDCProxy tokens. If not, subclass OIDCProxy to override the AccessToken construction. |
| 9 | Deleting /auth/login breaks any cached addon installs with old code | Old addons just fail to login with "Endpoint not found". User reinstalls. Document in release notes. |
| 10 | DCR endpoint becomes a registration spam vector | Rate-limit /oauth/register at the caddy layer. Periodically purge unused registered clients (>30d no activity) from in-memory store. |
| 11 | Addon's PKCE flow needs to send the SAME redirect_uri at both /authorize and /token — easy to typo | Helper builds the URI from a single source: `f"http://localhost:{port}/callback"` used in both places. Unit-testable. |
| 12 | If AUTH_BACKEND=inmemory in prod by accident, user data goes to local USERS dict | `AUTH_BACKEND` defaults to `authentik`. inmemory requires explicit opt-in. Log a WARNING at startup if inmemory is selected. |
