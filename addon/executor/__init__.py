"""Command executor — assembled from per-domain handler mixins.

Phase 5 (current): handler methods register themselves via ``@command()``
into :data:`addon.executor.registry.COMMAND_REGISTRY`. The dispatch
in ``_execute_command_internal`` is now a single registry lookup + gate
evaluation + call. Adding a new command requires only decorating a
method — no central switch to edit.

Phase 4 invariant: same 24 dispatchable commands, same gating against
the ``blendermcp_use_*`` Scene properties.
"""

from __future__ import annotations

import traceback

from ..preferences import get_prefs
from ._shared import SharedHelpersMixin
from .handlers.code_exec import CodeExecHandlersMixin
from .handlers.console import ConsoleHandlersMixin
from .handlers.hyper3d import Hyper3dHandlersMixin
from .handlers.msgbus import MsgbusHandlersMixin
from .handlers.polyhaven import PolyhavenHandlersMixin
from .handlers.scene import SceneHandlersMixin
from .handlers.sketchfab import SketchfabHandlersMixin
from .handlers.viewport import ViewportHandlersMixin
from .registry import COMMAND_REGISTRY, filter_kwargs


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
    """Routes incoming `{type, params}` commands to handler methods.

    Mixins provide all handler methods; `self.X()` resolution happens via
    MRO. Dispatch consults the decorator registry rather than a static
    dict — the set of available commands is the union of every method
    that opted in with ``@command(...)``.
    """

    def execute_command(self, command):
        """Execute a command in the main Blender thread"""
        try:
            return self._execute_command_internal(command)
        except Exception as e:
            print(f"Error executing command: {str(e)}")
            traceback.print_exc()
            return {"status": "error", "message": str(e)}

    def _execute_command_internal(self, command):
        """Internal command execution with proper context."""
        cmd_type = command.get("type")
        params = command.get("params", {})

        spec = COMMAND_REGISTRY.get(cmd_type)

        # Unknown command, OR known but gated off — both produce the same
        # response shape as the pre-Phase-5 dispatch dict (where a gated
        # command simply wasn't in the dict, so lookup fell through to
        # "Unknown command type"). Gates receive AddonPreferences (Phase 8);
        # before that they received bpy.context.scene.
        if spec is None or (
            spec.gate is not None and not spec.gate(get_prefs())
        ):
            return {"status": "error", "message": f"Unknown command type: {cmd_type}"}

        try:
            print(f"Executing handler for {cmd_type}")
            accepted = filter_kwargs(spec.func, params)
            result = spec.func(self, **accepted)
            print("Handler execution complete")
            return {"status": "success", "result": result}
        except Exception as e:
            print(f"Error in handler: {str(e)}")
            traceback.print_exc()
            return {"status": "error", "message": str(e)}


__all__ = ["BlenderCommandExecutor"]
