#!/usr/bin/env python3
"""One-command fixture runner: extract every PDF in fixtures/ into output/.

    python scripts/run_all_fixtures.py [--enable-ocr] [--output-dir output]

This is a thin wrapper around the CLI (`estimate_extractor extract`) for
convenience during development, and doubles as the "regenerate debug
outputs" command referenced by the test suite -- it does NOT touch
tests/expected/ golden files, which are updated by hand after manual
review.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES_DIR = REPO_ROOT / "fixtures"


def main() -> int:
    if not FIXTURES_DIR.exists() or not any(FIXTURES_DIR.glob("*.pdf")):
        print(
            f"No PDFs found in {FIXTURES_DIR}. Fixture PDFs contain PII and are "
            "gitignored -- place the six carrier PDFs there before running this "
            "script (see README 'Known limitations' / docs/troubleshooting.md).",
            file=sys.stderr,
        )
        return 4

    args = [sys.executable, "-m", "estimate_extractor", "extract", str(FIXTURES_DIR), "--recursive", "--debug"]
    args.extend(sys.argv[1:])
    print("Running:", " ".join(args))
    result = subprocess.run(args, cwd=REPO_ROOT)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
