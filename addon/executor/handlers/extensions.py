"""Extension enumeration handler.

``list_installed_extensions`` walks the addon module registry and
returns everything Blender knows about — both legacy addons (module
name = the addon folder, e.g. ``addon``) and 4.2+ extensions (module
name = ``bl_ext.<repo>.<pkg_id>``).

Read-only, ships as a plain @command dispatched through the standard
command_dispatch path. The install / uninstall side lives elsewhere
(``extension_install_request`` in drainer.py) because it needs the
consent flow and can't be a synchronous command.
"""

from __future__ import annotations

import bpy

from ..registry import command


class ExtensionsHandlersMixin:
    """``list_installed_extensions`` command."""

    @command("list_installed_extensions")
    def list_installed_extensions(self, include_disabled: bool = True):
        """Enumerate installed addons + extensions.

        Returns::

            {
              "extensions": [
                {"module": "bl_ext.user_default.blender_mcp",
                 "name": "Blender MCP", "version": [1, 5, 20],
                 "is_extension": True, "enabled": True,
                 "repo": "user_default", "id": "blender_mcp"},
                ...
              ],
              "legacy_addons": [
                {"module": "addon", "name": "Blender MCP",
                 "version": [1, 5, 17], "is_extension": False,
                 "enabled": True},
                ...
              ],
              "counts": {"extensions": N, "legacy": M, "total": N+M}
            }

        Legacy addons are the pre-4.2 install-from-disk shape. Extensions
        are 4.2+ ``bl_ext.<repo>.<id>``. Both are enumerated because a
        user may have the addon installed both ways during migration.
        """
        import addon_utils

        enabled_modules = set(bpy.context.preferences.addons.keys())

        extensions: list[dict] = []
        legacy: list[dict] = []

        for mod in addon_utils.modules():
            module_name = mod.__name__
            info = getattr(mod, "bl_info", None) or {}
            enabled = module_name in enabled_modules
            if not include_disabled and not enabled:
                continue

            is_extension = module_name.startswith("bl_ext.")
            entry: dict = {
                "module": module_name,
                "name": info.get("name") or module_name,
                "version": list(info.get("version") or ()),
                "is_extension": is_extension,
                "enabled": enabled,
            }
            # Extension modules follow bl_ext.<repo>.<pkg_id> — split so
            # the caller can address them via install/uninstall without
            # re-parsing the module name themselves.
            if is_extension:
                parts = module_name.split(".", 2)
                if len(parts) >= 3:
                    entry["repo"] = parts[1]
                    entry["id"] = parts[2]
                extensions.append(entry)
            else:
                legacy.append(entry)

        return {
            "extensions": extensions,
            "legacy_addons": legacy,
            "counts": {
                "extensions": len(extensions),
                "legacy": len(legacy),
                "total": len(extensions) + len(legacy),
            },
        }
