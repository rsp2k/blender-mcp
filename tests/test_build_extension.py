"""scripts/build_extension.py: the repository index."""

import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import build_extension as be


def test_build_index_uses_the_public_base_when_given(tmp_path, monkeypatch):
    z = tmp_path / "x.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("blender_manifest.toml", 'id = "x"\nversion = "1.0.0"\nname = "X"\n')
    monkeypatch.setenv("EXTENSIONS_BASE_URL", "https://mcp.example/extensions/")
    assert be.index_entry_from_archive(z)["archive_url"] == "https://mcp.example/extensions/x.zip"
    monkeypatch.delenv("EXTENSIONS_BASE_URL")
    assert be.index_entry_from_archive(z)["archive_url"] == "./x.zip"


def test_parse_compiled_reads_uv_output():
    text = """jaraco-classes==3.4.0
    # via keyring
pydantic_core==2.46.5 ; sys_platform == 'win32'
pywin32==312
"""
    assert be.parse_compiled(text) == {
        "jaraco-classes": "3.4.0", "pydantic-core": "2.46.5", "pywin32": "312"}


def test_committed_pins_cover_every_platform_and_python():
    pins = be._load_pins()
    for platform in be.BLENDER_PLATFORMS:
        for py in be.PYTHON_VERSIONS:
            got = pins[platform][py]
            assert got["fastmcp"].split(".")[0] == "3", (platform, py)
            assert "requests" in got


def test_windows_gets_pywin32_and_no_linux_only_packages():
    # mcp declares pywin32 for sys_platform == 'win32'; resolving on a Linux
    # build host used to drop it and add Linux-only keyring backends instead.
    pins = be._load_pins()
    for py in be.PYTHON_VERSIONS:
        assert "pywin32" in pins["windows-x64"][py], py
        for platform in ("linux-x64", "linux-arm64", "macos-x64", "macos-arm64"):
            assert "pywin32" not in pins[platform][py], (platform, py)
        for platform in ("windows-x64", "macos-x64", "macos-arm64"):
            assert "jeepney" not in pins[platform][py], (platform, py)
            assert "secretstorage" not in pins[platform][py], (platform, py)


def test_an_already_built_version_is_reused_not_rebuilt(tmp_path, monkeypatch):
    monkeypatch.setattr(be, "ROOT", tmp_path)
    monkeypatch.setattr(be, "DIST_DIR", tmp_path)
    monkeypatch.setattr(be, "_read_addon_version", lambda: "2026.1.1")
    monkeypatch.setattr(sys, "argv", ["build_extension.py"])
    for p in be.BLENDER_PLATFORMS:
        with zipfile.ZipFile(tmp_path / be.archive_name("2026.1.1", p), "w") as zf:
            zf.writestr("blender_manifest.toml",
                        f'id = "blender_mcp"\nversion = "2026.1.1"\nname = "B"\nplatforms = ["{p}"]\n')
    before = {z.name: z.read_bytes() for z in tmp_path.glob("*.zip")}

    def no_download(*a, **k):
        raise AssertionError("must not download for an already built version")
    monkeypatch.setattr(be, "_download_wheels", no_download)
    be.main()
    assert {z.name: z.read_bytes() for z in tmp_path.glob("*.zip")} == before
    assert (tmp_path / "index.json").exists()
