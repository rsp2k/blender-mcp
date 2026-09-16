#!/usr/bin/env python3
"""Build a Blender 4.2+ extension zip for BlenderMCP.

Output:
    dist/extensions/blender_mcp-<version>.zip     — the extension archive
    dist/extensions/index.json                    — the self-hosted repo index

Called manually or from ``scripts/bump_addon_version.py`` after a bump.
Uses ``python3 -m pip download`` for wheel fetching; ``uv pip`` doesn't
expose the ``download`` subcommand, and pip is the canonical tool for
per-platform wheel resolution anyway.

Why the zip contents look the way they do:

* Blender extensions import their zip contents as a package named by
  the manifest ``id``. We ship the current ``addon/`` package contents
  at the zip root, so the extension module resolves to ``blender_mcp``
  and every internal ``from ..foo import bar`` keeps working (all
  addon internals are relative imports, verified).
* Bundled wheels live under ``wheels/`` alongside the code. Blender's
  extension installer picks compatible wheels per host using the
  wheel-filename tags, so we ship the union across every platform we
  care about in one archive.
* The manifest at the root replaces ``bl_info`` for the extension
  path. The legacy ``addon.py`` shim still carries ``bl_info`` for
  users on Blender 3.2–4.1 who install via "Install from Disk."
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ADDON_SRC = ROOT / "addon"
MANIFEST_TEMPLATE = ROOT / "packaging" / "blender_manifest.toml.template"
DIST_DIR = ROOT / "dist" / "extensions"

# Runtime deps to bundle. Additive: transitives are pulled automatically
# by `pip download`. Pin at the top-level so the resolver picks
# consistent transitives across the platform runs.
#
# fastmcp pin: match pyproject.toml (>=3.3.1) with an explicit <4 cap.
# Per memory `fastmcp_pin_landmine`, five FastMCP/SDK private APIs would
# break on the 4.x bump (the _ping_handler monkey-patch, OIDCProxy
# internals, provider.update_default_scopes, client_storage kwarg,
# fastmcp.contrib.mcp_mixin). A version-neutral bump belongs on its own
# branch with a full auth-flow verification pass; not something the
# extension zip should silently drag in.
RUNTIME_DEPS = [
    "fastmcp>=3.3.1,<4",
    "requests>=2.34.2,<3",
]

# Platforms we ship wheels for. Each entry is what pip's --platform tag
# expects.
PLATFORMS = [
    "manylinux2014_x86_64",
    "manylinux_2_17_aarch64",
    "win_amd64",
    "macosx_11_0_x86_64",
    "macosx_11_0_arm64",
]

# Blender's extension-repo platform tokens. Distinct from pip's platform
# tags; used only in index.json's `platforms` field so the browser knows
# who to offer the extension to.
BLENDER_PLATFORMS = ["linux-x64", "linux-arm64", "windows-x64", "macos-x64", "macos-arm64"]

# Python versions Blender ships across the 4.2+ range we support. 3.11 =
# Blender 4.2 - 4.5 LTS. 3.14 = Blender 5.x (verified: install failure
# from Blender 5.2 reported "This Python version (3.14) isn't compatible
# with (3.11)"). One zip contains the union of wheels across every
# (platform, python) tuple; Blender picks the subset matching the host's
# tags at install time.
PYTHON_VERSIONS = ["3.11", "3.14"]


def _read_addon_version() -> str:
    text = (ADDON_SRC / "_version.py").read_text()
    m = re.search(r'__version__\s*=\s*"(\d+\.\d+\.\d+)"', text)
    if not m:
        sys.exit("ERROR: couldn't find __version__ in addon/_version.py")
    return m.group(1)


def _download_wheels(target_dir: Path) -> list[str]:
    """Fetch wheels for RUNTIME_DEPS across every (platform, python) combo.

    Runs ``pip download`` once per (platform, python-version) pair and
    dedupes filenames — pure-Python wheels (``*-py3-none-any.whl``)
    naturally coalesce because they have identical names across
    combinations, so the same file only appears once in the output.
    C-extension wheels come out as separate files per (cpXY, platform)
    tuple, which is exactly what we want Blender to have available.
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    for py in PYTHON_VERSIONS:
        for plat in PLATFORMS:
            print(f"  fetching wheels for {plat} (py{py})...")
            cmd = [
                sys.executable, "-m", "pip", "download",
                "--only-binary=:all:",
                "--python-version", py,
                "--platform", plat,
                "--dest", str(target_dir),
                *RUNTIME_DEPS,
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True)
            if proc.returncode != 0:
                print(proc.stderr, file=sys.stderr)
                sys.exit(f"ERROR: pip download failed for {plat} py{py}")
    files = sorted(f.name for f in target_dir.iterdir() if f.suffix == ".whl")
    print(f"  {len(files)} unique wheels resolved")
    return files


def _render_manifest(version: str, wheel_files: list[str]) -> str:
    template = MANIFEST_TEMPLATE.read_text()
    # The wheels list is TOML syntax — build it manually so we don't
    # need a TOML-writer dep. Paths are relative to the zip root.
    wheels_toml = "[\n" + "".join(
        f'    "./wheels/{name}",\n' for name in wheel_files
    ) + "]"
    return template.replace("__VERSION__", version).replace("__WHEELS__", wheels_toml)


def _build_zip(version: str, manifest_text: str, wheels_dir: Path) -> Path:
    DIST_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = DIST_DIR / f"blender_mcp-{version}.zip"
    # Blender is picky: the manifest MUST be at the archive root, and
    # the extension module contents live at the root too. We flatten
    # addon/ into the zip root.
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("blender_manifest.toml", manifest_text)
        for src in ADDON_SRC.rglob("*"):
            if "__pycache__" in src.parts or src.name.endswith(".pyc"):
                continue
            if not src.is_file():
                continue
            zf.write(src, arcname=str(src.relative_to(ADDON_SRC)))
        for whl in sorted(wheels_dir.iterdir()):
            if whl.suffix == ".whl":
                zf.write(whl, arcname=f"wheels/{whl.name}")
    return zip_path


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _write_index(version: str, zip_path: Path) -> Path:
    """Generate the extensions repo index.json listing THIS build.

    We overwrite the file wholesale rather than merging so the repo
    always advertises exactly one current version per extension — that
    matches Blender's own extension repo behavior for a self-hosted
    single-extension site, and it avoids stale entries silently
    lingering when a prior release gets yanked from disk.
    """
    entry = {
        "schema_version": "1.0.0",
        "id": "blender_mcp",
        "name": "Blender MCP",
        "tagline": "Multi-LLM collaboration on shared Blender scenes over MCP",
        "version": version,
        "type": "add-on",
        "maintainer": "Ryan Malloy <ryan@supported.systems>",
        "license": ["SPDX:MIT"],
        "tags": ["Development", "Pipeline"],
        "blender_version_min": "4.2.0",
        "permissions": {
            "network": "Talks to the BlenderMCP server over HTTPS + OAuth 2.1 to join the shared bus.",
            "files": "Downloads generated assets from Poly Haven, Sketchfab, and Hyper3D into a local cache.",
        },
        "website": "https://blender.bet/",
        "archive_url": f"./{zip_path.name}",
        "archive_size": zip_path.stat().st_size,
        "archive_hash": f"sha256:{_sha256(zip_path)}",
        "platforms": BLENDER_PLATFORMS,
    }
    index = {"version": "v1", "blocklist": [], "data": [entry]}
    index_path = DIST_DIR / "index.json"
    index_path.write_text(json.dumps(index, indent=2) + "\n")
    return index_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skip-download",
        action="store_true",
        help="Reuse dist/wheels-work/ instead of refetching (fast iteration).",
    )
    args = parser.parse_args()

    version = _read_addon_version()
    print(f"Building blender_mcp extension {version}")

    wheels_dir = ROOT / "dist" / "wheels-work"
    if args.skip_download and wheels_dir.exists():
        print(f"  reusing existing {wheels_dir}")
        wheel_files = sorted(
            f.name for f in wheels_dir.iterdir() if f.suffix == ".whl"
        )
    else:
        if wheels_dir.exists():
            shutil.rmtree(wheels_dir)
        wheel_files = _download_wheels(wheels_dir)

    manifest_text = _render_manifest(version, wheel_files)
    zip_path = _build_zip(version, manifest_text, wheels_dir)
    print(f"  wrote {zip_path.relative_to(ROOT)}  ({zip_path.stat().st_size:,} bytes)")

    index_path = _write_index(version, zip_path)
    print(f"  wrote {index_path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
