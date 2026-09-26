"""Assembly analysis and keyframe tools (feedback fx-5ZRSYENMByk).

Thin dispatch wrappers over the addon's AnalysisHandlersMixin: real
triangle interference between parts, world-space bounds, and keyframe
read/insert/interpolation that works under Blender 5.x slotted Actions.
Kept out of dispatch_component.py so it can grow without churning the
core dispatch surface; it reuses that component's auth + bus funnel.
"""

from __future__ import annotations

from fastmcp import Context
from fastmcp.contrib.mcp_mixin import MCPMixin, mcp_tool

from .dispatch_component import (
    DEFAULT_TIMEOUT_S,
    TIMEOUT_MEDIUM,
    BlenderDispatchComponent,
)


class BlenderAnalysisComponent(MCPMixin):
    """world_bounds, check_interference, get_keyframes, insert_keyframe, set_interpolation."""

    # Same auth, role gate, bus resolution and job tracking as every dispatch tool.
    _call = BlenderDispatchComponent._call

    @mcp_tool()
    async def world_bounds(
        self,
        objects: list[str] | str | None = None,
        frame: int | None = None,
        include_hidden: bool = False,
        exact: bool = False,
        target_uuid: str | None = None,
        _timeout: float = DEFAULT_TIMEOUT_S,
        bus_id: str | None = None,
        ctx: Context = None,
    ) -> str:
        """World-space min/max/size/center per object, plus their union.

        ``objects``: names, or omit for all visible geometry ("all" also
        includes hidden). ``frame`` evaluates at that frame and restores the
        scene afterwards. ``exact`` uses evaluated vertices for tight bounds;
        the default transforms the local bound box, which never
        under-reports but can over-report for rotated parts.
        """
        return await self._call(
            ctx, "world_bounds",
            {"objects": objects, "frame": frame, "include_hidden": include_hidden, "exact": exact},
            target_uuid, _timeout, bus_id=bus_id,
        )

    @mcp_tool()
    async def check_interference(
        self,
        objects: list[str] | str | None = None,
        frame: int | None = None,
        frames: list[int] | None = None,
        ignore_pairs: list[list[str]] | None = None,
        include_hidden: bool = False,
        tolerance: float = 0.0,
        check_containment: bool = True,
        sample_points: int = 3,
        target_uuid: str | None = None,
        _timeout: float = TIMEOUT_MEDIUM,
        bus_id: str | None = None,
        ctx: Context = None,
    ) -> str:
        """Which parts really intersect, not merely overlap in bounding box.

        Tests every pair whose world bounds overlap for triangle
        intersection on evaluated, world-space meshes, and reports
        ``intersecting`` pairs with their triangle-pair count and sample
        points, plus ``bbox_overlap_pairs`` so you can see how many
        box overlaps were false alarms. A part entirely inside another
        crosses no triangles; ``contained`` lists those (needs closed
        meshes with outward normals). Faces that only touch coplanar
        don't count as intersecting; raise ``tolerance`` (scene units)
        to catch near or touching contact. ``frames`` sweeps several
        frames of a keyframed scene; ``ignore_pairs`` skips intended
        contacts such as snap fits. Long sweeps return a job_id; follow
        it with blender_job_status.
        """
        return await self._call(
            ctx, "check_interference",
            {"objects": objects, "frame": frame, "frames": frames, "ignore_pairs": ignore_pairs,
             "include_hidden": include_hidden, "tolerance": tolerance,
             "check_containment": check_containment, "sample_points": sample_points},
            target_uuid, _timeout, bus_id=bus_id,
        )

    @mcp_tool()
    async def get_keyframes(
        self,
        object: str,
        data_path: str | None = None,
        owner: str = "object",
        target_uuid: str | None = None,
        _timeout: float = DEFAULT_TIMEOUT_S,
        bus_id: str | None = None,
        ctx: Context = None,
    ) -> str:
        """Keyframes on an object's assigned action, per channel.

        Works with Blender 5.x slotted Actions (``action.fcurves`` no longer
        exists there) and older actions alike. ``owner`` is "object",
        "data" (e.g. a light's energy) or "shape_keys". Filter one property
        with ``data_path`` such as "location".
        """
        return await self._call(
            ctx, "get_keyframes",
            {"object": object, "data_path": data_path, "owner": owner},
            target_uuid, _timeout, bus_id=bus_id,
        )

    @mcp_tool()
    async def insert_keyframe(
        self,
        object: str,
        data_path: str,
        frame: float,
        value: float | list[float] | bool | str | None = None,
        index: int = -1,
        overwrite: bool = False,
        interpolation: str | None = None,
        owner: str = "object",
        target_uuid: str | None = None,
        _timeout: float = DEFAULT_TIMEOUT_S,
        bus_id: str | None = None,
        ctx: Context = None,
    ) -> str:
        """Set a property and key it at a frame, in the only safe order.

        Goes frame, then value, then key: setting a value and then changing
        frame re-evaluates animation and silently discards it. Refuses to
        replace an existing key on the same channel and frame unless
        ``overwrite`` is true, and reports replaced values, so an authored
        pose can't be destroyed by accident. Omit ``value`` to key the
        current value. ``index`` keys one component (-1 = all).
        """
        return await self._call(
            ctx, "insert_keyframe",
            {"object": object, "data_path": data_path, "frame": frame, "value": value,
             "index": index, "overwrite": overwrite, "interpolation": interpolation,
             "owner": owner},
            target_uuid, _timeout, bus_id=bus_id,
        )

    @mcp_tool()
    async def set_interpolation(
        self,
        object: str,
        interpolation: str,
        data_path: str | None = None,
        frames: list[float] | None = None,
        owner: str = "object",
        target_uuid: str | None = None,
        _timeout: float = DEFAULT_TIMEOUT_S,
        bus_id: str | None = None,
        ctx: Context = None,
    ) -> str:
        """Set interpolation (CONSTANT, LINEAR, BEZIER, ...) on an object's
        keys, optionally one property and/or specific frames only."""
        return await self._call(
            ctx, "set_interpolation",
            {"object": object, "interpolation": interpolation, "data_path": data_path,
             "frames": frames, "owner": owner},
            target_uuid, _timeout, bus_id=bus_id,
        )
