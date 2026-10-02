"""Rig details in get_object_info / get_scene_info (addon/rig_info.py)."""

import ast
from pathlib import Path

from addon import rig_info as ri
from blender_mcp.chat.turn import SYSTEM_PROMPT
from blender_mcp.starter_prompts import STARTERS

ROOT = Path(__file__).parents[1]
B_BONES = ["root", "body", "handle.front", "handle.back", "face",
           "eye.L", "pupil.L", "eye.R", "pupil.R"]


def mascot_constants() -> dict:
    """WAVE, HANDLE_CLEAR and HOW_TO_POSE_KEY from build_clip_mascot.py,
    which imports bpy and so can't be imported here."""
    tree = ast.parse((ROOT / "onboarding/build_clip_mascot.py").read_text())
    want = {"WAVE", "HANDLE_CLEAR", "HOW_TO_POSE_KEY"}
    out = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            name = getattr(node.targets[0], "id", None)
            if name in want:
                out[name] = ast.literal_eval(node.value)
    assert set(out) == want
    return out


# ---- how_to_pose -------------------------------------------------------------

def test_how_to_pose_trims_and_ignores_non_text():
    assert ri.how_to_pose("  Pose the\n bones.  ") == "Pose the bones."
    for value in (None, "", "   ", 3, 1.5, ["x"], {"a": 1}):
        assert ri.how_to_pose(value) is None


def test_how_to_pose_is_capped():
    text = ri.how_to_pose("x" * (ri.MAX_HOW_TO_POSE_CHARS + 50))
    assert len(text) == ri.MAX_HOW_TO_POSE_CHARS and text.endswith("…")


def test_mascot_uses_the_same_property_name():
    assert mascot_constants()["HOW_TO_POSE_KEY"] == ri.HOW_TO_POSE_KEY


# ---- get_object_info ---------------------------------------------------------

def test_unparented_object_adds_nothing():
    assert ri.parent_fields(None, "OBJECT", "") == {}


def test_object_parent_is_just_named():
    assert ri.parent_fields("Table", "OBJECT", "") == {"parent": "Table"}


def test_bone_parent_names_the_bone_and_says_to_pose_it():
    out = ri.parent_fields("B. Clip", "BONE", "handle.front")
    assert out["parent"] == "B. Clip" and out["parent_type"] == "BONE"
    assert out["parent_bone"] == "handle.front"
    assert "pose bone" in out["pose_note"] and "rather than this object" in out["pose_note"]


def test_armature_lists_bones_and_the_path_to_key():
    out = ri.armature_fields(B_BONES, ["XYZ"] * len(B_BONES))
    assert out["bone_count"] == 9 and out["bones"] == B_BONES
    assert out["bone_rotation_mode"] == "XYZ"
    assert out["pose_path"] == 'pose.bones["<bone>"].rotation_euler'
    assert "bones_note" not in out


def test_armature_rotation_modes():
    assert ri.armature_fields(["a"], ["QUATERNION"])["pose_path"].endswith(".rotation_quaternion")
    assert ri.armature_fields(["a"], ["AXIS_ANGLE"])["pose_path"].endswith(".rotation_axis_angle")
    assert ri.armature_fields(["a"], ["ZXY"])["pose_path"].endswith(".rotation_euler")
    mixed = ri.armature_fields(["a", "b"], ["XYZ", "QUATERNION"])
    assert mixed["bone_rotation_mode"] == "mixed" and "pose_path" not in mixed
    assert "bone_rotation_mode" not in ri.armature_fields([], [])


def test_big_armature_is_capped():
    names = [f"b{i}" for i in range(ri.MAX_BONES + 5)]
    out = ri.armature_fields(names, ["QUATERNION"] * len(names))
    assert out["bone_count"] == len(names) and len(out["bones"]) == ri.MAX_BONES
    assert out["bones_note"] == f"first {ri.MAX_BONES} of {len(names)} bones"


# ---- get_scene_info ----------------------------------------------------------

def test_scene_rigs_brings_the_hint_and_fewer_bones():
    bones = [f"b{i}" for i in range(30)]
    out = ri.scene_rigs([{"name": "Rig", "bones": bones, "how_to_pose": " Pose me. "}])
    assert out == [{"name": "Rig", "bone_count": 30, "bones": bones[:ri.MAX_SCENE_BONES],
                    "how_to_pose": "Pose me."}]


def test_scene_rigs_puts_self_describing_rigs_first_and_caps():
    rigs = [{"name": f"plain{i}", "bones": ["a"]} for i in range(5)]
    rigs.append({"name": "B. Clip", "bones": B_BONES, "how_to_pose": "Pose bones."})
    out = ri.scene_rigs(rigs)
    assert len(out) == ri.MAX_SCENE_RIGS
    assert out[0]["name"] == "B. Clip" and out[0]["how_to_pose"] == "Pose bones."
    assert all("how_to_pose" not in r for r in out[1:])


def test_scene_without_armatures_has_no_rigs():
    assert ri.scene_rigs([]) == []


# ---- the B. Clip starters and the chat prompt ------------------------------

def starter(name):
    return next(s for s in STARTERS if s.name == name)


def test_wave_starter_names_the_bone_and_a_measured_range():
    c = mascot_constants()
    lo, hi = c["WAVE"]
    clear_lo, clear_hi = c["HANDLE_CLEAR"]["front"]
    assert clear_lo < lo < 0 < hi < clear_hi  # the wave sits well inside the clear range
    text = starter("starter_b_wave").text
    assert "handle.front bone" in text and "B. Clip armature" in text
    assert f"between {lo} and {hi} degrees" in text and "Y axis" in text


def test_top_hat_starter_parents_to_a_bone():
    assert "body bone of his rig" in starter("starter_b_top_hat").text


def test_desk_starter_moves_the_rig():
    assert "Move his rig" in starter("starter_b_desk").text


def test_chat_prompt_says_to_pose_bones_not_parts():
    p = " ".join(SYSTEM_PROMPT.split())
    assert "parent_bone in get_object_info" in p
    assert "never the part itself" in p and "how_to_pose" in p
