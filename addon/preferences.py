"""AddonPreferences — replaces the old Scene-property settings.

Why this matters: secrets (JWT, API keys) used to live as
``bpy.types.Scene`` properties. That meant every saved ``.blend`` file
shipped the user's tokens with it — a real leak through file-sharing,
git, bug reports, anything. AddonPreferences live in ``userpref.blend``
(in Blender's per-user config dir), never in the scene file.

One-shot migration from the legacy Scene properties runs from
``register()`` in addon/__init__.py (or addon.py during phases 7–8) on
first load after upgrade. After copying values into prefs, the old
Scene props are deleted; subsequent reads come from prefs only.

Two settings stay on Scene/WindowManager because they're transient
per-session state, not per-user config — keeping them out of prefs:

  - blendermcp_server_running  (runtime: are we connected?)
  - blendermcp_client_id       (runtime: display the sticky UUID)

Username/password fields and ``blendermcp_password_tmp`` (1.4.0) were
removed when the addon went OAuth-only — see ``oauth_login`` operator.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

import bpy
from bpy.props import BoolProperty, EnumProperty, StringProperty

if TYPE_CHECKING:
    pass


ADDON_PACKAGE_NAME = __package__ or "addon"

# Default server is the production deploy — fresh installs land on a
# working URL. Stored as a bare hostname; ``get_server_base_url`` adds
# the https:// scheme at use sites.
DEFAULT_SERVER_HOSTNAME = "mcp.blender.bet"


def get_server_base_url(prefs: Optional["BlenderMCPPreferences"] = None) -> str:
    """Return a canonical full URL ``https://<hostname>`` from prefs.server_url.

    The stored ``server_url`` is conceptually a hostname (``mcp.blender.bet``)
    — the addon prefs UI shows ``https://`` as a label prefix on the input,
    so users don't type the scheme. This helper builds the full URL at use
    sites and is defensive about three legacy/edge cases:

    - User pasted a full URL (``https://mcp.blender.bet/``) — scheme + path
      stripped, canonical form returned.
    - Old stored value with trailing ``/mcp`` (pre-1.4.0 layout) — stripped.
    - localhost/127.0.0.1 — returned with ``http://`` since local dev
      typically doesn't have TLS.
    """
    if prefs is None:
        prefs = get_prefs()
    raw = (prefs.server_url or DEFAULT_SERVER_HOSTNAME).strip()

    # If the user pasted a scheme, honor it (handy for ``http://localhost:8000``).
    if raw.startswith(("https://", "http://")):
        scheme, host = raw.split("://", 1)
    else:
        host = raw
        # Localhost defaults to http; anything else to https.
        scheme = "http" if host.startswith(("localhost", "127.0.0.1")) else "https"

    # Strip trailing slash + legacy /mcp path suffix.
    host = host.rstrip("/")
    if host.endswith("/mcp"):
        host = host[: -len("/mcp")]

    return f"{scheme}://{host}"


def _draw_login_section(self, layout):
    """Login / Logout UI block — used by both prefs panel and (optionally) sidebar."""
    has_jwt = bool(self.jwt_token)
    is_oauth = bool(self.oauth_client_id)

    box = layout.box()
    if has_jwt:
        method = "OAuth (browser)" if is_oauth else "password (legacy)"
        row = box.row(align=True)
        row.label(text=f"Logged in via {method}", icon='CHECKMARK')
        row.operator("blendermcp.logout", text="Logout", icon='UNLOCKED')
    else:
        col = box.column(align=True)
        col.label(text="Not logged in", icon='LOCKED')
        col.operator(
            "blendermcp.oauth_login",
            text="Login with OAuth (browser)",
            icon='URL',
        )


class BlenderMCPPreferences(bpy.types.AddonPreferences):
    """User-scoped configuration for the BlenderMCP addon."""

    bl_idname = ADDON_PACKAGE_NAME

    # --- Connection ---
    # Stored hostname-only ("mcp.blender.bet"). Prefs UI renders "https://"
    # as a label prefix. ``get_server_base_url()`` builds the full URL at
    # use sites + tolerates pasted full URLs for backwards compat.
    server_url: StringProperty(
        name="Server",
        description=(
            "BlenderMCP server hostname (e.g. mcp.blender.bet). The "
            "addon prepends https:// automatically. Paste a full URL if "
            "you need an explicit scheme (e.g. http://localhost:8000)."
        ),
        default=DEFAULT_SERVER_HOSTNAME,
    )
    jwt_token: StringProperty(
        name="JWT Token",
        description="Bearer token obtained via OAuth login. Populated by the Login operator.",
        subtype="PASSWORD",
        default="",
    )
    refresh_token: StringProperty(
        name="Refresh Token",
        description=(
            "Long-lived refresh token from the OAuth /token endpoint. "
            "Used by the bus client to mint new access tokens before "
            "the current JWT expires, so the session doesn't wedge mid-edit."
        ),
        subtype="PASSWORD",
        default="",
    )
    oauth_client_id: StringProperty(
        name="OAuth Client ID",
        description=(
            "Dynamic Client Registration ID this addon instance received "
            "from the MCP server's /register endpoint. Needed for the "
            "refresh grant flow. Populated by the 'Login with OAuth' operator."
        ),
        default="",
    )
    jwt_expires_at: StringProperty(
        name="JWT Expires At",
        description=(
            "Unix epoch (seconds) when the current access token expires. "
            "The bus client watches this and refreshes ~60s before."
        ),
        default="",
    )

    # --- Asset integrations ---
    use_polyhaven: BoolProperty(
        name="Use Poly Haven",
        description="Enable Poly Haven asset integration",
        default=False,
    )
    use_hyper3d: BoolProperty(
        name="Use Hyper3D Rodin",
        description="Enable Hyper3D Rodin generation integration",
        default=False,
    )
    hyper3d_mode: EnumProperty(
        name="Rodin Mode",
        description="Choose the platform used to call Rodin APIs",
        items=[
            ("MAIN_SITE", "hyper3d.ai", "hyper3d.ai"),
            ("FAL_AI", "fal.ai", "fal.ai"),
        ],
        default="MAIN_SITE",
    )
    hyper3d_api_key: StringProperty(
        name="Hyper3D API Key",
        description="API Key provided by Hyper3D",
        subtype="PASSWORD",
        default="",
    )
    use_sketchfab: BoolProperty(
        name="Use Sketchfab",
        description="Enable Sketchfab asset integration",
        default=False,
    )
    sketchfab_api_key: StringProperty(
        name="Sketchfab API Key",
        description="API Key provided by Sketchfab",
        subtype="PASSWORD",
        default="",
    )

    def draw(self, context):
        """Draw the prefs panel in Edit > Preferences > Add-ons > BlenderMCP."""
        layout = self.layout

        # --- Connection ---
        col = layout.column(align=True)
        col.label(text="Connection", icon='NETWORK_DRIVE')
        # "https://" label prefix + hostname-only field + Test Connection
        # button. The label gives users the visual cue that scheme is implied
        # without making them type or store it.
        row = col.row(align=True)
        row.label(text="https://")
        row.prop(self, "server_url", text="")
        row.operator("blendermcp.test_connection", text="", icon='URL')

        # --- Login / Logout ---
        col.separator()
        _draw_login_section(self, layout)

        # --- Asset integrations ---
        layout.separator()
        col = layout.column(align=True)
        col.label(text="Asset Integrations", icon='ASSET_MANAGER')
        col.prop(self, "use_polyhaven")
        col.prop(self, "use_hyper3d")
        if self.use_hyper3d:
            sub = col.column(align=True)
            sub.prop(self, "hyper3d_mode")
            sub.prop(self, "hyper3d_api_key")
            sub.operator(
                "blendermcp.set_hyper3d_free_trial_api_key",
                text="Use Free Trial Key",
            )
        col.prop(self, "use_sketchfab")
        if self.use_sketchfab:
            col.prop(self, "sketchfab_api_key")


def get_prefs(context=None) -> "BlenderMCPPreferences":
    """Return the BlenderMCPPreferences instance for the current Blender session."""
    if context is None:
        context = bpy.context
    return context.preferences.addons[ADDON_PACKAGE_NAME].preferences


# --- One-shot migration from legacy Scene properties --------------------------

_LEGACY_SCENE_PROP_MAP = {
    "server_url": "blendermcp_server_url",
    "jwt_token": "blendermcp_jwt_token",
    "use_polyhaven": "blendermcp_use_polyhaven",
    "use_hyper3d": "blendermcp_use_hyper3d",
    "hyper3d_mode": "blendermcp_hyper3d_mode",
    "hyper3d_api_key": "blendermcp_hyper3d_api_key",
    "use_sketchfab": "blendermcp_use_sketchfab",
    "sketchfab_api_key": "blendermcp_sketchfab_api_key",
}

_PREFS_DEFAULTS = {
    "server_url": DEFAULT_SERVER_HOSTNAME,
    "jwt_token": "",
    "use_polyhaven": False,
    "use_hyper3d": False,
    "hyper3d_mode": "MAIN_SITE",
    "hyper3d_api_key": "",
    "use_sketchfab": False,
    "sketchfab_api_key": "",
}


def migrate_from_scene(scene) -> Optional[list[str]]:
    """Copy legacy ``scene.blendermcp_*`` values into AddonPreferences.

    Idempotent: if a pref already has a non-default value, the existing
    pref is kept (we're loading a .blend that has scene props but the
    user's prefs are already migrated).

    Returns the list of pref field names that were updated, or None if
    nothing needed migrating.
    """
    prefs = get_prefs()
    migrated: list[str] = []

    for pref_field, scene_prop in _LEGACY_SCENE_PROP_MAP.items():
        if not hasattr(scene, scene_prop):
            continue
        legacy_value = getattr(scene, scene_prop)
        if legacy_value == _PREFS_DEFAULTS.get(pref_field):
            continue
        if getattr(prefs, pref_field) != _PREFS_DEFAULTS.get(pref_field):
            continue
        setattr(prefs, pref_field, legacy_value)
        migrated.append(pref_field)

    return migrated or None
