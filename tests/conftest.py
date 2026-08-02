from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES_DIR = REPO_ROOT / "fixtures" / "originals"


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    """The repo-root fixtures/originals/ directory containing real carrier
    PDFs (the six original estimates -- see fixtures/supplements/ for the
    matching supplement PDFs).

    These files contain PII and are gitignored (see .gitignore /
    docs/troubleshooting.md); integration tests that need them skip
    gracefully when the directory is empty (e.g. in a fresh checkout that
    hasn't had fixtures supplied locally).
    """
    return FIXTURES_DIR


def require_fixture(fixtures_dir: Path, filename: str) -> Path:
    path = fixtures_dir / filename
    if not path.exists():
        pytest.skip(f"fixture '{filename}' not present locally (PII files are gitignored)")
    return path
