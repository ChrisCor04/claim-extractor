"""Writers for the execution report JSON, execution report CSV, structured
audit, and unresolved-row summary (Phase 5.0 "Required reports").
Mirrors the established writer pattern used everywhere else in this
codebase (mapping/outputs.py, estimate_extractor/output/*): pure functions
over an already-built object, one function per artifact, plus one
"write everything" convenience function. Every report is a snapshot of the
persisted ExecutionPlan (execution_plan.py) -- the plan itself, saved after
every single task by execution_runner.py, is the durable source of truth;
these are human/audit-friendly views over it, not a second store."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from estimate_extractor.xactimate_lookup.execution_plan import (
    ExecutionPlan,
    ExecutionTask,
    TASK_FAILED,
    TASK_REVIEW_REQUIRED,
    TASK_SKIPPED,
)

TASK_CSV_COLUMNS = [
    "row_label",
    "task_id",
    "line_item_id",
    "source_order",
    "area_name",
    "section_name",
    "source_page",
    "description",
    "category",
    "selector",
    "lookup_strategy",
    "requested_lookup_strategy",
    "actual_lookup_strategy",
    "lookup_strategy_reason",
    "mapping_state_before_execution",
    "began_unmapped",
    "source_quantity",
    "source_unit",
    "expected_unit",
    "entered_quantity",
    "observed_quantity",
    "observed_unit",
    "observed_category",
    "observed_selector",
    "observed_description",
    "state",
    "trust_state",
    "stop_reason",
    "stop_detail",
    "evidence_path",
    "attempts",
    "started_at",
    "completed_at",
    "error",
    "recovery_outcome",
]


def _dump(data, pretty: bool = True) -> str:
    return json.dumps(data, indent=2 if pretty else None, default=str)


def _task_row(task: ExecutionTask) -> dict:
    return {
        "row_label": task.row_label,
        "task_id": task.task_id,
        "line_item_id": task.line_item_id,
        "source_order": task.source_order,
        "area_name": task.area_name,
        "section_name": task.section_name,
        "source_page": task.source_page,
        "description": task.description,
        "category": task.category,
        "selector": task.selector,
        "lookup_strategy": task.lookup_strategy,
        # Phase 5.5B: "requested" is the strategy fixed at plan-build
        # time (task.lookup_strategy itself -- never mutated); "actual"
        # and "reason" are set fresh at execution time by execution_
        # runner.py's _task_to_lookup_plan(), right before the search
        # happens. A mismatch between requested and actual (other than
        # the one legitimate case -- LOOKUP_STRATEGY_REVIEW_APPROVED
        # always maps to the trusted path) is exactly the audit trail
        # that would have caught the live "None None" CAT/SEL search
        # incident.
        "requested_lookup_strategy": task.lookup_strategy,
        "actual_lookup_strategy": task.actual_lookup_strategy,
        "lookup_strategy_reason": task.lookup_strategy_reason,
        # Phase 5.5: "unmapped" for a row that had no CAT/SEL at
        # plan-build time (see execution_plan.py's
        # include_unmapped_rows), "mapped" for the normal
        # approved-CAT/SEL path -- derived from began_unmapped rather
        # than stored twice.
        "mapping_state_before_execution": "unmapped" if task.began_unmapped else "mapped",
        "began_unmapped": task.began_unmapped,
        "source_quantity": task.source_quantity,
        "source_unit": task.source_unit,
        "expected_unit": task.expected_unit,
        "entered_quantity": task.entered_quantity,
        "observed_quantity": task.observed_quantity,
        "observed_unit": task.observed_unit,
        "observed_category": task.observed_category,
        "observed_selector": task.observed_selector,
        "observed_description": task.observed_description,
        "state": task.state,
        "trust_state": task.trust_state,
        "stop_reason": task.stop_reason,
        "stop_detail": task.stop_detail,
        "evidence_path": task.evidence_path,
        "attempts": task.attempts,
        "started_at": task.started_at,
        "completed_at": task.completed_at,
        "error": task.error,
        "recovery_outcome": task.recovery_outcome,
    }


def write_execution_report_json(plan: ExecutionPlan, path: Path, pretty: bool = True) -> None:
    """The full plan snapshot, plus the computed summary -- the single
    most complete artifact; every other report is a narrower view of the
    same data."""
    path.parent.mkdir(parents=True, exist_ok=True)
    summary = plan.summary()
    data = {
        **plan.to_dict(),
        "summary": {
            "completed": summary.completed,
            "review_required_count": len(summary.review_required_labels),
            "skipped": summary.skipped,
            "failed_count": len(summary.failed_labels),
            "total": summary.total,
        },
    }
    path.write_text(_dump(data, pretty), encoding="utf-8")


def write_execution_report_csv(plan: ExecutionPlan, path: Path) -> None:
    """One row per task, source order -- the same information as the
    JSON report, in the format a reviewer or adjuster can open directly
    in a spreadsheet."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, quoting=csv.QUOTE_MINIMAL)
        writer.writerow(TASK_CSV_COLUMNS)
        for task in plan.tasks:
            row = _task_row(task)
            writer.writerow([row[col] for col in TASK_CSV_COLUMNS])


def write_unresolved_row_summary(plan: ExecutionPlan, path: Path, pretty: bool = True) -> None:
    """Only the rows a human needs to look at (review-required, failed,
    skipped), grouped by reason -- the "Review Required: Row 6, Row 14,
    ..." shape, as a durable artifact rather than only a console print."""
    path.parent.mkdir(parents=True, exist_ok=True)
    unresolved = [t for t in plan.tasks if t.state in (TASK_REVIEW_REQUIRED, TASK_FAILED, TASK_SKIPPED)]
    data = {
        "plan_id": plan.plan_id,
        "project_slug": plan.project_slug,
        "generated_at": plan.updated_at,
        "review_required": [_task_row(t) for t in unresolved if t.state == TASK_REVIEW_REQUIRED],
        "failed": [_task_row(t) for t in unresolved if t.state == TASK_FAILED],
        "skipped": [_task_row(t) for t in unresolved if t.state == TASK_SKIPPED],
    }
    path.write_text(_dump(data, pretty), encoding="utf-8")


def write_structured_audit(plan: ExecutionPlan, path: Path, pretty: bool = True) -> None:
    """One audit entry per task, grouped by Xactimate group, in source
    order -- what was searched, what was selected, what was verified,
    and where the evidence for it lives. This is the "what happened, and
    what proves it" record for the whole run."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "plan_id": plan.plan_id,
        "project_slug": plan.project_slug,
        "source_filename": plan.source_filename,
        "created_at": plan.created_at,
        "run_state": plan.run_state,
        "groups": [
            {
                "group_id": group.group_id,
                "area_name": group.area_name,
                "section_name": group.section_name,
                "xactimate_group_name": group.xactimate_group_name,
                "parent_group_name": group.parent_group_name,
                "group_name_reviewed": group.group_name_reviewed,
                "group_state": group.state,
                "group_error": group.error,
                "tasks": [_task_row(t) for t in plan.tasks_in_group(group.group_id)],
            }
            for group in plan.groups
        ],
    }
    path.write_text(_dump(data, pretty), encoding="utf-8")


def write_all_execution_reports(plan: ExecutionPlan, project_dir: Path, pretty: bool = True) -> Path:
    """Writes every report under project_dir/execution/reports/ and
    returns that directory."""
    reports_dir = project_dir / "execution" / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    write_execution_report_json(plan, reports_dir / "execution_report.json", pretty)
    write_execution_report_csv(plan, reports_dir / "execution_report.csv")
    write_unresolved_row_summary(plan, reports_dir / "unresolved_row_summary.json", pretty)
    write_structured_audit(plan, reports_dir / "structured_audit.json", pretty)
    return reports_dir
