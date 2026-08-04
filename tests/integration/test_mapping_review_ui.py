"""Headless UI regression test for the Mapping Review screen (Phase 5.0
Priority 2). Uses Streamlit's ``AppTest`` harness to actually RUN
``ui/app.py`` end to end against a real, on-disk project -- not just an
import check -- so a runtime widget/rendering error (e.g. a KeyError from a
renamed column, or an empty-groups edge case) is caught the same way a
human clicking through the app would hit it.

Requires the ``ui`` optional dependency group (``pip install -e ".[ui]"``);
skips gracefully if streamlit isn't installed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("streamlit")

from streamlit.testing.v1 import AppTest  # noqa: E402

APP_PATH = str(Path(__file__).resolve().parents[2] / "src" / "estimate_extractor" / "ui" / "app.py")
PROJECTS_DIR = str(Path(__file__).resolve().parents[2] / "projects")

_ARANDA_PROJECT = Path(PROJECTS_DIR) / "aranda-insurance"


def _open_aranda_project() -> AppTest:
    at = AppTest.from_file(APP_PATH, default_timeout=30)
    at.run()
    assert not at.exception, f"App failed on initial load: {at.exception}"
    # Simulate the sidebar/Projects-tab flow: set the active project directly
    # via session state the same way project_summary.py's "Open" button does.
    at.session_state["active_project_slug"] = "aranda-insurance"
    at.run()
    assert not at.exception, f"App failed after opening a project: {at.exception}"
    return at


@pytest.mark.skipif(not _ARANDA_PROJECT.exists(), reason="aranda-insurance project fixture not present on disk")
def test_mapping_review_tab_renders_without_exception():
    at = _open_aranda_project()
    assert not at.exception


@pytest.mark.skipif(not _ARANDA_PROJECT.exists(), reason="aranda-insurance project fixture not present on disk")
def test_mapping_review_shows_grouped_expanders_for_every_area_section():
    at = _open_aranda_project()
    headers = [e.label for e in at.expander]
    # Every group present in the real Aranda extraction must have its own
    # expander -- this is the literal "grouped by Area / Section" requirement.
    for expected in ("Dwelling / Dwelling Roof", "Dwelling / Front Elevation", "(No area) / Fence", "(No area) / Debris Removal"):
        assert any(h.startswith(expected) for h in headers), f"missing group header starting with {expected!r} in {headers}"


@pytest.mark.skipif(not _ARANDA_PROJECT.exists(), reason="aranda-insurance project fixture not present on disk")
def test_mapping_review_readiness_filter_does_not_crash_on_empty_result():
    at = _open_aranda_project()
    readiness_selects = [sb for sb in at.selectbox if sb.key == "mapping_readiness_filter"]
    assert readiness_selects, "readiness filter selectbox not found"
    readiness_selects[0].set_value("Ready to approve").run()
    assert not at.exception
