#!/usr/bin/env python3
"""Bump the project version (CalVer, YYYY.MDD.N) everywhere it lives.

Why this shape: Blender validates extension versions as strict semver
(three integers, no leading zeros), and addons already in the field
parse the server's update hint with tuple(int(...)). So a PEP 440 style
2026.09.26.1 is out on both counts. YYYY.MDD.N keeps three plain
integers, sorts correctly across a year (926 < 1001 < 1231), and gives
same-day fixes a counter: 2026.926.0, 2026.926.1, then 2026.1001.0.

Files kept in lock-step:
  addon/_version.py         canonical __version__
  addon/__init__.py         bl_info tuple (AST-parsed by Blender, must be a literal)
  addon.py                  legacy single-file shim bl_info tuple
  pyproject.toml            server package version
  uv.lock                   the project's own entry (no re-resolve)

Usage:
    scripts/bump_addon_version.py              # today's CalVer, counter +1 if already released today
    scripts/bump_addon_version.py 2026.926.3   # exact
    scripts/bump_addon_version.py --no-build   # skip the extension zip rebuild

Verifies the three addon files agree before writing.
"""
from __future__ import annotations

import datetime
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ADDON_FILES = {
    "version_py": ROOT / "addon" / "_version.py",
    "init_py":    ROOT / "addon" / "__init__.py",
    "addon_py":   ROOT / "addon.py",
}
PATTERNS = {
    "version_py": re.compile(r'(__version__\s*=\s*")(\d+)\.(\d+)\.(\d+)(")'),
    "init_py":    re.compile(r'("version":\s*\()(\d+),\s*(\d+),\s*(\d+)(\))'),
    "addon_py":   re.compile(r'("version":\s*\()(\d+),\s*(\d+),\s*(\d+)(\))'),
}
PYPROJECT = ROOT / "pyproject.toml"
PYPROJECT_RE = re.compile(r'^(version\s*=\s*")[^"]*(")', re.M)
UV_LOCK = ROOT / "uv.lock"
UV_LOCK_RE = re.compile(r'(\[\[package\]\]\nname = "blender-mcp"\nversion = ")[^"]*(")')


def _read_version(path: Path, pat: re.Pattern) -> tuple[int, int, int]:
    m = pat.search(path.read_text())
    if not m:
        sys.exit(f"ERROR: couldn't find version pattern in {path}")
    return int(m.group(2)), int(m.group(3)), int(m.group(4))


def _calver_today(cur: tuple[int, int, int]) -> tuple[int, int, int]:
    today = datetime.date.today()
    year, mdd = today.year, today.month * 100 + today.day
    if cur[:2] == (year, mdd):
        return (year, mdd, cur[2] + 1)
    return (year, mdd, 0)


def _parse_exact(arg: str) -> tuple[int, int, int]:
    parts = arg.split(".")
    if len(parts) != 3 or not all(p.isdigit() and (p == "0" or not p.startswith("0")) for p in parts):
        sys.exit(f"ERROR: bad version {arg!r}; expected three integers without leading zeros")
    return int(parts[0]), int(parts[1]), int(parts[2])


def _sub_once(path: Path, pat: re.Pattern, replacement: str) -> None:
    text, n = pat.subn(replacement, path.read_text(), count=1)
    if n != 1:
        sys.exit(f"ERROR: {path.relative_to(ROOT)}: version pattern not found")
    path.write_text(text)


def main() -> None:
    positional = [a for a in sys.argv[1:] if not a.startswith("--")]
    arg = positional[0] if positional else ""

    versions = {name: _read_version(path, PATTERNS[name]) for name, path in ADDON_FILES.items()}
    distinct = set(versions.values())
    if len(distinct) != 1:
        for name, v in versions.items():
            print(f"  {name}: {v[0]}.{v[1]}.{v[2]}")
        sys.exit("ERROR: addon versions already drifted; reconcile manually before bumping")
    cur = distinct.pop()

    new = _parse_exact(arg) if arg else _calver_today(cur)
    if new <= cur:
        sys.exit(f"ERROR: {'.'.join(map(str, new))} is not newer than {'.'.join(map(str, cur))}")

    new_str = f"{new[0]}.{new[1]}.{new[2]}"
    for name, path in ADDON_FILES.items():
        if name == "version_py":
            replacement = rf'\g<1>{new_str}\g<5>'
        else:
            replacement = rf'\g<1>{new[0]}, {new[1]}, {new[2]}\g<5>'
        _sub_once(path, PATTERNS[name], replacement)
    _sub_once(PYPROJECT, PYPROJECT_RE, rf'\g<1>{new_str}\g<2>')
    _sub_once(UV_LOCK, UV_LOCK_RE, rf'\g<1>{new_str}\g<2>')

    print(f"bumped {'.'.join(map(str, cur))} → {new_str}")
    for path in (*ADDON_FILES.values(), PYPROJECT, UV_LOCK):
        print(f"  {path.relative_to(ROOT)}")

    # Refresh the self-hosted extension repo so index.json and the zip match.
    # Non-fatal so a bump can land offline; rerun build_extension.py later.
    build_script = ROOT / "scripts" / "build_extension.py"
    if build_script.exists() and "--no-build" not in sys.argv:
        print("running scripts/build_extension.py to refresh the extension repo...")
        if subprocess.run([sys.executable, str(build_script)]).returncode != 0:
            print(
                "WARNING: extension build failed; version files bumped but "
                "dist/extensions/ is stale. Rerun scripts/build_extension.py.",
                file=sys.stderr,
            )


if __name__ == "__main__":
    main()
