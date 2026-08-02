#!/usr/bin/env python3
"""Print a one-line-per-document summary table from output/*/extraction_report.json.

    python scripts/summarize_results.py [--output-dir output]

Intended to be run after scripts/run_all_fixtures.py (or any `extract`
invocation) to get a quick overview without opening each report by hand.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default=str(REPO_ROOT / "output"))
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    if not output_dir.exists():
        print(f"Output directory '{output_dir}' does not exist. Run extraction first.", file=sys.stderr)
        return 1

    report_paths = sorted(output_dir.glob("*/extraction_report.json"))
    if not report_paths:
        print(f"No extraction_report.json files found under '{output_dir}'.", file=sys.stderr)
        return 1

    header = (
        f"{'document':<32} {'status':<14} {'pages':>6} {'items':>6} "
        f"{'review':>7} {'warn':>5} {'fatal':>6} {'reconcile':>10}"
    )
    print(header)
    print("-" * len(header))

    worst = 0
    for path in report_paths:
        slug = path.parent.name
        data = json.loads(path.read_text())
        summary = data["summary"]
        reconciliation = data["reconciliation"]
        reconcile_label = (
            "PASS"
            if reconciliation.get("within_tolerance") is True
            else ("FAIL" if reconciliation.get("within_tolerance") is False else "N/A")
        )
        print(
            f"{slug:<32} {data['status']:<14} {summary['pages_total']:>6} "
            f"{summary['line_items_extracted']:>6} {summary['review_items']:>7} "
            f"{summary['warnings']:>5} {summary['fatal_errors']:>6} {reconcile_label:>10}"
        )
        if data["status"] == "failed":
            worst = max(worst, 2)
        elif data["status"] == "needs_review":
            worst = max(worst, 1)

    return worst


if __name__ == "__main__":
    raise SystemExit(main())
