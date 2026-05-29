#!/usr/bin/env python3
"""Bump the addon version across all three files it lives in.

Three files because Blender's bl_info parser is AST-only — it reads
addon.py + addon/__init__.py without importing them, so the version
tuple MUST be a literal in both. addon/_version.py is the canonical
__version__ for runtime/CI.

Usage:
    scripts/bump_addon_version.py 1.5.6
    scripts/bump_addon_version.py patch     # 1.5.5 → 1.5.6
    scripts/bump_addon_version.py minor     # 1.5.5 → 1.6.0
    scripts/bump_addon_version.py major     # 1.5.5 → 2.0.0
    scripts/bump_addon_version.py           # same as 'patch'

Verifies all three files are in sync BEFORE writing; aborts if they're
already drifted (would need manual reconciliation first).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILES = {
    "version_py": ROOT / "addon" / "_version.py",
    "init_py":    ROOT / "addon" / "__init__.py",
    "addon_py":   ROOT / "addon.py",
}

# Patterns that capture the existing version in each file.
PATTERNS = {
    "version_py": re.compile(r'(__version__\s*=\s*")(\d+)\.(\d+)\.(\d+)(")'),
    "init_py":    re.compile(r'("version":\s*\()(\d+),\s*(\d+),\s*(\d+)(\))'),
    "addon_py":   re.compile(r'("version":\s*\()(\d+),\s*(\d+),\s*(\d+)(\))'),
}


def _read_version(path: Path, pat: re.Pattern) -> tuple[int, int, int]:
    m = pat.search(path.read_text())
    if not m:
        sys.exit(f"ERROR: couldn't find version pattern in {path}")
    return int(m.group(2)), int(m.group(3)), int(m.group(4))


def _next_version(cur: tuple[int, int, int], arg: str) -> tuple[int, int, int]:
    M, m, p = cur
    if arg == "major":
        return (M + 1, 0, 0)
    if arg == "minor":
        return (M, m + 1, 0)
    if arg in ("patch", ""):
        return (M, m, p + 1)
    parts = arg.split(".")
    if len(parts) != 3 or not all(s.isdigit() for s in parts):
        sys.exit(f"ERROR: bad version arg {arg!r} — expected MAJOR.MINOR.PATCH or major/minor/patch")
    return (int(parts[0]), int(parts[1]), int(parts[2]))


def main() -> None:
    arg = sys.argv[1] if len(sys.argv) > 1 else "patch"

    # 1. Read all three; confirm they agree.
    versions = {name: _read_version(path, PATTERNS[name]) for name, path in FILES.items()}
    distinct = set(versions.values())
    if len(distinct) != 1:
        for name, v in versions.items():
            print(f"  {name}: {v[0]}.{v[1]}.{v[2]}")
        sys.exit("ERROR: addon versions already drifted; reconcile manually before bumping")
    cur = distinct.pop()

    # 2. Compute target.
    new = _next_version(cur, arg)
    if new == cur:
        sys.exit(f"ERROR: target version {new[0]}.{new[1]}.{new[2]} == current; nothing to bump")

    # 3. Rewrite all three in lock-step.
    new_str = f"{new[0]}.{new[1]}.{new[2]}"
    new_tuple = f"{new[0]}, {new[1]}, {new[2]}"
    for name, path in FILES.items():
        original = path.read_text()
        if name == "version_py":
            replacement = rf'\g<1>{new[0]}.{new[1]}.{new[2]}\g<5>'
        else:
            replacement = rf'\g<1>{new[0]}, {new[1]}, {new[2]}\g<5>'
        rewritten, n = PATTERNS[name].subn(replacement, original)
        if n != 1:
            sys.exit(f"ERROR: {name}: expected 1 replacement, got {n}")
        path.write_text(rewritten)

    print(f"bumped {cur[0]}.{cur[1]}.{cur[2]} → {new_str}")
    print("files updated:")
    for path in FILES.values():
        print(f"  {path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
