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
    save_execution_plan,
    task_has_committed_row,
)


def _normalized_item(line_item_id, description, area_name, section_name, quantity=10.0, unit="SQ"):
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
            "action": "remove_and_replace", "trade": "roofing", "component": "shingles",
            "material": "laminated", "attributes": {}, "quantity": quantity, "unit_of_measure": unit,
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
    status ("approved"/"rejected"/omitted for unreviewed). Reuses
    _normalized_item()/_mapped_item() above but allows each field to
    vary per row, which the fixed-shape ITEMS/_write_project() above
    does not."""
    project_dir = tmp_path / project_name
    (project_dir / "mapping").mkdir(parents=True)
    (project_dir / "review").mkdir(parents=True)

    normalized = [
        _normalized_item(
            e["line_item_id"], e.get("description", "A real source description"),
            e.get("area_name"), e.get("section_name"),
            quantity=e.get("quantity", 10.0), unit=e.get("unit", "SQ"),
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
