"""Starter prompts: one-click suggestions for the Chat panel in a new chat.

Each starter is an ordinary MCP prompt with no arguments. ``title`` is the
button label, and prompts/get returns one user message whose text is what
the add-on puts in the chat field (without sending it). Any MCP client
sees them too, e.g. in a slash-command picker.

The add-on finds them by ``_meta``, not by name:

    {"blender_mcp": {"starter": true, "group": "b_clip", "order": 1}}

``group`` says when they fit: ``b_clip`` for the B. Clip template (or a
scene with B in it), ``general`` for any scene. ``order`` sorts the
buttons. Names are stable; add new starters rather than renaming.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastmcp import FastMCP
from fastmcp.prompts import Prompt
from fastmcp.prompts.base import Message

META_KEY = "blender_mcp"


@dataclass(frozen=True)
class Starter:
    name: str
    title: str
    group: str
    description: str
    text: str


STARTERS: tuple[Starter, ...] = (
    Starter(
        "starter_b_wave", "Make B wave", "b_clip",
        "Animate B. Clip waving hello with his front handle.",
        "Make B. Clip wave hello by swinging his front handle back and forth "
        "a few times over about two seconds, hinged where it meets his jaw. "
        "I'll press Space to play it.",
    ),
    Starter(
        "starter_b_top_hat", "Give B a top hat", "b_clip",
        "Model a top hat and put it on B. Clip so it moves with him.",
        "Give B. Clip a black top hat that sits on his head and moves with him.",
    ),
    Starter(
        "starter_b_desk", "Put B on a desk", "b_clip",
        "Build a simple desk and stand B. Clip on it.",
        "Build a simple wooden desk and stand B. Clip on top of it, near the "
        "front edge, still facing the camera.",
    ),
    Starter(
        "starter_describe_scene", "Describe this scene", "general",
        "Summarise what is in the current scene.",
        "Describe what's in this scene: the objects, how they're arranged, "
        "and how it's lit.",
    ),
    Starter(
        "starter_table_chair", "Add a table and chair", "general",
        "Add a real-world-sized table and chair to the scene.",
        "Add a simple wooden table with a chair tucked under it, both "
        "real-world size and resting on the floor.",
    ),
    Starter(
        "starter_light_scene", "Light this scene nicely", "general",
        "Set up soft, flattering lighting for the scene's main subject.",
        "Light this scene nicely with a soft three-point setup aimed at the "
        "main subject, as seen from the camera.",
    ),
)


def starter_meta(starter: Starter, order: int) -> dict:
    return {META_KEY: {"starter": True, "group": starter.group, "order": order}}


def _render(text: str):
    def render() -> list[Message]:
        return [Message(text)]
    return render


def register_starter_prompts(server: FastMCP, prefix: str | None = "blender") -> None:
    """Add every starter to ``server``, named like MCPMixin prompts
    (``blender_starter_b_wave``)."""
    for order, starter in enumerate(STARTERS, start=1):
        server.add_prompt(Prompt.from_function(
            fn=_render(starter.text),
            name=f"{prefix}_{starter.name}" if prefix else starter.name,
            title=starter.title,
            description=starter.description,
            meta=starter_meta(starter, order),
        ))
