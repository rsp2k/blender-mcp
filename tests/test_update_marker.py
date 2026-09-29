"""addon/update_marker.py: "Updated to X" after the reload."""

import importlib.util
import json
import os
import time
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "update_marker", Path(__file__).resolve().parents[1] / "addon" / "update_marker.py")
marker = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(marker)


def test_marker_for_the_running_version_is_reported_once(tmp_path):
    marker.write(str(tmp_path), "2026.928.8", "2026.928.7")
    got = marker.consume(str(tmp_path), "2026.928.8")
    assert got["version"] == "2026.928.8" and got["previous"] == "2026.928.7"
    assert marker.consume(str(tmp_path), "2026.928.8") is None


def test_marker_for_another_version_is_dropped(tmp_path):
    marker.write(str(tmp_path), "2026.928.9", "2026.928.7")
    assert marker.consume(str(tmp_path), "2026.928.7") is None
    assert not os.path.exists(tmp_path / marker.MARKER)


def test_stale_marker_is_ignored(tmp_path):
    (tmp_path / marker.MARKER).write_text(json.dumps(
        {"version": "1", "previous": "0", "at": time.time() - marker.MAX_AGE_S - 5}))
    assert marker.consume(str(tmp_path), "1") is None
