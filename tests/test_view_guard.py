"""addon/view_guard.py and the chat's temporary look_at_viewport viewpoint."""

import importlib.util
from pathlib import Path

import pytest

from blender_mcp.chat import vision

_spec = importlib.util.spec_from_file_location(
    "view_guard", Path(__file__).resolve().parents[1] / "addon" / "view_guard.py")
view_guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(view_guard)


class Vec(list):
    def copy(self):
        return Vec(self)


class Shading:
    def __init__(self):
        self.type, self.color_type, self.light = "SOLID", "MATERIAL", "STUDIO"


class RV3D:
    def __init__(self):
        self.view_location = Vec([0, 0, 0])
        self.view_rotation = Vec([1, 0, 0, 0])
        self.view_distance = 10.0
        self.view_perspective = "PERSP"


class Space:
    def __init__(self):
        self.region_3d, self.shading = RV3D(), Shading()


def _move(space):
    space.region_3d.view_location[0] = 5  # in place, like mathutils vectors
    space.region_3d.view_rotation = Vec([0, 1, 0, 0])
    space.region_3d.view_distance = 2.5
    space.region_3d.view_perspective = "ORTHO"
    space.shading.type = "MATERIAL"


def test_view_and_shading_come_back_exactly():
    space = Space()
    with view_guard.preserved([space]):
        _move(space)
    assert space.region_3d.view_location == [0, 0, 0]
    assert space.region_3d.view_rotation == [1, 0, 0, 0]
    assert space.region_3d.view_distance == 10.0
    assert space.region_3d.view_perspective == "PERSP"
    assert space.shading.type == "SOLID"


def test_restored_even_when_the_capture_fails():
    space = Space()
    with pytest.raises(RuntimeError), view_guard.preserved([space]):
        _move(space)
        raise RuntimeError("render failed")
    assert space.shading.type == "SOLID"
    assert space.region_3d.view_distance == 10.0


def test_disabled_guard_leaves_the_change():
    space = Space()
    with view_guard.preserved([space], enabled=False):
        _move(space)
    assert space.shading.type == "MATERIAL"


def test_view_args_keeps_only_viewpoint_keys():
    args = {"question": "q", "angle": "top", "frame": "Tower 1", "shading": "", "x": 1}
    assert vision.view_args(args) == {"angle": "top", "frame": ["Tower 1"]}
    assert vision.view_args(None) == {}


async def test_capture_sends_the_viewpoint_to_the_screenshot():
    calls = []

    class Exec:
        async def call(self, name, args, extra=None):
            calls.append(extra)
            return True, "{}"

    await vision.capture(Exec(), {"angle": "iso", "shading": "MATERIAL"})
    assert calls[0]["angle"] == "iso" and calls[0]["shading"] == "MATERIAL"
    assert calls[0]["deselect"] is True
