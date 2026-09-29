"""place_object's absolute rotation, and duplicate_object in the chat's tool list."""

import pytest

from blender_mcp.chat.catalog import DEFAULT_TOOLS
from blender_mcp.dispatch_component import placement_params


def test_rotation_alone_is_enough():
    p = placement_params("Crate", rotation=[0, 0, 0])
    assert p["rotation"] == [0.0, 0.0, 0.0] and "rotate_by" not in p


def test_rotation_and_target_conflict():
    with pytest.raises(ValueError, match="either target or rotation"):
        placement_params("Cam", target=[0, 0, 0], rotation=[0, 0, 90])


def test_old_call_shape_unchanged():
    assert "rotation" not in placement_params("Crate", location=[1, 2, 3])


def test_chat_can_duplicate_without_python():
    assert "duplicate_object" in DEFAULT_TOOLS
    assert not DEFAULT_TOOLS["duplicate_object"].needs_confirm({})
