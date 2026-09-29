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
