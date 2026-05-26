"""BlenderMCP operators — Login, Start/Stop server, Set Hyper3D free-trial key.

Phase 7 added BLENDERMCP_OT_Login (replacing manual JWT paste with a
real /auth/login round-trip). Phase 8 swaps Scene-property reads for
AddonPreferences accessors.
"""

from __future__ import annotations

import threading
import traceback

import bpy
import requests  # for catching requests.exceptions.RequestException

from .. import state
from ..auth import LoginError, OAuthError, login, logout, oauth_login
from ..auth.login import _auth_base
from ..client import BlenderMCPClient
from ..client.bus_client import FASTMCP_AVAILABLE
from ..constants import RODIN_FREE_TRIAL_KEY
from ..executor import BlenderCommandExecutor
from ..identity import StickyUUIDManager
from ..preferences import get_prefs


class BLENDERMCP_OT_TestConnection(bpy.types.Operator):
    """Probe the configured server's /health endpoint and report status.

    Quick smoke test the user can run BEFORE clicking Login — confirms
    the server URL is reachable, the certificate validates, and the
    health check returns 200 with a sensible body. Saves a round of
    "wrong URL?" / "is the server up?" diagnostic guessing.

    Uses a 3-second timeout so a missing host fails fast in the UI
    rather than freezing Blender's prefs panel.
    """

    bl_idname = "blendermcp.test_connection"
    bl_label = "Test Connection"
    bl_description = "GET {server_url}/health with a 3s timeout; report status to the operator log"

    def execute(self, context):
        prefs = get_prefs(context)
        server_url = prefs.server_url
        if not server_url:
            self.report({'ERROR'}, "Server URL is empty")
            return {'CANCELLED'}

        health_url = f"{_auth_base(server_url)}/health"
        try:
            resp = requests.get(health_url, timeout=3.0)
        except requests.exceptions.Timeout:
            self.report({'ERROR'}, f"Timeout (>3s) hitting {health_url}")
            return {'CANCELLED'}
        except requests.exceptions.SSLError as e:
            self.report({'ERROR'}, f"TLS error: {e}")
            return {'CANCELLED'}
        except requests.exceptions.ConnectionError as e:
            self.report({'ERROR'}, f"Cannot reach {health_url}: {e}")
            return {'CANCELLED'}
        except requests.exceptions.RequestException as e:
            self.report({'ERROR'}, f"Network error: {e}")
            return {'CANCELLED'}

        if resp.status_code != 200:
            self.report({'ERROR'}, f"FAILED: HTTP {resp.status_code} from {health_url}")
            return {'CANCELLED'}

        # Compact body summary — /health returns
        #   {"status": "healthy", "buses": N, "clients_per_bus": {...}}
        try:
            body = resp.json()
            buses = body.get("buses", "?")
            status = body.get("status", "?")
            self.report({'INFO'}, f"OK — server {status}, {buses} bus(es) active")
        except ValueError:
            # /health didn't return JSON — odd but not fatal; still 200.
            self.report({'INFO'}, f"OK — HTTP 200 (non-JSON body, {len(resp.content)} bytes)")
        return {'FINISHED'}


class BLENDERMCP_OT_Login(bpy.types.Operator):
    """Exchange username/password for a JWT against the BlenderMCP server.

    Synchronous (blocks Blender's UI for the round-trip) — keeps the
    operator simple; the request usually completes in well under a
    second. On success: writes scene.blendermcp_jwt_token and clears the
    transient password field. On failure: surfaces a structured error
    via self.report({'ERROR'}, ...).
    """

    bl_idname = "blendermcp.login"
    bl_label = "Login to BlenderMCP Server"
    bl_description = "POST your username/password to /auth/login and store the returned JWT"

    def execute(self, context):
        scene = context.scene
        prefs = get_prefs(context)
        server_url = prefs.server_url
        username = prefs.username
        # Password is the only credential field still on Scene — transient,
        # never persisted, never shipped in .blend files.
        password = getattr(scene, "blendermcp_password_tmp", "")

        if not username or not password:
            self.report({'ERROR'}, "Username and password required")
            return {'CANCELLED'}

        try:
            payload = login(server_url, username, password)
        except LoginError as e:
            code = f" ({e.status_code})" if e.status_code else ""
            self.report({'ERROR'}, f"Login failed{code}: {e}")
            return {'CANCELLED'}
        except requests.exceptions.RequestException as e:
            self.report({'ERROR'}, f"Network error contacting {server_url}: {e}")
            return {'CANCELLED'}
        except Exception as e:
            self.report({'ERROR'}, f"Unexpected error: {e}")
            traceback.print_exc()
            return {'CANCELLED'}

        prefs.jwt_token = payload.get("access_token", "")
        # Store refresh token + computed expiry so the bus client can
        # pre-emptively rotate the JWT before it expires (avoiding the
        # mid-session 401 wedge). expires_in is a relative duration in
        # seconds; convert to absolute epoch so a Blender restart doesn't
        # confuse the math.
        prefs.refresh_token = payload.get("refresh_token", "")
        expires_in = int(payload.get("expires_in", 0))
        if expires_in:
            import time
            prefs.jwt_expires_at = str(int(time.time()) + expires_in)
        # Clear the password field so it's not lingering in the UI.
        scene.blendermcp_password_tmp = ""

        user = payload.get("user", {})
        who = user.get("username", username)
        self.report({'INFO'}, f"Logged in as {who}")
        return {'FINISHED'}


class BLENDERMCP_OT_OAuthLogin(bpy.types.Operator):
    """Authenticate via the MCP-spec OAuth flow (Authentik in prod).

    Spawns a worker thread that drives the RFC 8252 PKCE flow:
      1. POST /mcp/register — Dynamic Client Registration
      2. Open the user's system browser to /mcp/authorize
      3. Authentik authenticates the user in the browser
      4. Browser redirects to localhost callback; addon captures the code
      5. POST /mcp/token — exchange code for access+refresh tokens
      6. Store tokens in addon prefs

    The flow runs in a worker thread so Blender's UI doesn't freeze for
    the 30s-2min the user is in the browser. A bpy.app.timer polls the
    thread's result and updates the panel when complete.

    Use this against AUTH_BACKEND=authentik servers. The legacy
    password-based ``blendermcp.login`` is for AUTH_BACKEND=inmemory.
    """

    bl_idname = "blendermcp.oauth_login"
    bl_label = "Login with OAuth"
    bl_description = (
        "Open browser → authenticate via Authentik → store tokens in prefs. "
        "Use against MCP servers configured with AUTH_BACKEND=authentik."
    )

    def execute(self, context):
        prefs = get_prefs(context)
        server_url = prefs.server_url
        if not server_url:
            self.report({'ERROR'}, "Server URL is empty")
            return {'CANCELLED'}

        # Thread state — stored on the module so the timer callback can read.
        state._oauth_result = None
        state._oauth_error = None

        def _worker():
            try:
                token = oauth_login(server_url, timeout=300.0)
                state._oauth_result = token
            except OAuthError as e:
                state._oauth_error = str(e)
            except Exception as e:
                state._oauth_error = f"Unexpected: {e}"
                traceback.print_exc()

        thread = threading.Thread(target=_worker, daemon=True, name="oauth-login")
        thread.start()

        # Poll for completion on the main thread via bpy.app.timer.
        # The timer callback persists results into prefs (which IS main-thread).
        def _poll():
            if state._oauth_result is None and state._oauth_error is None:
                return 0.5  # keep polling
            if state._oauth_error:
                print(f"[BlenderMCP] OAuth login failed: {state._oauth_error}")
                state._oauth_error = None
                return None  # unregister timer

            tok = state._oauth_result
            state._oauth_result = None
            import time as _time
            prefs_now = get_prefs()
            prefs_now.jwt_token = tok["access_token"]
            prefs_now.refresh_token = tok.get("refresh_token", "")
            prefs_now.oauth_client_id = tok.get("client_id", "")
            expires_in = int(tok.get("expires_in", 0))
            if expires_in:
                prefs_now.jwt_expires_at = str(int(_time.time()) + expires_in)
            print(
                f"[BlenderMCP] OAuth login complete; access token expires in "
                f"{expires_in}s, client_id={prefs_now.oauth_client_id}"
            )
            return None  # unregister timer

        bpy.app.timers.register(_poll, first_interval=0.5)
        self.report({'INFO'}, "Browser opened — complete login there")
        return {'FINISHED'}


class BLENDERMCP_OT_Logout(bpy.types.Operator):
    """Disconnect, notify the server, clear the stored JWT.

    Order matters: disconnect FIRST (so the bus's SSE stream closes
    cleanly before the server invalidates its session-bound state),
    then notify the server via /auth/logout (best-effort — server
    failure does not block client cleanup), then clear local prefs.
    Always succeeds from the user's perspective: even on network
    failure, the local credentials are gone.
    """

    bl_idname = "blendermcp.logout"
    bl_label = "Logout"
    bl_description = "Disconnect, invalidate server-side refresh tokens, clear local JWT"

    def execute(self, context):
        prefs = get_prefs(context)
        scene = context.scene

        # 1. Disconnect first if connected — closes the bus client thread
        #    so the worker isn't holding an authenticated stream when we
        #    pull the JWT out from under it.
        if state._client is not None:
            try:
                state._client.stop()
            except Exception:
                pass  # best-effort — proceed with logout regardless
            state._client = None
        scene.blendermcp_server_running = False

        # 2. Tell the server (best-effort).
        token = prefs.jwt_token
        if token:
            try:
                logout(prefs.server_url, token)
            except Exception:
                pass  # logout helper already swallows; belt-and-suspenders

        # 3. Clear local credentials. Username stays — it's not a secret,
        #    and forcing the user to retype it on next login is annoying.
        prefs.jwt_token = ""
        prefs.refresh_token = ""
        prefs.jwt_expires_at = ""
        prefs.oauth_client_id = ""
        scene.blendermcp_password_tmp = ""

        self.report({'INFO'}, "Logged out")
        return {'FINISHED'}


class BLENDERMCP_OT_SetFreeTrialHyper3DAPIKey(bpy.types.Operator):
    bl_idname = "blendermcp.set_hyper3d_free_trial_api_key"
    bl_label = "Set Free Trial API Key"

    def execute(self, context):
        prefs = get_prefs(context)
        prefs.hyper3d_api_key = RODIN_FREE_TRIAL_KEY
        prefs.hyper3d_mode = 'MAIN_SITE'
        self.report({'INFO'}, "API Key set successfully!")
        return {'FINISHED'}


class BLENDERMCP_OT_StartServer(bpy.types.Operator):
    """Connect to the BlenderMCP server's _message_bus channel."""

    bl_idname = "blendermcp.start_server"
    bl_label = "Connect to BlenderMCP Server"
    bl_description = "Connect to the BlenderMCP server's _message_bus channel"

    def execute(self, context):
        scene = context.scene
        prefs = get_prefs(context)

        if not FASTMCP_AVAILABLE:
            self.report(
                {'ERROR'},
                "fastmcp not installed. Run: <blender_python> -m pip install fastmcp",
            )
            return {'CANCELLED'}

        if not prefs.jwt_token:
            self.report({'ERROR'}, "Not logged in. Click Login first to obtain a JWT.")
            return {'CANCELLED'}

        try:
            if state._executor is None:
                state._executor = BlenderCommandExecutor()

            if state._client is None:
                uuid_mgr = StickyUUIDManager()
                # Pass refresh creds so the worker can pre-emptively rotate
                # the JWT before it expires. Falls back gracefully (no refresh)
                # if refresh_token isn't set — e.g. for prefs migrated from
                # before the refresh-flow landed.
                expires_at = 0
                if prefs.jwt_expires_at:
                    try:
                        expires_at = int(prefs.jwt_expires_at)
                    except ValueError:
                        pass
                state._client = BlenderMCPClient(
                    server_url=prefs.server_url,
                    jwt_token=prefs.jwt_token,
                    client_uuid=uuid_mgr.get_client_id(),
                    executor=state._executor,  # injected so drainer exposes to scripts
                    refresh_token=prefs.refresh_token,
                    jwt_expires_at=expires_at,
                )
                scene.blendermcp_client_id = state._client.client_uuid

            state._client.start()
            scene.blendermcp_server_running = True
            self.report({'INFO'}, f"Connecting as {scene.blendermcp_client_id}")
        except Exception as e:
            self.report({'ERROR'}, f"Failed to start client: {e}")
            traceback.print_exc()
            return {'CANCELLED'}

        return {'FINISHED'}


class BLENDERMCP_OT_StopServer(bpy.types.Operator):
    """Disconnect from the MCP message bus."""

    bl_idname = "blendermcp.stop_server"
    bl_label = "Disconnect from BlenderMCP Server"
    bl_description = "Disconnect from the MCP message bus"

    def execute(self, context):
        scene = context.scene
        try:
            if state._client is not None:
                state._client.stop()
                state._client = None
            scene.blendermcp_server_running = False
            self.report({'INFO'}, "Disconnected")
        except Exception as e:
            self.report({'ERROR'}, f"Error during disconnect: {e}")
            traceback.print_exc()
        return {'FINISHED'}
