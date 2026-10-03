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


def test_pins_from_wheels_normalizes_names_and_skips_split_versions(capsys):
    pins = be.pins_from_wheels({
        "linux-x64": [
            "jaraco.classes-3.4.0-py3-none-any.whl",
            "pydantic_core-2.46.5-cp313-cp313-manylinux_2_17_x86_64.whl",
            "cryptography-50.0.2-cp311-abi3-manylinux2014_x86_64.whl",
            "foo-1.0-cp311-cp311-manylinux2014_x86_64.whl",
            "foo-2.0-cp313-cp313-manylinux2014_x86_64.whl",
        ],
        "macos-x64": ["cryptography-48.0.1-cp311-abi3-macosx_10_9_universal2.whl"],
    })
    assert pins["linux-x64"] == {"cryptography": "50.0.2", "jaraco-classes": "3.4.0",
                                 "pydantic-core": "2.46.5"}
    assert pins["macos-x64"] == {"cryptography": "48.0.1"}
    assert "foo resolved to ['1.0', '2.0']" in capsys.readouterr().err


def test_committed_pins_cover_every_platform_and_keep_fastmcp_on_3():
    pins = be._load_pins()
    for platform in be.BLENDER_PLATFORMS:
        assert pins[platform]["fastmcp"].split(".")[0] == "3", platform
        assert "requests" in pins[platform]


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
