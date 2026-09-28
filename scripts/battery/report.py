"""Aggregate battery results into a summary, a case x model matrix and a
failure list, and render them as Markdown. Pure Python.

A result record (one per model x case x repeat) looks like:

    {"model": "gateway:qwen3", "case": "box-exact", "repeat": 1, "ok": false,
     "seconds": 23.4, "tools": [{"name": ..., "ok": ..., "ms": ..., "error"?: ..., "head"?: ...}],
     "approvals": [{"prompt": ..., "answer": "deny"}], "reply": "...",
     "errors": [...], "checks": [{"check", "ok", "observed", "expect"}],
     "timed_out": false, "note": "..."}
"""

from __future__ import annotations

import statistics
from collections import OrderedDict
from typing import Any


def _median(xs: list[float]) -> float | None:
    return round(statistics.median(xs), 1) if xs else None


def merge(runs: list[list[dict]]) -> list[dict]:
    """Combine result lists; a later run replaces every earlier record for the
    same (model, case), so a rerun of a few cases updates a full run.
    Order: first appearance of each (model, case)."""
    order: list[tuple[str, str]] = []
    latest: dict[tuple[str, str], list[dict]] = {}
    for run in runs:
        fresh: dict[tuple[str, str], list[dict]] = {}
        for r in run:
            fresh.setdefault((r["model"], r["case"]), []).append(r)
        for key, recs in fresh.items():
            if key not in latest:
                order.append(key)
            latest[key] = recs
    return [r for key in order for r in latest[key]]


def summarize(results: list[dict]) -> list[dict]:
    """Per-model rows: runs, passed, pass rate, median turn s, avg tool calls, ..."""
    by_model: OrderedDict[str, list[dict]] = OrderedDict()
    for r in results:
        by_model.setdefault(r["model"], []).append(r)
    rows = []
    for model, rs in by_model.items():
        judged = [r for r in rs if not r.get("skipped")]
        passed = sum(1 for r in judged if r.get("ok"))
        times = [float(r["seconds"]) for r in judged if r.get("seconds") is not None]
        tools = [len(r.get("tools") or []) for r in judged]
        checks = [c for r in judged for c in r.get("checks") or []]
        rows.append({
            "model": model,
            "runs": len(judged),
            "passed": passed,
            "pass_rate": round(passed / len(judged), 3) if judged else None,
            "checks_passed": sum(1 for c in checks if c.get("ok")),
            "checks_total": len(checks),
            "median_s": _median(times),
            "avg_tools": round(sum(tools) / len(tools), 1) if tools else None,
            "approvals": sum(len(r.get("approvals") or []) for r in judged),
            "timeouts": sum(1 for r in judged if r.get("timed_out")),
            "skipped": len(rs) - len(judged),
        })
    return rows


def matrix(results: list[dict]) -> tuple[list[str], list[str], dict[tuple[str, str], dict]]:
    """(case ids, models, {(case, model): {"passed", "runs", "median_s"}}) in first-seen order."""
    cases: list[str] = []
    models: list[str] = []
    cells: dict[tuple[str, str], dict] = {}
    for r in results:
        if r["case"] not in cases:
            cases.append(r["case"])
        if r["model"] not in models:
            models.append(r["model"])
        cell = cells.setdefault((r["case"], r["model"]), {"passed": 0, "runs": 0, "times": [],
                                                           "skipped": 0})
        if r.get("skipped"):
            cell["skipped"] += 1
            continue
        cell["runs"] += 1
        cell["passed"] += 1 if r.get("ok") else 0
        if r.get("seconds") is not None:
            cell["times"].append(float(r["seconds"]))
    for cell in cells.values():
        cell["median_s"] = _median(cell.pop("times"))
    return cases, models, cells


def cell_text(cell: dict | None) -> str:
    if not cell or not cell["runs"]:
        return "skip" if cell and cell.get("skipped") else ""
    t = f" {cell['median_s']:.0f}s" if cell.get("median_s") is not None else ""
    if cell["runs"] == 1:
        return ("✓" if cell["passed"] else "✗") + t
    mark = "✓" if cell["passed"] == cell["runs"] else ("✗" if cell["passed"] == 0 else "~")
    return f"{mark} {cell['passed']}/{cell['runs']}{t}"


def failures(results: list[dict]) -> list[dict]:
    return [r for r in results if not r.get("ok") and not r.get("skipped")]


def _fmt(v: Any, limit: int = 200) -> str:
    s = v if isinstance(v, str) else repr(v)
    s = s.replace("\n", " ").replace("|", "\\|")
    return s if len(s) <= limit else s[: limit - 3] + "..."


def _pct(x: float | None) -> str:
    return "" if x is None else f"{x * 100:.0f}%"


def render_markdown(results: list[dict], meta: dict | None = None) -> str:
    meta = meta or {}
    out = ["# Chat battery report", ""]
    if meta:
        for k in ("started", "finished", "mcp_url", "client", "cases", "models", "repeat", "restored",
                  "merged_from"):
            if k in meta:
                out.append(f"- **{k}**: {_fmt(meta[k], 400)}")
        out.append("")

    out += ["## Summary", "",
            "| model | pass rate | runs | checks | median turn | avg tool calls | approvals | timeouts |",
            "|---|---|---|---|---|---|---|---|"]
    for row in summarize(results):
        med = "" if row["median_s"] is None else f"{row['median_s']}s"
        out.append(f"| {row['model']} | {_pct(row['pass_rate'])} ({row['passed']}/{row['runs']}) | "
                   f"{row['runs']} | {row['checks_passed']}/{row['checks_total']} | {med} | "
                   f"{'' if row['avg_tools'] is None else row['avg_tools']} | {row['approvals']} | "
                   f"{row['timeouts']} |")
    out.append("")

    cases, models, cells = matrix(results)
    out += ["## Case x model", "", "| case | " + " | ".join(models) + " |",
            "|---|" + "---|" * len(models)]
    for case in cases:
        out.append(f"| {case} | " + " | ".join(cell_text(cells.get((case, m))) for m in models) + " |")
    out.append("")

    fails = failures(results)
    out += ["## Failures", ""]
    if not fails:
        out += ["None.", ""]
    for r in fails:
        rep = f" (repeat {r['repeat']})" if r.get("repeat", 1) > 1 else ""
        out.append(f"### {r['case']} on {r['model']}{rep}")
        out.append("")
        if r.get("note"):
            out.append(f"- note: {_fmt(r['note'], 400)}")
        if r.get("timed_out"):
            out.append("- the turn timed out and was stopped")
        for c in r.get("checks") or []:
            if not c.get("ok"):
                out.append(f"- **{c['check']}** ({_fmt(c.get('expect'), 160)}): observed "
                           f"`{_fmt(c.get('observed'))}`")
        tools = ", ".join(f"{t.get('name')}{'' if t.get('ok') else '(failed)'}"
                          for t in r.get("tools") or [])
        out.append(f"- tools: {tools or 'none'}")
        for t in r.get("tools") or []:
            if not t.get("ok"):
                why = t.get("error") or "(no error text recorded)"
                out.append(f"  - {t.get('name')} failed: `{_fmt(why, 300)}`")
        if r.get("resnapshot"):
            out.append(f"- note: snapshot retaken, {_fmt(r['resnapshot'], 200)}")
        if r.get("approvals"):
            out.append("- approvals: " + "; ".join(
                f"{a.get('answer')}: {_fmt(a.get('prompt'), 120)}" for a in r["approvals"]))
        if r.get("errors"):
            out.append(f"- errors: {_fmt(r['errors'], 300)}")
        out.append(f"- reply: {_fmt(r.get('reply') or '(empty)', 400)}")
        out.append("")
    return "\n".join(out)
