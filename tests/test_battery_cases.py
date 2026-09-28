"""Chat battery: case loading and validation (scripts/battery/cases.py)."""

import sys
from pathlib import Path

import pytest

BATTERY = Path(__file__).resolve().parent.parent / "scripts" / "battery"
sys.path.insert(0, str(BATTERY))

from cases import (
    CaseError,
    blend_files,
    load_cases,
    load_file,
    parse_case,
)


def base(**over):
    raw = {"id": "a-case", "prompt": "Add a cube.", "checks": [{"object_exists": "Cube"}]}
    raw.update(over)
    return raw


def test_minimal_case_gets_defaults():
    c = parse_case(base())
    assert c.prompts == ["Add a cube."]
    assert c.setup == {"base": "empty"}
    assert c.approval == "never"
    assert c.checks == [{"check": "object_exists", "name": "Cube"}]
    assert c.title == "a-case"


def test_multi_turn_prompt_and_setup_forms():
    c = parse_case(base(prompt=["one", "two"], setup={"blend": "x.blend", "scene": "S"}))
    assert c.prompts == ["one", "two"]
    assert c.setup == {"base": "blend", "blend": "x.blend", "scene": "S"}
    c = parse_case(base(setup={"python": "print(1)"}))
    assert c.setup == {"base": "empty", "python": "print(1)"}
    assert parse_case(base(setup="none")).setup == {"base": "none"}


@pytest.mark.parametrize("over, fragment", [
    ({"id": "Bad Id"}, "lowercase"),
    ({"approval": "maybe"}, "approval"),
    ({"prompt": ""}, "prompt"),
    ({"prompt": []}, "prompt"),
    ({"timeout": 1}, "timeout"),
    ({"checks": []}, "checks"),
    ({"checks": [{"nope": 1}]}, "unknown check"),
    ({"checks": [{"dims_approx": {"name": "A"}}]}, "dims"),
    ({"checks": [{"dims_approx": {"name": "A", "dims": [1, 2]}}]}, "three numbers"),
    ({"checks": [{"has_material": {"name": "A", "color": "plaid"}}]}, "colour"),
    ({"checks": [{"location_approx": {"location": [0, 0, 0]}}]}, "selector"),
    ({"checks": [{"rests_on": {"name": "A", True: "B"}}]}, "base"),
    ({"setup": {"blend": "../etc/x.blend"}}, "battery directory"),
    ({"setup": {"blend": "x.txt"}}, ".blend"),
    ({"setup": {"weird": 1}}, "unknown setup"),
    ({"extra": 1}, "unknown keys"),
])
def test_invalid_cases_are_rejected(over, fragment):
    with pytest.raises(CaseError, match=fragment):
        parse_case(base(**over))


def test_missing_required_key():
    raw = base()
    del raw["checks"]
    with pytest.raises(CaseError, match="missing 'checks'"):
        parse_case(raw)


def test_load_select_and_duplicates(tmp_path):
    (tmp_path / "a.yaml").write_text(
        "- id: one\n  tags: [x]\n  prompt: p\n  checks: [{no_errors: true}]\n"
        "- id: two\n  prompt: p\n  setup: {blend: f.blend}\n  checks: [{no_errors: true}]\n")
    (tmp_path / "b.yaml").write_text("id: three\nprompt: p\nchecks: [{no_errors: true}]\n")
    assert [c.id for c in load_cases(tmp_path)] == ["one", "two", "three"]
    assert [c.id for c in load_cases(tmp_path, "tag:x")] == ["one"]
    assert [c.id for c in load_cases(tmp_path, "t*")] == ["two", "three"]
    assert [c.id for c in load_cases(tmp_path, "b.yaml, tag:x")] == ["one", "three"]
    assert blend_files(load_cases(tmp_path)) == {"f.blend"}
    (tmp_path / "c.yaml").write_text("id: one\nprompt: p\nchecks: [{no_errors: true}]\n")
    with pytest.raises(CaseError, match="duplicate"):
        load_cases(tmp_path)


def test_yaml_on_key_gives_a_clear_error(tmp_path):
    p = tmp_path / "x.yaml"
    p.write_text("id: x\nprompt: p\nchecks:\n  - rests_on: {name: A, on: B}\n")
    with pytest.raises(CaseError, match="YAML"):
        load_file(p)


def test_shipped_cases_all_load():
    cases = load_cases(BATTERY / "cases")
    assert len(cases) >= 15
    assert {c.approval for c in cases} == {"allow", "deny", "never"}
    for c in cases:
        assert c.checks, c.id
    # every blend a case names was copied in by hand; keep the list small
    assert len(blend_files(cases)) <= 5
