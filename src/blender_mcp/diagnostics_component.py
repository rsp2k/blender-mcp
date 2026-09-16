"""Diagnostics and installation MCP component.

Exposes the InstallationManager's detection and auto-install logic over MCP
without requiring authentication. This is the entry point for clients on
machines that don't have Blender (or the addon) set up yet — the message bus
is gated behind OAuth and can't help an empty-handed caller, but these tools
can.

**Server-local scope, not addon-connected.** These tools inspect the
filesystem and process table of the machine running the MCP server. On a
hosted bus (mcp.blender.bet is a cloud host with no Blender), they will
always report "not found" no matter how healthy the caller's local Blender
is. When called against a hosted bus, the tools include a
``hosted_bus_hint`` in the response pointing at
``blender_list_available_clients``, which IS the right tool to answer
"is my Blender connected?" — that one queries the bus, not the filesystem.
Verified in feedback bug-4Cg1LPmzULs.

Follows the mandatory MCPMixin pattern (see CLAUDE.md).
"""

import json
from typing import Optional

from fastmcp.contrib.mcp_mixin import MCPMixin, mcp_tool

from .installation_manager import get_installation_manager


# The hint text is the same across the three tools that suffer this
# confusion; keeping it as a constant so a wording fix lands everywhere.
_HOSTED_BUS_HINT = (
    "This tool inspects the machine running the MCP server, NOT any "
    "connected addon. If you're on a hosted bus (mcp.blender.bet or "
    "similar), the server has no Blender install — use "
    "blender_list_available_clients to see connected addons instead. "
    "Empty persistent+ephemeral arrays there mean the addon hasn't "
    "joined the bus; a populated list means it's connected and "
    "healthy."
)


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
        """Diagnose the SERVER's local Blender environment (filesystem +
        process table on the MCP-server host). NOT the connected addon.

        Only useful when the MCP server runs on the same machine as
        Blender — the ``uvx blender-mcp`` local-dev case. On a hosted
        bus (``mcp.blender.bet`` etc.), the server has no Blender
        install and this returns blender_not_found unconditionally.
        Use ``blender_list_available_clients`` instead — that queries
        the bus for connected addons, which is what you probably want.
        """
        try:
            diagnosis = self.manager.diagnose_connection_issue()
            instructions = self.manager.get_setup_instructions(diagnosis)
            payload: dict = {
                "status": "ok",
                "diagnosis": diagnosis,
                "instructions": instructions,
                "scope": "server-local",
            }
            # blender_not_found here is the "you're on a hosted bus"
            # case; add the pointer explicitly so LLMs don't chase the
            # wrong hypothesis.
            if isinstance(diagnosis, dict) and diagnosis.get("blender_installed") is False:
                payload["hosted_bus_hint"] = _HOSTED_BUS_HINT
            return json.dumps(payload)
        except Exception as e:
            return json.dumps({"status": "error", "error": str(e)})

    @mcp_tool()
    def find_blender(self) -> str:
        """Locate the Blender executable on the SERVER's filesystem.

        Server-local scope only. On a hosted bus (server has no
        Blender), always returns found=False. See ``check_status`` for
        the full explanation; use ``blender_list_available_clients``
        to answer "is my Blender connected?" instead.
        """
        try:
            path = self.manager.find_blender_executable()
            if path:
                return json.dumps({
                    "status": "ok",
                    "found": True,
                    "path": path,
                    "scope": "server-local",
                })
            return json.dumps({
                "status": "ok",
                "found": False,
                "scope": "server-local",
                "searched": self.manager._get_common_blender_paths(),
                "hint": "Install Blender 3.0+ from https://www.blender.org/download/",
                "hosted_bus_hint": _HOSTED_BUS_HINT,
            })
        except Exception as e:
            return json.dumps({"status": "error", "error": str(e)})

    @mcp_tool()
    def list_running_blender(self) -> str:
        """List Blender processes on the SERVER's machine.

        Server-local scope only. On a hosted bus (server has no
        Blender), always returns count=0. Use
        ``blender_list_available_clients`` to see addon clients
        connected over the bus.
        """
        try:
            instances = self.manager.check_running_blender_instances()
            payload: dict = {
                "status": "ok",
                "running": instances,
                "count": len(instances),
                "gui_count": sum(1 for p in instances if p.get("is_gui")),
                "scope": "server-local",
            }
            if not instances:
                payload["hosted_bus_hint"] = _HOSTED_BUS_HINT
            return json.dumps(payload)
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
        """Install the bundled BlenderMCP addon into the SERVER's local
        Blender install.

        Server-local scope only — this writes files onto the machine
        running the MCP server, not any remote client. Meaningful in
        the ``uvx blender-mcp`` local-dev case; on a hosted bus, use
        the extension repo (Preferences → Get Extensions on YOUR
        Blender) or the legacy Install-from-Disk flow — see
        https://docs.blender.bet/how-to/install-addon/.
        """
        try:
            path = blender_path or self.manager.find_blender_executable()
            if not path:
                return json.dumps({
                    "status": "error",
                    "error": "blender_not_found",
                    "hint": "Install Blender 3.0+ first (https://www.blender.org/download/), then re-run install_addon.",
                    "hosted_bus_hint": _HOSTED_BUS_HINT,
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
