"""A first-class, persisted, project-level execution plan (Phase 5.0
Priority 3).

Everything upstream is reused unmodified: the canonical Area/Section model
(via the denormalized ``area_name``/``section_name`` strings that already
flow through ``mapping/models.py``'s ``OriginalItem`` and
``review_service.build_effective_rows()`` -- canonical.py's ``area_id``/
``section_id`` foreign keys do not survive past the mapping stage today,
which is a real, separately-notable limitation, not something this module
tries to fix), the review/approval state (``review_service``), and the
Xactimate group-name vocabulary (``ui/group_name_service.py``). This module
adds the piece that didn't exist: one ``ExecutionTask`` per APPROVED line
item, grouped by Section (Xactimate's real "group" granularity), in source
document order, carrying quantity/unit provenance and execution state that
survives a process restart -- see ``execution_runner.py`` for the part that
actually talks to Xactimate.

A line item reaches this plan only via ``review_service.can_approve()``,
which already guarantees category, selector, quantity, and unit are
present -- so every task here has an unambiguous, human-approved CAT/SEL to
search for directly (``LOOKUP_STRATEGY_REVIEW_APPROVED``), not a
phrase-based description search. See docs/build-estimate.md.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path

from estimate_extractor.mapping.pipeline import DEFAULT_CONFIG_DIR
from estimate_extractor.ui import group_name_service
from estimate_extractor.ui.review_service import STATUS_APPROVED, build_effective_rows

DEFAULT_GROUP_NAMES_PATH = DEFAULT_CONFIG_DIR / "xactimate_group_names.yaml"

# Task execution states (Priority 6/8) -- persisted so a run can resume
# from exactly where it left off.
TASK_PENDING = "pending"
TASK_COMPLETED = "completed"
TASK_SKIPPED = "skipped"
TASK_REVIEW_REQUIRED = "review_required"
TASK_FAILED = "failed"
VALID_TASK_STATES = frozenset({TASK_PENDING, TASK_COMPLETED, TASK_SKIPPED, TASK_REVIEW_REQUIRED, TASK_FAILED})

# Group execution states (Priority 5) -- a group must reach VERIFIED before
# any task inside it is executed; never inferred, never skipped.
GROUP_PENDING = "pending"
GROUP_SELECTED = "selected"
GROUP_VERIFIED = "verified"
GROUP_IN_PROGRESS = "in_progress"
GROUP_COMPLETED = "completed"
GROUP_FAILED = "failed"

LOOKUP_STRATEGY_REVIEW_APPROVED = "review_approved_cat_sel"
RUN_STATE_NOT_STARTED = "not_started"
RUN_STATE_IN_PROGRESS = "in_progress"
RUN_STATE_PAUSED = "paused"
RUN_STATE_COMPLETED = "completed"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ExecutionPlanError(Exception):
    pass


def _dataclass_to_dict(obj) -> dict:
    return {f.name: getattr(obj, f.name) for f in fields(obj)}


@dataclass(slots=True)
class ExecutionTask:
    """One approved line item's journey through Xactimate. Quantity and
    unit are tracked as three INDEPENDENT values, never overwritten in
    place (Priority 7): ``source_*`` is what the carrier PDF said,
    ``entered_quantity``/``expected_unit`` is what execution is told to
    type, ``observed_*`` is what ``verify_commit()`` (Phase 4.8) actually
    read back from the committed row."""

    task_id: str
    line_item_id: str
    source_order: int
    area_name: str | None
    section_name: str | None
    description: str
    category: str
    selector: str
    lookup_strategy: str
    source_quantity: float
    source_unit: str | None
    expected_unit: str | None
    source_page: int | None = None
    entered_quantity: float | None = None
    observed_quantity: float | None = None
    observed_unit: str | None = None
    state: str = TASK_PENDING
    trust_state: str | None = None
    stop_reason: str | None = None
    stop_detail: str | None = None
    evidence_path: str | None = None
    attempts: int = 0
    started_at: str | None = None
    completed_at: str | None = None
    error: str | None = None

    @property
    def row_label(self) -> str:
        return f"Row {self.source_order + 1}"

    def to_dict(self) -> dict:
        return _dataclass_to_dict(self)

    @staticmethod
    def from_dict(data: dict) -> "ExecutionTask":
        known = {f.name for f in fields(ExecutionTask)}
        return ExecutionTask(**{k: v for k, v in data.items() if k in known})


@dataclass(slots=True)
class GroupExecutionState:
    """One Section-level Xactimate group. ``group_id`` is the section
    name (or a synthesized placeholder when a task has no section) --
    canonical section_id does not survive past the mapping stage today
    (see module docstring), so the name is the real identity key
    end-to-end, matching every other layer of this codebase."""

    group_id: str
    area_name: str | None
    section_name: str | None
    xactimate_group_name: str | None
    group_name_reviewed: bool
    task_ids: list[str] = field(default_factory=list)
    state: str = GROUP_PENDING
    error: str | None = None

    def to_dict(self) -> dict:
        return _dataclass_to_dict(self)

    @staticmethod
    def from_dict(data: dict) -> "GroupExecutionState":
        known = {f.name for f in fields(GroupExecutionState)}
        return GroupExecutionState(**{k: v for k, v in data.items() if k in known})


@dataclass(slots=True)
class ExecutionSummary:
    """Matches the exact reporting shape Phase 5.0 requires: a completed
    count, the labeled list of rows a human must look at, a skipped
    count, and a failed count -- never combined into one number."""

    completed: int
    review_required_labels: list[str]
    skipped: int
    failed_labels: list[str]
    total: int

    def render_text(self) -> str:
        lines = [f"Completed: {self.completed}", ""]
        lines.append("Review Required:")
        if self.review_required_labels:
            lines.append("")
            lines.extend(self.review_required_labels)
        lines.append("")
        lines.append(f"Skipped: {self.skipped}")
        lines.append("")
        lines.append(f"Failed: {len(self.failed_labels)}")
        return "\n".join(lines)


@dataclass(slots=True)
class ExecutionPlan:
    plan_id: str
    project_slug: str
    source_filename: str | None
    created_at: str
    groups: list[GroupExecutionState] = field(default_factory=list)
    tasks: list[ExecutionTask] = field(default_factory=list)
    run_state: str = RUN_STATE_NOT_STARTED
    resume_cursor: int | None = None  # index into `tasks` (source order) of the next task to attempt
    updated_at: str = field(default_factory=utc_now_iso)

    def task_by_id(self, task_id: str) -> ExecutionTask | None:
        return next((t for t in self.tasks if t.task_id == task_id), None)

    def group_by_id(self, group_id: str) -> GroupExecutionState | None:
        return next((g for g in self.groups if g.group_id == group_id), None)

    def tasks_in_group(self, group_id: str) -> list[ExecutionTask]:
        group = self.group_by_id(group_id)
        if group is None:
            return []
        by_id = {t.task_id: t for t in self.tasks}
        return [by_id[tid] for tid in group.task_ids if tid in by_id]

    def summary(self) -> ExecutionSummary:
        completed = [t for t in self.tasks if t.state == TASK_COMPLETED]
        review = [t for t in self.tasks if t.state == TASK_REVIEW_REQUIRED]
        skipped = [t for t in self.tasks if t.state == TASK_SKIPPED]
        failed = [t for t in self.tasks if t.state == TASK_FAILED]
        return ExecutionSummary(
            completed=len(completed),
            review_required_labels=[f"{t.row_label} ({t.line_item_id}): {t.stop_reason or 'unresolved'}" for t in review],
            skipped=len(skipped),
            failed_labels=[f"{t.row_label} ({t.line_item_id}): {t.error or t.stop_reason or 'unknown error'}" for t in failed],
            total=len(self.tasks),
        )

    def to_dict(self) -> dict:
        return {
            "plan_id": self.plan_id,
            "project_slug": self.project_slug,
            "source_filename": self.source_filename,
            "created_at": self.created_at,
            "groups": [g.to_dict() for g in self.groups],
            "tasks": [t.to_dict() for t in self.tasks],
            "run_state": self.run_state,
            "resume_cursor": self.resume_cursor,
            "updated_at": self.updated_at,
        }

    @staticmethod
    def from_dict(data: dict) -> "ExecutionPlan":
        return ExecutionPlan(
            plan_id=data["plan_id"],
            project_slug=data["project_slug"],
            source_filename=data.get("source_filename"),
            created_at=data["created_at"],
            groups=[GroupExecutionState.from_dict(g) for g in data.get("groups", [])],
            tasks=[ExecutionTask.from_dict(t) for t in data.get("tasks", [])],
            run_state=data.get("run_state", RUN_STATE_NOT_STARTED),
            resume_cursor=data.get("resume_cursor"),
            updated_at=data.get("updated_at", utc_now_iso()),
        )


def _plan_path(project_dir: Path) -> Path:
    return project_dir / "execution" / "execution_plan.json"


def save_execution_plan(plan: ExecutionPlan, project_dir: Path) -> None:
    plan.updated_at = utc_now_iso()
    path = _plan_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plan.to_dict(), indent=2, default=str), encoding="utf-8")


def load_execution_plan(project_dir: Path) -> ExecutionPlan | None:
    path = _plan_path(project_dir)
    if not path.exists():
        return None
    return ExecutionPlan.from_dict(json.loads(path.read_text(encoding="utf-8")))


def _resolve_xactimate_group_name(
    section_name: str | None, overrides: dict, group_config: group_name_service.GroupNameConfig
) -> tuple[str | None, bool]:
    if section_name and section_name in overrides:
        entry = overrides[section_name]
        if entry.get("reviewed_xactimate_group_name") or entry.get("allow_custom"):
            return (entry.get("reviewed_xactimate_group_name") or section_name), True
    suggestion = group_name_service.suggest_group_name(section_name or "", group_config)
    return (suggestion.suggested_group_name or section_name), False


def build_execution_plan(
    project_dir: Path,
    project_slug: str,
    *,
    source_filename: str | None = None,
    line_item_ids: list[str] | None = None,
    group_names_path: Path = DEFAULT_GROUP_NAMES_PATH,
) -> ExecutionPlan:
    """Builds a fresh ExecutionPlan from every APPROVED, executable line
    item in the project (or the subset named in `line_item_ids`, which
    must already be approved). Never talks to Xactimate -- see
    execution_runner.py for the part that does. Raises
    ExecutionPlanError if there is nothing approved to build, or if an
    approved row is somehow missing category/selector (would indicate a
    bug in review_service.can_approve()'s gate, not a normal outcome)."""
    all_rows = build_effective_rows(project_dir)
    order_by_id = {r["line_item_id"]: i for i, r in enumerate(all_rows)}

    approved_rows = [r for r in all_rows if r["status"] == STATUS_APPROVED]
    if line_item_ids is not None:
        wanted = set(line_item_ids)
        approved_rows = [r for r in approved_rows if r["line_item_id"] in wanted]

    if not approved_rows:
        raise ExecutionPlanError("No approved, executable line items found in this project -- nothing to build.")

    group_config = group_name_service.load_group_names(group_names_path)
    overrides = group_name_service.get_group_name_overrides(project_dir)

    groups: dict[str, GroupExecutionState] = {}
    tasks: list[ExecutionTask] = []

    for row in approved_rows:
        section_name = row.get("section_name")
        area_name = row.get("area_name")
        group_id = section_name or f"__ungrouped__{area_name or 'none'}"

        category = row.get("category")
        selector = row.get("selector")
        if not category or not selector:
            raise ExecutionPlanError(
                f"{row['line_item_id']} is marked approved but is missing category/selector -- "
                f"review_service.can_approve() should have blocked this; refusing to build a task for it."
            )

        if group_id not in groups:
            xactimate_group_name, reviewed = _resolve_xactimate_group_name(section_name, overrides, group_config)
            groups[group_id] = GroupExecutionState(
                group_id=group_id,
                area_name=area_name,
                section_name=section_name,
                xactimate_group_name=xactimate_group_name,
                group_name_reviewed=reviewed,
            )

        task = ExecutionTask(
            task_id=f"task_{row['line_item_id']}",
            line_item_id=row["line_item_id"],
            source_order=order_by_id[row["line_item_id"]],
            area_name=area_name,
            section_name=section_name,
            source_page=row.get("source_page"),
            description=row.get("mapped_description") or row.get("original_description") or "",
            category=category,
            selector=selector,
            lookup_strategy=LOOKUP_STRATEGY_REVIEW_APPROVED,
            source_quantity=row.get("quantity"),
            source_unit=row.get("unit"),
            expected_unit=row.get("unit"),
        )
        tasks.append(task)
        groups[group_id].task_ids.append(task.task_id)

    tasks.sort(key=lambda t: t.source_order)

    first_order: dict[str, int] = {}
    for t in tasks:
        gid = t.section_name or f"__ungrouped__{t.area_name or 'none'}"
        first_order.setdefault(gid, t.source_order)
    ordered_groups = sorted(groups.values(), key=lambda g: first_order.get(g.group_id, 10**9))

    now = utc_now_iso()
    plan_id = f"plan_{project_slug}_{now.replace(':', '').replace('-', '').replace('.', '').replace('+', '')}"
    return ExecutionPlan(
        plan_id=plan_id,
        project_slug=project_slug,
        source_filename=source_filename,
        created_at=now,
        groups=ordered_groups,
        tasks=tasks,
    )
