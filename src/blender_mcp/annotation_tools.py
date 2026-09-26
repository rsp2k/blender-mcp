"""Annotation write tools: let an LLM draw on the viewport to point things out.

Companion to the read tools (``blender_list_annotations`` /
``blender_get_annotation``) in dispatch_component. Strokes go on their own
annotation layer ("LLM" by default) so the user's marks are never touched
unless a layer is named explicitly. Arguments are validated here so a
malformed stroke fails immediately instead of after a bus round trip.
"""

from __future__ import annotations

import json

from fastmcp import Context
from fastmcp.contrib.mcp_mixin import MCPMixin, mcp_tool

from .bus_tools import _resolve_user_id, resolve_bus
from .client_role import check_role_or_reject

DEFAULT_TIMEOUT_S = 30.0
MAX_STROKE_POINTS = 10000
STYLES = ("box", "circle", "arrow")
SPACES = ("3D", "view")


def _err(error: str, **extra) -> str:
    return json.dumps({"status": "error", "error": error, **extra})


def normalize_points(points, space: str) -> list[list[float]]:
    """Validate stroke points; returns [[x, y, z], ...] or raises ValueError.

    "3D" needs [x, y, z] world coordinates. "view" takes [x, y] percentages
    of the viewport region (0-100); z is filled with 0.
    """
    if space not in SPACES:
        raise ValueError(f"space must be one of {list(SPACES)}")
    if not isinstance(points, (list, tuple)) or len(points) < 2:
        raise ValueError("points must be a list of at least 2 coordinates")
    if len(points) > MAX_STROKE_POINTS:
        raise ValueError(f"at most {MAX_STROKE_POINTS} points per stroke")
    out = []
    for i, p in enumerate(points):
        if not isinstance(p, (list, tuple)) or not all(
            isinstance(c, (int, float)) and not isinstance(c, bool) for c in p
        ):
            raise ValueError(f"point {i} must be a list of numbers")
        if space == "3D" and len(p) != 3:
            raise ValueError(f"point {i}: 3D points need [x, y, z]")
        if space == "view" and len(p) not in (2, 3):
            raise ValueError(f"point {i}: view points need [x, y]")
        out.append([float(c) for c in (list(p) + [0.0])[:3]])
    return out


def normalize_color(color) -> list[float] | None:
    if color is None:
        return None
    if (not isinstance(color, (list, tuple)) or len(color) not in (3, 4)
            or not all(isinstance(c, (int, float)) and 0.0 <= c <= 1.0 for c in color)):
        raise ValueError("color must be [r, g, b] with values from 0 to 1")
    return [float(c) for c in color[:3]]


def normalize_thickness(thickness) -> int | None:
    if thickness is None:
        return None
    if isinstance(thickness, bool) or not isinstance(thickness, (int, float)) or not 1 <= thickness <= 10:
        raise ValueError("thickness must be between 1 and 10")
    return int(thickness)


class BlenderAnnotationWriteComponent(MCPMixin):
    """add_annotation_stroke / annotate_object / clear_annotations."""

    async def _dispatch_command(self, ctx, command: str, params: dict,
                                target_uuid, timeout: float, bus_id) -> str:
        from .dispatch_component import _dispatch

        rejection = check_role_or_reject(f"blender_{command}", ctx, "llm-client")
        if rejection:
            return rejection
        user_id = _resolve_user_id(ctx)
        if not user_id:
            return _err("unauthenticated")
        resolved = await resolve_bus(user_id, bus_id)
        if not resolved["ok"]:
            return json.dumps(resolved)
        return await _dispatch(resolved["bus"], str(resolved["bus_id"]), command, params,
                               target_uuid, timeout, caller_sub=user_id)

    @mcp_tool()
    async def add_annotation_stroke(
        self,
        points: list[list[float]],
        layer: str = "LLM",
        color: list[float] | None = None,
        thickness: int | None = None,
        space: str = "3D",
        cyclic: bool = False,
        target_uuid: str | None = None,
        _timeout: float = DEFAULT_TIMEOUT_S,
        bus_id: str | None = None,
        ctx: Context = None,
    ) -> str:
        """Draw an annotation stroke in the user's viewport to point something out.

        space="3D" (default): ``points`` are world coordinates [x, y, z]; the
        mark sits in the scene. space="view": [x, y] as percentages of the
        viewport region (0-100, origin bottom-left); the mark stays fixed on
        screen as the view moves.

        Strokes go on the ``layer`` annotation layer ("LLM" by default,
        created orange and slightly thick so it stands out from the user's
        own marks). ``color`` ([r, g, b], 0-1) and ``thickness`` (1-10)
        apply to the whole layer: Blender stores them per layer. Use a
        different layer name for a different colour. ``cyclic`` closes the
        stroke. Returns the stroke id, readable with blender_get_annotation.
        """
        try:
            pts = normalize_points(points, space)
            col = normalize_color(color)
            thick = normalize_thickness(thickness)
        except ValueError as e:
            return _err("invalid_argument", detail=str(e))
        return await self._dispatch_command(ctx, "add_annotation_stroke", {
            "points": pts, "layer": layer, "color": col, "thickness": thick,
            "space": space, "cyclic": bool(cyclic),
        }, target_uuid, _timeout, bus_id)

    @mcp_tool()
    async def annotate_object(
        self,
        object: str,
        style: str = "box",
        label: str | None = None,
        layer: str = "LLM",
        color: list[float] | None = None,
        thickness: int | None = None,
        padding: float = 0.05,
        target_uuid: str | None = None,
        _timeout: float = DEFAULT_TIMEOUT_S,
        bus_id: str | None = None,
        ctx: Context = None,
    ) -> str:
        """Mark an object in the viewport so the user can see which one you mean.

        style="box" outlines its world-space bounding box, "circle" rings it
        at mid height, "arrow" points down at its top. ``padding`` grows the
        outline by that fraction of the object's size. Blender annotations
        can't hold text, so ``label`` is returned but not drawn; say it in
        your reply instead. Clear marks later with blender_clear_annotations.
        """
        if style not in STYLES:
            return _err("invalid_argument", detail=f"style must be one of {list(STYLES)}")
        if not object:
            return _err("invalid_argument", detail="object is required")
        try:
            col = normalize_color(color)
            thick = normalize_thickness(thickness)
        except ValueError as e:
            return _err("invalid_argument", detail=str(e))
        if not isinstance(padding, (int, float)) or not 0 <= padding <= 2:
            return _err("invalid_argument", detail="padding must be between 0 and 2")
        return await self._dispatch_command(ctx, "annotate_object", {
            "object": object, "style": style, "label": label, "layer": layer,
            "color": col, "thickness": thick, "padding": float(padding),
        }, target_uuid, _timeout, bus_id)

    @mcp_tool()
    async def clear_annotations(
        self,
        layer: str = "LLM",
        target_uuid: str | None = None,
        _timeout: float = DEFAULT_TIMEOUT_S,
        bus_id: str | None = None,
        ctx: Context = None,
    ) -> str:
        """Remove every stroke on one annotation layer ("LLM" by default).

        Only that layer is touched; the user's own annotation layers are left
        alone unless you name one explicitly. The layer itself is kept.
        """
        if not layer:
            return _err("invalid_argument", detail="layer is required")
        return await self._dispatch_command(ctx, "clear_annotations", {"layer": layer},
                                            target_uuid, _timeout, bus_id)
