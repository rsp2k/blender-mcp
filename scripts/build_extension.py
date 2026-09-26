#!/usr/bin/env python3
"""Build Blender 4.2+ extension archives for BlenderMCP, one per platform.

Output:
    dist/extensions/blender_mcp-<version>-<platform>.zip  — e.g. ..._linux_x64.zip
    dist/extensions/index.json                            — the self-hosted repo index

Each archive carries only its platform's wheels, and the index lists one
entry per archive (the layout `blender --command extension build
--split-platforms` plus `server-generate` produces). Blender installs the
entry whose `platforms` matches the running system.

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
* Bundled wheels live under ``wheels/`` alongside the code, only those
  pip resolves for the archive's platform (across our Python versions).
* The manifest at the root replaces ``bl_info`` for the extension
  path. The legacy ``addon.py`` shim still carries ``bl_info`` for
  users on Blender 3.2–4.1 who install via "Install from Disk."
"""

from __future__ import annotations

import argparse
import compileall
import hashlib
import json
import re
import shutil
import subprocess
import sys
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

# Blender platform token -> pip's --platform tag. One archive is built per
# entry, holding only the wheels pip resolves for that platform, so each
# user downloads roughly a fifth of the old all-platforms zip.
PLATFORM_WHEEL_TAGS = {
    "linux-x64": "manylinux2014_x86_64",
    "linux-arm64": "manylinux_2_17_aarch64",
    "windows-x64": "win_amd64",
    "macos-x64": "macosx_11_0_x86_64",
    "macos-arm64": "macosx_11_0_arm64",
}
BLENDER_PLATFORMS = list(PLATFORM_WHEEL_TAGS)

# Python versions Blender ships across the 4.2+ range we support:
#   3.11 — Blender 4.2 through 4.5 LTS
#   3.13 — Blender 5.2 (verified 2026-09-26 against Ubuntu 24.04 Blender
#          5.2.2, which rejected a cp311/cp314-only build with "This
#          Python version (3.13) isn't compatible with (3.14, 3.11)")
#   3.14 — future-proof for Blender 5.x releases that bump Python; kept
#          in the set because the wheels exist for our RUNTIME_DEPS and
#          the cost of shipping them is a few hundred KB
# One zip contains the union of wheels across every (platform, python)
# tuple; Blender picks the subset matching the host's tags at install
# time. An earlier version of this file listed only ["3.11", "3.14"]
# after Blender 5.0-preview reported it wanted 3.14; that assumption
# went stale when the shipped 5.2 LTS settled on 3.13 instead.
PYTHON_VERSIONS = ["3.11", "3.13", "3.14"]


def _read_addon_version() -> str:
    text = (ADDON_SRC / "_version.py").read_text()
    m = re.search(r'__version__\s*=\s*"(\d+\.\d+\.\d+)"', text)
    if not m:
        sys.exit("ERROR: couldn't find __version__ in addon/_version.py")
    return m.group(1)


def _download_wheels(work_dir: Path) -> dict[str, list[str]]:
    """Fetch wheels for RUNTIME_DEPS, one directory per Blender platform.

    Runs ``pip download`` once per (platform, python-version) pair into
    ``work_dir/<platform>/``, so the files pip resolves for a platform
    are exactly that platform's wheel set (its C extensions plus the
    pure-Python wheels, which are small and repeat in every directory).
    Returns {blender_platform: [wheel filenames]}.
    """
    result: dict[str, list[str]] = {}
    for platform, wheel_tag in PLATFORM_WHEEL_TAGS.items():
        target = work_dir / platform
        target.mkdir(parents=True, exist_ok=True)
        for py in PYTHON_VERSIONS:
            print(f"  fetching wheels for {platform} (py{py})...")
            cmd = [
                sys.executable, "-m", "pip", "download",
                "--only-binary=:all:",
                "--python-version", py,
                "--platform", wheel_tag,
                "--dest", str(target),
                *RUNTIME_DEPS,
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
            if proc.returncode != 0:
                print(proc.stderr, file=sys.stderr)
                sys.exit(f"ERROR: pip download failed for {platform} py{py}")
        result[platform] = _wheels_in(target)
        print(f"    {platform}: {len(result[platform])} wheels")
    return result


def _wheels_in(directory: Path) -> list[str]:
    return sorted(f.name for f in directory.iterdir() if f.suffix == ".whl")


def _render_manifest(version: str, wheel_files: list[str], platform: str) -> str:
    template = MANIFEST_TEMPLATE.read_text()
    # TOML written by hand so the build needs no TOML-writer dep. Paths
    # are relative to the zip root.
    wheels_toml = "[\n" + "".join(
        f'    "./wheels/{name}",\n' for name in wheel_files
    ) + "]"
    return (
        template.replace("__VERSION__", version)
        .replace("__WHEELS__", wheels_toml)
        .replace("__PLATFORMS__", json.dumps([platform]))
    )


def archive_name(version: str, platform: str) -> str:
    """Same naming as ``blender --command extension build --split-platforms``."""
    return f"blender_mcp-{version}-{platform.replace('-', '_')}.zip"


def _build_zip(version: str, platform: str, manifest_text: str, wheels_dir: Path,
               wheel_files: list[str]) -> Path:
    DIST_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = DIST_DIR / archive_name(version, platform)
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
        for name in wheel_files:
            zf.write(wheels_dir / name, arcname=f"wheels/{name}")
    return zip_path


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---- index generation --------------------------------------------------
# A pure-Python equivalent of `blender --command extension server-generate`
# (bl_pkg/cli/blender_ext.py, subcmd_server.generate), so the build works on
# hosts without Blender. Each archive's own manifest becomes one entry:
# `wheels` dropped, `python_versions` derived from the wheel tags with
# Blender's rules, archive_url/size/hash added. Blender groups entries by
# id and installs the one whose `platforms` includes the running system.

_WHEEL_VERSION_TAG = re.compile("([a-zA-Z]+)([0-9]+)")


def _python_versions_from_wheel(filename: str) -> set[tuple[int, ...]]:
    parts = filename[:-len(".whl")].split("-") if filename.endswith(".whl") else filename.split("-")
    if not 5 <= len(parts) <= 6:
        raise ValueError(f"wheel filename doesn't follow the spec: {filename!r}")
    python_tag, abi_tag = parts[-3], parts[-2]
    # A stable-ABI wheel (abi3) works on any 3.x, so it only reports (3,).
    from_abi = {(int(t[3:]),) for t in abi_tag.split(".") if t.startswith("abi") and t[3:].isdigit()}
    if from_abi:
        return from_abi
    versions: set[tuple[int, ...]] = set()
    for tag in python_tag.split("."):
        m = _WHEEL_VERSION_TAG.match(tag)
        if m is None or m.group(1).lower() not in ("py", "cp"):
            raise ValueError(f"unrecognized python tag {tag!r} in {filename!r}")
        number = m.group(2)
        versions.add((int(number[:1]), int(number[1:])) if len(number) > 1 else (int(number),))
    return versions


def python_versions_from_wheels(wheel_files: list[str]) -> list[str]:
    """Blender's python_versions_from_wheels, as sorted "3.11"-style strings."""
    major_only: set[tuple[int, ...]] = set()
    major_minor: set[tuple[int, ...]] = set()
    for path in wheel_files:
        for v in _python_versions_from_wheel(Path(path).name):
            if v[0] <= 2:
                continue
            (major_only if len(v) == 1 else major_minor).add(v)
    for v in major_minor:
        major_only.discard((v[0],))
    return [".".join(map(str, v)) for v in sorted(major_only | major_minor)]


def index_entry_from_archive(zip_path: Path) -> dict:
    import tomllib

    with zipfile.ZipFile(zip_path) as zf:
        manifest = tomllib.loads(zf.read("blender_manifest.toml").decode("utf-8"))
    generated = manifest.pop("build", {}).get("generated") or {}
    manifest.update({k: generated[k] for k in ("platforms", "wheels") if k in generated})
    wheels = manifest.pop("wheels", [])
    entry = {k: v for k, v in manifest.items() if v is not None}
    if wheels:
        entry["python_versions"] = python_versions_from_wheels(wheels)
    entry["archive_url"] = f"./{zip_path.name}"
    entry["archive_size"] = zip_path.stat().st_size
    entry["archive_hash"] = f"sha256:{_sha256(zip_path)}"
    return entry


def build_index(zip_paths: list[Path]) -> dict:
    """Index listing exactly these archives; refuses overlapping platforms
    for the same id and version, as Blender's server-generate does."""
    data = [index_entry_from_archive(p) for p in sorted(zip_paths, key=lambda p: p.name)]
    seen: dict[tuple[str, str, str], str] = {}
    for e in data:
        for platform in e.get("platforms") or ["*"]:
            key = (e["id"], e["version"], platform)
            if key in seen:
                raise ValueError(
                    f"{e['archive_url']} and {seen[key]} both claim {platform} for {e['id']} {e['version']}"
                )
            seen[key] = e["archive_url"]
    return {"version": "v1", "blocklist": [], "data": data}


def _write_index(zip_paths: list[Path]) -> Path:
    """Write index.json listing THIS build's archives only.

    Overwritten wholesale rather than merged, so the repo advertises exactly
    one current version and stale entries can't linger. Old zips stay on
    disk (immutable URLs), they're just no longer listed.
    """
    index_path = DIST_DIR / "index.json"
    index_path.write_text(json.dumps(build_index(zip_paths), indent=2) + "\n")
    return index_path


def _syntax_check_addon() -> None:
    """Fail the build if any addon/ module has a SyntaxError.

    Added 2026-09-26 after 1.5.30 shipped with an unparseable
    ui/operators.py (a persist_prefs call inserted between an if and
    its else, orphaning the else). Blender's extension enable failed
    on install with 'Error: invalid syntax', so the extension appeared
    to distribute correctly but wouldn't register — the failure mode
    was quiet enough that our own smoke check missed it. compileall
    against addon/ takes ~200ms and catches this class of bug at build
    time rather than at every user's `extension install` moment.
    """
    print("  syntax-checking addon/ with compileall...")
    ok = compileall.compile_dir(
        str(ADDON_SRC),
        quiet=1,           # print only errors
        force=True,        # re-check every file
        legacy=False,      # don't leave .pyc littered under source
    )
    if not ok:
        sys.exit(
            "ERROR: addon/ failed syntax check. Extension NOT built. "
            "Fix the SyntaxError above before rebuilding — a shipped "
            "extension with a syntax error installs cleanly but fails "
            "to register in Blender, which is worse than a build failure."
        )


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

    # Gate: no build proceeds if addon/ has a SyntaxError.
    _syntax_check_addon()

    work_dir = ROOT / "dist" / "wheels-work"
    reusable = args.skip_download and all((work_dir / p).is_dir() for p in BLENDER_PLATFORMS)
    if reusable:
        print(f"  reusing existing {work_dir}")
        wheels_by_platform = {p: _wheels_in(work_dir / p) for p in BLENDER_PLATFORMS}
    else:
        if work_dir.exists():
            shutil.rmtree(work_dir)
        wheels_by_platform = _download_wheels(work_dir)

    zip_paths = []
    for platform, wheel_files in wheels_by_platform.items():
        manifest_text = _render_manifest(version, wheel_files, platform)
        zip_path = _build_zip(version, platform, manifest_text, work_dir / platform, wheel_files)
        zip_paths.append(zip_path)
        print(f"  wrote {zip_path.relative_to(ROOT)}  ({zip_path.stat().st_size:,} bytes)")

    index_path = _write_index(zip_paths)
    print(f"  wrote {index_path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
