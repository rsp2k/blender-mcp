# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Merge battery results.json files into one report.

Later files win for any (model, case) they contain, so a rerun of a few
cases updates a full run:

    uv run scripts/battery/merge_results.py artifacts/battery/full/results.json \\
        artifacts/battery/rerun/results.json --out artifacts/battery/merged
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from report import merge, render_markdown, summarize


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("results", nargs="+", type=Path, help="results.json files, oldest first")
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    docs = [json.loads(p.read_text()) for p in args.results]
    results = merge([d.get("results") or [] for d in docs])
    meta = {"merged_from": [str(p) for p in args.results],
            "started": docs[0].get("meta", {}).get("started"),
            "finished": docs[-1].get("meta", {}).get("finished"),
            "restored": docs[-1].get("meta", {}).get("restored")}
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "results.json").write_text(json.dumps(
        {"meta": meta, "summary": summarize(results), "results": results}, indent=1, default=str))
    (args.out / "report.md").write_text(render_markdown(results, meta))
    print(f"wrote {args.out / 'report.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
