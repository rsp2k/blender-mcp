"""The binder clip: what the user attaches to their next chat message.

The selection stays clipped until turned off (it's what "this" means);
the viewport and a text block are clipped for the next message only.
Main thread only (reads bpy context).
"""

from __future__ import annotations

MAX_SELECTION = 50
OWN_TEXT = "BlenderMCP Chat"


def _vec(v) -> list[float]:
    return [round(float(x), 4) for x in v]


def selection_items(objects) -> list[dict]:
    items = []
    for ob in list(objects)[:MAX_SELECTION]:
        item = {"name": ob.name, "type": ob.type}
        try:
            item["location"] = _vec(ob.matrix_world.translation)
            item["dimensions"] = _vec(ob.dimensions)
        except (AttributeError, TypeError, ValueError):
            pass
        items.append(item)
    return items


def labels(n_selected: int, viewport: bool, text_name: str) -> list[str]:
    out = []
    if n_selected:
        out.append(f"{n_selected} selected")
    if viewport:
        out.append("viewport")
    if text_name:
        out.append(f'text "{text_name}"')
    return out


def summary(context) -> str:
    """One line for the panel: what the next message will carry."""
    wm = context.window_manager
    n = len(context.selected_objects) if wm.blendermcp_clip_selection else 0
    parts = labels(n, wm.blendermcp_clip_viewport, wm.blendermcp_clip_text)
    return "Clipped: " + ", ".join(parts) if parts else "Nothing clipped"


_AXES = ((0, 0, -1, "down, from the top"), (0, 0, 1, "up, from below"),
         (0, 1, 0, "along +Y, from the front"), (0, -1, 0, "along -Y, from the back"),
         (1, 0, 0, "along +X, from the left"), (-1, 0, 0, "along -X, from the right"))


def view_facing(forward) -> str:
    """Which way the viewport looks, when it's close to an axis."""
    fx, fy, fz = (float(v) for v in forward)
    best = max(_AXES, key=lambda a: a[0] * fx + a[1] * fy + a[2] * fz)
    dot = best[0] * fx + best[1] * fy + best[2] * fz
    return best[3] if dot > 0.94 else "at an angle"


def work_context(context) -> dict:
    """Where the user is working: mode, active object, frame, cursor, units,
    and the view. Always sent; small, and it settles "here" and "this"."""
    out: dict = {}
    try:
        scene = context.scene
        out["mode"] = context.mode
        active = context.view_layer.objects.active
        if active is not None:
            out["active"] = active.name
        out["frame"] = scene.frame_current
        out["cursor"] = _vec(scene.cursor.location)
        us = scene.unit_settings
        out["units"] = {"system": us.system, "length": us.length_unit,
                        "scale": round(float(us.scale_length), 6)}
        if bpy_file := getattr(context.blend_data, "filepath", ""):
            out["file"] = bpy_file.rsplit("/", 1)[-1]
        area = next((a for a in context.screen.areas if a.type == 'VIEW_3D'), None)
        if area is not None:
            r3d = area.spaces.active.region_3d
            from mathutils import Vector
            forward = r3d.view_rotation @ Vector((0, 0, -1))
            out["view"] = {"perspective": r3d.view_perspective.lower(),
                           "looking": view_facing(forward),
                           "shading": area.spaces.active.shading.type.lower()}
        if context.mode == 'EDIT_MESH' and active is not None and active.type == 'MESH':
            me = active.data
            out["edit_selection"] = {"verts": me.total_vert_sel, "edges": me.total_edge_sel,
                                     "faces": me.total_face_sel}
    except (AttributeError, TypeError, ValueError):
        pass
    return out


def gather(context) -> tuple[dict, list[str]]:
    """(attachments for blender_chat, short labels for the transcript)."""
    import bpy

    wm = context.window_manager
    out: dict = {"context": work_context(context)}
    selected = list(context.selected_objects) if wm.blendermcp_clip_selection else []
    if selected:
        out["selection"] = selection_items(selected)
    if wm.blendermcp_clip_viewport:
        out["viewport"] = True
    text_name = wm.blendermcp_clip_text
    text = bpy.data.texts.get(text_name) if text_name else None
    if text is not None and text_name != OWN_TEXT:
        out["text"] = {"name": text.name, "body": text.as_string()}
    else:
        text_name = ""
    return out, labels(len(selected), bool(out.get("viewport")), text_name)


def after_send(context) -> None:
    """The viewport and text clips are one-message; the selection clip stays."""
    wm = context.window_manager
    wm.blendermcp_clip_viewport = False
    wm.blendermcp_clip_text = ""


def register_props() -> None:
    import bpy
    wm = bpy.types.WindowManager
    wm.blendermcp_clip_selection = bpy.props.BoolProperty(
        name="Selection", default=True,
        description="Send the selected objects' names, types, positions and sizes, "
                    "so \"this\" and \"these\" mean them")
    wm.blendermcp_clip_viewport = bpy.props.BoolProperty(
        name="What I see", default=False,
        description="Send a picture of the 3D viewport with the next message")
    wm.blendermcp_clip_text = bpy.props.StringProperty(
        name="Text", default="",
        description="Send a text block's contents with the next message (notes, a spec, a script)")


def unregister_props() -> None:
    import bpy
    wm = bpy.types.WindowManager
    for name in ("blendermcp_clip_selection", "blendermcp_clip_viewport", "blendermcp_clip_text"):
        if hasattr(wm, name):
            delattr(wm, name)
