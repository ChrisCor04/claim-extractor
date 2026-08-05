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


@pytest.mark.skipif(not _ARANDA_PROJECT.exists(), reason="aranda-insurance project fixture not present on disk")
def test_confirm_project_with_xactimate_not_running_shows_resume_instructions_and_flags():
    """Phase 5.2 Stage 4/7/12: with no live Xactimate session available,
    'Confirm project' must clearly report that the project could not be
    positively identified (never silently proceed), give the exact
    resume instructions, and still surface capability flags so the user
    can see WHY Safe Autofill isn't available."""
    stale_plan_backup = _PLAN_PATH.read_text(encoding="utf-8") if _PLAN_PATH.exists() else None
    try:
        at = _open_aranda_project()
        build_buttons = [b for b in at.button if "Build / refresh execution plan" in b.label]
        build_buttons[0].click().run()
        assert not at.exception

        name_inputs = [t for t in at.text_input if "Xactimate project name" in t.label]
        assert name_inputs, "project name input not found"
        name_inputs[0].set_value("TEST").run()
        assert not at.exception

        confirm_buttons = [b for b in at.button if b.label == "Confirm project"]
        assert confirm_buttons, "Confirm project button not found"
        confirm_buttons[0].click().run()
        assert not at.exception

        errors = " ".join(e.value for e in at.error)
        assert "Could not positively identify the target Xactimate project" in errors
        assert "Open Xactimate" in errors

        flags_frames = [d.value for d in at.dataframe if "Capability" in d.value.columns]
        assert flags_frames, "capability flags table not rendered"
        flags_df = flags_frames[0]
        assert "Live adapter available" in flags_df["Capability"].values
        live_row = flags_df[flags_df["Capability"] == "Live adapter available"]
        assert live_row["Value"].iloc[0] == False  # noqa: E712 -- pandas bool comparison

        safe_autofill_row = flags_df[flags_df["Capability"] == "Safe Autofill available"]
        assert safe_autofill_row["Value"].iloc[0] == False  # noqa: E712

        execute_buttons = [b for b in at.button if b.label.startswith("Execute")]
        assert execute_buttons and execute_buttons[0].disabled is True
    finally:
        if stale_plan_backup is not None:
            _PLAN_PATH.write_text(stale_plan_backup, encoding="utf-8")
        elif _PLAN_PATH.exists():
            _PLAN_PATH.unlink()


@pytest.mark.skipif(not _ARANDA_PROJECT.exists(), reason="aranda-insurance project fixture not present on disk")
def test_unresolved_rows_section_and_export_buttons():
    """Phase 5.2 Stage 9: a REVIEW_REQUIRED task must appear in the
    prominent 'Rows requiring review' summary and the detailed table,
    and JSON/CSV export must be offered whenever a plan exists."""
    import json

    stale_plan_backup = _PLAN_PATH.read_text(encoding="utf-8") if _PLAN_PATH.exists() else None
    try:
        at = _open_aranda_project()
        build_buttons = [b for b in at.button if "Build / refresh execution plan" in b.label]
        build_buttons[0].click().run()
        assert not at.exception

        data = json.loads(_PLAN_PATH.read_text(encoding="utf-8"))
        assert data["tasks"], "expected at least one task in the built plan"
        data["tasks"][0]["state"] = "review_required"
        data["tasks"][0]["stop_reason"] = "ambiguous_candidates"
        data["tasks"][0]["stop_detail"] = "Simulated for test coverage."
        _PLAN_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")

        at.run()
        assert not at.exception

        warnings = " ".join(w.value for w in at.warning)
        assert "Rows requiring review: 1" in warnings

        download_labels = {b.label for b in at.download_button}
        assert "Download JSON" in download_labels
        assert "Download CSV" in download_labels
    finally:
        if stale_plan_backup is not None:
            _PLAN_PATH.write_text(stale_plan_backup, encoding="utf-8")
        elif _PLAN_PATH.exists():
            _PLAN_PATH.unlink()
