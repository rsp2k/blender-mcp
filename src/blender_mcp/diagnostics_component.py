"""Diagnostics and installation MCP component.

Exposes the InstallationManager's detection and auto-install logic over MCP
without requiring authentication. This is the entry point for clients on
machines that don't have Blender (or the addon) set up yet — the message bus
is gated behind OAuth and can't help an empty-handed caller, but these tools
can.

Follows the mandatory MCPMixin pattern (see CLAUDE.md).
"""

import json
from typing import Optional

from fastmcp.contrib.mcp_mixin import MCPMixin, mcp_tool

from .installation_manager import get_installation_manager


class BlenderDiagnosticsComponent(MCPMixin):
    """Unauthenticated diagnostics and install helpers.

    Every tool returns a JSON-encoded string so output is uniform with the
    bus tools. Errors are surfaced as `{"status": "error", "error": "..."}`
    rather than raised exceptions — these tools are meant to be safe to call
    from an MCP client that has no idea what state the host machine is in.
    """

    def __init__(self):
        self.manager = get_installation_manager()

    @mcp_tool()
    def check_status(self) -> str:
        """Run a complete environment diagnosis: is Blender installed, is the
        addon present, is it enabled, is anything running. Returns a structured
        report plus the action a client should take next.
        """
        try:
            diagnosis = self.manager.diagnose_connection_issue()
            instructions = self.manager.get_setup_instructions(diagnosis)
            return json.dumps({
                "status": "ok",
                "diagnosis": diagnosis,
                "instructions": instructions,
            })
        except Exception as e:
            return json.dumps({"status": "error", "error": str(e)})

    @mcp_tool()
    def find_blender(self) -> str:
        """Locate the Blender executable on this machine. Returns the path or
        a structured 'not found' response listing the locations that were
        searched, so the caller can suggest where to install."""
        try:
            path = self.manager.find_blender_executable()
            if path:
                return json.dumps({"status": "ok", "found": True, "path": path})
            return json.dumps({
                "status": "ok",
                "found": False,
                "searched": self.manager._get_common_blender_paths(),
                "hint": "Install Blender 3.0+ from https://www.blender.org/download/",
            })
        except Exception as e:
            return json.dumps({"status": "error", "error": str(e)})

    @mcp_tool()
    def list_running_blender(self) -> str:
        """List Blender processes currently running (GUI and headless)."""
        try:
            instances = self.manager.check_running_blender_instances()
            return json.dumps({
                "status": "ok",
                "running": instances,
                "count": len(instances),
                "gui_count": sum(1 for p in instances if p.get("is_gui")),
            })
        except Exception as e:
            return json.dumps({"status": "error", "error": str(e)})

    @mcp_tool()
    def check_addon_installed(self, blender_path: Optional[str] = None) -> str:
        """Check whether the BlenderMCP addon is installed in the given
        Blender install. If `blender_path` is omitted, auto-discovers."""
        try:
            path = blender_path or self.manager.find_blender_executable()
            if not path:
                return json.dumps({
                    "status": "error",
                    "error": "blender_not_found",
                    "hint": "Pass blender_path explicitly or install Blender first.",
                })
            installed, msg = self.manager.check_addon_installed(path)
            return json.dumps({
                "status": "ok",
                "installed": installed,
                "blender_path": path,
                "detail": msg,
            })
        except Exception as e:
            return json.dumps({"status": "error", "error": str(e)})

    @mcp_tool()
    def install_addon(self, blender_path: Optional[str] = None) -> str:
        """Install the bundled BlenderMCP addon into the discovered (or
        provided) Blender install. Will fail safely with a structured error
        if Blender isn't found — does not attempt to install Blender itself."""
        try:
            path = blender_path or self.manager.find_blender_executable()
            if not path:
                return json.dumps({
                    "status": "error",
                    "error": "blender_not_found",
                    "hint": "Install Blender 3.0+ first (https://www.blender.org/download/), then re-run install_addon.",
                })
            success, msg = self.manager.install_addon_automatically(path)
            return json.dumps({
                "status": "ok" if success else "error",
                "installed": success,
                "blender_path": path,
                "detail": msg,
            })
        except Exception as e:
            return json.dumps({"status": "error", "error": str(e)})
