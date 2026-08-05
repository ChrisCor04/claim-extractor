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
    ExecutionTask,
    GROUP_PENDING,
    LOOKUP_STRATEGY_REVIEW_APPROVED,
    TASK_COMPLETED,
    TASK_FAILED,
    TASK_PENDING,
    TASK_REVIEW_REQUIRED,
    TASK_SKIPPED,
    build_execution_plan,
    load_execution_plan,
    save_execution_plan,
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
