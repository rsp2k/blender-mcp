"""blender_report_user_tools: the add-on tells the server what its tool servers offer.

Add-on only. The set is kept in memory for the calling Blender's session and
used only by that Blender's chat turns (chat/user_tools.py). Wire contract:
docs-site/src/content/docs/how-to/tool-servers.mdx.
"""

import json
import logging
from typing import Any

from fastmcp import Context
from fastmcp.contrib.mcp_mixin import MCPMixin, mcp_tool

from ..bus_tools import _resolve_user_id, _session_from_ctx
from ..client_role import require_role
from . import user_tools

logger = logging.getLogger(__name__)


class BlenderUserToolsComponent(MCPMixin):
    """blender_report_user_tools."""

    @mcp_tool()
    @require_role("addon")
    async def report_user_tools(
        self,
        tools: list[dict[str, Any]],
        ctx: Context = None,
    ) -> str:
        """Report the tools of this Blender's own tool servers, for its Chat tab.

        ``tools`` is the full set, each ``{server, name, description,
        input_schema, trusted}``; it replaces any earlier report and an empty
        list clears it. At most 80 tools, descriptions cut to 1000
        characters, schemas at most 8 KB of JSON. Returns ``{"status": "ok",
        "accepted": n, "dropped": [{server, name, reason}]}``.
        """
        if not _resolve_user_id(ctx):
            return json.dumps({"status": "error", "error": "unauthenticated"})

        from ..message_bus import bus_manager

        session = _session_from_ctx(ctx)
        where = bus_manager.lookup_session(session)
        if not where:
            return json.dumps({"status": "error", "error": "not_registered",
                               "hint": "Register this Blender on the bus before reporting tools."})
        bus_id, blender_uuid = where
        accepted, dropped = user_tools.validate_report(tools)
        user_tools.registry.put(bus_id, blender_uuid, session, accepted)
        logger.info("user tools reported for %s on %s: %d accepted, %d dropped",
                    blender_uuid, bus_id, len(accepted), len(dropped))
        return json.dumps({"status": "ok", "accepted": len(accepted), "dropped": dropped})
