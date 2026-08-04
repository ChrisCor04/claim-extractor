"""Headless UI regression test for the Build Estimate screen (Phase 5.0
Priority 4). Uses Streamlit's AppTest harness against the real, on-disk
aranda-insurance project (which has exactly one approved line item,
TEST_CAT/TEST_SEL) to prove the tab renders and can build a plan without
touching Xactimate."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("streamlit")

from streamlit.testing.v1 import AppTest  # noqa: E402

APP_PATH = str(Path(__file__).resolve().parents[2] / "src" / "estimate_extractor" / "ui" / "app.py")
_ARANDA_PROJECT = Path(__file__).resolve().parents[2] / "projects" / "aranda-insurance"
_PLAN_PATH = _ARANDA_PROJECT / "execution" / "execution_plan.json"


def _open_aranda_project() -> AppTest:
    at = AppTest.from_file(APP_PATH, default_timeout=30)
    at.run()
    assert not at.exception
    at.session_state["active_project_slug"] = "aranda-insurance"
    at.run()
    assert not at.exception
    return at


@pytest.mark.skipif(not _ARANDA_PROJECT.exists(), reason="aranda-insurance project fixture not present on disk")
def test_build_estimate_tab_renders_without_exception():
    at = _open_aranda_project()
    assert not at.exception


@pytest.mark.skipif(not _ARANDA_PROJECT.exists(), reason="aranda-insurance project fixture not present on disk")
def test_build_execution_plan_button_builds_a_real_plan_from_approved_items():
    stale_plan_backup = None
    if _PLAN_PATH.exists():
        stale_plan_backup = _PLAN_PATH.read_text(encoding="utf-8")
    try:
        at = _open_aranda_project()
        build_buttons = [b for b in at.button if "Build / refresh execution plan" in b.label]
        assert build_buttons, "Build/refresh plan button not found"
        build_buttons[0].click().run()
        assert not at.exception

        assert _PLAN_PATH.exists(), "execution plan was not persisted to disk"
        import json

        data = json.loads(_PLAN_PATH.read_text(encoding="utf-8"))
        assert data["project_slug"] == "aranda-insurance"
        assert len(data["tasks"]) >= 1
        assert data["tasks"][0]["category"] == "TEST_CAT"
        assert data["tasks"][0]["selector"] == "TEST_SEL"
    finally:
        if stale_plan_backup is not None:
            _PLAN_PATH.write_text(stale_plan_backup, encoding="utf-8")
        elif _PLAN_PATH.exists():
            _PLAN_PATH.unlink()
