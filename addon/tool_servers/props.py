"""The Tool servers preference entries (bpy PropertyGroup).

Registered before BlenderMCPPreferences, which holds a CollectionProperty
of these. Edits schedule a debounced sync on the main thread; the runner
never reads these properties itself.
"""

from __future__ import annotations

import bpy
from bpy.props import BoolProperty, FloatProperty, StringProperty

from .config import DEFAULT_TIMEOUT_S, NAME_MAX, sanitize_name


def _changed(_self, _context) -> None:
    from .bridge import schedule_sync
    schedule_sync()


def _name_changed(self, context) -> None:
    clean = sanitize_name(self.name)
    if clean != self.name:
        self.name = clean  # runs this callback once more with the clean name
        return
    _changed(self, context)


class BLENDERMCP_PG_ToolServer(bpy.types.PropertyGroup):
    name: StringProperty(
        name="Name",
        description=(
            f"Short name: lowercase letters, digits and -, up to {NAME_MAX} characters. "
            "Shown on tool steps and used in tool names (pdf__read_text)"
        ),
        default="",
        maxlen=NAME_MAX + 8,  # typed text is cleaned to NAME_MAX on confirm
        update=_name_changed,
    )
    start: StringProperty(
        name="Start with",
        description=(
            "A command that starts a local MCP server (e.g. uvx mcp-pdf), "
            "or an https:// address of a remote one"
        ),
        default="",
        update=_changed,
    )
    env_text: StringProperty(
        name="Environment",
        description=(
            "KEY=value pairs for a local server, separated by ; "
            "(${VAR} reads an environment variable)"
        ),
        default="",
        update=_changed,
    )
    token: StringProperty(
        name="Token",
        description=(
            "Bearer token for a remote server. Write ${VAR} to read it from an "
            "environment variable and keep it out of Blender's preferences file"
        ),
        subtype="PASSWORD",
        default="",
        update=_changed,
    )
    trusted: BoolProperty(
        name="Trusted",
        description="Run this server's tools without asking. Off: each call asks Allow/Deny in the Chat tab",
        default=False,
        update=_changed,
    )
    enabled: BoolProperty(
        name="Enabled",
        description="Start this server when the add-on connects and offer its tools to chat",
        default=True,
        update=_changed,
    )
    timeout_s: FloatProperty(
        name="Timeout (s)",
        description="Seconds a single tool call may take",
        default=DEFAULT_TIMEOUT_S,
        min=1.0,
        max=600.0,
        step=100,
        precision=0,
        update=_changed,
    )
