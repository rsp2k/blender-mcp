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

Three settings stay on Scene/WindowManager because they're transient
per-session state, not per-user config — keeping them out of prefs:

  - blendermcp_server_running  (runtime: are we connected?)
  - blendermcp_client_id       (runtime: display the sticky UUID)
  - blendermcp_password_tmp    (transient: cleared after each login)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

import bpy
from bpy.props import BoolProperty, EnumProperty, StringProperty

if TYPE_CHECKING:
    pass


# Resolved at register-time — Blender hands us our own package name.
# Falls back to "addon" when imported outside the addon registration
# flow (e.g. unit tests).
ADDON_PACKAGE_NAME = __package__ or "addon"


class BlenderMCPPreferences(bpy.types.AddonPreferences):
    """User-scoped configuration for the BlenderMCP addon.

    Accessed via :func:`get_prefs` from anywhere in the addon code:

        prefs = get_prefs()
        if prefs.use_polyhaven:
            ...
    """

    bl_idname = ADDON_PACKAGE_NAME

    # --- Connection ---
    server_url: StringProperty(
        name="Server URL",
        description="BlenderMCP server's Streamable HTTP endpoint",
        default="http://localhost:8000/mcp",
    )
    username: StringProperty(
        name="Username",
        description="Account username for /auth/login. Persists across sessions.",
        default="",
    )
    jwt_token: StringProperty(
        name="JWT Token",
        description="Bearer token obtained via OAuth login. Populated by the Login operator.",
        subtype="PASSWORD",
        default="",
    )
    jwt_expires_at: StringProperty(
        name="JWT Expires At",
        description="ISO 8601 timestamp of when the access token expires",
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
        col = layout.column(align=True)
        col.label(text="Connection", icon='NETWORK_DRIVE')
        col.prop(self, "server_url")
        col.prop(self, "username")
        # JWT is shown but read-only-ish: clicking the field reveals it,
        # but the Login operator is the normal way to populate it.
        col.prop(self, "jwt_token")

        col.separator()
        col.label(text="Asset Integrations", icon='ASSET_MANAGER')
        col.prop(self, "use_polyhaven")
        col.prop(self, "use_hyper3d")
        if self.use_hyper3d:
            sub = col.column(align=True)
            sub.prop(self, "hyper3d_mode")
            sub.prop(self, "hyper3d_api_key")
        col.prop(self, "use_sketchfab")
        if self.use_sketchfab:
            col.prop(self, "sketchfab_api_key")


def get_prefs(context=None) -> "BlenderMCPPreferences":
    """Return the BlenderMCPPreferences instance for the current Blender session.

    Used by handlers (gating, API-key reads) and operators (writing JWT
    on login). Accepts an optional ``context``; defaults to
    ``bpy.context``.
    """
    if context is None:
        context = bpy.context
    return context.preferences.addons[ADDON_PACKAGE_NAME].preferences


# --- One-shot migration from legacy Scene properties --------------------------

# Mapping: AddonPreferences field name -> legacy Scene attribute name.
# Used by migrate_from_scene() on first register() after upgrade.
_LEGACY_SCENE_PROP_MAP = {
    "server_url": "blendermcp_server_url",
    "username": "blendermcp_username",
    "jwt_token": "blendermcp_jwt_token",
    "use_polyhaven": "blendermcp_use_polyhaven",
    "use_hyper3d": "blendermcp_use_hyper3d",
    "hyper3d_mode": "blendermcp_hyper3d_mode",
    "hyper3d_api_key": "blendermcp_hyper3d_api_key",
    "use_sketchfab": "blendermcp_use_sketchfab",
    "sketchfab_api_key": "blendermcp_sketchfab_api_key",
}

# Defaults the prefs ship with — if a legacy Scene value equals the default
# and the user never customized it, no need to "migrate" (just leave the
# prefs default in place). Also serves as the "is this a real user value?"
# check we use to decide whether to log a migration notice.
_PREFS_DEFAULTS = {
    "server_url": "http://localhost:8000/mcp",
    "username": "",
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
    nothing needed migrating. Callers (register()) can log a one-shot
    notice the first time something is actually moved.
    """
    prefs = get_prefs()
    migrated: list[str] = []

    for pref_field, scene_prop in _LEGACY_SCENE_PROP_MAP.items():
        if not hasattr(scene, scene_prop):
            continue
        legacy_value = getattr(scene, scene_prop)
        # Only migrate if the legacy value is "interesting" (differs from
        # the default) AND the current pref is still at its default. That
        # avoids overwriting a user's already-migrated prefs with the
        # default value from a fresh scene.
        if legacy_value == _PREFS_DEFAULTS.get(pref_field):
            continue
        if getattr(prefs, pref_field) != _PREFS_DEFAULTS.get(pref_field):
            continue
        setattr(prefs, pref_field, legacy_value)
        migrated.append(pref_field)

    return migrated or None
