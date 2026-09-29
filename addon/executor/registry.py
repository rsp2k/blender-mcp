"""Decorator-based command registry.

Handler methods opt in via ``@command("name", gate=...)``. At import
time each decorator populates :data:`COMMAND_REGISTRY`; ``execute_command``
in the facade looks up the spec, evaluates the optional gate against
``bpy.context.scene``, then calls the unbound method as
``spec.func(self, **filtered_params)``.

Why a registry (vs. the old dict literal): adding a handler now only
touches its own module. No editing of a central switch. Gates become
declarative — the gate function travels next to the handler it controls,
not in a faraway `if scene.use_X:` block.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CommandSpec:
    """One entry in the dispatch registry.

    Attributes:
        name: The command-name string used by `_execute_command_internal`.
        func: The (unbound) handler method. Called as ``func(self, **params)``.
        gate: Optional predicate; receives the AddonPreferences and returns
            True if the command is currently enabled. None = always enabled.
            Evaluated on every call, so toggling a preference takes effect
            immediately (no reconnect).
        disabled_hint: What to tell the caller when the gate is closed, e.g.
            how to enable the integration. Without it a gated-off command
            answers "Unknown command type", which reads like a missing
            feature rather than a switched-off one.
        undo: Whether the command changes the scene and so gets one undo
            step ("BlenderMCP: <name>"). False for read-only commands. May
            also be a predicate over the params, for commands that only
            sometimes change things (e.g. a dry_run flag).
    """

    name: str
    func: Callable
    gate: Callable[[Any], bool] | None = None
    disabled_hint: str | None = None
    undo: bool | Callable[[dict], bool] = True

    def wants_undo(self, params: dict) -> bool:
        """True when this call should become an undo step."""
        if callable(self.undo):
            try:
                return bool(self.undo(params if isinstance(params, dict) else {}))
            except Exception:  # noqa: BLE001  a broken predicate errs toward undoable
                return True
        return bool(self.undo)


COMMAND_REGISTRY: dict[str, CommandSpec] = {}


def command(
    name: str,
    *,
    gate: Callable[[Any], bool] | None = None,
    disabled_hint: str | None = None,
    undo: bool | Callable[[dict], bool] = True,
) -> Callable:
    """Register a handler method under ``name``.

    Usage::

        @command("get_scene_info")
        def get_scene_info(self):
            ...

        @command("download_polyhaven_asset",
                 gate=lambda scene: scene.blendermcp_use_polyhaven)
        def download_polyhaven_asset(self, asset_id, asset_type, ...):
            ...

    Read-only commands pass ``undo=False`` so they push no undo step.

    The decorator returns the function unchanged so normal method-style
    calls (``self.method()``) keep working alongside the dispatch path.
    """

    def decorator(fn: Callable) -> Callable:
        COMMAND_REGISTRY[name] = CommandSpec(
            name=name, func=fn, gate=gate, disabled_hint=disabled_hint,
            undo=undo,
        )
        return fn

    return decorator


def filter_kwargs(func: Callable, params: dict) -> dict:
    """Return only the keys of ``params`` that name a parameter of ``func``.

    Mirrors the pre-Phase-5 behavior where the dispatch lambdas pulled
    specific fields from the incoming params dict via ``p.get(...)``
    — extra keys were silently dropped. Without this filter, calling
    ``func(self, **params)`` with stray keys would raise TypeError.
    """
    sig = inspect.signature(func)
    return {k: v for k, v in params.items() if k in sig.parameters}
