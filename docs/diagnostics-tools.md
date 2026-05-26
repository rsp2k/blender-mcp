---
title: "Diagnostics tools"
description: "Five blender_* tools for environment diagnosis and addon install. Available on stdio and HTTP, no auth required."
---


Five tools for bootstrapping a host that doesn't have Blender or the addon set up yet. The bus tools can't help an empty-handed caller — they need an OAuth session that doesn't exist yet. These tools are deliberately **unauthenticated** and appear on **both** the stdio and HTTP builds.

Every tool returns a JSON-encoded string. Failures surface as `{"status":"error","error":"..."}` rather than raised exceptions.

## blender_check_status

Full environment diagnosis plus the action the caller should take next. The single highest-value diagnostics tool — call it first when something isn't connecting.

No parameters.

**Returns:**

```json
{
  "status": "ok",
  "diagnosis": {
    "blender_installed": true,
    "blender_path": "/usr/bin/blender",
    "addon_installed": false,
    "addon_enabled": false,
    "running_instances": [],
    ...
  },
  "instructions": "Install the addon: call blender_install_addon, then enable it from Edit > Preferences > Add-ons."
}
```

## blender_find_blender

Locate the Blender executable on the host. Searches PATH plus per-OS conventional install locations.

No parameters.

**Returns (found):**

```json
{"status":"ok","found":true,"path":"/Applications/Blender.app/Contents/MacOS/Blender"}
```

**Returns (not found):**

```json
{
  "status": "ok",
  "found": false,
  "searched": ["/usr/bin/blender", "/Applications/Blender.app/...", ...],
  "hint": "Install Blender 3.0+ from https://www.blender.org/download/"
}
```

## blender_list_running_blender

Enumerate Blender processes currently running on the host. Distinguishes GUI from headless.

No parameters.

**Returns:**

```json
{
  "status": "ok",
  "running": [
    {"pid": 12345, "is_gui": true, "command": "blender", "started_at": ...},
    {"pid": 67890, "is_gui": false, "command": "blender --background script.py", ...}
  ],
  "count": 2,
  "gui_count": 1
}
```

## blender_check_addon_installed

Verify the BlenderMCP addon is present in a Blender install.

| Parameter | Type | Default | Notes |
|---|---|---|---|
| `blender_path` | `str | null` | `null` | If omitted, auto-discovers via `find_blender`. |

**Returns:**

```json
{
  "status": "ok",
  "installed": true,
  "blender_path": "/usr/bin/blender",
  "detail": "Addon found at /home/user/.config/blender/4.0/scripts/addons/blender_mcp"
}
```

If Blender can't be found at all, returns `{"status":"error","error":"blender_not_found", "hint":"..."}`.

## blender_install_addon

Install the bundled BlenderMCP addon into the discovered (or provided) Blender install. Will not attempt to install Blender itself.

| Parameter | Type | Default | Notes |
|---|---|---|---|
| `blender_path` | `str | null` | `null` | If omitted, auto-discovers via `find_blender`. |

**Returns (success):**

```json
{
  "status": "ok",
  "installed": true,
  "blender_path": "/usr/bin/blender",
  "detail": "Installed to /home/user/.config/blender/4.0/scripts/addons/"
}
```

**Returns (Blender not found):**

```json
{
  "status": "error",
  "error": "blender_not_found",
  "hint": "Install Blender 3.0+ first (https://www.blender.org/download/), then re-run install_addon."
}
```

<Aside type="note">
Installation copies the addon files into the Blender user scripts directory. The user still has to enable the addon from Blender's Add-ons preferences UI (or restart Blender for it to appear) — the install step alone doesn't run register().
</Aside>

## Why these aren't behind OAuth

Diagnosing a broken setup needs to work before the caller has a way to authenticate. A user installing BlenderMCP for the first time has no credentials yet; their first MCP call is going to be `check_status`. Gating that behind auth would make the very first failure case unreachable.

This is also why both builders register the diagnostics component:

```python
# server_proper.py
def build_stdio_mcp() -> FastMCP:
    server = FastMCP("BlenderMCP")
    BlenderDiagnosticsComponent().register_all(mcp_server=server, prefix="blender")
    return server

def build_http_mcp() -> FastMCP:
    server = FastMCP("BlenderMCP")
    BlenderDiagnosticsComponent().register_all(mcp_server=server, prefix="blender")
    BlenderBusComponent().register_all(mcp_server=server, prefix="blender")
    return server
```

The HTTP build adds the bus tools on top; the stdio build doesn't, because there's no identity to carry over stdio.
