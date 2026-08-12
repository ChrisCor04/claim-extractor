"""Unit tests for the execution report writers (Phase 5.0 "Required
reports")."""

from __future__ import annotations

import csv
import json

from estimate_extractor.xactimate_lookup.execution_plan import (
    ExecutionPlan,
    ExecutionTask,
    GroupExecutionState,
    LOOKUP_STRATEGY_REVIEW_APPROVED,
    TASK_COMPLETED,
    TASK_FAILED,
    TASK_REVIEW_REQUIRED,
    TASK_SKIPPED,
)
from estimate_extractor.xactimate_lookup.execution_reports import (
    build_review_queue,
    write_all_execution_reports,
    write_execution_report_csv,
    write_execution_report_json,
    write_review_queue,
    write_structured_audit,
    write_unresolved_row_summary,
)


def _task(task_id, line_item_id, section_name, source_order, state, **overrides):
    defaults = dict(
        task_id=task_id, line_item_id=line_item_id, source_order=source_order,
        area_name="Dwelling", section_name=section_name, description="A description",
        category="SFG", selector="GUTA", lookup_strategy=LOOKUP_STRATEGY_REVIEW_APPROVED,
        source_quantity=5.0, source_unit="LF", expected_unit="LF", state=state,
    )
    defaults.update(overrides)
    return ExecutionTask(**defaults)


def _plan():
    tasks = [
        _task("t1", "line_0001", "Dwelling Roof", 0, TASK_COMPLETED, trust_state="VERIFIED", observed_quantity=5.0, observed_unit="LF"),
        _task("t2", "line_0002", "Dwelling Roof", 1, TASK_REVIEW_REQUIRED, trust_state="UNIT_MISMATCH", stop_detail="unit mismatch"),
        _task("t3", "line_0003", "Fence", 2, TASK_FAILED, error="boom"),
        _task("t4", "line_0004", "Fence", 3, TASK_SKIPPED, stop_detail="reviewer excluded this row"),
    ]
    groups = [
        GroupExecutionState(group_id="Dwelling Roof", area_name="Dwelling", section_name="Dwelling Roof",
                             xactimate_group_name="Dwelling Roof", group_name_reviewed=True, task_ids=["t1", "t2"]),
        GroupExecutionState(group_id="Fence", area_name=None, section_name="Fence",
                             xactimate_group_name="Fence", group_name_reviewed=True, task_ids=["t3", "t4"]),
    ]
    return ExecutionPlan(plan_id="p1", project_slug="test-project", source_filename="test.pdf", created_at="now", groups=groups, tasks=tasks)


def test_write_execution_report_json_includes_summary_and_all_tasks(tmp_path):
    plan = _plan()
    path = tmp_path / "execution_report.json"
    write_execution_report_json(plan, path)
    data = json.loads(path.read_text(encoding="utf-8"))

    assert data["plan_id"] == "p1"
    assert len(data["tasks"]) == 4
    assert data["summary"] == {"completed": 1, "review_required_count": 1, "skipped": 1, "failed_count": 1, "total": 4}


def test_write_execution_report_csv_one_row_per_task_in_source_order(tmp_path):
    plan = _plan()
    path = tmp_path / "execution_report.csv"
    write_execution_report_csv(plan, path)
    with path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    assert len(rows) == 4
    assert [r["line_item_id"] for r in rows] == ["line_0001", "line_0002", "line_0003", "line_0004"]
    assert rows[0]["state"] == TASK_COMPLETED
    assert rows[0]["trust_state"] == "VERIFIED"
    assert rows[1]["state"] == TASK_REVIEW_REQUIRED


def test_write_unresolved_row_summary_only_lists_non_completed(tmp_path):
    plan = _plan()
    path = tmp_path / "unresolved_row_summary.json"
    write_unresolved_row_summary(plan, path)
    data = json.loads(path.read_text(encoding="utf-8"))

    assert [r["line_item_id"] for r in data["review_required"]] == ["line_0002"]
    assert [r["line_item_id"] for r in data["failed"]] == ["line_0003"]
    assert [r["line_item_id"] for r in data["skipped"]] == ["line_0004"]
    assert data["review_required"][0]["stop_detail"] == "unit mismatch"


def test_write_structured_audit_groups_tasks_by_xactimate_group(tmp_path):
    plan = _plan()
    path = tmp_path / "structured_audit.json"
    write_structured_audit(plan, path)
    data = json.loads(path.read_text(encoding="utf-8"))

    assert len(data["groups"]) == 2
    roof = next(g for g in data["groups"] if g["group_id"] == "Dwelling Roof")
    fence = next(g for g in data["groups"] if g["group_id"] == "Fence")
    assert [t["line_item_id"] for t in roof["tasks"]] == ["line_0001", "line_0002"]
    assert [t["line_item_id"] for t in fence["tasks"]] == ["line_0003", "line_0004"]


def test_write_all_execution_reports_writes_every_file(tmp_path):
    plan = _plan()
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    reports_dir = write_all_execution_reports(plan, project_dir)

    assert reports_dir == project_dir / "execution" / "reports"
    assert (reports_dir / "execution_report.json").exists()
    assert (reports_dir / "execution_report.csv").exists()
    assert (reports_dir / "unresolved_row_summary.json").exists()
    assert (reports_dir / "review_queue.json").exists()
    assert (reports_dir / "structured_audit.json").exists()


def test_review_queue_accumulates_only_committed_review_tasks(tmp_path):
    plan = _plan()
    first = plan.task_by_id("t2")
    first.selected_category = "RFG"
    first.selected_selector = "GCR300"
    first.review_reason = "quantity OCR was partial"
    first.evidence_path = "evidence/t2.png"
    second = _task(
        "t5", "line_0005", "Dwelling Roof", 4, TASK_REVIEW_REQUIRED,
        trust_state="QUANTITY_MISMATCH", commit_state="committed",
        selected_category="RFG", selected_selector="FELT15",
        review_reason="quantity OCR was blank", evidence_path="evidence/t5.png",
    )
    noncommitted = _task(
        "t6", "line_0006", "Dwelling Roof", 5, TASK_REVIEW_REQUIRED,
        trust_state=None, commit_state="not_committed", stop_detail="ambiguous before write",
    )
    plan.tasks.extend([second, noncommitted])
    plan.groups[0].task_ids.extend(["t5", "t6"])

    queue = build_review_queue(plan)
    assert [item["task_id"] for item in queue] == ["t2", "t5"]
    assert queue[0] == {
        "task_id": "t2",
        "source_description": "A description",
        "group": "Dwelling Roof",
        "selected_category": "RFG",
        "selected_selector": "GCR300",
        "expected_quantity": 5.0,
        "expected_unit": "LF",
        "observed_quantity": None,
        "observed_unit": None,
        "review_reason": "quantity OCR was partial",
        "trust_state": "UNIT_MISMATCH",
        "evidence_path": "evidence/t2.png",
    }

    path = tmp_path / "review_queue.json"
    write_review_queue(plan, path)
    assert [item["task_id"] for item in json.loads(path.read_text(encoding="utf-8"))["items"]] == ["t2", "t5"]
