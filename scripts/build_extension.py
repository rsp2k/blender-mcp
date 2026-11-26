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
import os
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

# Exact wheels per Blender platform and Python version, committed. The build
# downloads exactly these with --no-deps; --relock (needs uv) resolves them.
# pip download can't do the resolving: --platform changes which wheel tags it
# accepts but markers like sys_platform == 'win32' are still evaluated on the
# build host, so a Linux build dropped pywin32 from the Windows zip and put
# Linux-only packages (jeepney, secretstorage) in every zip. uv resolves for
# the target platform. Versions differ per platform too: Intel Macs get an
# older cryptography, the last with a macOS 11 x86 wheel.
PINS_FILE = ROOT / "packaging" / "extension-wheels.lock.json"
PINS_FORMAT = 2

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
# The same platforms as uv's --python-platform targets, for --relock.
UV_TARGETS = {
    "linux-x64": "x86_64-manylinux_2_17",
    "linux-arm64": "aarch64-manylinux_2_17",
    "windows-x64": "x86_64-pc-windows-msvc",
    "macos-x64": "x86_64-apple-darwin",
    "macos-arm64": "aarch64-apple-darwin",
}

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


def _wheel_name_version(filename: str) -> tuple[str, str]:
    """``websockets-17.2-cp313-...whl`` -> ("websockets", "17.2"), name
    normalized the PEP 503 way so it matches pip's view of the project."""
    name, version = filename.split("-")[:2]
    return re.sub(r"[-_.]+", "-", name).lower(), version


def parse_compiled(text: str) -> dict[str, str]:
    """``name==version`` lines from ``uv pip compile`` -> {project: version}."""
    pins: dict[str, str] = {}
    for line in text.splitlines():
        line = line.split("#", 1)[0].split(";", 1)[0].strip()
        m = re.fullmatch(r"([A-Za-z0-9._-]+)==([^\s;]+)", line)
        if m:
            pins[re.sub(r"[-_.]+", "-", m.group(1)).lower()] = m.group(2)
    return pins


def _relock(previous: dict | None, upgrade: bool) -> dict:
    """Resolve RUNTIME_DEPS for every (platform, Python) with uv.

    Unless ``upgrade``, the previous pins for that platform are passed as
    constraints, so only missing packages are added and nothing else moves.
    """
    if shutil.which("uv") is None:
        sys.exit("ERROR: --relock needs uv (https://docs.astral.sh/uv/)")
    out: dict[str, dict[str, dict[str, str]]] = {}
    tmp = ROOT / "dist" / "relock"
    tmp.mkdir(parents=True, exist_ok=True)
    reqs = tmp / "requirements.in"
    reqs.write_text("".join(f"{d}\n" for d in RUNTIME_DEPS))
    for platform, target in UV_TARGETS.items():
        out[platform] = {}
        old: dict[str, str] = {}
        if previous and not upgrade:
            for per_py in previous["platforms"].get(platform, {}).values():
                old.update(per_py)
        cfile = tmp / f"constraints-{platform}.txt"
        cfile.write_text("".join(f"{n}=={v}\n" for n, v in sorted(old.items())))
        for py in PYTHON_VERSIONS:
            print(f"  resolving {platform} (py{py})...")
            proc = subprocess.run(
                ["uv", "pip", "compile", str(reqs), "--python-platform", target,
                 "--python-version", py, "--only-binary", ":all:", "-c", str(cfile),
                 "--no-header", "--no-annotate", "--quiet"],
                capture_output=True, text=True, check=False,
            )
            if proc.returncode != 0:
                print(proc.stderr, file=sys.stderr)
                sys.exit(f"ERROR: uv could not resolve {platform} py{py}")
            out[platform][py] = parse_compiled(proc.stdout)
    return {"format": PINS_FORMAT, "platforms": out}


def _upgrade_pins_format(old: dict) -> dict:
    """Format 1 was {platform: {project: version}}; reuse it as constraints."""
    return {"format": PINS_FORMAT,
            "platforms": {p: {py: dict(v) for py in PYTHON_VERSIONS} for p, v in old.items()}}


def _load_pins() -> dict[str, dict[str, dict[str, str]]]:
    """{platform: {python: {project: version}}} from PINS_FILE."""
    if not PINS_FILE.exists():
        sys.exit(f"ERROR: {PINS_FILE.relative_to(ROOT)} is missing. Run with --relock "
                 "(needs uv) to resolve and write it.")
    data = json.loads(PINS_FILE.read_text())
    if data.get("format") != PINS_FORMAT:
        sys.exit(f"ERROR: {PINS_FILE.name} is an older format. Run with --relock.")
    pins = data["platforms"]
    missing = [(p, py) for p in BLENDER_PLATFORMS for py in PYTHON_VERSIONS
               if py not in pins.get(p, {})]
    if missing:
        sys.exit(f"ERROR: {PINS_FILE.name} has no pins for {missing}. Run with --relock.")
    return pins


def _download_wheels(work_dir: Path, pins: dict[str, dict[str, dict[str, str]]]
                     ) -> dict[str, list[str]]:
    """Fetch exactly the pinned wheels, one directory per Blender platform.

    Runs ``pip download --no-deps`` once per (platform, python-version) pair
    into ``work_dir/<platform>/``; pure-Python wheels repeat across Pythons
    and land once. Returns {blender_platform: [wheel filenames]}.
    """
    result: dict[str, list[str]] = {}
    for platform, wheel_tag in PLATFORM_WHEEL_TAGS.items():
        target = work_dir / platform
        target.mkdir(parents=True, exist_ok=True)
        for py in PYTHON_VERSIONS:
            print(f"  fetching wheels for {platform} (py{py})...")
            cmd = [
                sys.executable, "-m", "pip", "download",
                "--no-deps",
                "--only-binary=:all:",
                "--python-version", py,
                "--platform", wheel_tag,
                "--dest", str(target),
                *(f"{n}=={v}" for n, v in sorted(pins[platform][py].items())),
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
    # Absolute when the public base is known, so the index works from any URL
    # (mcp.blender.bet/install as well as /extensions/index.json); relative
    # otherwise, which Blender resolves against wherever the index was read.
    base = os.environ.get("EXTENSIONS_BASE_URL", "").strip().rstrip("/")
    entry["archive_url"] = f"{base}/{zip_path.name}" if base else f"./{zip_path.name}"
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
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="Rebuild archives that already exist for this version. Changes the "
             "bytes behind a published version; bump the version instead.",
    )
    parser.add_argument(
        "--relock",
        action="store_true",
        help=f"Resolve RUNTIME_DEPS per platform with uv and rewrite {PINS_FILE.name}, "
             "keeping existing versions; then bump the version to ship it.",
    )
    parser.add_argument(
        "--upgrade",
        action="store_true",
        help="With --relock: ignore the existing pins and take the newest versions.",
    )
    args = parser.parse_args()

    if args.relock:
        previous = json.loads(PINS_FILE.read_text()) if PINS_FILE.exists() else None
        if previous and previous.get("format") != PINS_FORMAT:
            previous = _upgrade_pins_format(previous)
        PINS_FILE.write_text(json.dumps(_relock(previous, args.upgrade), indent=2) + "\n")
        print(f"  wrote {PINS_FILE.relative_to(ROOT)}")
        return

    version = _read_addon_version()

    # A published version never changes: users and image pins hold its hash.
    # `make prod` runs this on every deploy, so an unchanged version reuses
    # its archives and only rewrites the index.
    existing = [DIST_DIR / archive_name(version, p) for p in BLENDER_PLATFORMS]
    if all(z.exists() for z in existing) and not args.rebuild:
        print(f"blender_mcp extension {version} is already built; reusing its archives "
              "(bump the version to ship changes)")
        index_path = _write_index(existing)
        print(f"  wrote {index_path.relative_to(ROOT)}")
        return

    print(f"Building blender_mcp extension {version}")

    # Gate: no build proceeds if addon/ has a SyntaxError.
    _syntax_check_addon()

    work_dir = ROOT / "dist" / "wheels-work"
    reusable = args.skip_download and all((work_dir / p).is_dir() for p in BLENDER_PLATFORMS)
    if reusable:
        print(f"  reusing existing {work_dir}")
        wheels_by_platform = {p: _wheels_in(work_dir / p) for p in BLENDER_PLATFORMS}
    else:
        pins = _load_pins()
        if work_dir.exists():
            shutil.rmtree(work_dir)
        wheels_by_platform = _download_wheels(work_dir, pins)

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
