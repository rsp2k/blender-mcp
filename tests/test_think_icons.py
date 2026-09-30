"""Clip's thinking loop: which frame shows at a given time."""

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "addon_icons", Path(__file__).resolve().parents[1] / "addon" / "ui" / "icons.py")
icons = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(icons)


class _Preview:
    def __init__(self, n):
        self.icon_id = n


def test_frames_cycle_at_the_loop_rate(monkeypatch):
    fake = {f"think_{i:02d}": _Preview(100 + i) for i in range(16)}
    fake["clip_mascot"] = _Preview(1)
    monkeypatch.setattr(icons, "_collection", fake)
    assert icons.think_frame(0.0) == 100
    assert icons.think_frame(1.0 / icons.THINK_FPS) == 101
    assert icons.think_frame(16.0 / icons.THINK_FPS) == 100  # wraps after 16


def test_no_frames_means_no_animation(monkeypatch):
    monkeypatch.setattr(icons, "_collection", {"clip_mascot": _Preview(1)})
    assert icons.think_frame(3.0) == 0
    monkeypatch.setattr(icons, "_collection", None)
    assert icons.think_frame(3.0) == 0
