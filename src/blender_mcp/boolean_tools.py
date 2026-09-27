"""Boolean operations that report what they did, plus a mesh-health query.

Thin dispatch wrappers over the addon's BooleanHandlersMixin. Exact booleans
fail silently in ways that look like success (a DIFFERENCE that does nothing,
a UNION that drops an operand, coplanar faces that leave non-manifold edges),
so the boolean tool measures the target before and after each operand and
reports warnings; see addon/boolean_checks.py for the rules. Operation and
solver names are validated here so a typo fails before the bus round trip.
"""

from __future__ import annotations

import json

from fastmcp import Context
from fastmcp.contrib.mcp_mixin import MCPMixin, mcp_tool

from .dispatch_component import (
    DEFAULT_TIMEOUT_S,
    TIMEOUT_MEDIUM,
    BlenderDispatchComponent,
)

# Mirrors addon/boolean_checks.py. Not imported from there: the server runs
# from its installed package, where the addon/ tree isn't importable.
OPERATIONS = ("DIFFERENCE", "UNION", "INTERSECT")
SOLVERS = ("EXACT", "FLOAT", "MANIFOLD")


def normalize_operation(operation) -> str:
    op = str(operation or "").strip().upper()
    if op not in OPERATIONS:
        raise ValueError(f"operation must be one of {', '.join(OPERATIONS)}, got {operation!r}")
    return op


def normalize_solver(solver) -> str:
    s = str(solver or "").strip().upper()
    if s == "FAST":
        raise ValueError(
            "solver 'FAST' no longer exists in Blender 5.x; use 'FLOAT' (its replacement), "
            "'EXACT' (robust, slower) or 'MANIFOLD' (fast, needs manifold inputs)"
        )
    if s not in SOLVERS:
        raise ValueError(f"solver must be one of {', '.join(SOLVERS)}, got {solver!r}")
    return s


def _err(error: str, **extra) -> str:
    return json.dumps({"status": "error", "error": error, **extra})


def _names(value) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not value or not all(isinstance(v, str) and v for v in value):
        raise ValueError("expected an object name or a non-empty list of object names")
    return value


class BlenderBooleanComponent(MCPMixin):
    """mesh_health and boolean."""

    # Same auth, role gate, bus resolution and job tracking as every dispatch tool.
    _call = BlenderDispatchComponent._call

    @mcp_tool()
    async def mesh_health(
        self,
        objects: list[str] | str,
        evaluated: bool = False,
        target_uuid: str | None = None,
        _timeout: float = DEFAULT_TIMEOUT_S,
        bus_id: str | None = None,
        ctx: Context = None,
    ) -> str:
        """Is this mesh a clean closed solid? Check before and after booleans.

        Per object: vertex/edge/face counts, non-manifold edges broken down
        into open (boundary), shared by 3+ faces, wire and flipped-normal
        edges, loose vertices, zero-area faces, connected shells, whether it
        is closed, and volume in world units (null when the mesh isn't closed,
        because an open mesh has no meaningful volume). ``evaluated`` measures
        the mesh with its modifiers applied; the default reads the stored mesh.
        Nothing is modified.
        """
        try:
            names = _names(objects)
        except ValueError as e:
            return _err("invalid_argument", detail=str(e))
        return await self._call(
            ctx, "mesh_health", {"objects": names, "evaluated": evaluated},
            target_uuid, _timeout, bus_id=bus_id,
        )

    @mcp_tool()
    async def boolean(
        self,
        target: str,
        operands: list[str] | str,
        operation: str = "DIFFERENCE",
        solver: str = "EXACT",
        apply: bool = True,
        check: bool = True,
        force: bool = False,
        use_self: bool = False,
        use_hole_tolerant: bool = False,
        coplanar_tolerance: float = 1e-4,
        coplanar_nudge: float | None = None,
        keep_operands: bool = True,
        hide_operands: bool = True,
        target_uuid: str | None = None,
        _timeout: float = TIMEOUT_MEDIUM,
        bus_id: str | None = None,
        ctx: Context = None,
    ) -> str:
        """Boolean mesh operands into a target, and report whether it worked.

        ``operation``: DIFFERENCE, UNION or INTERSECT. ``solver``: EXACT
        (robust, the default), FLOAT (fast but corrupts on coplanar faces) or
        MANIFOLD (fast, needs clean closed inputs); Blender 5.x has no FAST.
        Operands are applied one at a time, each step reporting face count,
        volume and non-manifold edges before and after, so a bad result can be
        traced to the operand that caused it.

        With ``check`` (default), the call is refused, and nothing changes,
        when the target or an operand isn't a closed manifold solid, or when an
        operand's faces lie in the same plane as the target's (coincident, or
        abutting solids). Both make booleans produce wrong geometry without any
        error. Fix the inputs, pass ``coplanar_nudge`` (a small distance, in
        scene units, to push a temporary copy of each operand outward so no
        faces share a plane), or pass ``force`` to run anyway.

        Warnings flag outcomes that look like success but aren't: a
        DIFFERENCE/INTERSECT that changed nothing, a UNION that dropped its
        operand or got smaller than an input, a DIFFERENCE that increased the
        volume, and new non-manifold edges. ``use_self`` also unions
        overlapping parts within the target (clean up duplicated or overlapping
        pieces before trimming). ``apply=False`` leaves the modifiers on the
        target for inspection. Operands are kept and hidden by default;
        ``keep_operands=False`` deletes them after applying.
        """
        try:
            names = _names(operands)
            op = normalize_operation(operation)
            slv = normalize_solver(solver)
            if not isinstance(target, str) or not target:
                raise ValueError("target must be an object name")
            if coplanar_tolerance <= 0:
                raise ValueError("coplanar_tolerance must be positive")
            if coplanar_nudge is not None and coplanar_nudge <= 0:
                raise ValueError("coplanar_nudge must be a positive distance")
        except (ValueError, TypeError) as e:
            return _err("invalid_argument", detail=str(e))
        return await self._call(
            ctx, "boolean",
            {
                "target": target, "operands": names, "operation": op, "solver": slv,
                "apply": apply, "check": check, "force": force, "use_self": use_self,
                "use_hole_tolerant": use_hole_tolerant,
                "coplanar_tolerance": coplanar_tolerance, "coplanar_nudge": coplanar_nudge,
                "keep_operands": keep_operands, "hide_operands": hide_operands,
            },
            target_uuid, _timeout, bus_id=bus_id,
        )
