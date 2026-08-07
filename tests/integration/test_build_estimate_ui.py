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

# Phase 5.5A: a real, on-disk project with ZERO approved rows and all
# 42 line items missing CAT/SEL -- the exact scenario that reproduced
# the early-return bug live (reported by the user against this same
# project).
_UNMAPPED_PROJECT = Path(__file__).resolve().parents[2] / "projects" / "aranda-insurance-v3"
_UNMAPPED_PLAN_PATH = _UNMAPPED_PROJECT / "execution" / "execution_plan.json"


def _open_aranda_project() -> AppTest:
    at = AppTest.from_file(APP_PATH, default_timeout=30)
    at.run()
    assert not at.exception
    at.session_state["active_project_slug"] = "aranda-insurance"
    at.run()
    assert not at.exception
    return at


def _open_unmapped_project() -> AppTest:
    at = AppTest.from_file(APP_PATH, default_timeout=30)
    at.run()
    assert not at.exception
    at.session_state["active_project_slug"] = "aranda-insurance-v3"
    at.run()
    assert not at.exception
    return at


def _confirm_project(at: AppTest, monkeypatch, project_name: str, *, application_verified=True, project_verified=True) -> None:
    """Confirms `project_name` against a deterministic FakeXactimateAdapter
    (never a real Windows/Xactimate session) -- makes these tests behave
    the same regardless of whether Xactimate happens to be running on
    whatever machine runs the suite, unlike the live-environment-coupled
    'not running' test above."""
    from estimate_extractor.ui.components import build_estimate_panel as bep
    from estimate_extractor.xactimate_lookup.adapter import FakeXactimateAdapter

    monkeypatch.setattr(
        bep, "_construct_windows_adapter",
        lambda name: FakeXactimateAdapter(application_verified=application_verified, project_verified=project_verified),
    )
    name_inputs = [t for t in at.text_input if "Xactimate project name" in t.label]
    assert name_inputs, "project name input not found"
    name_inputs[0].set_value(project_name).run()
    assert not at.exception

    confirm_buttons = [b for b in at.button if b.label == "Confirm project"]
    assert confirm_buttons, "Confirm project button not found"
    confirm_buttons[0].click().run()
    assert not at.exception


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


# ---------------------------------------------------------------------
# Phase 5.5A: the Build Estimate page must render project confirmation
# (and, once confirmed as exactly TEST, the unmapped-row checkbox) even
# when there are zero approved rows -- reproduced live by the user
# against aranda-insurance-v3 (42 rows, 0 approved, all missing CAT/
# SEL): the page rendered ONLY the Build button and an empty-plan
# error, with no way to ever reach "Confirm project".
# ---------------------------------------------------------------------


@pytest.mark.skipif(not _UNMAPPED_PROJECT.exists(), reason="aranda-insurance-v3 project fixture not present on disk")
def test_zero_approved_rows_still_renders_project_confirmation_controls():
    """Requirement 1: opening a project with zero approved rows, before
    clicking anything, must still render the project-confirmation
    controls -- not just the Build button and an empty-plan error."""
    stale_plan_backup = _UNMAPPED_PLAN_PATH.read_text(encoding="utf-8") if _UNMAPPED_PLAN_PATH.exists() else None
    try:
        at = _open_unmapped_project()

        name_inputs = [t for t in at.text_input if "Xactimate project name" in t.label]
        assert name_inputs, "Xactimate project name input not rendered -- early-return regression"

        confirm_buttons = [b for b in at.button if b.label == "Confirm project"]
        assert confirm_buttons, "Confirm project button not rendered -- early-return regression"

        build_buttons = [b for b in at.button if "Build / refresh execution plan" in b.label]
        assert build_buttons, "Build button should still be present"
    finally:
        if stale_plan_backup is not None:
            _UNMAPPED_PLAN_PATH.write_text(stale_plan_backup, encoding="utf-8")
        elif _UNMAPPED_PLAN_PATH.exists():
            _UNMAPPED_PLAN_PATH.unlink()


@pytest.mark.skipif(not _UNMAPPED_PROJECT.exists(), reason="aranda-insurance-v3 project fixture not present on disk")
def test_confirming_test_reveals_the_unmapped_row_checkbox(monkeypatch):
    """Requirement 2: confirming the project as exactly TEST reveals the
    'Include rows missing CAT/SEL and search by description' checkbox,
    even though zero rows are approved."""
    stale_plan_backup = _UNMAPPED_PLAN_PATH.read_text(encoding="utf-8") if _UNMAPPED_PLAN_PATH.exists() else None
    try:
        at = _open_unmapped_project()
        _confirm_project(at, monkeypatch, "TEST")

        successes = " ".join(s.value for s in at.success)
        assert "Confirmed" in successes

        checkboxes = [c for c in at.checkbox if "Include rows missing CAT/SEL" in c.label]
        assert checkboxes, "TEST-only unmapped-row checkbox not shown after confirming TEST"
    finally:
        if stale_plan_backup is not None:
            _UNMAPPED_PLAN_PATH.write_text(stale_plan_backup, encoding="utf-8")
        elif _UNMAPPED_PLAN_PATH.exists():
            _UNMAPPED_PLAN_PATH.unlink()


@pytest.mark.skipif(not _UNMAPPED_PROJECT.exists(), reason="aranda-insurance-v3 project fixture not present on disk")
def test_checking_the_checkbox_and_rebuilding_produces_the_42_task_plan(monkeypatch):
    """Requirement 3: checking the box and building produces a plan that
    includes all 42 real, unmapped-but-otherwise-eligible rows -- no
    approval required, no row's stored status changed."""
    import json

    stale_plan_backup = _UNMAPPED_PLAN_PATH.read_text(encoding="utf-8") if _UNMAPPED_PLAN_PATH.exists() else None
    try:
        at = _open_unmapped_project()
        _confirm_project(at, monkeypatch, "TEST")

        checkboxes = [c for c in at.checkbox if "Include rows missing CAT/SEL" in c.label]
        assert checkboxes
        checkboxes[0].check().run()
        assert not at.exception

        counts_metrics = {m.label: m.value for m in at.metric}
        assert counts_metrics.get("Unmapped, description-search") == "42"
        assert counts_metrics.get("Mapped rows") == "0"

        build_buttons = [b for b in at.button if "Build / refresh execution plan" in b.label]
        build_buttons[0].click().run()
        assert not at.exception

        successes = " ".join(s.value for s in at.success)
        assert "Built a plan with 42 task(s)" in successes

        data = json.loads(_UNMAPPED_PLAN_PATH.read_text(encoding="utf-8"))
        assert len(data["tasks"]) == 42
        # Every task is missing CAT or SEL (or both) -- some rows, like
        # line_0003, have a partial machine-suggested category with no
        # selector, which is still "unmapped" for this purpose.
        assert all(not (t["category"] and t["selector"]) for t in data["tasks"])
        assert all(t["lookup_strategy"] == "test_description_first" for t in data["tasks"])
        assert all(t["began_unmapped"] is True for t in data["tasks"])
    finally:
        if stale_plan_backup is not None:
            _UNMAPPED_PLAN_PATH.write_text(stale_plan_backup, encoding="utf-8")
        elif _UNMAPPED_PLAN_PATH.exists():
            _UNMAPPED_PLAN_PATH.unlink()


@pytest.mark.skipif(not _UNMAPPED_PROJECT.exists(), reason="aranda-insurance-v3 project fixture not present on disk")
def test_multi_group_plan_shows_informational_note_but_never_disables_execute(monkeypatch):
    """Phase 5.5C Stage 10 (revised per live feedback): a plan spanning
    more than one group, built against a FakeXactimateAdapter (multi_
    group_creation_available defaults False, matching every adapter in
    this codebase today), must show an INFORMATIONAL note about the
    known Xactimate group-nesting limit -- but Execute must stay
    enabled. run_execution_plan()'s group loop already catches an
    ensure_group() ancestry failure per-group (marks that group's tasks
    Review Required, continues to the next group) rather than aborting
    or writing to the wrong group, so forcing one-group-at-a-time here
    would add friction without adding safety. See docs/build-estimate.md
    Phase 5.5C."""
    stale_plan_backup = _UNMAPPED_PLAN_PATH.read_text(encoding="utf-8") if _UNMAPPED_PLAN_PATH.exists() else None
    try:
        at = _open_unmapped_project()
        _confirm_project(at, monkeypatch, "TEST")

        checkboxes = [c for c in at.checkbox if "Include rows missing CAT/SEL" in c.label]
        checkboxes[0].check().run()
        assert not at.exception

        build_buttons = [b for b in at.button if "Build / refresh execution plan" in b.label]
        build_buttons[0].click().run()
        assert not at.exception

        groups_metric = next(m.value for m in at.metric if m.label == "Groups in plan")
        assert int(groups_metric) > 1, "this test requires a real multi-group plan to exercise the note"

        infos = " ".join(i.value for i in at.info)
        assert "This plan spans" in infos
        assert "Execute will run all groups" in infos

        # Never a blocking error, and Execute must stay enabled.
        errors = " ".join(e.value for e in at.error)
        assert "Multi-group execution is not currently available" not in errors

        execute_buttons = [b for b in at.button if b.label in ("Execute", "Execute (Safe Autofill)")]
        assert execute_buttons, "Execute button not found"
        assert execute_buttons[0].disabled is False

        # The optional one-group selector is still offered as a
        # convenience, just never forced.
        selectors = [s for s in at.selectbox if s.label == "Group to run this session"]
        assert selectors, "optional one-group selector not offered for a multi-group plan"

        selectors[0].set_value(selectors[0].options[0]).run()
        assert not at.exception
        rebuild_buttons = [b for b in at.button if "Build one-group plan for the selected group" in b.label]
        assert rebuild_buttons
        rebuild_buttons[0].click().run()
        assert not at.exception

        import json
        data = json.loads(_UNMAPPED_PLAN_PATH.read_text(encoding="utf-8"))
        assert len(data["groups"]) == 1
        successes_after = " ".join(s.value for s in at.success)
        assert "Built a one-group plan" in successes_after
    finally:
        if stale_plan_backup is not None:
            _UNMAPPED_PLAN_PATH.write_text(stale_plan_backup, encoding="utf-8")
        elif _UNMAPPED_PLAN_PATH.exists():
            _UNMAPPED_PLAN_PATH.unlink()


@pytest.mark.skipif(not _UNMAPPED_PROJECT.exists(), reason="aranda-insurance-v3 project fixture not present on disk")
def test_non_test_confirmation_does_not_show_the_checkbox(monkeypatch):
    """Requirement 4 / hard scope boundary: confirming any project other
    than exactly TEST must never offer the unmapped-row checkbox."""
    stale_plan_backup = _UNMAPPED_PLAN_PATH.read_text(encoding="utf-8") if _UNMAPPED_PLAN_PATH.exists() else None
    try:
        at = _open_unmapped_project()
        _confirm_project(at, monkeypatch, "some-production-claim")

        successes = " ".join(s.value for s in at.success)
        assert "Confirmed" in successes  # confirmation itself succeeds...

        checkboxes = [c for c in at.checkbox if "Include rows missing CAT/SEL" in c.label]
        assert not checkboxes, "checkbox must never appear for a non-TEST confirmed project"
    finally:
        if stale_plan_backup is not None:
            _UNMAPPED_PLAN_PATH.write_text(stale_plan_backup, encoding="utf-8")
        elif _UNMAPPED_PLAN_PATH.exists():
            _UNMAPPED_PLAN_PATH.unlink()


@pytest.mark.skipif(not _UNMAPPED_PROJECT.exists(), reason="aranda-insurance-v3 project fixture not present on disk")
def test_unchecked_checkbox_still_shows_the_approved_only_empty_plan_message(monkeypatch):
    """Requirement 5: even after confirming TEST, leaving the checkbox
    unchecked and clicking Build must still behave exactly like the
    normal approved-only path -- zero approved rows here, so the
    original empty-plan message is still shown, not a silently
    different outcome."""
    stale_plan_backup = _UNMAPPED_PLAN_PATH.read_text(encoding="utf-8") if _UNMAPPED_PLAN_PATH.exists() else None
    try:
        at = _open_unmapped_project()
        _confirm_project(at, monkeypatch, "TEST")

        checkboxes = [c for c in at.checkbox if "Include rows missing CAT/SEL" in c.label]
        assert checkboxes
        assert checkboxes[0].value is False  # unchecked by default

        build_buttons = [b for b in at.button if "Build / refresh execution plan" in b.label]
        build_buttons[0].click().run()
        assert not at.exception

        errors = " ".join(e.value for e in at.error)
        assert "No approved, executable line items found in this project" in errors
    finally:
        if stale_plan_backup is not None:
            _UNMAPPED_PLAN_PATH.write_text(stale_plan_backup, encoding="utf-8")
        elif _UNMAPPED_PLAN_PATH.exists():
            _UNMAPPED_PLAN_PATH.unlink()


@pytest.mark.skipif(not _ARANDA_PROJECT.exists(), reason="aranda-insurance project fixture not present on disk")
def test_existing_approved_row_workflow_still_works(monkeypatch):
    """Requirement 6: the restructured page must not disturb the
    ordinary approved-only workflow -- aranda-insurance's one approved
    TEST_CAT/TEST_SEL row still builds, and project confirmation still
    works exactly as before, with no TEST-only checkbox involved."""
    stale_plan_backup = _PLAN_PATH.read_text(encoding="utf-8") if _PLAN_PATH.exists() else None
    try:
        at = _open_aranda_project()
        build_buttons = [b for b in at.button if "Build / refresh execution plan" in b.label]
        build_buttons[0].click().run()
        assert not at.exception

        successes = " ".join(s.value for s in at.success)
        assert "Built a plan with" in successes

        _confirm_project(at, monkeypatch, "TEST")
        successes2 = " ".join(s.value for s in at.success)
        assert "Confirmed" in successes2
    finally:
        if stale_plan_backup is not None:
            _PLAN_PATH.write_text(stale_plan_backup, encoding="utf-8")
        elif _PLAN_PATH.exists():
            _PLAN_PATH.unlink()


# ---------------------------------------------------------------------
# Phase 5.5B, Objective 3 (UI half) / Objective 4: stop-reason and
# remaining-count visibility, and TEST-only reset/rebuild actions.
# ---------------------------------------------------------------------


@pytest.mark.skipif(not _ARANDA_PROJECT.exists(), reason="aranda-insurance project fixture not present on disk")
def test_execution_stop_reason_and_remaining_count_are_displayed():
    """Requirement 9: a plan with some terminal and some still-pending
    tasks shows both 'Execution stopped after row X because: ...' and
    'Remaining unattempted rows: N' -- built only from persisted state,
    so it survives a UI rerun."""
    import json

    stale_plan_backup = _PLAN_PATH.read_text(encoding="utf-8") if _PLAN_PATH.exists() else None
    try:
        at = _open_aranda_project()
        build_buttons = [b for b in at.button if "Build / refresh execution plan" in b.label]
        build_buttons[0].click().run()
        assert not at.exception

        data = json.loads(_PLAN_PATH.read_text(encoding="utf-8"))
        assert data["tasks"], "expected at least one task"
        data["tasks"][0]["state"] = "failed"
        data["tasks"][0]["stop_reason"] = "no_results"
        data["tasks"][0]["stop_detail"] = "Simulated for test coverage."
        _PLAN_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")

        at.run()
        assert not at.exception

        captions = " | ".join(c.value for c in at.caption)
        assert "Execution stopped after Row 1 because:" in captions
        assert "Remaining unattempted rows:" in captions
    finally:
        if stale_plan_backup is not None:
            _PLAN_PATH.write_text(stale_plan_backup, encoding="utf-8")
        elif _PLAN_PATH.exists():
            _PLAN_PATH.unlink()


@pytest.mark.skipif(not _UNMAPPED_PROJECT.exists(), reason="aranda-insurance-v3 project fixture not present on disk")
def test_reset_unfinished_button_preserves_completed_tasks_via_ui(monkeypatch):
    """Objective 4 (UI half): clicking 'Reset unfinished TEST execution'
    resets non-completed tasks back to pending and leaves a completed
    task alone -- exercised through the real UI button, not just the
    backend function directly."""
    import json

    stale_plan_backup = _UNMAPPED_PLAN_PATH.read_text(encoding="utf-8") if _UNMAPPED_PLAN_PATH.exists() else None
    try:
        at = _open_unmapped_project()
        _confirm_project(at, monkeypatch, "TEST")
        checkboxes = [c for c in at.checkbox if "Include rows missing CAT/SEL" in c.label]
        checkboxes[0].check().run()
        build_buttons = [b for b in at.button if "Build / refresh execution plan" in b.label]
        build_buttons[0].click().run()
        assert not at.exception

        data = json.loads(_UNMAPPED_PLAN_PATH.read_text(encoding="utf-8"))
        data["tasks"][0]["state"] = "completed"
        data["tasks"][0]["trust_state"] = "VERIFIED"
        data["tasks"][1]["state"] = "review_required"
        data["tasks"][1]["stop_reason"] = "ambiguous_candidates"
        _UNMAPPED_PLAN_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")

        at.run()
        assert not at.exception
        reset_buttons = [b for b in at.button if b.label == "Reset unfinished TEST execution"]
        assert reset_buttons, "Reset button not shown for a confirmed TEST project"
        reset_buttons[0].click().run()
        assert not at.exception

        after = json.loads(_UNMAPPED_PLAN_PATH.read_text(encoding="utf-8"))
        assert after["tasks"][0]["state"] == "completed"  # untouched
        assert after["tasks"][1]["state"] == "pending"  # reset
    finally:
        if stale_plan_backup is not None:
            _UNMAPPED_PLAN_PATH.write_text(stale_plan_backup, encoding="utf-8")
        elif _UNMAPPED_PLAN_PATH.exists():
            _UNMAPPED_PLAN_PATH.unlink()
