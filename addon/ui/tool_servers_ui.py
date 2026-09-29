"""Tool servers section of the add-on preferences: list, details, Add/Remove/Test."""

from __future__ import annotations

import bpy
from bpy.props import IntProperty

_STATE_ICONS = {
    "starting": "TIME",
    "running": "CHECKMARK",
    "error": "ERROR",
    "off": "RADIOBUT_OFF",
    "none": "RADIOBUT_OFF",
}


def _status(name: str) -> tuple[str, str]:
    from ..tool_servers.bridge import status_for
    from ..tool_servers.runner import describe
    return describe(status_for(name) if name else None)


class BLENDERMCP_UL_ToolServers(bpy.types.UIList):
    bl_idname = "BLENDERMCP_UL_tool_servers"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index=0):
        state, text = _status(item.name)
        if not item.enabled and state in ("none", "off"):
            text = "off"
        row = layout.row(align=True)
        row.prop(item, "enabled", text="")
        row.label(text=item.name or "(no name)", icon='UNLOCKED' if item.trusted else 'LOCKED')
        sub = row.row()
        sub.alert = state == "error"
        sub.label(text=text, icon=_STATE_ICONS.get(state, 'BLANK1'))


def _prefs(context):
    from ..preferences import get_prefs
    return get_prefs(context)


def _unique_name(existing: set[str]) -> str:
    n = 1
    while f"server-{n}" in existing:
        n += 1
    return f"server-{n}"


class BLENDERMCP_OT_ToolServerAdd(bpy.types.Operator):
    """Add a tool server"""

    bl_idname = "blendermcp.tool_server_add"
    bl_label = "Add tool server"
    bl_options = {'INTERNAL'}  # noqa: RUF012 - Blender reads a plain set

    def execute(self, context):
        from ..preferences import persist_prefs
        prefs = _prefs(context)
        item = prefs.tool_servers.add()
        item.name = _unique_name({s.name for s in prefs.tool_servers if s != item})
        prefs.tool_servers_index = len(prefs.tool_servers) - 1
        persist_prefs()
        return {'FINISHED'}


class BLENDERMCP_OT_ToolServerRemove(bpy.types.Operator):
    """Remove the selected tool server (stops it)"""

    bl_idname = "blendermcp.tool_server_remove"
    bl_label = "Remove tool server"
    bl_options = {'INTERNAL'}  # noqa: RUF012 - Blender reads a plain set

    @classmethod
    def poll(cls, context):
        prefs = _prefs(context)
        return 0 <= prefs.tool_servers_index < len(prefs.tool_servers)

    def execute(self, context):
        from ..preferences import persist_prefs
        from ..tool_servers.bridge import schedule_sync
        prefs = _prefs(context)
        prefs.tool_servers.remove(prefs.tool_servers_index)
        prefs.tool_servers_index = min(prefs.tool_servers_index, len(prefs.tool_servers) - 1)
        persist_prefs()
        schedule_sync()
        return {'FINISHED'}


class BLENDERMCP_OT_ToolServerTest(bpy.types.Operator):
    """Start (or restart) this server now and list its tools"""

    bl_idname = "blendermcp.tool_server_test"
    bl_label = "Test tool server"
    bl_options = {'INTERNAL'}  # noqa: RUF012 - Blender reads a plain set

    index: IntProperty(default=-1)

    def execute(self, context):
        from ..tool_servers.bridge import test_server
        prefs = _prefs(context)
        index = self.index if self.index >= 0 else prefs.tool_servers_index
        # Returns at once; the status line fills in as the server comes up.
        problem = test_server(index)
        if problem:
            self.report({'WARNING'}, problem)
            return {'CANCELLED'}
        return {'FINISHED'}


def draw_tool_servers_section(layout, prefs) -> None:
    from ..tool_servers.bridge import get_bridge
    from ..tool_servers.config import kind_of, name_problem

    col = layout.column(align=True)
    col.label(text="Tool servers", icon='TOOL_SETTINGS')
    col.label(text="MCP servers whose tools the chat assistant can use. They run on this computer.")

    row = layout.row()
    row.template_list(
        BLENDERMCP_UL_ToolServers.bl_idname, "", prefs, "tool_servers",
        prefs, "tool_servers_index", rows=3,
    )
    side = row.column(align=True)
    side.operator(BLENDERMCP_OT_ToolServerAdd.bl_idname, text="", icon='ADD')
    side.operator(BLENDERMCP_OT_ToolServerRemove.bl_idname, text="", icon='REMOVE')

    idx = prefs.tool_servers_index
    if 0 <= idx < len(prefs.tool_servers):
        item = prefs.tool_servers[idx]
        box = layout.box()
        detail = box.column(align=True)
        detail.prop(item, "name")
        problem = name_problem(item.name)
        if problem is None and sum(1 for s in prefs.tool_servers if s.name == item.name) > 1:
            problem = "another server has this name"
        if problem:
            warn = detail.row()
            warn.alert = True
            warn.label(text=problem, icon='ERROR')
        detail.prop(item, "start")
        remote = kind_of(item.start) == "http"
        if remote:
            detail.prop(item, "token")
        else:
            detail.prop(item, "env_text")
        opts = box.row(align=True)
        opts.prop(item, "enabled")
        opts.prop(item, "trusted")
        opts.prop(item, "timeout_s")

        state, text = _status(item.name)
        status = box.row(align=True)
        line = status.row()
        line.alert = state == "error"
        kind = "remote" if remote else "local command"
        line.label(text=f"{kind} · {text}", icon=_STATE_ICONS.get(state, 'BLANK1'))
        test = status.row()
        test.enabled = bool(item.start.strip())
        op = test.operator(BLENDERMCP_OT_ToolServerTest.bl_idname, text="Test", icon='PLAY')
        op.index = idx
        if not item.trusted:
            box.label(text="Each call asks Allow/Deny in the chat.", icon='INFO')

    bridge = get_bridge(create=False)
    report = bridge.last_report if bridge is not None else {}
    if report.get("error"):
        err = layout.row()
        err.alert = True
        err.label(text=f"Couldn't offer tools to chat: {report['error'][:90]}", icon='ERROR')
    for d in (report.get("dropped") or [])[:5]:
        layout.label(text=f"Left out {d.get('server')}·{d.get('name')}: {d.get('reason')}"[:110],
                     icon='INFO')


TOOL_SERVER_CLASSES = (
    BLENDERMCP_UL_ToolServers,
    BLENDERMCP_OT_ToolServerAdd,
    BLENDERMCP_OT_ToolServerRemove,
    BLENDERMCP_OT_ToolServerTest,
)
