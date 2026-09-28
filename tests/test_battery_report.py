"""Chat battery: result aggregation and the Markdown report (scripts/battery/report.py)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "battery"))

from report import cell_text, failures, matrix, merge, render_markdown, summarize


def rec(model, case, ok, seconds=10.0, tools=1, repeat=1, **extra):
    return {"model": model, "case": case, "ok": ok, "seconds": seconds, "repeat": repeat,
            "tools": [{"name": "create_mesh", "ok": True}] * tools, "approvals": [], "errors": [],
            "reply": "done", "timed_out": False,
            "checks": [{"check": "object_exists", "ok": ok, "observed": "x", "expect": "name=A"}],
            **extra}


RESULTS = [
    rec("gateway:qwen3", "box", True, 10, tools=1),
    rec("gateway:qwen3", "chair", False, 30, tools=5, timed_out=True),
    rec("gateway:gemma4", "box", True, 20, tools=2),
    rec("gateway:gemma4", "chair", True, 40, tools=3),
    {"model": "anthropic:claude-sonnet-5", "case": "box", "repeat": 1, "skipped": True, "ok": False},
]


def test_summarize():
    rows = {r["model"]: r for r in summarize(RESULTS)}
    q = rows["gateway:qwen3"]
    assert (q["runs"], q["passed"], q["pass_rate"]) == (2, 1, 0.5)
    assert q["median_s"] == 20.0 and q["avg_tools"] == 3.0 and q["timeouts"] == 1
    assert (q["checks_passed"], q["checks_total"]) == (1, 2)
    g = rows["gateway:gemma4"]
    assert g["pass_rate"] == 1.0 and g["median_s"] == 30.0
    a = rows["anthropic:claude-sonnet-5"]
    assert a["runs"] == 0 and a["skipped"] == 1 and a["pass_rate"] is None
    assert list(rows) == ["gateway:qwen3", "gateway:gemma4", "anthropic:claude-sonnet-5"]


def test_matrix_and_cells_with_repeats():
    rs = [rec("m", "c", True, 10, repeat=1), rec("m", "c", False, 30, repeat=2), rec("m", "d", True, 5)]
    cases, models, cells = matrix(rs)
    assert cases == ["c", "d"] and models == ["m"]
    assert cells[("c", "m")] == {"passed": 1, "runs": 2, "skipped": 0, "median_s": 20.0}
    assert cell_text(cells[("c", "m")]) == "~ 1/2 20s"
    assert cell_text(cells[("d", "m")]) == "✓ 5s"
    assert cell_text({"passed": 0, "runs": 1, "median_s": None}) == "✗"
    assert cell_text({"passed": 0, "runs": 0, "skipped": 1, "median_s": None}) == "skip"
    assert cell_text(None) == ""


def test_failures_skip_passes_and_skipped():
    assert [(r["model"], r["case"]) for r in failures(RESULTS)] == [("gateway:qwen3", "chair")]


def test_merge_later_run_replaces_same_model_case():
    full = [rec("m", "a", False), rec("m", "b", True), rec("n", "a", True)]
    rerun = [rec("m", "a", True, repeat=1), rec("m", "a", True, repeat=2), rec("m", "c", False)]
    merged = merge([full, rerun])
    assert [(r["model"], r["case"], r["ok"]) for r in merged] == [
        ("m", "a", True), ("m", "a", True), ("m", "b", True), ("n", "a", True), ("m", "c", False)]


def test_render_markdown():
    md = render_markdown(RESULTS, {"started": "t0", "models": ["gateway:qwen3"]})
    assert "| gateway:qwen3 | 50% (1/2) | 2 | 1/2 | 20.0s | 3.0 | 0 | 1 |" in md
    assert "| box | ✓ 10s | ✓ 20s | skip |" in md
    assert "### chair on gateway:qwen3" in md
    assert "the turn timed out" in md
    assert "**object_exists** (name=A): observed `x`" in md
    assert render_markdown([], {}).count("None.") == 1
    # pipes in replies must not break the table layout
    md = render_markdown([rec("m", "c", False, reply="a | b")])
    assert "a \\| b" in md
