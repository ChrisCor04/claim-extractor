"""Unit tests for the persisted, project-level ExecutionPlan (Phase 5.0
Priority 3). Mirrors tests/unit/test_review_service.py's fixture pattern
(raw normalized/mapped JSON written to a tmp project dir) so building a
plan is exercised the same way review_service itself is tested."""

from __future__ import annotations

import json

import pytest

from estimate_extractor.ui import review_service
from estimate_extractor.xactimate_lookup.execution_plan import (
    ExecutionPlan,
    ExecutionPlanError,
    ExecutionPlanOverwriteRefused,
    ExecutionTask,
    GROUP_PENDING,
    LOOKUP_STRATEGY_REVIEW_APPROVED,
    LOOKUP_STRATEGY_TEST_DESCRIPTION_FIRST,
    PAIR_ACTIVATED_PENDING_BINDING,
    PAIR_UNACTIVATED,
    RUN_STATE_COMPLETED,
    TASK_COMMIT_STATE_COMMITTED,
    TASK_COMMIT_STATE_NOT_COMMITTED,
    TASK_COMPLETED,
    TASK_FAILED,
    TASK_PENDING,
    TASK_REVIEW_REQUIRED,
    TASK_SKIPPED,
    build_execution_plan,
    classify_unmapped_rows,
    commit_state_from_trust_state,
    diagnose_run,
    load_execution_plan,
    reset_unfinished_tasks,
    restricted_plan_path,
    restricted_reports_dir,
    save_execution_plan,
    task_has_committed_row,
)


def _normalized_item(
    line_item_id, description, area_name, section_name, quantity=10.0, unit="SQ",
    *, action="remove_and_replace", trade="roofing", component="shingles", material="laminated",
):
    return {
        "line_item_id": line_item_id,
        "original": {
            "description": description,
            "quantity": quantity,
            "unit_of_measure": unit,
            "coverage_id": "coverage_001",
            "section_name": section_name,
            "area_name": area_name,
            "source_pages": [3],
            "notes": [],
            "extraction_confidence": 0.95,
            "extraction_needs_review": False,
            "extraction_warnings": [],
        },
        "normalized": {
            "action": action, "trade": trade, "component": component,
            "material": material, "attributes": {}, "quantity": quantity, "unit_of_measure": unit,
        },
        "confidence": {"overall": 0.9, "action": 0.9, "trade": 0.9, "component": 0.9, "material": 0.9},
        "needs_review": False,
        "review_reasons": [],
    }


def _mapped_item(line_item_id, category=None, selector=None, status="mapped"):
    best_match = None
    if category and selector:
        best_match = {
            "mapping_id": f"m_{line_item_id}", "category": category, "selector": selector,
            "activity": "install", "description": f"{category}/{selector} description", "confidence": 0.95,
        }
    return {
        "line_item_id": line_item_id,
        "coverage_id": "coverage_001",
        "normalization": {
            "action": "remove_and_replace", "trade": "roofing", "component": "shingles",
            "material": "laminated", "attributes": {}, "quantity": 10.0, "unit_of_measure": "SQ",
        },
        "mapping": {
            "status": status, "best_match": best_match, "alternatives": [],
            "needs_review": status != "mapped", "review_reasons": [],
        },
    }


def _write_project(tmp_path, items):
    """`items` is a list of (line_item_id, area_name, section_name, category, selector)."""
    project_dir = tmp_path / "test-project"
    (project_dir / "mapping").mkdir(parents=True)
    (project_dir / "review").mkdir(parents=True)

    normalized = [_normalized_item(lid, f"Description for {lid}", area, section) for lid, area, section, _, _ in items]
    mapped = [_mapped_item(lid, category=cat, selector=sel) for lid, _, _, cat, sel in items]

    (project_dir / "mapping" / "normalized_estimate.json").write_text(json.dumps(normalized), encoding="utf-8")
    (project_dir / "mapping" / "mapped_estimate.json").write_text(json.dumps(mapped), encoding="utf-8")
    return project_dir


def _approve_all(project_dir, line_item_ids):
    for lid in line_item_ids:
        review_service.approve_item(project_dir, lid, reviewer="test")


ITEMS = [
    ("line_0001", "Dwelling", "Dwelling Roof", "RFG", "3TAB"),
    ("line_0002", "Dwelling", "Dwelling Roof", "RFG", "RIDGC"),
    ("line_0003", "Dwelling", "Front Elevation", "SFG", "GUTA"),
    ("line_0004", None, "Fence", "FEN", "WOOD6"),
]


def test_build_execution_plan_raises_when_nothing_approved(tmp_path):
    project_dir = _write_project(tmp_path, ITEMS)
    with pytest.raises(ExecutionPlanError, match="No approved"):
        build_execution_plan(project_dir, "test-project")


def test_build_execution_plan_only_includes_approved_items(tmp_path):
    project_dir = _write_project(tmp_path, ITEMS)
    _approve_all(project_dir, ["line_0001", "line_0003"])

    plan = build_execution_plan(project_dir, "test-project")
    assert {t.line_item_id for t in plan.tasks} == {"line_0001", "line_0003"}
    assert all(t.lookup_strategy == LOOKUP_STRATEGY_REVIEW_APPROVED for t in plan.tasks)
    assert all(t.state == TASK_PENDING for t in plan.tasks)


def test_build_execution_plan_groups_by_section_in_source_order(tmp_path):
    project_dir = _write_project(tmp_path, ITEMS)
    _approve_all(project_dir, [lid for lid, *_ in ITEMS])

    plan = build_execution_plan(project_dir, "test-project")
    assert len(plan.tasks) == 4
    group_names = [g.section_name for g in plan.groups]
    # Dwelling Roof appears first in source order (line_0001/0002), then
    # Front Elevation (line_0003), then Fence (line_0004) -- never
    # alphabetical.
    assert group_names == ["Dwelling Roof", "Front Elevation", "Fence"]

    roof_group = plan.group_by_id("Dwelling Roof")
    assert len(roof_group.task_ids) == 2
    assert roof_group.state == GROUP_PENDING
    assert plan.tasks_in_group("Dwelling Roof")[0].line_item_id == "line_0001"


def test_build_execution_plan_carries_quantity_and_unit_provenance(tmp_path):
    project_dir = _write_project(tmp_path, ITEMS)
    _approve_all(project_dir, ["line_0001"])
    plan = build_execution_plan(project_dir, "test-project")
    task = plan.tasks[0]
    assert task.source_quantity == 10.0
    assert task.source_unit == "SQ"
    assert task.expected_unit == "SQ"
    assert task.source_page == 3
    assert task.entered_quantity is None
    assert task.observed_quantity is None
    assert task.observed_unit is None


def test_build_execution_plan_missing_line_item_ids_filter_narrows_scope(tmp_path):
    project_dir = _write_project(tmp_path, ITEMS)
    _approve_all(project_dir, [lid for lid, *_ in ITEMS])
    plan = build_execution_plan(project_dir, "test-project", line_item_ids=["line_0002"])
    assert [t.line_item_id for t in plan.tasks] == ["line_0002"]


# ---------------------------------------------------------------------
# Phase 5.5C Stage 10: restrict_to_group_id -- the one-group-at-a-time
# UI fallback rebuilds the plan restricted to a single group's own
# rows, since live investigation found Xactimate cannot reliably create
# more than two sibling groups per session (see docs/build-estimate.md
# Phase 5.5C). A run must never silently include a second group's rows.
# ---------------------------------------------------------------------


def test_build_execution_plan_restrict_to_group_id_filters_to_one_group(tmp_path):
    project_dir = _write_project(tmp_path, ITEMS)
    _approve_all(project_dir, [lid for lid, *_ in ITEMS])

    plan = build_execution_plan(project_dir, "test-project", restrict_to_group_id="Dwelling Roof")

    assert len(plan.groups) == 1
    assert plan.groups[0].group_id == "Dwelling Roof"
    assert {t.line_item_id for t in plan.tasks} == {"line_0001", "line_0002"}


def test_build_execution_plan_restrict_to_group_id_never_pulls_in_another_group(tmp_path):
    """The restricted plan's tasks must be an exact subset of the
    unrestricted plan's tasks for that group -- never more, never a
    different group's rows leaking in via a group_id formula mismatch."""
    project_dir = _write_project(tmp_path, ITEMS)
    _approve_all(project_dir, [lid for lid, *_ in ITEMS])

    full_plan = build_execution_plan(project_dir, "test-project")
    full_group_task_ids = {t.line_item_id for t in full_plan.tasks_in_group("Front Elevation")}

    restricted_plan = build_execution_plan(project_dir, "test-project", restrict_to_group_id="Front Elevation")
    restricted_task_ids = {t.line_item_id for t in restricted_plan.tasks}

    assert restricted_task_ids == full_group_task_ids == {"line_0003"}
    assert len(restricted_plan.groups) == 1


def test_build_execution_plan_restrict_to_group_id_raises_when_no_rows_match(tmp_path):
    project_dir = _write_project(tmp_path, ITEMS)
    _approve_all(project_dir, [lid for lid, *_ in ITEMS])

    with pytest.raises(ExecutionPlanError, match="No approved"):
        build_execution_plan(project_dir, "test-project", restrict_to_group_id="Nonexistent Group")


def test_save_and_load_execution_plan_round_trips(tmp_path):
    project_dir = _write_project(tmp_path, ITEMS)
    _approve_all(project_dir, ["line_0001", "line_0004"])
    plan = build_execution_plan(project_dir, "test-project")
    plan.tasks[0].state = TASK_COMPLETED
    plan.tasks[0].observed_quantity = 10.0
    plan.tasks[0].observed_unit = "SQ"
    plan.tasks[0].trust_state = "VERIFIED"
    plan.resume_cursor = 1

    save_execution_plan(plan, project_dir)
    reloaded = load_execution_plan(project_dir)

    assert reloaded is not None
    assert reloaded.plan_id == plan.plan_id
    assert reloaded.resume_cursor == 1
    assert len(reloaded.tasks) == 2
    completed = reloaded.task_by_id("task_line_0001")
    assert completed.state == TASK_COMPLETED
    assert completed.observed_quantity == 10.0
    assert completed.trust_state == "VERIFIED"


def test_load_execution_plan_returns_none_when_absent(tmp_path):
    project_dir = tmp_path / "no-plan-project"
    project_dir.mkdir()
    assert load_execution_plan(project_dir) is None


def test_execution_summary_matches_required_report_format():
    plan = ExecutionPlan(plan_id="p1", project_slug="s", source_filename=None, created_at="now")
    plan.tasks = [
        ExecutionTask(
            task_id=f"t{i}", line_item_id=f"line_{i:04d}", source_order=i, area_name=None, section_name="Roof",
            description="d", category="C", selector="S", lookup_strategy=LOOKUP_STRATEGY_REVIEW_APPROVED,
            source_quantity=1.0, source_unit="EA", expected_unit="EA", state=state,
        )
        for i, state in enumerate(
            [TASK_COMPLETED] * 31 + [TASK_REVIEW_REQUIRED] * 4 + [TASK_SKIPPED] * 2 + [TASK_FAILED] * 1
        )
    ]
    summary = plan.summary()
    assert summary.completed == 31
    assert len(summary.review_required_labels) == 4
    assert summary.skipped == 2
    assert len(summary.failed_labels) == 1
    text = summary.render_text()
    assert "Completed: 31" in text
    assert "Review Required:" in text
    assert "Skipped: 2" in text
    assert "Failed: 1" in text


# ---------------------------------------------------------------------
# Phase 5.5: TEST-only inclusion of rows missing CAT/SEL, searched by
# description instead of being excluded from the plan entirely.
# ---------------------------------------------------------------------


def _write_flexible_project(tmp_path, project_name, entries):
    """`entries`: list of dicts with keys line_item_id, description,
    area_name, section_name, quantity, unit, category, selector,
    status ("approved"/"rejected"/omitted for unreviewed), and
    optionally action/trade/component/material (each defaults to the
    same fixed values _normalized_item() always used, so every
    existing caller that never sets them is byte-for-byte unaffected).
    Reuses _normalized_item()/_mapped_item() above but allows each
    field to vary per row, which the fixed-shape ITEMS/_write_project()
    above does not."""
    project_dir = tmp_path / project_name
    (project_dir / "mapping").mkdir(parents=True)
    (project_dir / "review").mkdir(parents=True)

    normalized_kwargs = ("action", "trade", "component", "material")
    normalized = [
        _normalized_item(
            e["line_item_id"], e.get("description", "A real source description"),
            e.get("area_name"), e.get("section_name"),
            quantity=e.get("quantity", 10.0), unit=e.get("unit", "SQ"),
            **{k: e[k] for k in normalized_kwargs if k in e},
        )
        for e in entries
    ]
    mapped = [_mapped_item(e["line_item_id"], category=e.get("category"), selector=e.get("selector")) for e in entries]
    (project_dir / "mapping" / "normalized_estimate.json").write_text(json.dumps(normalized), encoding="utf-8")
    (project_dir / "mapping" / "mapped_estimate.json").write_text(json.dumps(mapped), encoding="utf-8")

    for e in entries:
        if e.get("status") == "approved":
            review_service.approve_item(project_dir, e["line_item_id"], reviewer="test")
        elif e.get("status") == "rejected":
            review_service.reject_item(project_dir, e["line_item_id"], reviewer="test")
    return project_dir


_UNMAPPED_TEST_ENTRIES = [
    {"line_item_id": "line_mapped", "area_name": "Dwelling", "section_name": "Dwelling Roof",
     "category": "RFG", "selector": "FELT15", "status": "approved"},
    {"line_item_id": "line_unmapped_eligible", "area_name": "Dwelling", "section_name": "Dwelling Roof",
     "description": "Roofing felt - 15 lb.", "quantity": 33.66, "unit": "SQ"},
    {"line_item_id": "line_missing_description", "area_name": "Dwelling", "section_name": "Exterior",
     "description": "", "quantity": 10.0, "unit": "LF"},
    {"line_item_id": "line_missing_quantity", "area_name": "Dwelling", "section_name": "Exterior",
     "description": "A real source description", "quantity": None, "unit": "LF"},
    {"line_item_id": "line_missing_unit", "area_name": "Dwelling", "section_name": "Exterior",
     "description": "A real source description", "quantity": 10.0, "unit": None},
    {"line_item_id": "line_unresolved_group", "area_name": None, "section_name": None,
     "description": "A real source description", "quantity": 10.0, "unit": "LF"},
    {"line_item_id": "line_rejected_unmapped", "area_name": "Dwelling", "section_name": "Dwelling Roof",
     "description": "A real source description", "quantity": 10.0, "unit": "LF", "status": "rejected"},
]


def test_normal_builder_excludes_unmapped_rows_by_default(tmp_path):
    """Requirement 1: the normal execution-plan builder remains
    approved-only -- include_unmapped_rows defaults to False, so an
    unmapped-but-otherwise-eligible row is still excluded exactly as
    before this phase."""
    project_dir = _write_flexible_project(tmp_path, "proj", _UNMAPPED_TEST_ENTRIES)
    plan = build_execution_plan(project_dir, "proj")
    assert [t.line_item_id for t in plan.tasks] == ["line_mapped"]
    assert plan.tasks[0].lookup_strategy == LOOKUP_STRATEGY_REVIEW_APPROVED
    assert plan.tasks[0].began_unmapped is False


def test_include_unmapped_rows_adds_eligible_unmapped_rows_for_test_project(tmp_path):
    """Requirement 2: the TEST-only option includes otherwise eligible
    rows missing CAT/SEL, without requiring them to be approved and
    without changing their stored review status."""
    project_dir = _write_flexible_project(tmp_path, "proj", _UNMAPPED_TEST_ENTRIES)

    plan = build_execution_plan(project_dir, "proj", include_unmapped_rows=True, xactimate_project_name="TEST")

    ids = {t.line_item_id for t in plan.tasks}
    assert "line_mapped" in ids
    assert "line_unmapped_eligible" in ids
    assert "line_missing_description" not in ids
    assert "line_missing_quantity" not in ids
    assert "line_missing_unit" not in ids
    assert "line_unresolved_group" not in ids
    assert "line_rejected_unmapped" not in ids

    unmapped_task = plan.task_by_id("task_line_unmapped_eligible")
    assert unmapped_task.category is None
    assert unmapped_task.selector is None
    assert unmapped_task.lookup_strategy == LOOKUP_STRATEGY_TEST_DESCRIPTION_FIRST
    assert unmapped_task.began_unmapped is True

    # Never required approval, never touched the row's stored status.
    effective = review_service.build_effective_rows(project_dir, line_item_ids=["line_unmapped_eligible"])[0]
    assert effective["status"] == review_service.STATUS_UNREVIEWED

    mapped_task = plan.task_by_id("task_line_mapped")
    assert mapped_task.lookup_strategy == LOOKUP_STRATEGY_REVIEW_APPROVED
    assert mapped_task.began_unmapped is False


def test_include_unmapped_rows_refuses_when_project_is_not_test(tmp_path):
    """Requirement 3: the option refuses any project other than TEST."""
    project_dir = _write_flexible_project(tmp_path, "proj", _UNMAPPED_TEST_ENTRIES)

    with pytest.raises(ExecutionPlanError, match="TEST"):
        build_execution_plan(project_dir, "proj", include_unmapped_rows=True, xactimate_project_name="production-claim-42")

    with pytest.raises(ExecutionPlanError, match="TEST"):
        build_execution_plan(project_dir, "proj", include_unmapped_rows=True, xactimate_project_name=None)


def test_classify_unmapped_rows_blocks_missing_description(tmp_path):
    """Requirement 4: rows missing description remain blocked."""
    project_dir = _write_flexible_project(tmp_path, "proj", _UNMAPPED_TEST_ENTRIES)
    eligibility = classify_unmapped_rows(project_dir)
    assert "line_missing_description" in {r["line_item_id"] for r in eligibility.blocked_missing_description}
    assert "line_missing_description" not in {r["line_item_id"] for r in eligibility.unmapped_eligible}


def test_classify_unmapped_rows_blocks_missing_quantity(tmp_path):
    """Requirement 5: rows missing quantity remain blocked."""
    project_dir = _write_flexible_project(tmp_path, "proj", _UNMAPPED_TEST_ENTRIES)
    eligibility = classify_unmapped_rows(project_dir)
    assert "line_missing_quantity" in {r["line_item_id"] for r in eligibility.blocked_missing_quantity}
    assert "line_missing_quantity" not in {r["line_item_id"] for r in eligibility.unmapped_eligible}


def test_classify_unmapped_rows_blocks_missing_unit(tmp_path):
    """Requirement 6: rows missing unit remain blocked."""
    project_dir = _write_flexible_project(tmp_path, "proj", _UNMAPPED_TEST_ENTRIES)
    eligibility = classify_unmapped_rows(project_dir)
    assert "line_missing_unit" in {r["line_item_id"] for r in eligibility.blocked_missing_unit}
    assert "line_missing_unit" not in {r["line_item_id"] for r in eligibility.unmapped_eligible}


def test_classify_unmapped_rows_blocks_unresolved_group(tmp_path):
    """Requirement 7: unresolved groups remain blocked."""
    project_dir = _write_flexible_project(tmp_path, "proj", _UNMAPPED_TEST_ENTRIES)
    eligibility = classify_unmapped_rows(project_dir)
    assert "line_unresolved_group" in {r["line_item_id"] for r in eligibility.blocked_unresolved_group}
    assert "line_unresolved_group" not in {r["line_item_id"] for r in eligibility.unmapped_eligible}


def test_classify_unmapped_rows_excludes_rejected_rows_and_counts_mapped_correctly(tmp_path):
    """A human-rejected row is never included, mapped or not -- and the
    counts() view matches the UI's required breakdown."""
    project_dir = _write_flexible_project(tmp_path, "proj", _UNMAPPED_TEST_ENTRIES)
    eligibility = classify_unmapped_rows(project_dir)
    all_ids = {r["line_item_id"] for bucket in (
        eligibility.mapped, eligibility.unmapped_eligible, eligibility.blocked_missing_description,
        eligibility.blocked_missing_quantity, eligibility.blocked_missing_unit, eligibility.blocked_unresolved_group,
    ) for r in bucket}
    assert "line_rejected_unmapped" not in all_ids

    counts = eligibility.counts()
    assert counts["mapped"] == 1
    assert counts["unmapped_eligible"] == 1
    assert counts["blocked_missing_description"] == 1
    assert counts["blocked_missing_quantity"] == 1
    assert counts["blocked_missing_unit"] == 1
    assert counts["blocked_unresolved_group"] == 1


def test_unmapped_task_carries_normalized_context_for_description_first_search(tmp_path):
    """The normalized action/trade/component/material needed by the
    existing description-first phrase generator/ranker are carried onto
    the task ONLY for a began_unmapped task -- never for the normal
    approved-CAT/SEL path (unchanged behavior)."""
    project_dir = _write_flexible_project(tmp_path, "proj", _UNMAPPED_TEST_ENTRIES)
    plan = build_execution_plan(project_dir, "proj", include_unmapped_rows=True, xactimate_project_name="TEST")

    unmapped_task = plan.task_by_id("task_line_unmapped_eligible")
    assert unmapped_task.normalized_trade == "roofing"
    assert unmapped_task.normalized_component == "shingles"

    mapped_task = plan.task_by_id("task_line_mapped")
    assert mapped_task.normalized_trade is None
    assert mapped_task.normalized_component is None


# ---------------------------------------------------------------------
# Phase 5.5B, Objective 4: reset/rebuild plan maintenance.
# ---------------------------------------------------------------------


def _plan_with_mixed_states():
    plan = ExecutionPlan(plan_id="p1", project_slug="s", source_filename=None, created_at="now")
    plan.tasks = [
        ExecutionTask(
            task_id="t_completed", line_item_id="line_completed", source_order=0, area_name=None, section_name="Roof",
            description="d", category="RFG", selector="FELT15", lookup_strategy=LOOKUP_STRATEGY_REVIEW_APPROVED,
            source_quantity=1.0, source_unit="SQ", expected_unit="SQ", state=TASK_COMPLETED,
            trust_state="VERIFIED", observed_quantity=1.0, observed_unit="SQ", started_at="t1", completed_at="t2",
        ),
        ExecutionTask(
            task_id="t_review", line_item_id="line_review", source_order=1, area_name=None, section_name="Roof",
            description="d", category=None, selector=None, lookup_strategy=LOOKUP_STRATEGY_TEST_DESCRIPTION_FIRST,
            source_quantity=1.0, source_unit="SQ", expected_unit="SQ", state=TASK_REVIEW_REQUIRED,
            began_unmapped=True, stop_reason="ambiguous_candidates", stop_detail="d", started_at="t1", completed_at="t2",
        ),
        ExecutionTask(
            task_id="t_failed", line_item_id="line_failed", source_order=2, area_name=None, section_name="Roof",
            description="d", category=None, selector=None, lookup_strategy=LOOKUP_STRATEGY_TEST_DESCRIPTION_FIRST,
            source_quantity=1.0, source_unit="SQ", expected_unit="SQ", state=TASK_FAILED,
            began_unmapped=True, stop_reason="no_results", error="boom", started_at="t1", completed_at="t2",
        ),
        ExecutionTask(
            task_id="t_pending", line_item_id="line_pending", source_order=3, area_name=None, section_name="Roof",
            description="d", category=None, selector=None, lookup_strategy=LOOKUP_STRATEGY_TEST_DESCRIPTION_FIRST,
            source_quantity=1.0, source_unit="SQ", expected_unit="SQ", state=TASK_PENDING, began_unmapped=True,
        ),
    ]
    from estimate_extractor.xactimate_lookup.execution_plan import GroupExecutionState
    plan.groups = [GroupExecutionState(
        group_id="Roof", area_name=None, section_name="Roof", xactimate_group_name="Roof",
        group_name_reviewed=True, task_ids=["t_completed", "t_review", "t_failed", "t_pending"],
    )]
    return plan


def test_resetting_unfinished_test_tasks_does_not_reset_completed_tasks(tmp_path):
    """Requirement 10: Reset unfinished TEST execution resets REVIEW_
    REQUIRED/FAILED tasks back to pending, leaves an already-PENDING
    task alone, and -- critically -- never touches a TASK_COMPLETED
    task unless full_reset=True is explicitly requested."""
    plan = _plan_with_mixed_states()
    project_dir = tmp_path / "proj"
    project_dir.mkdir()

    reset_count = reset_unfinished_tasks(plan, project_dir, full_reset=False)

    assert reset_count == 2  # t_review and t_failed; t_pending was already pending
    assert plan.task_by_id("t_completed").state == TASK_COMPLETED
    assert plan.task_by_id("t_completed").observed_quantity == 1.0  # untouched
    assert plan.task_by_id("t_review").state == TASK_PENDING
    assert plan.task_by_id("t_review").stop_reason is None
    assert plan.task_by_id("t_failed").state == TASK_PENDING
    assert plan.task_by_id("t_failed").error is None
    assert plan.task_by_id("t_pending").state == TASK_PENDING

    reloaded = load_execution_plan(project_dir)
    assert reloaded.task_by_id("t_completed").state == TASK_COMPLETED  # persisted correctly


def test_full_reset_also_resets_completed_tasks_when_explicitly_requested(tmp_path):
    plan = _plan_with_mixed_states()
    project_dir = tmp_path / "proj"
    project_dir.mkdir()

    reset_count = reset_unfinished_tasks(plan, project_dir, full_reset=True)

    assert reset_count == 3  # completed, review, failed -- not the already-pending one
    assert plan.task_by_id("t_completed").state == TASK_PENDING
    assert plan.task_by_id("t_completed").observed_quantity is None


# ---------------------------------------------------------------------
# Phase 5.9 (live-caught): a task can carry state == TASK_REVIEW_REQUIRED
# while a real row genuinely committed to Xactimate -- reset_unfinished_
# tasks() must not treat that as "unfinished, safe to retry", or the
# next Execute duplicates a real row. See task_has_committed_row().
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "trust_state,commit_state,state,expected",
    [
        (None, None, TASK_COMPLETED, True),  # state alone is sufficient
        ("QUANTITY_MISMATCH", None, TASK_REVIEW_REQUIRED, True),  # legacy: inferred from trust_state
        ("VERIFIED", None, TASK_REVIEW_REQUIRED, True),
        ("VERIFICATION_FAILED", None, TASK_REVIEW_REQUIRED, False),  # delta stayed 0 -- nothing landed
        (None, None, TASK_REVIEW_REQUIRED, False),  # never even reached commit
        (None, TASK_COMMIT_STATE_COMMITTED, TASK_REVIEW_REQUIRED, True),  # explicit field wins
        ("VERIFIED", TASK_COMMIT_STATE_NOT_COMMITTED, TASK_REVIEW_REQUIRED, False),  # explicit field wins over trust_state
    ],
)
def test_task_has_committed_row(trust_state, commit_state, state, expected):
    task = ExecutionTask(
        task_id="t", line_item_id="line_1", source_order=0, area_name=None, section_name="Roof",
        description="d", category=None, selector=None, lookup_strategy=LOOKUP_STRATEGY_TEST_DESCRIPTION_FIRST,
        source_quantity=1.0, source_unit="SQ", expected_unit="SQ",
        state=state, trust_state=trust_state, commit_state=commit_state,
    )
    assert task_has_committed_row(task) is expected


def test_commit_state_from_trust_state():
    assert commit_state_from_trust_state("VERIFIED") == TASK_COMMIT_STATE_COMMITTED
    assert commit_state_from_trust_state("QUANTITY_MISMATCH") == TASK_COMMIT_STATE_COMMITTED
    assert commit_state_from_trust_state("VERIFICATION_FAILED") == TASK_COMMIT_STATE_NOT_COMMITTED
    assert commit_state_from_trust_state(None) == TASK_COMMIT_STATE_COMMITTED


def test_reset_unfinished_tasks_preserves_a_committed_but_unverified_row(tmp_path):
    """The exact live incident: a task genuinely committed (trust_state
    QUANTITY_MISMATCH -- a real row landed, just with a quantity OCR
    couldn't confirm) but state == TASK_REVIEW_REQUIRED. Reset unfinished
    must leave it alone, same as a TASK_COMPLETED task."""
    plan = _plan_with_mixed_states()
    committed_review = ExecutionTask(
        task_id="t_committed_review", line_item_id="line_committed_review", source_order=4,
        area_name=None, section_name="Roof", description="d", category=None, selector=None,
        lookup_strategy=LOOKUP_STRATEGY_TEST_DESCRIPTION_FIRST, source_quantity=1.0, source_unit="SQ",
        expected_unit="SQ", state=TASK_REVIEW_REQUIRED, began_unmapped=True,
        trust_state="QUANTITY_MISMATCH", started_at="t1", completed_at="t2",
    )
    plan.tasks.append(committed_review)
    plan.groups[0].task_ids.append(committed_review.task_id)
    project_dir = tmp_path / "proj"
    project_dir.mkdir()

    reset_count = reset_unfinished_tasks(plan, project_dir, full_reset=False)

    assert reset_count == 2  # t_review and t_failed only -- NOT t_committed_review
    assert plan.task_by_id("t_committed_review").state == TASK_REVIEW_REQUIRED
    assert plan.task_by_id("t_committed_review").trust_state == "QUANTITY_MISMATCH"


def test_reset_unfinished_tasks_full_reset_still_resets_committed_but_unverified_rows(tmp_path):
    """full_reset=True is the explicit, deliberate "start completely
    over" escape hatch -- it must still be able to reset a committed-
    but-unverified row, exactly like a TASK_COMPLETED one."""
    plan = _plan_with_mixed_states()
    committed_review = ExecutionTask(
        task_id="t_committed_review", line_item_id="line_committed_review", source_order=4,
        area_name=None, section_name="Roof", description="d", category=None, selector=None,
        lookup_strategy=LOOKUP_STRATEGY_TEST_DESCRIPTION_FIRST, source_quantity=1.0, source_unit="SQ",
        expected_unit="SQ", state=TASK_REVIEW_REQUIRED, began_unmapped=True,
        trust_state="QUANTITY_MISMATCH", started_at="t1", completed_at="t2",
    )
    plan.tasks.append(committed_review)
    plan.groups[0].task_ids.append(committed_review.task_id)
    project_dir = tmp_path / "proj"
    project_dir.mkdir()

    reset_unfinished_tasks(plan, project_dir, full_reset=True)

    assert plan.task_by_id("t_committed_review").state == TASK_PENDING
    assert plan.task_by_id("t_committed_review").trust_state is None


# ---------------------------------------------------------------------
# Live-caught: reset_unfinished_tasks(full_reset=True) reset `state`
# back to PENDING but left `physical_state_uncertain` stale from the
# prior run -- run_execution_plan()'s own pre-loop/per-task resume
# guards check exactly that flag (alongside commit_state, which WAS
# already cleared) and hard-stop the whole run before task 1 ever
# starts, even on a plan that was just "genuinely started over".
# ---------------------------------------------------------------------


def test_full_reset_clears_stale_physical_state_uncertain(tmp_path):
    plan = _plan_with_mixed_states()
    stale_physical = ExecutionTask(
        task_id="t_stale_physical", line_item_id="line_stale_physical", source_order=4,
        area_name=None, section_name="Roof", description="d", category=None, selector=None,
        lookup_strategy=LOOKUP_STRATEGY_TEST_DESCRIPTION_FIRST, source_quantity=1.0, source_unit="SQ",
        expected_unit="SQ", state=TASK_REVIEW_REQUIRED, began_unmapped=True,
        physical_state_uncertain=True, stop_reason="physical_state_uncertain",
        started_at="t1", completed_at="t2",
    )
    plan.tasks.append(stale_physical)
    plan.groups[0].task_ids.append(stale_physical.task_id)
    project_dir = tmp_path / "proj"
    project_dir.mkdir()

    reset_unfinished_tasks(plan, project_dir, full_reset=True)

    task = plan.task_by_id("t_stale_physical")
    assert task.state == TASK_PENDING
    assert task.physical_state_uncertain is False
    assert task.stop_reason is None

    reloaded = load_execution_plan(project_dir)
    assert reloaded.task_by_id("t_stale_physical").physical_state_uncertain is False  # persisted correctly


def test_reset_unfinished_tasks_leaves_physical_state_uncertain_when_not_full_reset(tmp_path):
    """The distinction is intentional and scoped to full_reset=True only
    (per the fix's own scope): task_has_committed_row() already treats
    physical_state_uncertain=True as "unsafe to retry" (see its own
    docstring), so a plain "Reset unfinished" pass leaves such a task
    completely untouched -- same as any other committed-evidence task --
    rather than silently resetting it. That existing protection must
    keep working exactly as before; this fix only changes what happens
    once full_reset=True deliberately overrides it."""
    plan = _plan_with_mixed_states()
    stale_physical = ExecutionTask(
        task_id="t_stale_physical", line_item_id="line_stale_physical", source_order=4,
        area_name=None, section_name="Roof", description="d", category=None, selector=None,
        lookup_strategy=LOOKUP_STRATEGY_TEST_DESCRIPTION_FIRST, source_quantity=1.0, source_unit="SQ",
        expected_unit="SQ", state=TASK_REVIEW_REQUIRED, began_unmapped=True,
        physical_state_uncertain=True, stop_reason="physical_state_uncertain",
        started_at="t1", completed_at="t2",
    )
    plan.tasks.append(stale_physical)
    plan.groups[0].task_ids.append(stale_physical.task_id)
    project_dir = tmp_path / "proj"
    project_dir.mkdir()

    reset_count = reset_unfinished_tasks(plan, project_dir, full_reset=False)

    task = plan.task_by_id("t_stale_physical")
    assert reset_count == 2  # t_review and t_failed only -- NOT t_stale_physical
    assert task.state == TASK_REVIEW_REQUIRED  # left completely untouched, unchanged from before this fix
    assert task.physical_state_uncertain is True


# ---------------------------------------------------------------------
# Live-caught (second edge of the same bug): the fix above only cleared
# physical_state_uncertain for a task TRANSITIONING into TASK_PENDING.
# A task already sitting in TASK_PENDING (e.g. because an earlier,
# pre-fix full_reset() call already flipped its state but left the flag
# stale, or any other prior process left it pending with stale fields)
# hit the function's own "already pending -> no-op" early-continue and
# was never sanitized at all, even under full_reset=True. Proven live:
# task_line_0002 persisted as exactly {state: pending, physical_state_
# uncertain: true} and re-triggered the pre-loop hard stop on the very
# next full-reset "start over" attempt.
# ---------------------------------------------------------------------


def test_full_reset_clears_stale_physical_state_uncertain_on_already_pending_task(tmp_path):
    """The exact persisted shape live-caught in task_line_0002: state is
    ALREADY pending (no transition happens), but physical_state_
    uncertain is stale True from an earlier interrupted process."""
    plan = _plan_with_mixed_states()
    already_pending_but_stale = ExecutionTask(
        task_id="t_already_pending_stale", line_item_id="line_already_pending_stale", source_order=4,
        area_name=None, section_name="Roof", description="d", category=None, selector=None,
        lookup_strategy=LOOKUP_STRATEGY_TEST_DESCRIPTION_FIRST, source_quantity=1.0, source_unit="SQ",
        expected_unit="SQ", state=TASK_PENDING, began_unmapped=True,
        physical_state_uncertain=True,
    )
    plan.tasks.append(already_pending_but_stale)
    plan.groups[0].task_ids.append(already_pending_but_stale.task_id)
    project_dir = tmp_path / "proj"
    project_dir.mkdir()

    reset_unfinished_tasks(plan, project_dir, full_reset=True)

    task = plan.task_by_id("t_already_pending_stale")
    assert task.state == TASK_PENDING
    assert task.physical_state_uncertain is False

    reloaded = load_execution_plan(project_dir)
    assert reloaded.task_by_id("t_already_pending_stale").physical_state_uncertain is False


def test_full_reset_gives_already_pending_task_identical_cleanup_to_a_transitioning_one(tmp_path):
    """An already-pending task must end up in EXACTLY the same clean
    state as a non-pending task does after the same full_reset=True
    call -- not just physical_state_uncertain, every execution-derived
    field a full reset is supposed to wipe."""
    plan = ExecutionPlan(plan_id="p1", project_slug="s", source_filename=None, created_at="now")

    def _dirty_task(task_id, source_order, state):
        return ExecutionTask(
            task_id=task_id, line_item_id=f"line_{task_id}", source_order=source_order,
            area_name=None, section_name="Roof", description="d", category=None, selector=None,
            lookup_strategy=LOOKUP_STRATEGY_TEST_DESCRIPTION_FIRST, source_quantity=1.0, source_unit="SQ",
            expected_unit="SQ", state=state, began_unmapped=True,
            physical_state_uncertain=True, trust_state="QUANTITY_MISMATCH",
            commit_state=TASK_COMMIT_STATE_COMMITTED, stop_reason="physical_state_uncertain",
            stop_detail="d", error="e", actual_lookup_strategy="description_search",
            lookup_strategy_reason="r", started_at="t1", completed_at="t2", recovery_outcome="recovered",
            observed_category="RFG", observed_selector="240", observed_description="od",
            observed_activity="+", observed_quantity=9.0, observed_unit="SQ", entered_quantity=9.0,
        )

    already_pending = _dirty_task("t_already_pending", 0, TASK_PENDING)
    transitioning = _dirty_task("t_transitioning", 1, TASK_REVIEW_REQUIRED)
    plan.tasks = [already_pending, transitioning]
    from estimate_extractor.xactimate_lookup.execution_plan import GroupExecutionState
    plan.groups = [GroupExecutionState(
        group_id="Roof", area_name=None, section_name="Roof", xactimate_group_name="Roof",
        group_name_reviewed=True, task_ids=["t_already_pending", "t_transitioning"],
    )]
    project_dir = tmp_path / "proj"
    project_dir.mkdir()

    reset_unfinished_tasks(plan, project_dir, full_reset=True)

    fields_to_compare = (
        "state", "physical_state_uncertain", "trust_state", "commit_state", "stop_reason", "stop_detail",
        "error", "actual_lookup_strategy", "lookup_strategy_reason", "started_at", "completed_at",
        "recovery_outcome", "observed_category", "observed_selector", "observed_description",
        "observed_activity", "observed_quantity", "observed_unit", "entered_quantity",
    )
    already_pending_task = plan.task_by_id("t_already_pending")
    transitioning_task = plan.task_by_id("t_transitioning")
    for name in fields_to_compare:
        assert getattr(already_pending_task, name) == getattr(transitioning_task, name), (
            f"{name} differs: already-pending={getattr(already_pending_task, name)!r} vs "
            f"transitioning={getattr(transitioning_task, name)!r}"
        )
    assert already_pending_task.state == TASK_PENDING
    assert already_pending_task.physical_state_uncertain is False


def test_reset_unfinished_tasks_leaves_already_pending_stale_task_untouched_when_not_full_reset(tmp_path):
    """Non-full-reset semantics for THIS exact already-pending-plus-
    stale-flag shape must remain unchanged: task_has_committed_row()
    already treats physical_state_uncertain=True as unsafe-to-retry, so
    even without the "already pending" early-continue, this task would
    still be left alone by the first guard clause. Confirms the
    refactor didn't disturb that."""
    plan = _plan_with_mixed_states()
    already_pending_but_stale = ExecutionTask(
        task_id="t_already_pending_stale", line_item_id="line_already_pending_stale", source_order=4,
        area_name=None, section_name="Roof", description="d", category=None, selector=None,
        lookup_strategy=LOOKUP_STRATEGY_TEST_DESCRIPTION_FIRST, source_quantity=1.0, source_unit="SQ",
        expected_unit="SQ", state=TASK_PENDING, began_unmapped=True,
        physical_state_uncertain=True,
    )
    plan.tasks.append(already_pending_but_stale)
    plan.groups[0].task_ids.append(already_pending_but_stale.task_id)
    project_dir = tmp_path / "proj"
    project_dir.mkdir()

    reset_count = reset_unfinished_tasks(plan, project_dir, full_reset=False)

    task = plan.task_by_id("t_already_pending_stale")
    assert reset_count == 2  # t_review and t_failed only, exactly as before this fix
    assert task.state == TASK_PENDING
    assert task.physical_state_uncertain is True  # untouched -- full_reset=True is required to clear this


# ---------------------------------------------------------------------
# Phase 5.9: save_execution_plan() refuses to silently shrink a plan.
# ---------------------------------------------------------------------


def test_save_execution_plan_refuses_to_shrink_an_existing_plan(tmp_path):
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    big_plan = ExecutionPlan(plan_id="real", project_slug="s", source_filename=None, created_at="now")
    big_plan.tasks = [
        ExecutionTask(
            task_id=f"t{i}", line_item_id=f"line_{i}", source_order=i, area_name=None, section_name="Roof",
            description="d", category=None, selector=None, lookup_strategy=LOOKUP_STRATEGY_TEST_DESCRIPTION_FIRST,
            source_quantity=1.0, source_unit="SQ", expected_unit="SQ",
        )
        for i in range(5)
    ]
    save_execution_plan(big_plan, project_dir)

    small_plan = ExecutionPlan(plan_id="throwaway_diagnostic", project_slug="s", source_filename=None, created_at="now")
    small_plan.tasks = [big_plan.tasks[0]]

    with pytest.raises(ExecutionPlanOverwriteRefused):
        save_execution_plan(small_plan, project_dir)

    # Refused -- the real 5-task plan must still be on disk, untouched.
    reloaded = load_execution_plan(project_dir)
    assert len(reloaded.tasks) == 5


def test_save_execution_plan_allows_shrink_when_explicitly_requested(tmp_path):
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    big_plan = ExecutionPlan(plan_id="real", project_slug="s", source_filename=None, created_at="now")
    big_plan.tasks = [
        ExecutionTask(
            task_id=f"t{i}", line_item_id=f"line_{i}", source_order=i, area_name=None, section_name="Roof",
            description="d", category=None, selector=None, lookup_strategy=LOOKUP_STRATEGY_TEST_DESCRIPTION_FIRST,
            source_quantity=1.0, source_unit="SQ", expected_unit="SQ",
        )
        for i in range(5)
    ]
    save_execution_plan(big_plan, project_dir)

    small_plan = ExecutionPlan(plan_id="deliberate_rebuild", project_slug="s", source_filename=None, created_at="now")
    small_plan.tasks = [big_plan.tasks[0]]

    save_execution_plan(small_plan, project_dir, allow_shrink=True)  # must not raise

    reloaded = load_execution_plan(project_dir)
    assert len(reloaded.tasks) == 1


def test_save_execution_plan_never_refuses_when_growing_or_same_size(tmp_path):
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    plan = ExecutionPlan(plan_id="p", project_slug="s", source_filename=None, created_at="now")
    plan.tasks = [
        ExecutionTask(
            task_id="t0", line_item_id="line_0", source_order=0, area_name=None, section_name="Roof",
            description="d", category=None, selector=None, lookup_strategy=LOOKUP_STRATEGY_TEST_DESCRIPTION_FIRST,
            source_quantity=1.0, source_unit="SQ", expected_unit="SQ",
        )
    ]
    save_execution_plan(plan, project_dir)
    save_execution_plan(plan, project_dir)  # same size -- must not raise

    plan.tasks.append(ExecutionTask(
        task_id="t1", line_item_id="line_1", source_order=1, area_name=None, section_name="Roof",
        description="d", category=None, selector=None, lookup_strategy=LOOKUP_STRATEGY_TEST_DESCRIPTION_FIRST,
        source_quantity=1.0, source_unit="SQ", expected_unit="SQ",
    ))
    save_execution_plan(plan, project_dir)  # growing -- must not raise


# ---------------------------------------------------------------------
# Phase 5.24: restricted execution plans -- an intentionally restricted
# plan (e.g. a validation subset built via build_execution_plan(...,
# line_item_ids=[...])) must checkpoint/resume independently of, and
# never overwrite/shrink/corrupt, the canonical project-wide plan.
# ---------------------------------------------------------------------


def _five_task_plan(plan_id="real"):
    plan = ExecutionPlan(plan_id=plan_id, project_slug="s", source_filename=None, created_at="now")
    plan.tasks = [
        ExecutionTask(
            task_id=f"t{i}", line_item_id=f"line_{i}", source_order=i, area_name=None, section_name="Roof",
            description="d", category=None, selector=None, lookup_strategy=LOOKUP_STRATEGY_TEST_DESCRIPTION_FIRST,
            source_quantity=1.0, source_unit="SQ", expected_unit="SQ",
        )
        for i in range(5)
    ]
    return plan


def test_restricted_plan_path_is_deterministic_and_distinct_from_canonical(tmp_path):
    project_dir = tmp_path / "proj"
    path_a = restricted_plan_path(project_dir, "validation-run")
    path_b = restricted_plan_path(project_dir, "validation-run")
    path_other = restricted_plan_path(project_dir, "other-run")

    assert path_a == path_b  # same name -> same path, every time (resumable)
    assert path_a != path_other
    from estimate_extractor.xactimate_lookup.execution_plan import _plan_path
    assert path_a != _plan_path(project_dir)
    assert path_a.parent != project_dir / "execution"


def test_restricted_plan_save_cannot_alter_or_shrink_the_canonical_plan(tmp_path):
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    canonical = _five_task_plan("canonical")
    save_execution_plan(canonical, project_dir)

    restricted = ExecutionPlan(plan_id="restricted", project_slug="s", source_filename=None, created_at="now")
    restricted.tasks = [canonical.tasks[0]]  # a genuine 1-task SUBSET
    path = restricted_plan_path(project_dir, "six-task-validation")

    # Must succeed without allow_shrink=True and without ever touching
    # the canonical plan's own path -- these are two DIFFERENT files.
    save_execution_plan(restricted, project_dir, plan_path=path)

    canonical_reloaded = load_execution_plan(project_dir)
    assert len(canonical_reloaded.tasks) == 5
    assert canonical_reloaded.plan_id == "canonical"

    restricted_reloaded = load_execution_plan(project_dir, plan_path=path)
    assert len(restricted_reloaded.tasks) == 1
    assert restricted_reloaded.plan_id == "restricted"


def test_restricted_plan_state_can_checkpoint_and_resume(tmp_path):
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    path = restricted_plan_path(project_dir, "resume-check")
    restricted = ExecutionPlan(plan_id="restricted", project_slug="s", source_filename=None, created_at="now")
    restricted.tasks = [
        ExecutionTask(
            task_id="t0", line_item_id="line_0", source_order=0, area_name=None, section_name="Roof",
            description="d", category=None, selector=None, lookup_strategy=LOOKUP_STRATEGY_TEST_DESCRIPTION_FIRST,
            source_quantity=1.0, source_unit="SQ", expected_unit="SQ",
        ),
    ]
    save_execution_plan(restricted, project_dir, plan_path=path)

    reloaded = load_execution_plan(project_dir, plan_path=path)
    assert reloaded is not None
    assert reloaded.plan_id == "restricted"
    reloaded.tasks[0].state = TASK_COMPLETED
    save_execution_plan(reloaded, project_dir, plan_path=path)

    resumed = load_execution_plan(project_dir, plan_path=path)
    assert resumed.tasks[0].state == TASK_COMPLETED

    # No canonical plan was ever created by any of this.
    assert load_execution_plan(project_dir) is None


def test_restricted_plan_own_shrink_protection_still_applies(tmp_path):
    """The overwrite-shrink guard generalizes to whatever plan lives at
    the target path -- a restricted plan is just as protected against
    an accidental shrink as the canonical plan is."""
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    path = restricted_plan_path(project_dir, "shrink-check")
    big = ExecutionPlan(plan_id="big_restricted", project_slug="s", source_filename=None, created_at="now")
    big.tasks = [
        ExecutionTask(
            task_id=f"t{i}", line_item_id=f"line_{i}", source_order=i, area_name=None, section_name="Roof",
            description="d", category=None, selector=None, lookup_strategy=LOOKUP_STRATEGY_TEST_DESCRIPTION_FIRST,
            source_quantity=1.0, source_unit="SQ", expected_unit="SQ",
        )
        for i in range(3)
    ]
    save_execution_plan(big, project_dir, plan_path=path)

    small = ExecutionPlan(plan_id="smaller_restricted", project_slug="s", source_filename=None, created_at="now")
    small.tasks = [big.tasks[0]]

    with pytest.raises(ExecutionPlanOverwriteRefused):
        save_execution_plan(small, project_dir, plan_path=path)


def test_restricted_reports_dir_is_distinct_from_canonical_reports(tmp_path):
    project_dir = tmp_path / "proj"
    reports_dir = restricted_reports_dir(project_dir, "validation-run")
    assert reports_dir != project_dir / "execution" / "reports"
    assert "validation-run" in reports_dir.name or "validation-run" in str(reports_dir)


def test_reset_unfinished_tasks_respects_plan_path(tmp_path):
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    path = restricted_plan_path(project_dir, "reset-check")
    restricted = ExecutionPlan(plan_id="restricted", project_slug="s", source_filename=None, created_at="now")
    restricted.tasks = [
        ExecutionTask(
            task_id="t0", line_item_id="line_0", source_order=0, area_name=None, section_name="Roof",
            description="d", category=None, selector=None, lookup_strategy=LOOKUP_STRATEGY_TEST_DESCRIPTION_FIRST,
            source_quantity=1.0, source_unit="SQ", expected_unit="SQ", state=TASK_REVIEW_REQUIRED,
        ),
    ]
    save_execution_plan(restricted, project_dir, plan_path=path)

    reset_count = reset_unfinished_tasks(restricted, project_dir, plan_path=path)

    assert reset_count == 1
    assert restricted.tasks[0].state == TASK_PENDING
    reloaded = load_execution_plan(project_dir, plan_path=path)
    assert reloaded.tasks[0].state == TASK_PENDING
    assert load_execution_plan(project_dir) is None  # canonical untouched/never created


def test_diagnose_run_reports_exact_counts_and_stop_reason(tmp_path):
    """Requirement 9 (backend half): diagnose_run() reports exact
    completed/review_required/no_match/failed/skipped/not_attempted
    counts and a non-guessed stop reason string."""
    plan = _plan_with_mixed_states()
    plan.groups[0].state = "completed"

    diagnostics = diagnose_run(plan)

    assert diagnostics.completed == 1
    assert diagnostics.review_required == 1
    assert diagnostics.no_match == 1  # t_failed's stop_reason is "no_results"
    assert diagnostics.failed == 0
    assert diagnostics.not_attempted == 1
    assert diagnostics.remaining_unattempted == 1
    assert diagnostics.stopped_after_row == "Row 3"  # t_failed, source_order=2 -> "Row 3"
    assert diagnostics.stop_reason_summary  # non-empty, never fabricated beyond what's known


# ---------------------------------------------------------------------
# Phase 5.23 (R&R Stage 1-2): CoordinatedPair integration through the
# real build_execution_plan() -> save/load -> reset pipeline, using
# actual project files (not the isolated fake-task unit tests in
# test_coordinated_pairs.py). Reuses _write_flexible_project()'s new
# optional action/trade/component/material overrides.
# ---------------------------------------------------------------------

_PAIR_TEST_ENTRIES = [
    {"line_item_id": "line_remove", "area_name": "Dwelling", "section_name": "Dwelling Roof",
     "description": "Remove 3 tab shingles", "quantity": 30.19, "unit": "SQ",
     "action": "remove", "trade": "roofing", "component": "composition_shingles", "material": "3-tab"},
    {"line_item_id": "line_replace", "area_name": "Dwelling", "section_name": "Dwelling Roof",
     "description": "3 tab shingles", "quantity": 33.33, "unit": "SQ",
     "action": "unknown", "trade": "roofing", "component": "composition_shingles", "material": "3-tab"},
    {"line_item_id": "line_ordinary", "area_name": "Dwelling", "section_name": "Dwelling Roof",
     "description": "Drip edge", "quantity": 100.0, "unit": "LF",
     "action": "unknown", "trade": "roofing", "component": "drip_edge", "material": None},
]


def _build_pair_plan(tmp_path, entries=_PAIR_TEST_ENTRIES):
    project_dir = _write_flexible_project(tmp_path, "pair-proj", entries)
    return build_execution_plan(project_dir, "pair-proj", include_unmapped_rows=True, xactimate_project_name="TEST"), project_dir


def test_build_execution_plan_detects_and_persists_a_coordinated_pair(tmp_path):
    plan, _ = _build_pair_plan(tmp_path)

    assert len(plan.coordinated_pairs) == 1
    pair = plan.coordinated_pairs[0]
    assert pair.remove_task_id == "task_line_remove"
    assert pair.replace_task_id == "task_line_replace"
    assert pair.pair_state == PAIR_UNACTIVATED
    assert pair.activation_task_id == "task_line_remove"
    assert pair.expected_minus_quantity == 30.19
    assert pair.expected_minus_unit == "SQ"
    assert pair.expected_plus_quantity == 33.33
    assert pair.expected_plus_unit == "SQ"

    remove_task = plan.task_by_id("task_line_remove")
    replace_task = plan.task_by_id("task_line_replace")
    ordinary_task = plan.task_by_id("task_line_ordinary")
    assert remove_task.coordinated_pair_id == pair.pair_id
    assert replace_task.coordinated_pair_id == pair.pair_id
    assert ordinary_task.coordinated_pair_id is None


def test_ordinary_unpaired_tasks_completely_unaffected_by_pair_detection(tmp_path):
    """A project with no remove/install pairing shape at all must
    produce zero coordinated pairs and leave every task's
    coordinated_pair_id at its default None."""
    entries = [e for e in _PAIR_TEST_ENTRIES if e["line_item_id"] != "line_remove"]
    plan, _ = _build_pair_plan(tmp_path, entries)
    assert plan.coordinated_pairs == []
    assert all(t.coordinated_pair_id is None for t in plan.tasks)


def test_coordinated_pair_survives_save_and_load(tmp_path):
    plan, project_dir = _build_pair_plan(tmp_path)
    pair = plan.coordinated_pairs[0]
    pair.pair_state = PAIR_ACTIVATED_PENDING_BINDING
    pair.minus_binding = {"category": "RFG", "selector": "240", "activity": "-"}
    pair.plus_binding = {"category": "RFG", "selector": "240", "activity": "+"}
    save_execution_plan(plan, project_dir)

    reloaded = load_execution_plan(project_dir)
    assert len(reloaded.coordinated_pairs) == 1
    reloaded_pair = reloaded.coordinated_pairs[0]
    assert reloaded_pair.pair_id == pair.pair_id
    assert reloaded_pair.pair_state == PAIR_ACTIVATED_PENDING_BINDING
    assert reloaded_pair.minus_binding == {"category": "RFG", "selector": "240", "activity": "-"}
    assert reloaded_pair.plus_binding == {"category": "RFG", "selector": "240", "activity": "+"}
    assert reloaded.task_by_id("task_line_remove").coordinated_pair_id == pair.pair_id


def test_execution_plan_from_dict_without_coordinated_pairs_key_loads_cleanly():
    """Backward compatibility: a plan persisted before this field
    existed has no "coordinated_pairs" key in its JSON at all."""
    data = {
        "plan_id": "p1", "project_slug": "old-project", "source_filename": None,
        "created_at": "2020-01-01T00:00:00+00:00", "groups": [], "tasks": [],
    }
    plan = ExecutionPlan.from_dict(data)
    assert plan.coordinated_pairs == []


def test_full_reset_clears_coordinated_pair_state_and_both_members(tmp_path):
    plan, project_dir = _build_pair_plan(tmp_path)
    pair = plan.coordinated_pairs[0]
    pair.pair_state = PAIR_ACTIVATED_PENDING_BINDING
    pair.activation_task_id = "task_line_remove"
    pair.minus_binding = {"category": "RFG", "selector": "240", "activity": "-"}
    pair.plus_binding = {"category": "RFG", "selector": "240", "activity": "+"}
    pair.minus_written = True
    pair.review_reason = "stale"
    pair.uncertainty_reason = "stale"
    remove_task = plan.task_by_id("task_line_remove")
    replace_task = plan.task_by_id("task_line_replace")
    remove_task.state = TASK_REVIEW_REQUIRED
    replace_task.state = TASK_REVIEW_REQUIRED
    save_execution_plan(plan, project_dir)

    reset_count = reset_unfinished_tasks(plan, project_dir, full_reset=True)

    assert reset_count >= 2
    pair = plan.pair_by_id(pair.pair_id)
    assert pair.pair_state == PAIR_UNACTIVATED
    assert pair.activation_task_id is None
    assert pair.minus_binding is None
    assert pair.plus_binding is None
    assert pair.minus_written is False
    assert pair.review_reason is None
    assert pair.uncertainty_reason is None
    assert plan.task_by_id("task_line_remove").state == TASK_PENDING
    assert plan.task_by_id("task_line_replace").state == TASK_PENDING


def test_partial_reset_protects_both_members_of_an_activated_pair(tmp_path):
    """An activated-but-incomplete pair (real physical activity already
    plausibly happened) must be COMPLETELY protected from an ordinary
    partial reset -- even though the individual task carries no commit_
    state of its own yet (Stage 3 doesn't exist to set one). Only
    full_reset=True may clear it -- see pair_has_physical_activity()."""
    plan, project_dir = _build_pair_plan(tmp_path)
    pair = plan.coordinated_pairs[0]
    pair.pair_state = PAIR_ACTIVATED_PENDING_BINDING
    remove_task = plan.task_by_id("task_line_remove")
    remove_task.state = TASK_REVIEW_REQUIRED
    remove_task.stop_detail = "some non-commit reason"
    save_execution_plan(plan, project_dir)

    reset_unfinished_tasks(plan, project_dir, full_reset=False)

    pair = plan.pair_by_id(pair.pair_id)
    assert pair.pair_state == PAIR_ACTIVATED_PENDING_BINDING  # untouched
    assert plan.task_by_id("task_line_remove").state == TASK_REVIEW_REQUIRED  # untouched
    assert plan.task_by_id("task_line_remove").stop_detail == "some non-commit reason"


def test_resetting_one_paired_member_cannot_leave_the_other_stale(tmp_path):
    """The pair is still unactivated (no physical activity at all), so
    an ordinary partial reset applies -- but it must reset BOTH members
    coherently, never just the one that happened to need it."""
    plan, project_dir = _build_pair_plan(tmp_path)
    remove_task = plan.task_by_id("task_line_remove")
    replace_task = plan.task_by_id("task_line_replace")
    remove_task.state = TASK_REVIEW_REQUIRED
    remove_task.stop_reason = "some_prior_task_local_stop"
    # replace_task is left at its default TASK_PENDING.
    save_execution_plan(plan, project_dir)

    reset_unfinished_tasks(plan, project_dir, full_reset=False)

    assert plan.task_by_id("task_line_remove").state == TASK_PENDING
    assert plan.task_by_id("task_line_replace").state == TASK_PENDING
    assert plan.pair_by_id(plan.coordinated_pairs[0].pair_id).pair_state == PAIR_UNACTIVATED


def test_pair_has_physical_activity_identifies_the_activation_boundary(tmp_path):
    """The exact helper resume logic must use to know a pair already
    crossed the activation boundary and must never be re-activated."""
    from estimate_extractor.xactimate_lookup.execution_plan import pair_has_physical_activity

    plan, _ = _build_pair_plan(tmp_path)
    pair = plan.coordinated_pairs[0]
    assert pair_has_physical_activity(pair) is False  # fresh, unactivated

    pair.pair_state = PAIR_ACTIVATED_PENDING_BINDING
    assert pair_has_physical_activity(pair) is True
