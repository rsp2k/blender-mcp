"""Command executor — assembled from per-domain handler mixins.

BlenderCommandExecutor is a thin class: it inherits handler methods from
nine sibling mixins (8 domains + shared helpers), then provides the
dispatch glue. The dispatch dict in `_execute_command_internal` is kept
exactly as it was in the pre-split monolith — Phase 5 of the refactor
swaps it for a decorator-based registry.

Phase 4 invariant: same 27 dispatchable commands, same gating against
the `blendermcp_use_*` Scene properties.
"""

from __future__ import annotations

import traceback

import bpy

from ._shared import SharedHelpersMixin
from .handlers.code_exec import CodeExecHandlersMixin
from .handlers.console import ConsoleHandlersMixin
from .handlers.hyper3d import Hyper3dHandlersMixin
from .handlers.msgbus import MsgbusHandlersMixin
from .handlers.polyhaven import PolyhavenHandlersMixin
from .handlers.scene import SceneHandlersMixin
from .handlers.sketchfab import SketchfabHandlersMixin
from .handlers.viewport import ViewportHandlersMixin


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

    The mixins provide all handler methods; `self.X()` resolution
    happens via MRO. Phase 4 preserves the original behavior bit-for-bit.
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
        """Internal command execution with proper context"""
        cmd_type = command.get("type")
        params = command.get("params", {})

        # Special-case handler for checking PolyHaven status (early-return path
        # preserved from the original — gates this command even when the toggle
        # is off, which the regular dispatch dict would also do).
        if cmd_type == "get_polyhaven_status":
            return {"status": "success", "result": self.get_polyhaven_status()}

        # Base handlers that are always available
        handlers = {
            "get_scene_info": self.get_scene_info,
            "get_object_info": self.get_object_info,
            "get_viewport_screenshot": self.get_viewport_screenshot,
            "execute_code": self.execute_code,
            "get_console_output": lambda p=params: self.get_console_output(
                level=p.get("level", "all"),
                page=p.get("page", 1),
                page_size=p.get("page_size", 50)
            ),
            "console_operations": lambda p=params: self.console_operations(
                operation=p.get("operation", "get_info"),
                params=p.get("params", None)
            ),
            "msgbus_clear_by_owner": lambda p=params: self.msgbus_clear_by_owner(
                owner_id=p.get("owner_id", "default")
            ),
            "msgbus_publish_rna": lambda p=params: self.msgbus_publish_rna(
                data_path=p.get("data_path", None),
                key=p.get("key", None)
            ),
            "msgbus_subscribe_rna": lambda p=params: self.msgbus_subscribe_rna(
                owner_id=p.get("owner_id", "default"),
                data_path=p.get("data_path"),
                notify_type=p.get("notify_type", "UPDATE"),
                persistent=p.get("persistent", True)
            ),
            "msgbus_get_notifications": lambda p=params: self.msgbus_get_notifications(
                owner_id=p.get("owner_id", None),
                clear=p.get("clear", False)
            ),
            "msgbus_list_subscriptions": lambda p=params: self.msgbus_list_subscriptions(
                owner_id=p.get("owner_id", None)
            ),
            "browse_data": lambda p=params: self.browse_data(
                collection=p.get("collection", None),
                item_name=p.get("item_name", None),
                page=p.get("page", 1),
                page_size=p.get("page_size", 50),
                detail_level=p.get("detail_level", "summary")
            ),
            "get_polyhaven_status": self.get_polyhaven_status,
            "get_hyper3d_status": self.get_hyper3d_status,
            "get_sketchfab_status": self.get_sketchfab_status,
        }

        # Add Polyhaven handlers only if enabled
        if bpy.context.scene.blendermcp_use_polyhaven:
            polyhaven_handlers = {
                "get_polyhaven_categories": self.get_polyhaven_categories,
                "search_polyhaven_assets": self.search_polyhaven_assets,
                "download_polyhaven_asset": self.download_polyhaven_asset,
                "set_texture": self.set_texture,
            }
            handlers.update(polyhaven_handlers)

        # Add Hyper3d handlers only if enabled
        if bpy.context.scene.blendermcp_use_hyper3d:
            hyper3d_handlers = {
                "create_rodin_job": self.create_rodin_job,
                "poll_rodin_job_status": self.poll_rodin_job_status,
                "import_generated_asset": self.import_generated_asset,
            }
            handlers.update(hyper3d_handlers)

        # Add Sketchfab handlers only if enabled
        if bpy.context.scene.blendermcp_use_sketchfab:
            sketchfab_handlers = {
                "search_sketchfab_models": self.search_sketchfab_models,
                "download_sketchfab_model": self.download_sketchfab_model,
            }
            handlers.update(sketchfab_handlers)

        handler = handlers.get(cmd_type)
        if handler:
            try:
                print(f"Executing handler for {cmd_type}")
                result = handler(**params)
                print("Handler execution complete")
                return {"status": "success", "result": result}
            except Exception as e:
                print(f"Error in handler: {str(e)}")
                traceback.print_exc()
                return {"status": "error", "message": str(e)}
        else:
            return {"status": "error", "message": f"Unknown command type: {cmd_type}"}


__all__ = ["BlenderCommandExecutor"]
