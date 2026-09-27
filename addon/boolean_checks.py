"""Pure checks for boolean operations: argument validation and outcome verdicts.

No bpy import, so the server and tests can use it. The mesh measurements come
from ``executor/handlers/booleans.py``; this module only decides what they
mean. Every warning here is a failure mode seen in practice (model-home's
Clagstone roof and walls): a boolean that silently did nothing, a DIFFERENCE
that made the mesh bigger, a UNION that lost an operand, and non-manifold
edges that stay invisible until a later boolean goes wrong.
"""

from __future__ import annotations

OPERATIONS = ("DIFFERENCE", "UNION", "INTERSECT")
SOLVERS = ("EXACT", "FLOAT", "MANIFOLD")
# Relative volume tolerance: booleans on closed meshes move volume by float
# noise at most; anything beyond this is a real change.
VOLUME_REL_EPS = 1e-4
VOLUME_ABS_EPS = 1e-9


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


def input_problems(health: dict) -> list[str]:
    """Reasons a mesh is unsafe as a boolean input (empty = fine)."""
    problems = []
    if health.get("non_manifold_edges", 0):
        parts = []
        for key, label in (("boundary_edges", "open"), ("multi_face_edges", "shared by 3+ faces"),
                           ("wire_edges", "wire"), ("non_contiguous_edges", "flipped-normal")):
            if health.get(key):
                parts.append(f"{health[key]} {label}")
        problems.append(f"{health['non_manifold_edges']} non-manifold edges ({', '.join(parts)})")
    if not health.get("faces"):
        problems.append("no faces")
    vol = health.get("volume")
    if vol is not None and vol < 0:
        problems.append("normals point inward (negative volume)")
    return problems


def _vol_close(a: float, b: float) -> bool:
    return abs(a - b) <= max(VOLUME_ABS_EPS, VOLUME_REL_EPS * max(abs(a), abs(b)))


def verdict(operation: str, before: dict, operand: dict, after: dict) -> list[str]:
    """Warnings for one boolean step, from mesh health before/after.

    ``before`` is the target before the step, ``operand`` the operand's
    health, ``after`` the target afterwards. Each dict carries face/vertex
    counts, non_manifold_edges and volume (None when the mesh isn't closed).
    """
    warnings = []
    op = normalize_operation(operation)

    unchanged = after.get("faces") == before.get("faces") and after.get("vertices") == before.get("vertices")
    if unchanged and op in ("DIFFERENCE", "INTERSECT"):
        warnings.append(
            f"{op} changed nothing (face and vertex counts identical); "
            "the operand may not intersect, or the solver silently failed"
        )
    elif unchanged and op == "UNION" and operand.get("faces"):
        warnings.append(
            "UNION left the target's face and vertex counts unchanged; the operand was "
            "dropped, unless it lies entirely inside the target"
        )

    nm_before, nm_after = before.get("non_manifold_edges", 0), after.get("non_manifold_edges", 0)
    if nm_after > nm_before:
        warnings.append(
            f"result has {nm_after} non-manifold edges (was {nm_before}); the mesh is corrupt "
            "even if it renders fine, and later booleans on it will misbehave"
        )

    vb, vo, va = before.get("volume"), operand.get("volume"), after.get("volume")
    if va is None:
        if vb is not None:
            warnings.append("result is no longer a closed solid, so its volume can't be checked")
    elif vb is not None:
        if op == "DIFFERENCE" and va > vb and not _vol_close(va, vb):
            warnings.append(f"DIFFERENCE increased the volume ({vb:.6g} -> {va:.6g}); part of the mesh was lost or inverted")
        if op == "UNION":
            floor = max(vb, vo) if vo is not None else vb
            if va < floor and not _vol_close(va, floor):
                warnings.append(
                    f"UNION is smaller than an input ({va:.6g} < {floor:.6g}); a union can never "
                    "shrink, so an operand or part of the target was dropped"
                )
        if op == "INTERSECT":
            ceiling = min(vb, vo) if vo is not None else vb
            if va > ceiling and not _vol_close(va, ceiling):
                warnings.append(f"INTERSECT is larger than an input ({va:.6g} > {ceiling:.6g})")
        if va <= VOLUME_ABS_EPS and op != "INTERSECT":
            warnings.append("the result is empty")
    if va is not None and va < 0:
        warnings.append("result normals point inward (negative volume)")
    return warnings
