"""BlenderMCP operators — OAuth login, Logout, Connect/Disconnect, Test, free-trial-key.

The legacy password-login (BLENDERMCP_OT_Login / /auth/login) was removed
in 1.4.0 when the addon went OAuth-only — clients now authenticate
exclusively via the RFC 8252 PKCE flow (BLENDERMCP_OT_OAuthLogin), which
works against any MCP-spec OAuth server (Authentik in prod, the in-memory
provider for local dev).
"""

from __future__ import annotations

import threading
import traceback

import bpy
import requests  # for catching requests.exceptions.RequestException

from .. import state
from ..auth import OAuthError, logout, oauth_login
from ..client import BlenderMCPClient
from ..client.bus_client import FASTMCP_AVAILABLE
from ..constants import RODIN_FREE_TRIAL_KEY
from ..executor import BlenderCommandExecutor
from ..identity import StickyUUIDManager
from ..preferences import get_client_label, get_prefs, get_server_base_url


class BLENDERMCP_OT_TestConnection(bpy.types.Operator):
    """Probe the configured server's /health endpoint and report status.

    Quick smoke test the user can run BEFORE clicking Login — confirms
    the server URL is reachable, the certificate validates, and the
    health check returns 200 with a sensible body.
    """

    bl_idname = "blendermcp.test_connection"
    bl_label = "Test Connection"
    bl_description = "GET <server>/health with a 3s timeout; report status to the operator log"

    def execute(self, context):
        prefs = get_prefs(context)
        base_url = get_server_base_url(prefs)
        health_url = f"{base_url}/health"
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

        try:
            body = resp.json()
            buses = body.get("buses", "?")
            status = body.get("status", "?")
            self.report({'INFO'}, f"OK — server {status}, {buses} bus(es) active")
        except ValueError:
            self.report({'INFO'}, f"OK — HTTP 200 (non-JSON body, {len(resp.content)} bytes)")
        return {'FINISHED'}


class BLENDERMCP_OT_OAuthLogin(bpy.types.Operator):
    """Authenticate via the MCP-spec OAuth flow (RFC 8252 PKCE + browser).

    Spawns a worker thread that drives the flow:
      1. POST /register — Dynamic Client Registration (RFC 7591)
      2. Open the user's system browser to /authorize
      3. User authenticates against the upstream IDP (Authentik in prod)
      4. Browser redirects to localhost callback; addon captures the code
      5. POST /token — exchange code for access+refresh tokens
      6. Store tokens in addon prefs

    The flow runs in a worker thread so Blender's UI doesn't freeze for
    the 30s-2min the user is in the browser. A bpy.app.timer polls the
    thread's result and updates prefs (main-thread-only) when complete.
    """

    bl_idname = "blendermcp.oauth_login"
    bl_label = "Login with OAuth"
    bl_description = (
        "Open browser → authenticate via the upstream IDP → store tokens "
        "in prefs. Works against any MCP-spec OAuth server."
    )

    def execute(self, context):
        prefs = get_prefs(context)
        base_url = get_server_base_url(prefs)

        # Thread state — stored on the module so the timer callback can read.
        state._oauth_result = None
        state._oauth_error = None

        def _worker():
            try:
                token = oauth_login(base_url, timeout=300.0)
                state._oauth_result = token
            except OAuthError as e:
                state._oauth_error = str(e)
            except Exception as e:
                state._oauth_error = f"Unexpected: {e}"
                traceback.print_exc()

        thread = threading.Thread(target=_worker, daemon=True, name="oauth-login")
        thread.start()

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
    """Disconnect, notify the server, clear stored credentials.

    Order matters: disconnect FIRST (so the bus's SSE stream closes
    cleanly before the server invalidates its session-bound state),
    then notify the server via the OAuth /revoke endpoint (best-effort),
    then clear local prefs.
    """

    bl_idname = "blendermcp.logout"
    bl_label = "Logout"
    bl_description = "Disconnect, invalidate server-side refresh tokens, clear local JWT"

    def execute(self, context):
        prefs = get_prefs(context)
        scene = context.scene
        base_url = get_server_base_url(prefs)

        # 1. Disconnect first if connected.
        if state._client is not None:
            try:
                state._client.stop()
            except Exception:
                pass
            state._client = None
        scene.blendermcp_server_running = False

        # 2. Tell the server (best-effort).
        token = prefs.jwt_token
        if token:
            try:
                logout(base_url, token)
            except Exception:
                pass

        # 3. Clear local credentials.
        prefs.jwt_token = ""
        prefs.refresh_token = ""
        prefs.jwt_expires_at = ""
        prefs.oauth_client_id = ""

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
            self.report({'ERROR'}, "Not logged in. Click Login in prefs first to obtain a JWT.")
            return {'CANCELLED'}

        base_url = get_server_base_url(prefs)

        try:
            if state._executor is None:
                state._executor = BlenderCommandExecutor()

            if state._client is None:
                uuid_mgr = StickyUUIDManager()
                expires_at = 0
                if prefs.jwt_expires_at:
                    try:
                        expires_at = int(prefs.jwt_expires_at)
                    except ValueError:
                        pass
                state._client = BlenderMCPClient(
                    server_url=base_url,
                    jwt_token=prefs.jwt_token,
                    client_uuid=uuid_mgr.get_client_id(),
                    executor=state._executor,
                    refresh_token=prefs.refresh_token,
                    jwt_expires_at=expires_at,
                    label=get_client_label(prefs),
                    bus_id=prefs.default_bus_id or None,
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


# ---- Phase I7: bus management operators ---------------------------------

def _api_call(method: str, path: str, prefs, body: dict | None = None) -> dict:
    """Synchronous wrapper for a server REST call. Returns parsed JSON or
    raises a Python-side Exception with a readable message.

    Uses prefs.jwt_token for Bearer auth. Builds the URL via
    get_server_base_url so dev (http://localhost:8000) + prod
    (https://mcp.blender.bet) both work without explicit scheme handling.
    """
    base = get_server_base_url(prefs).rstrip("/")
    url = f"{base}{path}"
    headers = {
        "Authorization": f"Bearer {prefs.jwt_token}",
        "Accept": "application/json",
    }
    if body is not None:
        headers["Content-Type"] = "application/json"
    resp = requests.request(method, url, headers=headers, json=body, timeout=15.0)
    if resp.status_code >= 400:
        # Try to surface the server's structured error detail if present.
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:
            detail = resp.text
        raise RuntimeError(f"HTTP {resp.status_code}: {detail}")
    return resp.json()


class BLENDERMCP_OT_RefreshBuses(bpy.types.Operator):
    """Refresh the list of buses I'm a member of (Phase I7)."""

    bl_idname = "blendermcp.refresh_buses"
    bl_label = "Refresh Buses"
    bl_description = (
        "Fetch the list of buses you can connect to (your personal bus + "
        "any shared buses you've joined). Required before the Bus dropdown "
        "shows shared options."
    )

    def execute(self, context):
        prefs = get_prefs(context)
        if not prefs.jwt_token:
            self.report({'ERROR'}, "Login first (no JWT token)")
            return {'CANCELLED'}
        try:
            r = _api_call("GET", "/api/buses", prefs)
            state._buses = r.get("buses", [])
        except Exception as e:
            self.report({'ERROR'}, f"Refresh failed: {e}")
            return {'CANCELLED'}
        self.report({'INFO'}, f"Found {len(state._buses)} bus(es)")
        return {'FINISHED'}


class BLENDERMCP_OT_CreateBus(bpy.types.Operator):
    """Create a new shared bus owned by me. Prompts for name."""

    bl_idname = "blendermcp.create_bus"
    bl_label = "Create Bus"
    bl_description = "Create a new shared bus you own; invite others via Join code"

    name: bpy.props.StringProperty(name="Bus name", default="")
    description: bpy.props.StringProperty(name="Description", default="")

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=400)

    def draw(self, context):
        self.layout.prop(self, "name")
        self.layout.prop(self, "description")

    def execute(self, context):
        prefs = get_prefs(context)
        name = (self.name or "").strip()
        if not name:
            self.report({'ERROR'}, "Name required")
            return {'CANCELLED'}
        try:
            r = _api_call("POST", "/api/buses", prefs,
                          {"name": name, "description": self.description or ""})
        except Exception as e:
            self.report({'ERROR'}, f"Create failed: {e}")
            return {'CANCELLED'}
        # Refresh the cached list so the new bus shows up in the dropdown.
        try:
            r2 = _api_call("GET", "/api/buses", prefs)
            state._buses = r2.get("buses", [])
        except Exception:
            pass
        self.report({'INFO'}, f"Created bus: {r['name']} ({r['bus_id'][:8]}...)")
        return {'FINISHED'}


class BLENDERMCP_OT_JoinBus(bpy.types.Operator):
    """Join a bus using an invitation code. Prompts for the code."""

    bl_idname = "blendermcp.join_bus"
    bl_label = "Join Bus"
    bl_description = "Paste an invitation code (BMI-XXXXXXXXXX) to join a shared bus"

    code: bpy.props.StringProperty(name="Invitation code", default="BMI-")

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=400)

    def draw(self, context):
        self.layout.label(text="Paste invitation code (BMI-XXXXXXXXXX):")
        self.layout.prop(self, "code", text="")

    def execute(self, context):
        prefs = get_prefs(context)
        code = (self.code or "").strip().upper()
        if not code or not code.startswith("BMI-"):
            self.report({'ERROR'}, "Code must start with BMI-")
            return {'CANCELLED'}
        try:
            r = _api_call("POST", "/api/buses/join", prefs, {"code": code})
        except Exception as e:
            self.report({'ERROR'}, f"Join failed: {e}")
            return {'CANCELLED'}
        try:
            r2 = _api_call("GET", "/api/buses", prefs)
            state._buses = r2.get("buses", [])
        except Exception:
            pass
        self.report({'INFO'}, f"Joined: {r.get('name', '?')} ({r.get('status')})")
        return {'FINISHED'}


class BLENDERMCP_OT_LeaveBus(bpy.types.Operator):
    """Leave the currently-selected bus."""

    bl_idname = "blendermcp.leave_bus"
    bl_label = "Leave Bus"
    bl_description = "Leave the currently selected bus (cannot leave personal or own bus)"

    def execute(self, context):
        prefs = get_prefs(context)
        bus_id = prefs.default_bus_id
        if not bus_id:
            self.report({'ERROR'}, "No bus selected (default_bus_id is empty)")
            return {'CANCELLED'}
        try:
            _api_call("POST", f"/api/buses/{bus_id}/leave", prefs)
        except Exception as e:
            self.report({'ERROR'}, f"Leave failed: {e}")
            return {'CANCELLED'}
        prefs.default_bus_id = ""  # back to personal
        try:
            r2 = _api_call("GET", "/api/buses", prefs)
            state._buses = r2.get("buses", [])
        except Exception:
            pass
        self.report({'INFO'}, "Left bus; selection reset to personal")
        return {'FINISHED'}


class BLENDERMCP_OT_InviteToBus(bpy.types.Operator):
    """Generate an invitation code for the selected bus + copy to clipboard."""

    bl_idname = "blendermcp.invite_to_bus"
    bl_label = "Invite to Bus"
    bl_description = "Generate an invitation code for the selected bus + copy it to the clipboard"

    role: bpy.props.EnumProperty(
        name="Role",
        items=[
            ("member", "Member", "Full dispatch + read access"),
            ("guest", "Guest", "Read-only — can list clients but not dispatch"),
        ],
        default="member",
    )

    def invoke(self, context, event):
        prefs = get_prefs(context)
        if not prefs.default_bus_id:
            self.report({'ERROR'}, "Select a non-personal bus first")
            return {'CANCELLED'}
        return context.window_manager.invoke_props_dialog(self, width=300)

    def draw(self, context):
        self.layout.prop(self, "role")

    def execute(self, context):
        prefs = get_prefs(context)
        try:
            r = _api_call("POST", f"/api/buses/{prefs.default_bus_id}/invite",
                          prefs, {"role": self.role})
        except Exception as e:
            self.report({'ERROR'}, f"Invite failed: {e}")
            return {'CANCELLED'}
        code = r["code"]
        context.window_manager.clipboard = code
        self.report({'INFO'}, f"Code {code} copied to clipboard (24h, single-use, role={r['role']})")
        return {'FINISHED'}
