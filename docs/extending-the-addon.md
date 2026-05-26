---
title: "Add an addon command"
description: "Register a new dispatchable command on a handler mixin. One decorator, no central switch to edit."
---


The addon's command registry is decorator-based. Adding a command touches one file — the handler mixin where it belongs. No central dispatch dict to update.

## Where commands live

<FileTree>
- addon/
  - executor/
    - __init__.py             BlenderCommandExecutor (MRO assembly)
    - registry.py             @command decorator, COMMAND_REGISTRY
    - _shared.py              SharedHelpersMixin
    - handlers/
      - scene.py              SceneHandlersMixin
      - viewport.py           ViewportHandlersMixin
      - code_exec.py          CodeExecHandlersMixin
      - console.py            ConsoleHandlersMixin
      - msgbus.py             MsgbusHandlersMixin
      - polyhaven.py          PolyhavenHandlersMixin
      - hyper3d.py            Hyper3dHandlersMixin
      - sketchfab.py          SketchfabHandlersMixin
</FileTree>

Each mixin is plain Python. Methods decorated with `@command("name")` show up in `COMMAND_REGISTRY` at import time.

## Steps

<Steps>

1. **Pick a home.** A new viewport-related command goes on `ViewportHandlersMixin` in `addon/executor/handlers/viewport.py`. A brand-new domain (say, `compositor`) gets its own mixin in `addon/executor/handlers/compositor.py` and is added to the MRO in `addon/executor/__init__.py`.

2. **Write the method.**

   ```python
   # addon/executor/handlers/viewport.py
   from ..registry import command
   import bpy

   class ViewportHandlersMixin:
       @command("set_viewport_shading")
       def set_viewport_shading(self, shading="MATERIAL"):
           """Switch viewport shading mode (WIREFRAME, SOLID, MATERIAL, RENDERED)."""
           for area in bpy.context.screen.areas:
               if area.type == 'VIEW_3D':
                   for space in area.spaces:
                       if space.type == 'VIEW_3D':
                           space.shading.type = shading
                           return {"success": True, "shading": shading}
           return {"error": "No 3D viewport found"}
   ```

3. **Optionally gate it.** Commands tied to a preference (Poly Haven, Hyper3D, Sketchfab integrations) use the `gate=` argument. The gate receives the `AddonPreferences` singleton:

   ```python
   from ...preferences import get_prefs

   _viewport_extras_enabled = lambda prefs: prefs.use_polyhaven  # example

   @command("set_viewport_shading", gate=_viewport_extras_enabled)
   def set_viewport_shading(self, shading="MATERIAL"):
       ...
   ```

   A gated command that's currently disabled looks indistinguishable from an unknown command — both return `{"status": "error", "message": "Unknown command type: ..."}`.

4. **Dispatch it.** From any LLM client, send a job whose payload type is `job_dispatch` and whose `script` calls into Blender. The script runs in a namespace with `bpy`, `bmesh`, `mathutils`, and `executor` in scope — `executor` is the live `BlenderCommandExecutor` instance, so you can call your new method directly:

   ```python
   # llm-side
   await client.call_tool("blender_send_message", {
       "target_uuid": "blender-demo01",
       "from_uuid": "llm-mine",
       "priority": "info",
       "payload": {
           "message_type": "job_dispatch",
           "job_id": "job-xyz",
           "script": "result = executor.set_viewport_shading(shading='WIREFRAME'); print(result)",
       },
   })
   ```

5. **(Optional) call by command name.** If you'd rather not write a script, you can send a non-`job_dispatch` payload that the addon executor handles directly. The drainer ignores anything that isn't `job_dispatch`, so for command-style dispatch use the script wrapper above. The registry name is the contract LLM clients code against.

</Steps>

## What the decorator actually does

```python
# addon/executor/registry.py
COMMAND_REGISTRY: dict[str, CommandSpec] = {}

def command(name: str, *, gate=None):
    def decorator(fn):
        COMMAND_REGISTRY[name] = CommandSpec(name=name, func=fn, gate=gate)
        return fn
    return decorator
```

`CommandSpec` is a frozen dataclass with `(name, func, gate)`. The function is **unbound** at registration time; dispatch calls it as `spec.func(self, **filtered_params)` where `self` is the executor instance assembled from every mixin.

`filter_kwargs(func, params)` strips any keys from the incoming params dict that aren't named parameters of the function. Stray fields from the wire are silently dropped — mirrors the pre-decorator dispatch behavior, where each handler pulled the fields it cared about and ignored the rest.

## Shared helpers

`SharedHelpersMixin` (`addon/executor/_shared.py`) provides `_get_aabb(obj)`, `_clean_imported_glb(...)`, `_get_data_item_info(...)`. Mix it in last so MRO resolution finds these as `self._get_aabb(...)` from any other mixin.

## Reference

- [@command decorator](/reference/command-decorator/) — full registry/CommandSpec/filter_kwargs docs
- [Addon commands](/reference/addon-commands/) — every shipped command, grouped by domain
- [Addon preferences](/reference/addon-preferences/) — gate predicates read from here
