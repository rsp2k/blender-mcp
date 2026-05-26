---
title: "Addon package tour"
description: "Why the addon is a package, not a single file. Walk through auth/, client/, executor/, ui/ — what each does and why the split."
---


The addon used to be one ~2000-line `addon.py`. Phases 4, 6, 8, and 9 refactored it into a proper package. This page walks the layout and explains why each split exists.

## Where things live

<FileTree>
- addon/
  - __init__.py            bl_info, register/unregister, Scene-to-prefs migration
  - _version.py            version tuple (must match bl_info)
  - _compat.py             cross-Blender-version helpers
  - constants.py           protocol + REQ_HEADERS + free-trial keys
  - identity.py            sticky UUID generation
  - preferences.py         AddonPreferences class
  - state.py               _client, _executor singletons (no module-globals)
  - auth/
    - __init__.py
    - login.py             pure-requests OAuth client
  - client/
    - __init__.py          constants, public re-exports
    - bus_client.py        BlenderMCPClient (lifecycle + asyncio loop)
    - message_pump.py      incoming notification filter
    - drainer.py           main-thread timer, priority-queue drain
    - job_reporter.py      blender_job_update reply
  - executor/
    - __init__.py          BlenderCommandExecutor (MRO assembly)
    - registry.py          @command + CommandSpec + filter_kwargs
    - _shared.py           SharedHelpersMixin
    - handlers/
      - scene.py
      - viewport.py
      - code_exec.py
      - console.py
      - msgbus.py
      - polyhaven.py
      - hyper3d.py
      - sketchfab.py
  - ui/
    - __init__.py          CLASSES tuple for register order
    - panel.py             View3D sidebar
    - operators.py         Login, Start/Stop operators
</FileTree>

## Why split it at all?

A single 2k-line file works for an addon that does one thing. The post-bus design has at least four orthogonal concerns:

1. **Auth** — talks to the server over HTTP, no `bpy` needed
2. **Transport** — asyncio + FastMCP client, also no `bpy` needed
3. **Execution** — Blender main-thread script runner, hard `bpy` dep
4. **UI** — panels and operators, hard `bpy.types` dep

Keeping all four in one file meant any import dragged in everything. Tests for the auth flow had to fake `bpy`. Tests for the dispatch logic had to spin up an asyncio loop. The cyclomatic complexity of `register()` was scary.

The package layout means **`from addon.auth import login` is bpy-free**. Tests can import the auth module without faking the Blender environment. Same for the transport-layer modules — `message_pump`, `drainer`, `job_reporter` all run against a stub client object.

## auth/

One module: `login.py`. Pure `requests`. Two functions: `login(server_url, username, password)` and `refresh_token(server_url, refresh)`. Both raise `LoginError` (with `.status_code`) on non-2xx.

The `_auth_base()` helper strips trailing `/mcp` from the server URL — the panel stores the MCP endpoint, but `/auth/*` is at the FastAPI root.

**Why split**: callable from anywhere — operators, integration tests, ad-hoc scripts. No coupling to Blender.

## client/

Four modules:

- **bus_client.py** — `BlenderMCPClient` class. Holds connection state (URL, JWT, FastMCP client, asyncio loop, priority queue) and orchestrates startup/shutdown. Its `_on_message` and `_drain_queue` are thin shims that delegate to the other three modules.
- **message_pump.py** — `handle_message(client, message)`. Filters MCP notifications down to `_message_bus` log records, decodes `data`, pushes onto the priority heap.
- **drainer.py** — `drain_queue(client)`. Blender main-thread timer callback. Pops the highest-priority entry, validates `target_uuid` and `message_type`, calls `execute_script`.
- **job_reporter.py** — `submit_job_update(client, job_id, status, result, error)`. Marshals a `blender_job_update` tool call onto the asyncio loop from the main thread.

**Why split**: each module is independently testable with a stub `BlenderMCPClient`. The drainer is the hottest path; isolating it from connection lifecycle made the timer logic far easier to reason about. `message_pump` and `job_reporter` are pure functions over an injected `client`.

The cross-imports are TYPE_CHECKING-only to avoid an import cycle:

```python
# message_pump.py
if TYPE_CHECKING:
    from .bus_client import BlenderMCPClient
```

## executor/

The command executor. `BlenderCommandExecutor` is assembled from eight handler mixins plus `SharedHelpersMixin` via Python's MRO:

```python
# executor/__init__.py
class BlenderCommandExecutor(
    SceneHandlersMixin,
    ViewportHandlersMixin,
    CodeExecHandlersMixin,
    ConsoleHandlersMixin,
    MsgbusHandlersMixin,
    PolyhavenHandlersMixin,
    Hyper3dHandlersMixin,
    SketchfabHandlersMixin,
    SharedHelpersMixin,
):
    def _execute_command_internal(self, command):
        spec = COMMAND_REGISTRY.get(command["type"])
        if spec is None or (spec.gate and not spec.gate(get_prefs())):
            return {"status": "error", "message": f"Unknown command type: ..."}
        accepted = filter_kwargs(spec.func, command.get("params", {}))
        return {"status": "success", "result": spec.func(self, **accepted)}
```

Dispatch is a single dict lookup plus a gate check. **No central `if/elif` chain.** Adding a command touches only the file where the command belongs.

**Why split this way**: each domain (scene, viewport, msgbus, polyhaven, etc.) is independently editable. The `@command` decorator's registry-on-import behavior means file-level imports are the registration step — no separate `register_commands()` call to keep in sync.

The decorator-based registry's payoff is concrete: a new command requires touching exactly one file (the relevant mixin). Previously, a new command required editing both the handler method AND the central dispatch dict — which is exactly the kind of two-place-edit that's easy to forget on a long-running PR.

See [@command decorator](/reference/command-decorator/) for the mechanism.

## ui/

`panel.py` and `operators.py`. Both import `bpy` heavily. `__init__.py` exposes a `CLASSES` tuple for register order.

**Why split from the rest**: the UI is the only part of the addon that has hard `bpy.types.Operator` and `bpy.types.Panel` references at module load. Keeping them in their own subpackage means the rest of the addon is importable in test contexts.

## state.py

```python
# addon/state.py
_client: Optional["BlenderMCPClient"] = None
_executor: Optional["BlenderCommandExecutor"] = None
```

Two singletons. Lives in its own module so callers can `from addon import state; state._client = ...` without worrying about which file owns them. Phase 6 motivation: previously these were globals in `addon.py`; moving them into a named module made the dependency explicit.

<Aside type="note">
"Global variables are a code smell." These two are the unavoidable singletons of a Blender addon — there's exactly one bus client and one executor per addon instance per Blender process. Putting them in `state.py` is the compromise: still globals, but visibly so, and accessible via a single dotted path.
</Aside>

## __init__.py — the entry point

`register()` and `unregister()` are Blender's contract. Both do bpy imports **lazily** so the package can be imported in non-Blender contexts:

```python
def register():
    import bpy
    from . import state
    from .client.bus_client import FASTMCP_AVAILABLE
    from .preferences import BlenderMCPPreferences, migrate_from_scene
    from .ui import CLASSES as _CLASSES
    ...
```

The migration from legacy Scene properties (Phase 8) runs here, on first register after upgrade. Legacy props are removed after migration so they don't ride along in newly-saved `.blend` files.

## The single-file fallback

`addon.py` at the repo root still exists — about 30 lines that import from `addon/` and re-export `bl_info`, `register`, `unregister`. Blender's "Install Add-on" single-file path keeps working. Users who prefer the directory layout zip up `addon/` and install that.

## Related

- [@command decorator](/reference/command-decorator/) — the registry mechanism
- [Addon preferences](/reference/addon-preferences/) — Phase 8's headline change
- [Architecture](/explanation/architecture/) — where the addon sits in the system
- [Add an addon command](/how-to/add-an-addon-command/) — the practical payoff of the layout
