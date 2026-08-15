"""App-facing boundary for the experimental grouped fast executor."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .fast_group_executor import (
    ExecutableGroupPlan, WindowsGroupBatchUI, compile_executable_group_plan,
    execute_group_first_plan, load_executable_group_plan,
)
from .shadow_quick_entry_plan import build_shadow_plan, render_shadow_report


@dataclass(frozen=True, slots=True)
class PreparedFastGroupedRun:
    shadow_plan: dict[str, Any]
    executable_plan: ExecutableGroupPlan
    json_path: Path
    report_path: Path
    planning_seconds: float


def prepare_fast_grouped_run(project_dir: Path) -> PreparedFastGroupedRun:
    """Map once offline, validate every payload, and persist the review artifact."""
    started = time.perf_counter()
    shadow = build_shadow_plan(project_dir)
    executable = compile_executable_group_plan(shadow)
    if len(shadow["items"]) != sum(len(group.items) for group in executable.groups):
        raise RuntimeError("fast grouped plan refused: not every source line has one executable payload")
    output = project_dir / "execution" / "fast_grouped"
    output.mkdir(parents=True, exist_ok=True)
    json_path = output / "shadow_quick_entry_plan.json"
    report_path = output / "shadow_quick_entry_plan.md"
    json_path.write_text(json.dumps(shadow, indent=2), encoding="utf-8")
    report_path.write_text(render_shadow_report(shadow), encoding="utf-8")
    return PreparedFastGroupedRun(
        shadow, executable, json_path, report_path, time.perf_counter() - started,
    )


def review_rows(shadow: dict[str, Any]) -> list[dict[str, Any]]:
    return [{
        "group": item["group"], "source_description": item["original_description"],
        "CAT": item["execution_category"], "SEL": item["execution_selector"],
        "quantity": item["quantity"], "unit": item["unit"],
        "resolution": item["resolution"], "action": item["source_action"],
        "BIDITM": (item["execution_category"], item["execution_selector"]) == ("DOR", "BIDITM"),
        "source_pricing": item["source_pricing"],
    } for item in shadow["items"]]


def execute_saved_fast_grouped_run(
    plan_path: Path, project_name: str, evidence_dir: Path,
    *, ui_factory: Callable[[str, Path], Any] = WindowsGroupBatchUI,
) -> dict[str, Any]:
    """Live boundary: JSON payload + UI only; no mapper or catalog input."""
    plan = load_executable_group_plan(plan_path)
    if plan.project != project_name:
        plan = ExecutableGroupPlan(project_name, plan.groups, plan.source_schema_version)
    ui = ui_factory(project_name, evidence_dir)
    report = execute_group_first_plan(plan, ui)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    report_path = evidence_dir / "fast_grouped_execution_report.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    report["report_path"] = str(report_path)
    return report
