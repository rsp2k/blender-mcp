"""What get_object_info and get_scene_info say about rigs (no bpy import).

A mesh parented to a bone moves with that bone, so animating the mesh
object itself swings it about the wrong pivot. Chat models only find the
bones if the scene tools name them, so these helpers turn plain data from
the handlers into a few small, capped fields: the parent bone of an object,
an armature's bone names, and the optional ``how_to_pose`` custom property
a scene author can put on any object (the B. Clip template does).
"""

from __future__ import annotations

HOW_TO_POSE_KEY = "how_to_pose"
MAX_HOW_TO_POSE_CHARS = 1200
MAX_BONES = 40  # names listed by get_object_info
MAX_SCENE_RIGS = 3  # armatures summarised by get_scene_info
MAX_SCENE_BONES = 16  # names per armature there; the system prompt is short
_ROTATION_PROPS = {"QUATERNION": "rotation_quaternion", "AXIS_ANGLE": "rotation_axis_angle"}


def how_to_pose(value) -> str | None:
    """The ``how_to_pose`` text, whitespace-trimmed and capped; None unless a
    non-empty string (custom properties can hold anything)."""
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    if not text:
        return None
    if len(text) > MAX_HOW_TO_POSE_CHARS:
        text = text[: MAX_HOW_TO_POSE_CHARS - 1] + "…"
    return text


def parent_fields(parent: str | None, parent_type: str | None, parent_bone: str | None) -> dict:
    """``parent`` (and ``parent_bone`` for bone parenting) for an object's info;
    empty for an unparented object, so existing output doesn't change."""
    if not parent:
        return {}
    out = {"parent": parent}
    if parent_type and parent_type != "OBJECT":
        out["parent_type"] = parent_type
    if parent_type == "BONE" and parent_bone:
        out["parent_bone"] = parent_bone
        out["pose_note"] = (f"moves with bone {parent_bone!r} of {parent!r}; to pose or "
                            "animate it, rotate that pose bone rather than this object")
    return out


def armature_fields(bone_names: list[str], rotation_modes: list[str], limit: int = MAX_BONES) -> dict:
    """Bone names (capped) and how their rotation is stored, for an armature.

    ``rotation_modes`` holds each pose bone's rotation_mode in the same order;
    one shared mode comes back as a string, otherwise "mixed".
    """
    out: dict = {"bone_count": len(bone_names), "bones": list(bone_names[:limit])}
    if len(bone_names) > limit:
        out["bones_note"] = f"first {limit} of {len(bone_names)} bones"
    modes = sorted(set(rotation_modes))
    if len(modes) == 1:
        out["bone_rotation_mode"] = modes[0]
        prop = _ROTATION_PROPS.get(modes[0], "rotation_euler")  # the rest are Euler orders
        out["pose_path"] = f'pose.bones["<bone>"].{prop}'
    elif modes:
        out["bone_rotation_mode"] = "mixed"
    return out


def scene_rigs(rigs: list[dict]) -> list[dict]:
    """The ``rigs`` entry of get_scene_info: at most MAX_SCENE_RIGS armatures,
    each ``{"name", "bone_count", "bones", "how_to_pose"?}`` with fewer bone
    names than get_object_info gives.

    ``rigs`` items carry ``name``, ``bones`` (names) and optionally
    ``how_to_pose`` (the raw custom property value). Armatures with a
    how_to_pose text come first, since they are the ones that explain
    themselves.
    """
    ordered = sorted(rigs, key=lambda r: how_to_pose(r.get("how_to_pose")) is None)
    out = []
    for rig in ordered[:MAX_SCENE_RIGS]:
        bones = list(rig.get("bones") or [])
        row = {"name": rig["name"], "bone_count": len(bones), "bones": bones[:MAX_SCENE_BONES]}
        text = how_to_pose(rig.get("how_to_pose"))
        if text:
            row["how_to_pose"] = text
        out.append(row)
    return out
