"""Battery case files: loading, normalising and validating.

A case is one YAML mapping (a file may hold one mapping or a list of them):

    id: box-exact                 # unique, [a-z0-9-]
    title: Exact box dimensions
    tags: [primitives]
    setup: empty                  # or {blend: x.blend, scene: "H1 Closed"} or {python: "..."}
    prompt: "Add a box ..."       # or a list of strings for a multi-turn case
    approval: never               # allow | deny | never (never = a request fails the case)
    timeout: 180                  # seconds per turn
    checks:
      - object_exists: {name: Crate}
      - reply_contains: "crate"

Pure Python, no bpy: the runner and the tests both import it.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from checks import CHECK_TYPES, normalise_check

APPROVALS = ("allow", "deny", "never")
DEFAULT_TIMEOUT = 240
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class CaseError(ValueError):
    pass


@dataclass
class Case:
    id: str
    title: str
    prompts: list[str]
    setup: dict
    approval: str = "never"
    timeout: float = DEFAULT_TIMEOUT
    tags: list[str] = field(default_factory=list)
    checks: list[dict] = field(default_factory=list)
    source: str = ""

    def to_dict(self) -> dict:
        return {"id": self.id, "title": self.title, "prompts": self.prompts, "setup": self.setup,
                "approval": self.approval, "timeout": self.timeout, "tags": self.tags,
                "checks": self.checks, "source": self.source}


def normalise_setup(raw: Any, where: str) -> dict:
    """{"base": "empty"|"blend"|"none", "blend"?, "scene"?, "python"?}."""
    if raw is None or raw == "empty":
        return {"base": "empty"}
    if raw == "none":
        return {"base": "none"}
    if isinstance(raw, str):
        raise CaseError(f"{where}: setup must be 'empty', 'none' or a mapping, not {raw!r}")
    if not isinstance(raw, dict):
        raise CaseError(f"{where}: setup must be a mapping")
    unknown = set(raw) - {"blend", "python", "scene", "base"}
    if unknown:
        raise CaseError(f"{where}: unknown setup keys {sorted(unknown)}")
    out: dict = {}
    if "blend" in raw:
        blend = str(raw["blend"])
        if not blend.endswith(".blend"):
            raise CaseError(f"{where}: setup.blend must name a .blend file")
        if blend.startswith("/") or ".." in Path(blend).parts:
            raise CaseError(f"{where}: setup.blend may not leave the battery directory")
        out = {"base": "blend", "blend": blend}
    else:
        base = raw.get("base", "empty")
        if base not in ("empty", "none"):
            raise CaseError(f"{where}: setup.base must be empty or none")
        out = {"base": base}
    if "scene" in raw:
        out["scene"] = str(raw["scene"])
    if "python" in raw:
        if not isinstance(raw["python"], str) or not raw["python"].strip():
            raise CaseError(f"{where}: setup.python must be a non-empty string")
        out["python"] = raw["python"]
    return out


def parse_case(raw: Any, source: str = "") -> Case:
    where = source or "case"
    if not isinstance(raw, dict):
        raise CaseError(f"{where}: a case must be a mapping")
    for key in ("id", "prompt", "checks"):
        if key not in raw:
            raise CaseError(f"{where}: missing '{key}'")
    unknown = set(raw) - {"id", "title", "tags", "setup", "prompt", "approval", "timeout", "checks"}
    if unknown:
        raise CaseError(f"{where}: unknown keys {sorted(unknown)}")
    cid = str(raw["id"])
    if not ID_RE.match(cid):
        raise CaseError(f"{where}: id {cid!r} must be lowercase letters, digits and dashes")
    where = f"{source}:{cid}" if source else cid

    prompt = raw["prompt"]
    prompts = [prompt] if isinstance(prompt, str) else prompt
    if not isinstance(prompts, list) or not prompts or not all(
            isinstance(p, str) and p.strip() for p in prompts):
        raise CaseError(f"{where}: prompt must be a non-empty string or list of strings")

    approval = str(raw.get("approval", "never"))
    if approval not in APPROVALS:
        raise CaseError(f"{where}: approval must be one of {APPROVALS}")

    try:
        timeout = float(raw.get("timeout", DEFAULT_TIMEOUT))
    except (TypeError, ValueError) as e:
        raise CaseError(f"{where}: timeout must be a number") from e
    if not 5 <= timeout <= 900:
        raise CaseError(f"{where}: timeout must be between 5 and 900 seconds")

    tags = raw.get("tags") or []
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        raise CaseError(f"{where}: tags must be a list of strings")

    checks_raw = raw["checks"]
    if not isinstance(checks_raw, list) or not checks_raw:
        raise CaseError(f"{where}: checks must be a non-empty list")
    checks = []
    for i, c in enumerate(checks_raw):
        try:
            checks.append(normalise_check(c))
        except ValueError as e:
            raise CaseError(f"{where}: check {i + 1}: {e}") from e

    return Case(
        id=cid, title=str(raw.get("title") or cid), prompts=[p.strip() for p in prompts],
        setup=normalise_setup(raw.get("setup"), where), approval=approval, timeout=timeout,
        tags=[t.strip() for t in tags], checks=checks, source=source,
    )


def load_file(path: Path) -> list[Case]:
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as e:
        raise CaseError(f"{path.name}: invalid YAML: {e}") from e
    items = data if isinstance(data, list) else [data]
    return [parse_case(item, path.name) for item in items]


def load_cases(case_dir: Path, select: str | None = None) -> list[Case]:
    """All cases under ``case_dir``, filtered by ``select``.

    ``select`` is a comma list; each term is ``tag:<name>`` or a glob matched
    against the case id and the file name (``primitives-*``, ``blend-*.yaml``).
    A case is kept when any term matches. Duplicate ids are an error.
    """
    cases: list[Case] = []
    seen: dict[str, str] = {}
    for path in sorted(case_dir.glob("*.y*ml")):
        for case in load_file(path):
            if case.id in seen:
                raise CaseError(f"duplicate case id {case.id!r} in {path.name} and {seen[case.id]}")
            seen[case.id] = path.name
            cases.append(case)
    if not select:
        return cases
    terms = [t.strip() for t in select.split(",") if t.strip()]
    return [c for c in cases if any(matches(c, t) for t in terms)]


def matches(case: Case, term: str) -> bool:
    if term.startswith("tag:"):
        return term[4:] in case.tags
    return fnmatch.fnmatch(case.id, term) or fnmatch.fnmatch(case.source, term)


def blend_files(cases: list[Case]) -> set[str]:
    return {c.setup["blend"] for c in cases if c.setup.get("base") == "blend"}


__all__ = ["APPROVALS", "CHECK_TYPES", "Case", "CaseError", "blend_files", "load_cases",
           "load_file", "matches", "parse_case"]
