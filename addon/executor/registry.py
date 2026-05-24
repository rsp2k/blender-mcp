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
from dataclasses import dataclass
from typing import Any, Callable, Optional


@dataclass(frozen=True)
class CommandSpec:
    """One entry in the dispatch registry.

    Attributes:
        name: The command-name string used by `_execute_command_internal`.
        func: The (unbound) handler method. Called as ``func(self, **params)``.
        gate: Optional predicate; receives ``bpy.context.scene`` and returns
            True if the command is currently enabled. None = always enabled.
    """

    name: str
    func: Callable
    gate: Optional[Callable[[Any], bool]] = None


COMMAND_REGISTRY: dict[str, CommandSpec] = {}


def command(name: str, *, gate: Optional[Callable[[Any], bool]] = None) -> Callable:
    """Register a handler method under ``name``.

    Usage::

        @command("get_scene_info")
        def get_scene_info(self):
            ...

        @command("download_polyhaven_asset",
                 gate=lambda scene: scene.blendermcp_use_polyhaven)
        def download_polyhaven_asset(self, asset_id, asset_type, ...):
            ...

    The decorator returns the function unchanged so normal method-style
    calls (``self.method()``) keep working alongside the dispatch path.
    """

    def decorator(fn: Callable) -> Callable:
        COMMAND_REGISTRY[name] = CommandSpec(name=name, func=fn, gate=gate)
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
