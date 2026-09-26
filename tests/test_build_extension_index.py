"""Per-platform extension index: matches Blender's server-generate rules."""

import importlib.util
import zipfile
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "build_extension", Path(__file__).resolve().parent.parent / "scripts" / "build_extension.py"
)
be = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(be)


def _archive(tmp_path: Path, platform: str, wheels: list[str], version: str = "2026.926.99") -> Path:
    manifest = be._render_manifest(version, wheels, platform)
    path = tmp_path / be.archive_name(version, platform)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("blender_manifest.toml", manifest)
        for w in wheels:
            zf.writestr(f"wheels/{w}", b"")
    return path


def test_archive_name_matches_split_platforms_convention():
    assert be.archive_name("2026.926.13", "linux-x64") == "blender_mcp-2026.926.13-linux_x64.zip"
    assert be.archive_name("2026.926.13", "macos-arm64") == "blender_mcp-2026.926.13-macos_arm64.zip"


@pytest.mark.parametrize("wheels,expected", [
    (["a-1-cp311-cp311-manylinux2014_x86_64.whl", "b-1-py3-none-any.whl"], ["3.11"]),
    (["a-1-cp311-cp311-win_amd64.whl", "a-1-cp313-cp313-win_amd64.whl"], ["3.11", "3.13"]),
    (["cryptography-50-cp311-abi3-manylinux_2_28_x86_64.whl"], ["3"]),
    (["c-1-cp311-abi3-win_amd64.whl", "d-1-cp313-cp313-win_amd64.whl"], ["3.13"]),
    (["e-1-py2.py3-none-any.whl"], ["3"]),
])
def test_python_versions_follow_blender_rules(wheels, expected):
    assert be.python_versions_from_wheels(wheels) == expected


def test_index_has_one_entry_per_platform_archive(tmp_path):
    zips = [
        _archive(tmp_path, "linux-x64", ["a-1-cp313-cp313-manylinux2014_x86_64.whl", "b-1-py3-none-any.whl"]),
        _archive(tmp_path, "windows-x64", ["a-1-cp313-cp313-win_amd64.whl", "b-1-py3-none-any.whl"]),
    ]
    index = be.build_index(zips)
    assert index["version"] == "v1" and index["blocklist"] == []
    by_platform = {tuple(e["platforms"]): e for e in index["data"]}
    assert set(by_platform) == {("linux-x64",), ("windows-x64",)}
    linux = by_platform[("linux-x64",)]
    assert linux["id"] == "blender_mcp" and linux["version"] == "2026.926.99"
    assert linux["archive_url"] == "./blender_mcp-2026.926.99-linux_x64.zip"
    assert linux["archive_hash"].startswith("sha256:") and linux["archive_size"] > 0
    assert linux["python_versions"] == ["3.13"]
    assert "wheels" not in linux and "permissions" in linux


def test_overlapping_platforms_are_refused(tmp_path):
    a = _archive(tmp_path, "linux-x64", ["b-1-py3-none-any.whl"])
    b = tmp_path / "blender_mcp-2026.926.99-linux_x64-copy.zip"
    b.write_bytes(a.read_bytes())
    with pytest.raises(ValueError, match="both claim linux-x64"):
        be.build_index([a, b])
