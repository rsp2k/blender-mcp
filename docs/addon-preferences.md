---
title: "Addon preferences"
description: "BlenderMCPPreferences fields: connection, asset integrations. Migration from pre-Phase-8 Scene properties."
---


`BlenderMCPPreferences` is an `AddonPreferences` subclass that holds all per-user addon config. Lives in `addon/preferences.py`. Accessed everywhere via `get_prefs()`.

`AddonPreferences` are stored in Blender's `userpref.blend` (in the per-user config dir), **not** in scene files. This is the headline distinction from the pre-Phase-8 layout, where secrets sat on `bpy.types.Scene` and shipped with every `.blend` file someone shared.

## Fields

### Connection

| Field | Type | Default | Notes |
|---|---|---|---|
| `server_url` | `StringProperty` | `http://localhost:8000/mcp` | The Streamable HTTP endpoint. `/auth/login` is derived by stripping `/mcp`. |
| `username` | `StringProperty` | `""` | Persists across sessions; used by the Login operator. |
| `jwt_token` | `StringProperty` (PASSWORD) | `""` | Populated by the Login operator. Bearer token for MCP requests. |
| `jwt_expires_at` | `StringProperty` | `""` | ISO 8601 timestamp of access-token expiry. |

### Asset integrations

| Field | Type | Default | Notes |
|---|---|---|---|
| `use_polyhaven` | `BoolProperty` | `False` | Enables Poly Haven commands. |
| `use_hyper3d` | `BoolProperty` | `False` | Enables Hyper3D Rodin commands. |
| `hyper3d_mode` | `EnumProperty` | `MAIN_SITE` | One of `MAIN_SITE` (hyperhuman.deemos.com) or `FAL_AI` (fal.ai). |
| `hyper3d_api_key` | `StringProperty` (PASSWORD) | `""` | Free trial key is shipped as a constant. |
| `use_sketchfab` | `BoolProperty` | `False` | Enables Sketchfab commands. |
| `sketchfab_api_key` | `StringProperty` (PASSWORD) | `""` | Sketchfab v3 API token. |

## Reading prefs

```python
from addon.preferences import get_prefs

prefs = get_prefs()
if prefs.use_polyhaven:
    api_key = prefs.hyper3d_api_key
    ...
```

`get_prefs(context=None)` defaults to `bpy.context`. Pass an explicit context when running inside an operator or panel where the active context might differ.

## Transient state — what stays on Scene

Three properties remain on `bpy.types.Scene`/`WindowManager` because they're per-session state, not per-user config:

| Property | Purpose |
|---|---|
| `blendermcp_server_running` | Is the bus client connected right now? |
| `blendermcp_client_id` | The sticky UUID for display in the panel. |
| `blendermcp_password_tmp` | In-flight password before Login runs; cleared on success. |

These get cleaned out on `unregister()` so they never leak into saved `.blend` files.

## Migration from legacy Scene properties

Pre-Phase-8 installs stored everything on Scene. A one-shot migration runs in `register()` on first load after upgrade:

```python
# addon/preferences.py
_LEGACY_SCENE_PROP_MAP = {
    "server_url":       "blendermcp_server_url",
    "username":         "blendermcp_username",
    "jwt_token":        "blendermcp_jwt_token",
    "use_polyhaven":    "blendermcp_use_polyhaven",
    "use_hyper3d":      "blendermcp_use_hyper3d",
    "hyper3d_mode":     "blendermcp_hyper3d_mode",
    "hyper3d_api_key":  "blendermcp_hyper3d_api_key",
    "use_sketchfab":    "blendermcp_use_sketchfab",
    "sketchfab_api_key":"blendermcp_sketchfab_api_key",
}
```

`migrate_from_scene(scene)` copies legacy values into prefs only when:

1. The legacy value differs from the pref's default (so we don't migrate placeholders), **and**
2. The pref is currently at its default (so we don't overwrite an already-migrated user value).

After migration, `register()` deletes the legacy Scene properties so they stop riding along in newly-saved `.blend` files.

<Aside type="caution">
A `.blend` saved before the migration still contains the legacy properties. Opening it after upgrade triggers the migration on the in-memory scene. **Save the file** to scrub them from disk — otherwise the next person to open the file still gets the old JWT token in the Scene properties.
</Aside>

## Drawing the prefs UI

`BlenderMCPPreferences.draw()` renders a compact panel in Edit > Preferences > Add-ons > BlenderMCP:

- **Connection**: server URL, username, JWT (revealed on click). Next to the server URL field is a **Test Connection** button (URL icon) that probes the configured server before you bother with login.
- **Asset Integrations**: a checkbox per integration; Hyper3D and Sketchfab reveal mode/key fields when enabled.

## Test Connection button

`BLENDERMCP_OT_TestConnection` (`blendermcp.test_connection` operator) does a quick smoke test against the configured `server_url` before the user clicks Login. Useful for catching wrong URLs, missing trailing slashes, expired certs, and unreachable hosts without producing a "wrong credentials?" red herring from the login flow.

**What it does:**

```python
GET {server_url stripped of /mcp}/health   # 3s timeout
```

**What it reports** (via `self.report(...)` in Blender's operator log):

| Outcome | Message |
|---|---|
| 200 + healthy JSON | `OK, server healthy, {buses} bus(es) active` (green INFO) |
| HTTP 4xx/5xx | `FAILED: HTTP {code} from {url}` |
| Timeout (>3s) | `Timeout (>3s) hitting {url}` |
| TLS failure | `TLS error: {detail}` |
| Connection refused / DNS failure | `Cannot reach {url}: {detail}` |
| Empty URL | `Server URL is empty` |

The 3-second timeout is deliberate — Blender's preferences panel freezes during operator execution, so a stuck network call would freeze the UI. Fast failure beats hung UI.

## Reference

- [@command decorator](/reference/command-decorator/) — gates read `AddonPreferences`
- [Architecture](/explanation/architecture/) — where prefs sit in the addon package
- [Addon package tour](/explanation/addon-package-tour/) — Phase 8 motivation in context
