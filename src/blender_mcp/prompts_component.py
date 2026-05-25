"""MCP prompts for Blender script-writing + dispatch guidance.

These 8 prompts are *skeletal* — they bootstrap the LLM with the right
shape of Blender domain knowledge without trying to be a full bpy
reference. The user explicitly opted out of a bpy-docs scrape in the
plan (that's a separate multi-day project, deferred).

Why no user-id check: prompts are pure templates. They don't dispatch
anything to a Blender peer, they don't touch the bus, and they're
useful even when no peer is connected. So they're safe to register in
the stdio build too — useful for a user prototyping a script offline
before connecting Blender.

Highest-leverage prompt: ``dispatch_recipe(command)``. The MCP tool
surface is large (24 dispatch tools, 4 resources, 4 bus tools); a
client that doesn't already know the shape of ``blender_execute_code``
or ``blender_download_polyhaven_asset`` would otherwise have to call
``list_tools`` and walk the schema. ``dispatch_recipe`` short-circuits
that with one canonical example per command.
"""

from __future__ import annotations

from fastmcp.contrib.mcp_mixin import MCPMixin, mcp_prompt
from fastmcp.prompts.base import Message


# Static source-of-truth for dispatch_recipe. Mirrors the @mcp_tool
# signatures in dispatch_component.py; the wire shape is the dict
# passed to ``c.call_tool(tool_name, arguments)`` from a FastMCP client.
# Keeping it inline (rather than introspecting dispatch_component) makes
# the prompt self-contained and easy to scan — and rendering it doesn't
# need to import dispatch_component at all (no circular-import risk).
_DISPATCH_RECIPES: dict[str, dict] = {
    # ---- Tier 1: always-on core
    "get_scene_info": {},
    "get_object_info": {"name": "Cube"},
    "browse_data": {
        "collection": "objects",
        "page": 1,
        "page_size": 50,
        "detail_level": "summary",
    },
    "execute_code": {"code": "import bpy\nprint(bpy.data.objects.keys())"},
    "get_viewport_screenshot": {
        "filepath": "/tmp/viewport.png",
        "max_size": 800,
        "format": "png",
    },
    "get_console_output": {"level": "all", "page": 1, "page_size": 50},
    "console_operations": {"operation": "get_info", "params": {}},
    # ---- Tier 2: status probes
    "get_polyhaven_status": {},
    "get_hyper3d_status": {},
    "get_sketchfab_status": {},
    # ---- Tier 3: msgbus
    "msgbus_clear_by_owner": {"owner_id": "my-watcher"},
    "msgbus_publish_rna": {"data_path": "frame_current"},
    "msgbus_subscribe_rna": {
        "data_path": "frame_current",
        "owner_id": "my-watcher",
        "persistent": True,
    },
    "msgbus_get_notifications": {"owner_id": "my-watcher", "clear": False},
    "msgbus_list_subscriptions": {"owner_id": "my-watcher"},
    # ---- Tier 3: PolyHaven
    "get_polyhaven_categories": {"asset_type": "hdris"},
    "search_polyhaven_assets": {"asset_type": "hdris", "categories": "outdoor"},
    "download_polyhaven_asset": {
        "asset_id": "kloofendal_43d_clear_puresky",
        "asset_type": "hdris",
        "resolution": "1k",
    },
    "set_texture": {"object_name": "Cube", "texture_id": "wood_planks_brown"},
    # ---- Tier 3: Hyper3D Rodin
    "create_rodin_job": {"text_prompt": "a wooden chair, low poly"},
    "poll_rodin_job_status": {"subscription_key": "<from create_rodin_job>"},
    "import_generated_asset": {
        "name": "GeneratedChair",
        "task_uuid": "<from create_rodin_job, MAIN_SITE mode>",
    },
    # ---- Tier 3: Sketchfab
    "search_sketchfab_models": {"query": "low poly tree", "downloadable": True},
    "download_sketchfab_model": {"uid": "<uid from search results>"},
}


def _msg(text: str) -> list[Message]:
    """Wrap a string in the user-role Message shape FastMCP expects.

    Modern FastMCP (3.3+) expects ``Message(content, role=...)`` rather than
    the older ``PromptMessage(role=..., content=TextContent(...))`` form.
    ``Message`` auto-serializes strings into the wire shape.
    """
    return [Message(text, role="user")]


class BlenderPromptsComponent(MCPMixin):
    """8 skeletal prompts for Blender scripting + dispatch.

    Pure templates — no bus dispatch, no addon round-trip. Safe to
    register in both stdio and HTTP builds. ``mcp_prompt`` produces
    prompts registered at the FastMCP server's prompt registry; clients
    expose them via ``list_prompts`` / ``get_prompt``.
    """

    @mcp_prompt()
    def script_writing_assistant(self, goal: str) -> list[Message]:
        """Outline the bpy patterns that fit the user's stated goal."""
        text = (
            f"You're writing a Blender Python script. Goal: {goal}\n\n"
            "Standard imports + safety boilerplate:\n"
            "```python\n"
            "import bpy, bmesh, mathutils\n"
            "from mathutils import Vector, Matrix\n"
            "# Get the active scene + collection up front; never assume context.\n"
            "scene = bpy.context.scene\n"
            "collection = bpy.context.collection\n"
            "```\n\n"
            "When choosing an API surface:\n"
            "- **bpy.ops.*** — high-level operators (UI-equivalent). Works only\n"
            "  when the right context is active. Wrap in `bpy.context.temp_override(...)`\n"
            "  if running headless or in a script context that differs from a viewport.\n"
            "- **bpy.data.*** — direct data access (objects, meshes, materials).\n"
            "  Doesn't depend on context. Prefer this for non-interactive scripts.\n"
            "- **bmesh** — low-level mesh editing. Required for vertex/edge/face\n"
            "  topology work that operators can't express.\n\n"
            "Always finish a script with `bpy.context.view_layer.update()` if you\n"
            "mutated geometry, and `bpy.ops.wm.save_mainfile()` if the goal includes\n"
            "persisting changes. Use `blender_execute_code(code=...)` to dispatch."
        )
        return _msg(text)

    @mcp_prompt()
    def modeling_workflow(self, object_type: str) -> list[Message]:
        """bmesh vs operator vs modifier decision tree for a target object type."""
        text = (
            f"You want to model: {object_type}\n\n"
            "Decision tree for Blender modeling:\n\n"
            "1. **Operator path** (`bpy.ops.mesh.primitive_*_add`) — fastest when\n"
            "   the shape is one of Blender's primitives (cube, uv_sphere, cylinder,\n"
            "   ico_sphere, cone, torus, plane). Combine with `bpy.ops.transform.*`\n"
            "   for positioning. Cheapest cognitive load.\n\n"
            "2. **Modifier path** — start from a primitive then layer modifiers\n"
            "   (Subdivision Surface, Solidify, Mirror, Array, Boolean). Best for\n"
            "   parametric, easily-tweakable results. Use\n"
            "   `obj.modifiers.new(name=..., type='SUBSURF')` to add programmatically.\n\n"
            "3. **bmesh path** — drop down when you need vertex-level control\n"
            "   (custom topology, procedural extrusion, non-primitive shapes):\n"
            "   ```python\n"
            "   bm = bmesh.new()\n"
            "   bmesh.ops.create_cube(bm, size=1.0)\n"
            "   # ...mutate bm.verts, bm.edges, bm.faces...\n"
            "   me = bpy.data.meshes.new('Foo')\n"
            "   bm.to_mesh(me); bm.free()\n"
            "   obj = bpy.data.objects.new('Foo', me); collection.objects.link(obj)\n"
            "   ```\n\n"
            "4. **Geometry Nodes** — non-destructive, procedural, harder to script\n"
            "   from Python. Reach for it only if the result needs runtime parameters\n"
            "   the user will tweak from the UI."
        )
        return _msg(text)

    @mcp_prompt()
    def rendering_setup(self, engine: str = "cycles") -> list[Message]:
        """Cycles/Eevee setup checklist for a target render."""
        normalized = engine.lower()
        engine_block = (
            "scene.render.engine = 'CYCLES'\n"
            "scene.cycles.device = 'GPU'  # requires Preferences > System > CUDA/OptiX/Metal\n"
            "scene.cycles.samples = 128\n"
            "scene.cycles.use_denoising = True"
        ) if normalized == "cycles" else (
            "scene.render.engine = 'BLENDER_EEVEE_NEXT'  # 4.2+; use 'BLENDER_EEVEE' on older\n"
            "scene.eevee.taa_render_samples = 64\n"
            "scene.eevee.use_raytracing = True  # 4.2+ Eevee Next only"
        )
        text = (
            f"Render setup checklist for `{engine}`:\n\n"
            "Render engine + sampling:\n"
            "```python\n"
            "import bpy\n"
            "scene = bpy.context.scene\n"
            f"{engine_block}\n"
            "```\n\n"
            "Resolution + output:\n"
            "```python\n"
            "scene.render.resolution_x = 1920\n"
            "scene.render.resolution_y = 1080\n"
            "scene.render.resolution_percentage = 100\n"
            "scene.render.filepath = '/tmp/render_'  # frame number is appended\n"
            "scene.render.image_settings.file_format = 'PNG'\n"
            "scene.render.image_settings.color_depth = '16'\n"
            "```\n\n"
            "Camera + world:\n"
            "- Confirm `scene.camera` is set (often None in freshly-created scenes)\n"
            "- For Cycles, build the world out of `ShaderNodeTexEnvironment +\n"
            "  ShaderNodeBackground` (PolyHaven HDRIs ship this graph automatically)\n\n"
            "Dispatch:\n"
            "```\n"
            "blender_execute_code(code=\"bpy.ops.render.render(write_still=True)\")\n"
            "```"
        )
        return _msg(text)

    @mcp_prompt()
    def material_creation(self, material_kind: str) -> list[Message]:
        """Node-tree skeleton for a target material kind."""
        text = (
            f"Material recipe for: {material_kind}\n\n"
            "Skeletal Principled-BSDF setup. Customize the input values for your kind.\n\n"
            "```python\n"
            "import bpy\n"
            "mat = bpy.data.materials.new(name='MyMaterial')\n"
            "mat.use_nodes = True\n"
            "nt = mat.node_tree\n"
            "nt.nodes.clear()\n"
            "\n"
            "bsdf = nt.nodes.new('ShaderNodeBsdfPrincipled')\n"
            "bsdf.location = (0, 0)\n"
            "out = nt.nodes.new('ShaderNodeOutputMaterial')\n"
            "out.location = (300, 0)\n"
            "nt.links.new(bsdf.outputs['BSDF'], out.inputs['Surface'])\n"
            "\n"
            "# --- tune for your material_kind ---\n"
            "bsdf.inputs['Base Color'].default_value = (0.8, 0.2, 0.2, 1.0)\n"
            "bsdf.inputs['Roughness'].default_value = 0.4\n"
            "bsdf.inputs['Metallic'].default_value = 0.0\n"
            "# transmission for glass: bsdf.inputs['Transmission Weight'].default_value = 1.0\n"
            "# emission for screens:  bsdf.inputs['Emission Color'].default_value = (...)\n"
            "#                        bsdf.inputs['Emission Strength'].default_value = 2.0\n"
            "\n"
            "# Apply to active object\n"
            "obj = bpy.context.active_object\n"
            "if obj.data.materials:\n"
            "    obj.data.materials[0] = mat\n"
            "else:\n"
            "    obj.data.materials.append(mat)\n"
            "```\n\n"
            "For texture-driven materials, use `blender_download_polyhaven_asset`\n"
            "with `asset_type=\"textures\"` then `blender_set_texture` to skip the\n"
            "node-tree boilerplate entirely."
        )
        return _msg(text)

    @mcp_prompt()
    def animation_basics(self, animation_type: str) -> list[Message]:
        """Keyframe vs driver vs constraint decision guide."""
        text = (
            f"Animation approach for: {animation_type}\n\n"
            "Three primary mechanisms, picked by how the motion is *driven*:\n\n"
            "**Keyframes** — when motion is hand-authored on a timeline.\n"
            "```python\n"
            "import bpy\n"
            "obj = bpy.context.active_object\n"
            "obj.location = (0, 0, 0)\n"
            "obj.keyframe_insert(data_path='location', frame=1)\n"
            "obj.location = (5, 0, 0)\n"
            "obj.keyframe_insert(data_path='location', frame=60)\n"
            "# F-curve interpolation defaults to bezier; change with\n"
            "# obj.animation_data.action.fcurves[i].keyframe_points[j].interpolation\n"
            "```\n\n"
            "**Drivers** — when one property is a formula of others (rig logic,\n"
            "derived values, parametric).\n"
            "```python\n"
            "fcurve = obj.driver_add('rotation_euler', 2)  # Z axis\n"
            "drv = fcurve.driver\n"
            "drv.expression = 'frame * 0.1'\n"
            "```\n\n"
            "**Constraints** — when motion follows another object (follow path,\n"
            "track to, copy transform, IK).\n"
            "```python\n"
            "c = obj.constraints.new(type='TRACK_TO')\n"
            "c.target = bpy.data.objects['Camera']\n"
            "c.track_axis = 'TRACK_NEGATIVE_Z'\n"
            "c.up_axis = 'UP_Y'\n"
            "```\n\n"
            "Don't forget to set the scene's frame range:\n"
            "`scene.frame_start = 1; scene.frame_end = 120`"
        )
        return _msg(text)

    @mcp_prompt()
    def debug_assistant(self, error_text: str) -> list[Message]:
        """Interpret a Blender traceback from a dispatched script and suggest a fix."""
        text = (
            "A script dispatched via `blender_execute_code` returned an error.\n"
            "Traceback:\n\n"
            "```\n"
            f"{error_text}\n"
            "```\n\n"
            "Diagnostic checklist (work top to bottom):\n\n"
            "1. **Context errors** (`RuntimeError: Operator bpy.ops.X.poll() failed,\n"
            "   context is incorrect`) — `bpy.ops.*` needs a specific UI context.\n"
            "   Wrap the call in `with bpy.context.temp_override(area=..., region=...)`\n"
            "   or rewrite using `bpy.data.*` instead. Most often: select the object\n"
            "   first (`bpy.context.view_layer.objects.active = obj; obj.select_set(True)`).\n\n"
            "2. **KeyError on bpy.data.X['name']** — the object/material/mesh doesn't\n"
            "   exist. Run `blender_get_scene_info` or read `blender://scene/objects`\n"
            "   to confirm the actual name. Names get suffixed (`.001`, `.002`) when\n"
            "   duplicates are added.\n\n"
            "3. **AttributeError on a node socket** — Blender renames sockets between\n"
            "   versions (Principled BSDF's `'Transmission'` became `'Transmission Weight'`\n"
            "   in 4.0+). Check `[s.name for s in node.inputs]`.\n\n"
            "4. **Pickle/segfault** — never serialize bpy objects across threads. The\n"
            "   addon's executor runs everything on the main thread for this reason.\n\n"
            "5. **No traceback, just blank result** — likely a `print` that wrote to\n"
            "   stdout but the addon captured stderr. Read `blender://console/error`\n"
            "   to see the addon-side log."
        )
        return _msg(text)

    @mcp_prompt()
    def asset_creation_strategy(self, asset_kind: str) -> list[Message]:
        """Hyper3D vs PolyHaven vs Sketchfab vs manual — pick the right pipeline."""
        text = (
            f"Asset acquisition strategy for: {asset_kind}\n\n"
            "Four pipelines, ordered by speed-to-result:\n\n"
            "**1. Hyper3D Rodin (text-to-3D)** — best when the asset doesn't exist\n"
            "   in any catalog (custom geometry, stylized shapes, novel objects).\n"
            "   Returns a clean GLB. ~30-60s per generation.\n"
            "   ```\n"
            "   blender_create_rodin_job(text_prompt=\"a wooden chair, low poly\")\n"
            "   blender_poll_rodin_job_status(subscription_key=\"...\")\n"
            "   blender_import_generated_asset(name=\"MyChair\", task_uuid=\"...\")\n"
            "   ```\n\n"
            "**2. PolyHaven** — high-quality HDRIs, PBR textures, and CC0 models.\n"
            "   Best for environments + surfaces. Free, no API key.\n"
            "   ```\n"
            "   blender_search_polyhaven_assets(asset_type=\"hdris\", categories=\"outdoor\")\n"
            "   blender_download_polyhaven_asset(asset_id=\"...\", asset_type=\"hdris\")\n"
            "   ```\n\n"
            "**3. Sketchfab** — vast catalog of user-uploaded models. Better for\n"
            "   recognizable objects (vehicles, props, characters). Requires API key.\n"
            "   ```\n"
            "   blender_search_sketchfab_models(query=\"low poly tree\")\n"
            "   blender_download_sketchfab_model(uid=\"...\")\n"
            "   ```\n\n"
            "**4. Manual via `blender_execute_code`** — when you need exact\n"
            "   geometry control, or the asset is a procedural primitive. Use\n"
            "   `blender_modeling_workflow` prompt for guidance on the right API.\n\n"
            "Combine freely: Rodin a custom shape, PolyHaven a wood texture for it,\n"
            "Sketchfab a backdrop tree, manual procedural ground plane."
        )
        return _msg(text)

    @mcp_prompt()
    def dispatch_recipe(self, command: str) -> list[Message]:
        """Emit the exact ``blender_<command>`` MCP call shape for one of the 24 commands.

        ``command`` is the bare addon command name (e.g. ``get_scene_info``,
        ``download_polyhaven_asset``). The prompt returns the corresponding
        ``blender_<command>(...)`` invocation with all required + sensible
        default kwargs filled in.
        """
        normalized = command.removeprefix("blender_")
        recipe = _DISPATCH_RECIPES.get(normalized)

        if recipe is None:
            known = "\n".join(f"  - {k}" for k in sorted(_DISPATCH_RECIPES))
            text = (
                f"Unknown command: {command!r}\n\n"
                "Known dispatch commands (24 total):\n"
                f"{known}\n\n"
                "Pass any of these names (with or without the ``blender_`` prefix)\n"
                "to ``dispatch_recipe`` for its canonical MCP call shape."
            )
            return _msg(text)

        # Render kwargs as ``key=<json-literal>`` for readability.
        import json
        if recipe:
            kwarg_lines = ",\n    ".join(
                f"{k}={json.dumps(v)}" for k, v in recipe.items()
            )
            call = f"blender_{normalized}(\n    {kwarg_lines}\n)"
        else:
            call = f"blender_{normalized}()"

        text = (
            f"Canonical MCP call for ``{normalized}``:\n\n"
            "```python\n"
            f"{call}\n"
            "```\n\n"
            "All dispatch tools also accept these optional kwargs:\n"
            "- ``target_uuid: str`` — explicit Blender client UUID (omit to auto-pick\n"
            "  if exactly one is connected; required when multiple are)\n"
            "- ``_timeout: float`` — seconds to wait for the addon's reply before\n"
            "  returning ``{\"status\": \"timeout\"}``\n\n"
            "Return shape (JSON string):\n"
            "```json\n"
            "{\n"
            "  \"status\": \"completed\" | \"failed\" | \"timeout\" | \"no_client\" |\n"
            "             \"ambiguous_target\" | \"unknown_target\",\n"
            f"  \"command\": {json.dumps(normalized)},\n"
            "  \"target_uuid\": \"<chosen Blender client UUID>\",\n"
            "  \"job_id\": \"j-<12 hex chars>\",\n"
            "  \"result\": <addon-side return value>,\n"
            "  \"error\": \"<empty string on success>\"\n"
            "}\n"
            "```"
        )
        return _msg(text)
