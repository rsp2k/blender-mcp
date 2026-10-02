"""The chat message field: Enter sends, a starter fills without sending.

The field is ``WindowManager.blendermcp_chat_input`` and its update hook
sends whatever lands in it, which is what makes Enter work. Assigning the
property from Python fires that same hook synchronously, so fill() arms a
one-shot guard in chat_state for exactly that assignment. No bpy here: ``wm``
is anything with a ``blendermcp_chat_input`` attribute.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .state import chat_state


def on_input_changed(wm: Any, send: Callable[[str], tuple[bool, Any]]) -> None:
    """The field's update hook. Clearing the field re-fires this with an empty
    value, which is a no-op. A message that couldn't be sent stays in the
    field for the Send button."""
    raw = wm.blendermcp_chat_input or ""
    if chat_state.take_prefill(raw):
        return  # a starter put this here; the user reviews it and presses Enter
    text = raw.strip()
    if not text:
        return
    started, _problem = send(text)
    if started:
        wm.blendermcp_chat_input = ""


def fill(wm: Any, text: str) -> None:
    """Put ``text`` in the message field without sending it."""
    chat_state.arm_prefill(text)
    try:
        wm.blendermcp_chat_input = text
    finally:
        # The hook ran (and took the guard) during the assignment. Clear it
        # anyway in case it didn't fire, so a later Enter isn't swallowed.
        chat_state.disarm_prefill()
