"""Which server tools the chat model may call, and which need an Allow click.

Schemas come from the server's own registered tools, minus the arguments
that aim a call somewhere (``target_uuid``, ``bus_id``) or change how it is
delivered (``_timeout``, ``store``); the executor sets those itself, so the
model can only ever act on the Blender that sent the message.

The model sees bare names (``create_mesh``); the server tool is
``blender_<name>``. ``CHAT_TOOLS`` replaces the default list with any
Blender-targeting tool except the ones in DENIED.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

PREFIX = "blender_"
STRIPPED_ARGS = frozenset({"target_uuid", "bus_id", "_timeout", "store", "ctx"})
PREVIEW_LINES = 30
MAX_DESCRIPTION = 1200
LOOK = "look_at_viewport"


def _always(_args: dict) -> bool:
    return True


def _code_preview(args: dict) -> str:
    lines = str(args.get("code") or "").splitlines()
    head = "\n".join(lines[:PREVIEW_LINES])
    more = len(lines) - PREVIEW_LINES
    return head + (f"\n… ({more} more lines)" if more > 0 else "")


def _args_preview(args: dict) -> str:
    import json

    return json.dumps(args, indent=1, default=str)[:1500]


@dataclass(frozen=True)
class Policy:
    confirm: Callable[[dict], bool] | None = None
    preview: Callable[[dict], str] = _args_preview
    action: str = ""

    def needs_confirm(self, args: dict) -> bool:
        return bool(self.confirm and self.confirm(args))


_PLAIN = Policy()

# The curated v1 list, in the order the model sees it.
DEFAULT_TOOLS: dict[str, Policy] = {
    "get_scene_info": _PLAIN,
    "get_object_info": _PLAIN,
    "list_scene_objects": _PLAIN,
    "set_view": _PLAIN,
    LOOK: _PLAIN,
    "annotate_object": _PLAIN,
    "clear_annotations": _PLAIN,
    "add_primitive": _PLAIN,
    "create_mesh": _PLAIN,
    "place_object": _PLAIN,
    "duplicate_object": _PLAIN,
    "set_color": _PLAIN,
    "assign_material": _PLAIN,
    "make_pbr_material": _PLAIN,
    "set_viewport_shading": _PLAIN,
    "render_view": _PLAIN,
    "mesh_health": _PLAIN,
    "world_bounds": _PLAIN,
    "execute_code": Policy(confirm=_always, preview=_code_preview, action="run this Python"),
    "scene_defaults": Policy(confirm=lambda a: bool(a.get("remove")),
                             action="delete Blender's untouched startup objects"),
    "remove_interior": Policy(confirm=lambda a: not a.get("dry_run"),
                              action="flip and delete faces inside a mesh"),
}

# Tools that need approval when an operator adds them through CHAT_TOOLS.
_CONFIRM_IF_ADDED = {
    "new_file": "replace the open file with a new one",
    "open_file": "open a different .blend file",
    "revert_file": "revert the file to its saved state",
    "boolean": "apply a boolean to a mesh",
    "delete_uploads": "delete uploaded files",
    "console_operations": "run a console operation",
}

# Never offered: bus plumbing, control, installs, workers, and the generic
# ``submit`` (which takes any command and would bypass this list).
DENIED = frozenset({
    "submit", "install_extension", "request_control", "release_control",
    "force_release_control", "get_control_state", "reset_pump", "spawn_worker",
    "stop_worker", "offer_merge", "offer_reload", "get_merge_result", "send_message",
})


@dataclass(frozen=True)
class Entry:
    name: str
    server_name: str | None  # None for virtual tools (look_at_viewport)
    description: str
    parameters: dict[str, Any]
    policy: Policy
    # Set for tools from the add-on's own tool servers (chat/user_tools.py):
    # the server name and the tool's name there, sent in user_tool_call.
    user_server: str | None = None
    user_tool: str | None = None
    user_timeout_s: int | None = None


def strip_schema(schema: dict | None) -> dict:
    out = copy.deepcopy(schema or {"type": "object", "properties": {}})
    props = out.get("properties") or {}
    for key in STRIPPED_ARGS:
        props.pop(key, None)
    out["properties"] = props
    if "required" in out:
        out["required"] = [r for r in out["required"] if r not in STRIPPED_ARGS]
        if not out["required"]:
            del out["required"]
    out.setdefault("type", "object")
    return out


LOOK_ENTRY = Entry(
    name=LOOK,
    server_name=None,
    description=(
        "Look at the user's 3D viewport: takes a screenshot (annotations drawn in) "
        "and a vision model answers your question about it. Use it to check "
        "placement, framing or appearance after changing the scene. angle, frame "
        "and shading apply only to this look: the user's own view and shading are "
        "put back straight after."
    ),
    parameters={
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "What to look for or describe."},
            "angle": {"type": "string",
                      "enum": ["front", "back", "left", "right", "top", "bottom", "iso"],
                      "description": "Look from this angle instead of the user's current one."},
            "frame": {"type": "array", "items": {"type": "string"},
                      "description": "Object names to fill the picture with."},
            "shading": {"type": "string", "enum": ["SOLID", "MATERIAL", "RENDERED"],
                        "description": "MATERIAL to see real materials and colours."},
        },
        "required": ["question"],
    },
    policy=_PLAIN,
)


def _policy_for(name: str) -> Policy:
    if name in DEFAULT_TOOLS:
        return DEFAULT_TOOLS[name]
    if name in _CONFIRM_IF_ADDED:
        return Policy(confirm=_always, action=_CONFIRM_IF_ADDED[name])
    return _PLAIN


async def build_catalog(server, wanted: frozenset[str] | None = None,
                        vision: bool = False) -> list[Entry]:
    """Entries for the tools the model gets this turn.

    ``wanted`` (bare names, from CHAT_TOOLS) replaces the default list;
    ``vision`` adds look_at_viewport when screenshots can be fetched.
    """
    registered = {t.name: t for t in await server.list_tools(run_middleware=False)}
    names = list(wanted) if wanted else list(DEFAULT_TOOLS)
    out: list[Entry] = []
    for name in names:
        name = name.removeprefix(PREFIX)
        if name in DENIED or any(e.name == name for e in out):
            continue
        if name == LOOK:
            if vision:
                out.append(LOOK_ENTRY)
            continue
        tool = registered.get(PREFIX + name)
        if tool is None:
            continue
        params = tool.parameters or {}
        # Only tools that act on a Blender; bus/admin tools have no target.
        if "target_uuid" not in (params.get("properties") or {}):
            continue
        out.append(Entry(
            name=name,
            server_name=tool.name,
            description=(tool.description or "")[:MAX_DESCRIPTION],
            parameters=strip_schema(params),
            policy=_policy_for(name),
        ))
    return out
