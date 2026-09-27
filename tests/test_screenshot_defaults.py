"""get_viewport_screenshot: filepath is optional and defaults to a temp file."""

import asyncio
import datetime
import inspect
import os

import pytest

from addon import screenshot_paths as sp
from blender_mcp.dispatch_component import BlenderDispatchComponent


def test_filepath_is_optional_in_the_tool_signature():
    sig = inspect.signature(BlenderDispatchComponent.get_viewport_screenshot)
    assert sig.parameters["filepath"].default is None


@pytest.fixture
def comp(monkeypatch):
    c = BlenderDispatchComponent()
    sent = []

    async def fake_call(ctx, command, params, target_uuid, timeout, bus_id=None, store_as=None):
        sent.append((command, params))
        return "{}"

    monkeypatch.setattr(c, "_call", fake_call)
    c.sent = sent
    return c


def test_omitted_filepath_is_not_sent(comp):
    asyncio.run(comp.get_viewport_screenshot(max_size=800))
    assert comp.sent[-1] == ("get_viewport_screenshot", {"max_size": 800, "format": "png"})


def test_given_filepath_is_passed_through(comp):
    asyncio.run(comp.get_viewport_screenshot(filepath="/tmp/x.png"))
    assert comp.sent[-1][1]["filepath"] == "/tmp/x.png"


def test_default_path_is_unique_under_the_subdir(tmp_path):
    now = datetime.datetime(2026, 9, 27, 15, 4, 5, tzinfo=datetime.timezone.utc)
    a = sp.default_screenshot_path("png", base_dir=str(tmp_path), now=now)
    b = sp.default_screenshot_path("png", base_dir=str(tmp_path), now=now)
    assert a != b
    assert os.path.dirname(a) == str(tmp_path / sp.SUBDIR)
    assert os.path.isdir(os.path.dirname(a))
    assert os.path.basename(a).startswith("viewport-20260927-150405-")


@pytest.mark.parametrize("fmt,ext", [("png", "png"), ("JPG", "jpg"), ("jpeg", "jpg"),
                                     ("exr", "exr"), ("targa", "tga"), ("", "png")])
def test_extension_matches_format(fmt, ext):
    assert sp.extension_for(fmt) == ext


def test_falls_back_to_os_temp_dir():
    path = sp.default_screenshot_path("png", base_dir=None)
    assert sp.SUBDIR in path
    os.rmdir(os.path.dirname(path)) if not os.listdir(os.path.dirname(path)) else None
