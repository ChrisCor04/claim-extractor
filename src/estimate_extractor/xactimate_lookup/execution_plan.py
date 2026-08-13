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
from estimate_extractor.ui.review_service import STATUS_APPROVED, STATUS_REJECTED, build_effective_rows
from estimate_extractor.xactimate_lookup.coordinated_pairs import detect_coordinated_pairs, pair_id_for
from estimate_extractor.xactimate_lookup.models import LOOKUP_PATH_DESCRIPTION_SEARCH, LOOKUP_PATH_TRUSTED

DEFAULT_GROUP_NAMES_PATH = DEFAULT_CONFIG_DIR / "xactimate_group_names.yaml"

# Task execution states (Priority 6/8) -- persisted so a run can resume
# from exactly where it left off.
TASK_PENDING = "pending"
TASK_COMPLETED = "completed"
TASK_SKIPPED = "skipped"
TASK_REVIEW_REQUIRED = "review_required"
TASK_FAILED = "failed"
VALID_TASK_STATES = frozenset({TASK_PENDING, TASK_COMPLETED, TASK_SKIPPED, TASK_REVIEW_REQUIRED, TASK_FAILED})

# Phase 5.9: commit evidence, tracked SEPARATELY from `state` (execution
# outcome) and `trust_state` (post-commit verification confidence) --
# see ExecutionTask.commit_state's own docstring and task_has_committed_
# row() below. A live incident showed `state == TASK_REVIEW_REQUIRED`
# alone cannot answer "did a real row land in Xactimate" -- 13 of 14
# rows physically committed during a Phase 5.8A run carried
# TASK_REVIEW_REQUIRED because their post-commit OCR verification came
# back below VERIFIED confidence, not because nothing committed.
TASK_COMMIT_STATE_NOT_COMMITTED = "not_committed"
TASK_COMMIT_STATE_PHYSICAL_ITEM_CREATED_UNCONFIRMED = "physical_item_created_unconfirmed"
TASK_COMMIT_STATE_COMMITTED = "committed"

#: verify_commit()'s trust_state values that mean the row-count delta
#: stayed at 0 through the whole polling window -- i.e. commit_item()
#: was called but nothing structurally landed in the grid. The ONE
#: trust_state that is NOT evidence of a real committed row, despite
#: outcome.committed having been True at the Python level.
_TRUST_STATES_WITHOUT_A_LANDED_ROW = frozenset({"VERIFICATION_FAILED"})

# Group execution states (Priority 5) -- a group must reach VERIFIED before
# any task inside it is executed; never inferred, never skipped.
GROUP_PENDING = "pending"
GROUP_SELECTED = "selected"
GROUP_VERIFIED = "verified"
GROUP_IN_PROGRESS = "in_progress"
GROUP_COMPLETED = "completed"
GROUP_FAILED = "failed"

# Coordinated-pair states (Phase 5.23, R&R Stage 1-2). A pair only ever
# advances forward through this sequence during real Stage 3 (live)
# execution, which does not exist yet as of Stage 1-2 -- every pair
# detect_coordinated_pairs() produces starts and, in this phase, stays
# at PAIR_UNACTIVATED. The remaining states are defined now so the
# persisted schema is already correct for Stage 3 to fill in later,
# and so reset/resume semantics can be implemented and tested against
# them today (see reset_unfinished_tasks()).
PAIR_UNACTIVATED = "unactivated"
PAIR_ACTIVATED_PENDING_BINDING = "activated_pending_binding"
PAIR_BOTH_BOUND = "both_bound"
PAIR_MINUS_VERIFIED = "minus_verified"
PAIR_PLUS_VERIFIED = "plus_verified"
PAIR_BOTH_VERIFIED = "both_verified"
PAIR_SATISFIED = "satisfied"
PAIR_REVIEW_REQUIRED = "review_required"
PAIR_PHYSICAL_STATE_UNCERTAIN = "physical_state_uncertain"
VALID_PAIR_STATES = frozenset({
    PAIR_UNACTIVATED, PAIR_ACTIVATED_PENDING_BINDING, PAIR_BOTH_BOUND,
    PAIR_MINUS_VERIFIED, PAIR_PLUS_VERIFIED, PAIR_BOTH_VERIFIED,
    PAIR_SATISFIED, PAIR_REVIEW_REQUIRED, PAIR_PHYSICAL_STATE_UNCERTAIN,
})

#: Task-level stop reason for a task that belongs to a coordinated pair
#: but reached the ordinary per-task execution loop before Stage 3
#: (live coordinated activation/quantity/verification) exists to
#: safely handle it. Never a fabricated success -- see
#: run_execution_plan()'s coordinated-pair guard.
STOP_REASON_COORDINATED_PAIR_EXECUTION_NOT_IMPLEMENTED = "coordinated_pair_execution_not_implemented"

LOOKUP_STRATEGY_REVIEW_APPROVED = "review_approved_cat_sel"
#: Phase 5.5: a row with no CAT/SEL yet (missing, not rejected) but a
#: real description/quantity/unit/group, searched live via the
#: existing description-first lookup path instead of being excluded
#: from the plan entirely. Only ever produced by
#: build_execution_plan(include_unmapped_rows=True, ...), which itself
#: refuses anything but the exact Xactimate project "TEST" -- see
#: TEST_ONLY_PROJECT_NAME.
LOOKUP_STRATEGY_TEST_DESCRIPTION_FIRST = "test_description_first"
RUN_STATE_NOT_STARTED = "not_started"
RUN_STATE_IN_PROGRESS = "in_progress"
RUN_STATE_PAUSED = "paused"
RUN_STATE_COMPLETED = "completed"

#: Phase 5.5: the ONLY Xactimate project allowed to include unmapped
#: (CAT/SEL-missing) rows in an execution plan -- see
#: build_execution_plan()'s include_unmapped_rows parameter. Never
#: relaxed to a pattern/prefix match; exact string equality only.
TEST_ONLY_PROJECT_NAME = "TEST"

#: Phase 5.5D: bumped whenever a persisted ExecutionPlan's on-disk
#: shape can no longer be trusted to carry every field the CURRENT
#: execution code depends on. Every plan built before this constant
#: existed has no `schema_version` key at all and loads as 1 (see
#: ExecutionPlan.from_dict) -- always < CURRENT_SCHEMA_VERSION, never
#: silently treated as current. See is_plan_stale().
CURRENT_SCHEMA_VERSION = 2


#: Phase 5.5D Stage 8: the fixed vocabulary run_execution_plan() sets
#: on ExecutionPlan.stop_reason_category at every distinct exit point
#: -- so a caller (the UI, this module's own diagnose_run(), a test)
#: can tell exactly why the most recent run ended where it did without
#: re-deriving it from run_state/task states, and never has to guess
#: between "normal completion" and every category of hard stop.
STOP_REASON_NORMAL_COMPLETION = "normal_completion"
STOP_REASON_PROJECT_VERIFICATION_FAILURE = "project_verification_failure"
STOP_REASON_PROJECT_LEVEL_HARD_STOP = "project_level_hard_stop"
STOP_REASON_GROUP_VERIFICATION_FAILURE = "group_verification_failure"
STOP_REASON_GROUP_SETUP_BLOCKED = "group_setup_blocked"
STOP_REASON_ADAPTER_EXCEPTION = "adapter_exception"
STOP_REASON_PROTECTED_ROW_REFUSAL = "protected_row_cleanup_refusal"
STOP_REASON_TASK_LEVEL_STOPS = "task_level_safety_stops"


def is_plan_stale(plan: "ExecutionPlan") -> bool:
    """True if `plan` was built by older code than CURRENT_SCHEMA_
    VERSION expects -- see build_estimate_panel.py, which refuses to
    Execute (Preview/inspection stay available) against a stale plan
    rather than silently running current routing logic against task
    records an older build_execution_plan() produced. Rebuilding the
    plan (the existing "Build / refresh execution plan" / "Rebuild
    TEST plan" actions) always produces a current-schema plan."""
    return plan.schema_version < CURRENT_SCHEMA_VERSION


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ExecutionPlanError(Exception):
    pass


class ExecutionPlanOverwriteRefused(ExecutionPlanError):
    """Phase 5.9: raised by save_execution_plan() when it would replace
    an EXISTING persisted plan with a plan carrying materially FEWER
    tasks -- the exact live incident this guards against: a small
    throwaway diagnostic/synthetic plan (built by a scratch script)
    silently clobbering a real, larger, already-tracked project plan.
    Pass allow_shrink=True for a deliberate, intentional replace (the
    UI's "Rebuild TEST plan" / "Build one-group plan" actions do this
    explicitly -- a human chose to replace the plan, which is a
    fundamentally different situation from a script accidentally
    reusing a real project's directory)."""


def task_has_committed_row(task: "ExecutionTask") -> bool:
    """Phase 5.9: the single source of truth for "is there real
    evidence this task's row landed in Xactimate" -- callers (reset_
    unfinished_tasks(), run_execution_plan()'s pre-execution guard, the
    UI's single-row retry action) MUST use this instead of reading
    `task.state`/`task.trust_state` directly, since neither alone is
    safe: `state == TASK_REVIEW_REQUIRED` does NOT mean nothing
    committed (see ExecutionTask.commit_state's docstring), and a bare
    `trust_state is not None` check would incorrectly count
    VERIFICATION_FAILED (commit_item() was called but the row-count
    delta never moved off 0 -- nothing actually landed) as committed.

    Prefers the explicit `commit_state` field (set by every task
    executed under Phase 5.9 or later); falls back to inferring from
    `trust_state` for a plan built before that field existed, so old
    persisted plans are never silently treated as "nothing committed"
    just because they predate this phase."""
    if task.physical_state_uncertain:
        # A retry is unsafe until a human reconciles whether the prior UI
        # action left a row, duplicate, or disappearance behind.
        return True
    if task.state == TASK_COMPLETED:
        return True
    if task.commit_state == TASK_COMMIT_STATE_COMMITTED:
        return True
    if task.commit_state == TASK_COMMIT_STATE_PHYSICAL_ITEM_CREATED_UNCONFIRMED:
        return True
    if task.commit_state == TASK_COMMIT_STATE_NOT_COMMITTED:
        return False
    return task.trust_state is not None and task.trust_state not in _TRUST_STATES_WITHOUT_A_LANDED_ROW


def commit_state_from_trust_state(trust_state: str | None) -> str:
    """Phase 5.9: the ONE place that maps a verify_commit() trust_state
    to TASK_COMMIT_STATE_COMMITTED/NOT_COMMITTED -- used by execution_
    runner._apply_outcome_to_task() right after outcome.committed is
    True and a real verification result came back. `trust_state=None`
    (verification unsupported/unavailable) is treated as COMMITTED,
    not NOT_COMMITTED -- commit_item() itself succeeded and this
    function is never called for the `not outcome.committed` case, so
    the conservative assumption is a row landed, protecting it from
    automatic retry rather than risking a duplicate."""
    if trust_state in _TRUST_STATES_WITHOUT_A_LANDED_ROW:
        return TASK_COMMIT_STATE_NOT_COMMITTED
    return TASK_COMMIT_STATE_COMMITTED


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
    category: str | None
    selector: str | None
    lookup_strategy: str
    source_quantity: float
    source_unit: str | None
    expected_unit: str | None
    source_page: int | None = None
    entered_quantity: float | None = None
    observed_quantity: float | None = None
    observed_unit: str | None = None
    #: Phase 5.5: True only for a task built by
    #: build_execution_plan(include_unmapped_rows=True, ...) from a row
    #: that had no CAT/SEL at plan-build time (category/selector above
    #: are both None). Never set for the normal approved-only path.
    began_unmapped: bool = False
    #: Phase 5.5: normalization context carried through ONLY for
    #: began_unmapped tasks, so the existing description-first phrase
    #: generator and ranker (generate_search_phrase(),
    #: rank_dropdown_results()) have the same inputs they already use
    #: elsewhere in this codebase. Always None for the normal
    #: approved-CAT/SEL path -- unchanged from today's behavior.
    normalized_action: str | None = None
    normalized_trade: str | None = None
    normalized_component: str | None = None
    normalized_material: str | None = None
    #: Phase 5.23 (R&R Stage 1-2): set only when coordinated_pairs.
    #: detect_coordinated_pairs() paired this task with a complementary
    #: remove/replace partner -- see CoordinatedPair below. Once set,
    #: run_execution_plan()'s per-task loop must never run this task
    #: through the ordinary independent single-task path; see its own
    #: coordinated-pair guard. None for every ordinary, unpaired task
    #: -- completely inert/unchanged for the vast majority of tasks.
    coordinated_pair_id: str | None = None
    #: Phase 5.5: OCR-observed values read back after a successful live
    #: commit of an originally-unmapped row (see execution_runner.py's
    #: _apply_outcome_to_task()). Corroborating/informational only --
    #: never treated as human-approved, never written back over an
    #: existing reviewed CAT/SEL in review_service's own state.
    observed_category: str | None = None
    observed_selector: str | None = None
    observed_description: str | None = None
    observed_activity: str | None = None
    #: Exact candidate identity from the positively selected dropdown row.
    #: Unlike observed_* (post-commit OCR), these values come from UIA and
    #: remain the authoritative audit record when later OCR is noisy.
    selected_category: str | None = None
    selected_selector: str | None = None
    selected_description: str | None = None
    #: Phase 5.5B: audit trail of the routing decision execution_
    #: runner.py's _task_to_lookup_plan() actually made -- `lookup_
    #: strategy` above is the REQUESTED strategy, fixed at plan-build
    #: time and never mutated; `actual_lookup_strategy` (one of
    #: LOOKUP_PATH_TRUSTED/LOOKUP_PATH_DESCRIPTION_SEARCH, from
    #: models.py) and `lookup_strategy_reason` are set fresh at
    #: execution time, right before the search happens, so a
    #: began_unmapped task being silently routed to CAT/SEL is always
    #: visible in the report rather than inferred after the fact.
    actual_lookup_strategy: str | None = None
    lookup_strategy_reason: str | None = None
    state: str = TASK_PENDING
    trust_state: str | None = None
    #: Human-facing reason a physically committed task needs review.
    #: None for fully verified tasks and for tasks that never committed.
    review_reason: str | None = None
    #: True only for unexplained physical/mechanical state. OCR-only
    #: uncertainty must never set this flag.
    physical_state_uncertain: bool = False
    #: Phase 5.9: whether a real row is known to have landed in
    #: Xactimate for this task -- set explicitly by execution_runner.
    #: _apply_outcome_to_task() based on orchestrator.execute_plan()'s
    #: own commit/verification outcome, INDEPENDENT of `state`/`trust_
    #: state`. `state == TASK_REVIEW_REQUIRED` does NOT mean nothing
    #: committed -- a row can genuinely land and still carry a low-
    #: confidence trust_state (QUANTITY_MISMATCH, UNIT_MISMATCH, etc.).
    #: None on any plan built before this field existed -- see
    #: task_has_committed_row(), which every caller MUST use instead of
    #: reading this field directly, since it also handles that legacy
    #: fallback.
    commit_state: str | None = None
    stop_reason: str | None = None
    stop_detail: str | None = None
    evidence_path: str | None = None
    attempts: int = 0
    started_at: str | None = None
    completed_at: str | None = None
    error: str | None = None
    #: Set only when a task-level adapter error was hit (see
    #: execution_runner.py's per-task exception handler) -- "recovered"
    #: if adapter.recover() then completed without raising, "recovery_
    #: failed" if it also raised. None means no adapter-error recovery
    #: was ever attempted for this task.
    recovery_outcome: str | None = None
    #: Phase 5.5D Stage 7: the ordered, bounded search-attempt trail for
    #: a began_unmapped/test_description_first task -- one dict per
    #: attempt (attempt_number, search_type, search_text, result_count,
    #: top_candidate_score, decision, stop_reason, advanced_reason). See
    #: execution_runner._run_test_description_first_attempts(). Empty
    #: for any task that was never routed through that bounded sequence
    #: (e.g. a normal review_approved CAT/SEL task).
    search_attempts: list = field(default_factory=list)

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
    #: Phase 5.5B: the Xactimate group this group must be created under.
    #: None means "the live project root" (resolved at execution time
    #: by the adapter's own expected_project_name -- see
    #: WindowsXactimateAdapter.ensure_group()'s parent_group_name
    #: parameter). The current Area/Section model has no parent/child
    #: structure at all (see this module's own docstring: canonical
    #: area_id/section_id foreign keys don't survive past the mapping
    #: stage), so every execution group built by build_execution_plan()
    #: is top-level -- this field exists so that is an explicit,
    #: auditable fact in the plan, not an unstated assumption.
    parent_group_name: str | None = None
    task_ids: list[str] = field(default_factory=list)
    state: str = GROUP_PENDING
    error: str | None = None
    #: Phase 5.7: informational-only ancestry evidence from
    #: WindowsXactimateAdapter.ensure_group() -- set when the group was
    #: created successfully and is uniquely locatable/selectable by
    #: name, but landed at an unexpected nesting depth (product
    #: requirement: ancestry is no longer a blocking safety condition
    #: for this TEST workflow, see ensure_group()'s own docstring).
    #: None whenever the group already existed, was placed exactly as
    #: requested, or ancestry couldn't be confidently read either way.
    #: Never affects `state`/`error` -- purely a note for the final
    #: report and UI.
    position_warning: str | None = None

    def to_dict(self) -> dict:
        return _dataclass_to_dict(self)

    @staticmethod
    def from_dict(data: dict) -> "GroupExecutionState":
        known = {f.name for f in fields(GroupExecutionState)}
        return GroupExecutionState(**{k: v for k, v in data.items() if k in known})


@dataclass(slots=True)
class CoordinatedPair:
    """One complementary remove/replace source task pair (Phase 5.23,
    R&R Stage 1-2) -- see coordinated_pairs.detect_coordinated_pairs()
    for how remove_task_id/replace_task_id were identified.

    Stage 1-2 only ever constructs and persists these at
    PAIR_UNACTIVATED; every field past pair_state/the two task ids and
    the expected quantities exists so Stage 3 (live coordinated
    activation/quantity/verification, NOT implemented yet) has a
    correct place to record its progress without a later schema
    change. Binding/write/verification fields are deliberately
    duck-typed dicts (not a windows_adapter type), matching this
    module's existing convention of staying adapter-agnostic (see
    LookupOutcome.verification in xactimate_lookup/models.py)."""

    pair_id: str
    remove_task_id: str
    replace_task_id: str
    pair_state: str = PAIR_UNACTIVATED
    #: Source-derived, fixed at detection time -- the two INDEPENDENT
    #: quantities this pair must eventually write to the "-" and "+"
    #: physical rows respectively. Mirrors ExecutionTask's own
    #: source_quantity: never overwritten in place.
    expected_minus_quantity: float | None = None
    expected_minus_unit: str | None = None
    expected_plus_quantity: float | None = None
    expected_plus_unit: str | None = None
    #: Which task performs the single candidate-activation click. Set
    #: explicitly (by convention, remove_task_id) rather than assumed
    #: implicitly at every call site.
    activation_task_id: str | None = None
    #: Physical binding placeholders -- populated only once Stage 3
    #: activation proves a real "-"/"+" pair landed. None until then.
    minus_binding: dict | None = None
    plus_binding: dict | None = None
    minus_written: bool = False
    plus_written: bool = False
    minus_verified_ok: bool = False
    plus_verified_ok: bool = False
    #: Human-facing explanation when pair_state == PAIR_REVIEW_REQUIRED.
    review_reason: str | None = None
    #: Human-facing explanation when pair_state ==
    #: PAIR_PHYSICAL_STATE_UNCERTAIN -- mirrors ExecutionTask.stop_
    #: detail's role for physical_state_uncertain.
    uncertainty_reason: str | None = None
    #: coordinated_pairs.PairDetection.reason at detection time --
    #: audit/debugging only, never consumed by execution logic.
    detection_reason: str | None = None

    def to_dict(self) -> dict:
        return _dataclass_to_dict(self)

    @staticmethod
    def from_dict(data: dict) -> "CoordinatedPair":
        known = {f.name for f in fields(CoordinatedPair)}
        return CoordinatedPair(**{k: v for k, v in data.items() if k in known})


def pair_has_physical_activity(pair: "CoordinatedPair") -> bool:
    """True once a pair has left PAIR_UNACTIVATED -- i.e. a real
    candidate activation has plausibly already happened on the live
    grid. Mirrors task_has_committed_row()'s role at the pair level:
    the single source of truth reset_unfinished_tasks() and any future
    Stage 3 resume logic MUST use instead of comparing pair_state
    directly, so this decision only ever lives in one place."""
    return pair.pair_state != PAIR_UNACTIVATED


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
class RunDiagnostics:
    """Phase 5.5B, Objective 3: exact, non-guessed accounting of what a
    run did and why it stopped where it did -- built ONLY from the
    persisted ExecutionPlan/GroupExecutionState/ExecutionTask state
    (never a live adapter), so it is available any time, including
    right after a UI rerun that lost its own in-memory state. Where the
    persisted state genuinely doesn't distinguish between two possible
    causes, `stop_reason_summary` says so explicitly rather than
    guessing one."""

    completed: int
    review_required: int
    no_match: int
    failed: int
    skipped: int
    not_attempted: int
    total: int
    routed_by_cat_sel: int
    routed_by_description: int
    skipped_already_terminal_last_run: int
    stopped_after_row: str | None
    stop_reason_summary: str
    remaining_unattempted: int


def diagnose_run(plan: "ExecutionPlan") -> RunDiagnostics:
    completed = sum(1 for t in plan.tasks if t.state == TASK_COMPLETED)
    skipped = sum(1 for t in plan.tasks if t.state == TASK_SKIPPED)
    not_attempted = sum(1 for t in plan.tasks if t.state == TASK_PENDING)
    review_required = sum(1 for t in plan.tasks if t.state == TASK_REVIEW_REQUIRED)
    # "no_match" is not its own TASK_* state (see execution_runner.py's
    # _apply_outcome_to_task()) -- a NO_MATCH ranking decision becomes
    # TASK_FAILED with stop_reason "no_results"; split it back out here
    # so the two are never silently conflated into one "failed" count.
    no_match = sum(1 for t in plan.tasks if t.state == TASK_FAILED and t.stop_reason == "no_results")
    failed = sum(1 for t in plan.tasks if t.state == TASK_FAILED and t.stop_reason != "no_results")
    total = len(plan.tasks)

    routed_by_cat_sel = sum(1 for t in plan.tasks if t.actual_lookup_strategy == LOOKUP_PATH_TRUSTED)
    routed_by_description = sum(1 for t in plan.tasks if t.actual_lookup_strategy == LOOKUP_PATH_DESCRIPTION_SEARCH)

    attempted_or_terminal = [t for t in plan.tasks if t.state != TASK_PENDING]
    stopped_after_row = max(attempted_or_terminal, key=lambda t: t.source_order).row_label if attempted_or_terminal else None

    failed_groups = [g for g in plan.groups if g.state == GROUP_FAILED]
    if not_attempted == 0:
        reason = "Run completed -- every task reached a terminal state; nothing remains unattempted."
    elif plan.run_state == RUN_STATE_PAUSED and not failed_groups:
        reason = (
            "Project-level hard stop: Xactimate's application/project could not be re-verified before a "
            "group started (run_state=paused). See docs/build-estimate.md 'Confirm project' / resume."
        )
    elif failed_groups:
        names = "; ".join(f"{g.xactimate_group_name or g.group_id} ({g.error})" for g in failed_groups)
        reason = f"Group verification failure for: {names}."
    else:
        reason = (
            "Task-level safety stops and/or tasks not yet attempted this run -- see each task's own "
            "stop_reason/stop_detail (no single project- or group-level cause applies)."
        )

    return RunDiagnostics(
        completed=completed, review_required=review_required, no_match=no_match, failed=failed,
        skipped=skipped, not_attempted=not_attempted, total=total,
        routed_by_cat_sel=routed_by_cat_sel, routed_by_description=routed_by_description,
        skipped_already_terminal_last_run=plan.last_run_skipped_already_terminal,
        stopped_after_row=stopped_after_row, stop_reason_summary=reason,
        remaining_unattempted=not_attempted,
    )


@dataclass(slots=True)
class ExecutionPlan:
    plan_id: str
    project_slug: str
    source_filename: str | None
    created_at: str
    groups: list[GroupExecutionState] = field(default_factory=list)
    tasks: list[ExecutionTask] = field(default_factory=list)
    #: Phase 5.23 (R&R Stage 1-2): every coordinated remove/replace pair
    #: detect_coordinated_pairs() identified at plan-build time. Empty
    #: for a plan with no such pairs (the ordinary case) and for every
    #: plan persisted before this field existed -- see from_dict()'s
    #: own default.
    coordinated_pairs: list[CoordinatedPair] = field(default_factory=list)
    run_state: str = RUN_STATE_NOT_STARTED
    resume_cursor: int | None = None  # index into `tasks` (source order) of the next task to attempt
    updated_at: str = field(default_factory=utc_now_iso)
    #: Phase 5.5B: how many tasks were ALREADY terminal (not PENDING)
    #: when the most recent run_execution_plan() call began -- i.e.
    #: skipped on resume, not re-attempted. Set fresh at the start of
    #: every call (see execution_runner.py); 0 for a plan that has
    #: never been run, or whose most recent run started from scratch.
    last_run_skipped_already_terminal: int = 0
    #: Phase 5.5D: defaults to CURRENT_SCHEMA_VERSION -- any ExecutionPlan
    #: constructed directly in Python code (by build_execution_plan(),
    #: by a test, by any future caller) IS current by construction. The
    #: only place this ever reads as stale is ExecutionPlan.from_dict()
    #: loading OLD PERSISTED JSON that predates this field entirely
    #: (every plan saved before this phase) -- see is_plan_stale(),
    #: from_dict()'s own `data.get("schema_version", 1)`.
    schema_version: int = CURRENT_SCHEMA_VERSION
    #: Phase 5.5D: set by run_execution_plan() to exactly ONE fixed
    #: label explaining why the most recent run ended where it did --
    #: see execution_runner.STOP_REASON_CATEGORIES. None for a plan
    #: that has never been run.
    stop_reason_category: str | None = None

    def task_by_id(self, task_id: str) -> ExecutionTask | None:
        return next((t for t in self.tasks if t.task_id == task_id), None)

    def group_by_id(self, group_id: str) -> GroupExecutionState | None:
        return next((g for g in self.groups if g.group_id == group_id), None)

    def pair_by_id(self, pair_id: str) -> CoordinatedPair | None:
        return next((p for p in self.coordinated_pairs if p.pair_id == pair_id), None)

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
            "coordinated_pairs": [p.to_dict() for p in self.coordinated_pairs],
            "run_state": self.run_state,
            "resume_cursor": self.resume_cursor,
            "updated_at": self.updated_at,
            "last_run_skipped_already_terminal": self.last_run_skipped_already_terminal,
            "schema_version": self.schema_version,
            "stop_reason_category": self.stop_reason_category,
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
            coordinated_pairs=[CoordinatedPair.from_dict(p) for p in data.get("coordinated_pairs", [])],
            run_state=data.get("run_state", RUN_STATE_NOT_STARTED),
            resume_cursor=data.get("resume_cursor"),
            updated_at=data.get("updated_at", utc_now_iso()),
            last_run_skipped_already_terminal=data.get("last_run_skipped_already_terminal", 0),
            schema_version=data.get("schema_version", 1),
            stop_reason_category=data.get("stop_reason_category"),
        )


def _plan_path(project_dir: Path) -> Path:
    return project_dir / "execution" / "execution_plan.json"


def save_execution_plan(plan: ExecutionPlan, project_dir: Path, *, allow_shrink: bool = False) -> None:
    """Phase 5.9: refuses (raises ExecutionPlanOverwriteRefused, writes
    nothing) if a plan ALREADY exists at `project_dir`'s path and `plan`
    has FEWER tasks than it -- unless `allow_shrink=True`. Every normal
    resumable save (execution_runner.py re-saving the SAME plan object
    after each task) never shrinks the task count, so this is invisible
    to that path; only a genuinely different, smaller plan being saved
    over a larger existing one trips it. See ExecutionPlanOverwriteRefused."""
    path = _plan_path(project_dir)
    if not allow_shrink and path.exists():
        try:
            existing_task_count = len(json.loads(path.read_text(encoding="utf-8")).get("tasks", []))
        except (json.JSONDecodeError, OSError):
            existing_task_count = 0
        if len(plan.tasks) < existing_task_count:
            raise ExecutionPlanOverwriteRefused(
                f"Refusing to save a {len(plan.tasks)}-task plan (plan_id={plan.plan_id!r}) over the "
                f"existing {existing_task_count}-task plan at {path} -- pass allow_shrink=True if this is a "
                f"deliberate rebuild/replace, not an accidental overwrite."
            )
    plan.updated_at = utc_now_iso()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plan.to_dict(), indent=2, default=str), encoding="utf-8")


def load_execution_plan(project_dir: Path) -> ExecutionPlan | None:
    path = _plan_path(project_dir)
    if not path.exists():
        return None
    return ExecutionPlan.from_dict(json.loads(path.read_text(encoding="utf-8")))


def reset_unfinished_tasks(plan: ExecutionPlan, project_dir: Path, *, full_reset: bool = False) -> int:
    """Phase 5.5B, Objective 4: resets every task back to TASK_PENDING
    EXCEPT TASK_COMPLETED ones (REVIEW_REQUIRED, FAILED, SKIPPED are all
    reset; an already-PENDING task is a no-op) -- so a run that stopped
    partway through can be safely retried without re-approving, re-
    building, or losing whatever DID successfully commit. Never resets
    a completed task unless the caller explicitly passes
    `full_reset=True` (a deliberate, disposable full reset -- e.g. the
    user wants to genuinely start over, including rows that already
    committed). Also resets the group states so `_ensure_select_verify_
    group()` re-verifies each group fresh on the next run, and clears
    `resume_cursor`/`run_state` back to a re-runnable state. Returns the
    number of tasks actually reset. Persists immediately.

    Phase 5.9 (live-caught): "TASK_COMPLETED" alone was never a
    sufficient "leave it alone" test -- a task whose row genuinely
    committed but whose post-commit OCR verification came back below
    VERIFIED confidence carries `state == TASK_REVIEW_REQUIRED`, which
    the ORIGINAL version of this function reset straight back to
    PENDING. The next Execute would then re-search and re-commit it,
    producing a real DUPLICATE row in Xactimate (confirmed live: 13 of
    14 committed rows in a real run were in exactly this state). Now
    uses task_has_committed_row() -- not `state` alone -- to decide what
    "already finished, leave it alone" means, same `full_reset` escape
    hatch as before for a genuine, deliberate start-over.

    Live-caught again: an already-TASK_PENDING task used to be a
    complete no-op even under `full_reset=True`, on the theory that
    "already pending" already means "clean". That's false whenever a
    PRIOR call left it pending with stale execution-derived fields
    still attached -- in particular `physical_state_uncertain`, which
    the ORIGINAL full_reset fix above cleared only for a task
    transitioning INTO pending, never for one already sitting there.
    run_execution_plan()'s pre-loop guard checks that flag on every
    PENDING task and hard-stops the whole run before task 1 if it's
    still True, regardless of how it got there -- so a genuine full
    reset must sanitize an already-pending task exactly like any other,
    or "start completely over" silently doesn't. `reset_count` still
    only counts actual state transitions (unchanged meaning/tests) --
    an already-pending task is sanitized but not counted as "reset".

    Phase 5.23 (R&R Stage 1-2): a coordinated pair resets or stays
    protected as ONE unit, never per-member -- otherwise a partial
    reset could leave one member back at PENDING while its partner
    (and the pair's own persisted binding/write/verification progress)
    stays exactly where it was, which is precisely the "stale half"
    this mechanism exists to prevent. `pair_has_physical_activity()` is
    the single source of truth for pair-level protection (mirrors
    `task_has_committed_row()` at the pair level) -- computed once,
    BEFORE any pair's state is touched, so clearing an unprotected
    pair's own state below can never corrupt this decision for itself
    or any other pair."""
    protected_pair_ids = {
        pair.pair_id for pair in plan.coordinated_pairs
        if not full_reset and pair_has_physical_activity(pair)
    }
    for pair in plan.coordinated_pairs:
        if pair.pair_id in protected_pair_ids:
            continue
        pair.pair_state = PAIR_UNACTIVATED
        pair.activation_task_id = None
        pair.minus_binding = None
        pair.plus_binding = None
        pair.minus_written = False
        pair.plus_written = False
        pair.minus_verified_ok = False
        pair.plus_verified_ok = False
        pair.review_reason = None
        pair.uncertainty_reason = None

    reset_count = 0
    for task in plan.tasks:
        if task.coordinated_pair_id in protected_pair_ids:
            # The whole pair is protected -- leave BOTH members exactly
            # as they are, regardless of this task's own individual
            # state (which may not itself carry commit evidence yet --
            # Stage 3 doesn't exist -- the PAIR's activity is what
            # matters). Mirrors task_has_committed_row()'s single-task
            # protection, applied coherently to the pair as a unit.
            continue
        if not full_reset and (task.state == TASK_COMPLETED or task_has_committed_row(task)):
            continue
        already_pending = task.state == TASK_PENDING
        if already_pending and not full_reset:
            continue
        task.state = TASK_PENDING
        task.stop_reason = None
        task.stop_detail = None
        task.error = None
        task.trust_state = None
        task.commit_state = None
        task.actual_lookup_strategy = None
        task.lookup_strategy_reason = None
        task.started_at = None
        task.completed_at = None
        task.recovery_outcome = None
        if full_reset:
            task.observed_category = None
            task.observed_selector = None
            task.observed_description = None
            task.observed_activity = None
            task.observed_quantity = None
            task.observed_unit = None
            task.entered_quantity = None
            # Live-caught: physical_state_uncertain is the OTHER field
            # (besides commit_state, already cleared above) run_
            # execution_plan()'s pre-loop/per-task resume guards check to
            # hard-stop the whole run before touching anything -- a task
            # reset to PENDING with this still True from a PRIOR run (the
            # grid state that made it True may no longer even exist, e.g.
            # after the live estimate was manually cleared) immediately
            # re-triggers that same hard stop before task 1 ever runs,
            # even though nothing about the current physical state is
            # actually in question. A genuine full reset must clear it
            # alongside commit_state, or it isn't actually a full reset.
            task.physical_state_uncertain = False
        if not already_pending:
            reset_count += 1

    for group in plan.groups:
        group_tasks = plan.tasks_in_group(group.group_id)
        if any(t.state == TASK_PENDING for t in group_tasks):
            group.state = GROUP_PENDING
            group.error = None

    plan.run_state = RUN_STATE_NOT_STARTED if reset_count == len(plan.tasks) else RUN_STATE_IN_PROGRESS
    if any(t.state == TASK_PENDING for t in plan.tasks):
        plan.resume_cursor = None
    save_execution_plan(plan, project_dir)
    return reset_count


def _row_group_id(row: dict) -> str:
    """The same group_id formula build_execution_plan()'s main loop
    uses, factored out so restrict_to_group_id filtering (Phase 5.5C
    Stage 10) and the loop can never silently drift apart."""
    section_name = row.get("section_name")
    area_name = row.get("area_name")
    return section_name or f"__ungrouped__{area_name or 'none'}"


def _resolve_xactimate_group_name(
    section_name: str | None, overrides: dict, group_config: group_name_service.GroupNameConfig
) -> tuple[str | None, bool]:
    if section_name and section_name in overrides:
        entry = overrides[section_name]
        if entry.get("reviewed_xactimate_group_name") or entry.get("allow_custom"):
            return (entry.get("reviewed_xactimate_group_name") or section_name), True
    suggestion = group_name_service.suggest_group_name(section_name or "", group_config)
    return (suggestion.suggested_group_name or section_name), False


def _is_valid_quantity(value) -> bool:
    """Same bar `can_approve()` implicitly relies on downstream (a
    quantity Xactimate can actually be given) -- present and a real
    positive number. `bool` is excluded explicitly since it's a `int`
    subclass in Python and `True`/`False` are never a real quantity."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


@dataclass(slots=True)
class UnmappedRowEligibility:
    """Phase 5.5: classifies every row in a project into exactly one
    bucket for the TEST-only "include unmapped rows" option -- used by
    both `build_execution_plan(include_unmapped_rows=True, ...)` (to
    build the actual task list) and the Build Estimate UI (to show
    counts before anything runs), so the two never drift apart."""

    mapped: list[dict] = field(default_factory=list)
    unmapped_eligible: list[dict] = field(default_factory=list)
    blocked_missing_description: list[dict] = field(default_factory=list)
    blocked_missing_quantity: list[dict] = field(default_factory=list)
    blocked_missing_unit: list[dict] = field(default_factory=list)
    blocked_unresolved_group: list[dict] = field(default_factory=list)

    def counts(self) -> dict:
        return {
            "mapped": len(self.mapped),
            "unmapped_eligible": len(self.unmapped_eligible),
            "blocked_missing_description": len(self.blocked_missing_description),
            "blocked_missing_quantity": len(self.blocked_missing_quantity),
            "blocked_missing_unit": len(self.blocked_missing_unit),
            "blocked_unresolved_group": len(self.blocked_unresolved_group),
        }


def classify_unmapped_rows(
    project_dir: Path, *, group_names_path: Path = DEFAULT_GROUP_NAMES_PATH
) -> UnmappedRowEligibility:
    """Read-only preview of what `build_execution_plan(include_unmapped_
    rows=True, ...)` would do -- never talks to Xactimate, never changes
    any row's stored review status. A row already approved with CAT/SEL
    is "mapped" (the normal path, unaffected). A rejected row is never
    included, mapped or not -- a human already said no to it. Every
    other row missing CAT/SEL is bucketed by the first reason (in this
    order) it would be blocked; a row with no blocking reason is
    `unmapped_eligible`."""
    all_rows = build_effective_rows(project_dir)
    group_config = group_name_service.load_group_names(group_names_path)
    overrides = group_name_service.get_group_name_overrides(project_dir)

    result = UnmappedRowEligibility()
    for row in all_rows:
        if row["status"] == STATUS_APPROVED and row.get("category") and row.get("selector"):
            result.mapped.append(row)
            continue
        if row["status"] == STATUS_REJECTED:
            continue
        if row.get("category") and row.get("selector"):
            # Mapped but not yet approved -- the normal approved-only
            # path already excludes it; this TEST-only option only ever
            # adds CAT/SEL-missing rows, never bypasses ordinary
            # mapping approval for rows that already have a mapping.
            continue
        if not (row.get("mapped_description") or row.get("original_description")):
            result.blocked_missing_description.append(row)
            continue
        if not _is_valid_quantity(row.get("quantity")):
            result.blocked_missing_quantity.append(row)
            continue
        if not row.get("unit"):
            result.blocked_missing_unit.append(row)
            continue
        xactimate_group_name, _reviewed = _resolve_xactimate_group_name(row.get("section_name"), overrides, group_config)
        if not xactimate_group_name:
            result.blocked_unresolved_group.append(row)
            continue
        result.unmapped_eligible.append(row)
    return result


def build_execution_plan(
    project_dir: Path,
    project_slug: str,
    *,
    source_filename: str | None = None,
    line_item_ids: list[str] | None = None,
    group_names_path: Path = DEFAULT_GROUP_NAMES_PATH,
    include_unmapped_rows: bool = False,
    xactimate_project_name: str | None = None,
    restrict_to_group_id: str | None = None,
) -> ExecutionPlan:
    """Builds a fresh ExecutionPlan from every APPROVED, executable line
    item in the project (or the subset named in `line_item_ids`, which
    must already be approved). Never talks to Xactimate -- see
    execution_runner.py for the part that does. Raises
    ExecutionPlanError if there is nothing approved to build, or if an
    approved row is somehow missing category/selector (would indicate a
    bug in review_service.can_approve()'s gate, not a normal outcome).

    Phase 5.5: when `include_unmapped_rows=True` AND
    `xactimate_project_name` is exactly `TEST_ONLY_PROJECT_NAME`, ALSO
    includes rows with no CAT/SEL yet that otherwise have a real
    description/quantity/unit/resolved group (see
    `classify_unmapped_rows()`) -- searched live via the existing
    description-first lookup path (`LOOKUP_STRATEGY_TEST_DESCRIPTION_
    FIRST`) instead of the trusted CAT/SEL path. These rows are never
    required to be approved and their stored review status is never
    touched. Any other project name (including None) raises
    ExecutionPlanError immediately -- this is a fast, cheap sanity
    check only; the authoritative safety gate is the LIVE, positively-
    verified adapter check execution_runner.py performs again before
    actually running any such task."""
    if include_unmapped_rows and xactimate_project_name != TEST_ONLY_PROJECT_NAME:
        raise ExecutionPlanError(
            f"include_unmapped_rows is only permitted for the exact Xactimate project "
            f"{TEST_ONLY_PROJECT_NAME!r} (got {xactimate_project_name!r}) -- refusing to build a plan "
            f"that includes unmapped rows for any other project."
        )

    all_rows = build_effective_rows(project_dir)
    order_by_id = {r["line_item_id"]: i for i, r in enumerate(all_rows)}

    approved_rows = [r for r in all_rows if r["status"] == STATUS_APPROVED]
    eligible_rows = list(approved_rows)
    unmapped_ids: set[str] = set()
    if include_unmapped_rows:
        eligibility = classify_unmapped_rows(project_dir, group_names_path=group_names_path)
        eligible_rows.extend(eligibility.unmapped_eligible)
        unmapped_ids = {r["line_item_id"] for r in eligibility.unmapped_eligible}

    if line_item_ids is not None:
        wanted = set(line_item_ids)
        eligible_rows = [r for r in eligible_rows if r["line_item_id"] in wanted]

    if restrict_to_group_id is not None:
        # Phase 5.5C Stage 10: the multi-group Xactimate sibling-creation
        # mechanism is not reliably solved for more than two groups per
        # session (see docs/build-estimate.md Phase 5.5C) -- the UI
        # offers a one-group-at-a-time fallback that rebuilds the plan
        # restricted to a single group's own rows, so a run never
        # attempts a second group's "New Group" creation in the same
        # session. Never silently expands beyond the requested group.
        eligible_rows = [r for r in eligible_rows if _row_group_id(r) == restrict_to_group_id]

    if not eligible_rows:
        raise ExecutionPlanError("No approved, executable line items found in this project -- nothing to build.")

    group_config = group_name_service.load_group_names(group_names_path)
    overrides = group_name_service.get_group_name_overrides(project_dir)

    groups: dict[str, GroupExecutionState] = {}
    tasks: list[ExecutionTask] = []

    for row in eligible_rows:
        section_name = row.get("section_name")
        area_name = row.get("area_name")
        group_id = _row_group_id(row)

        category = row.get("category")
        selector = row.get("selector")
        is_unmapped = row["line_item_id"] in unmapped_ids
        if not is_unmapped and (not category or not selector):
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
            # Phase 5.5D Stage 6/7 (live-caught): for a row that BEGAN
            # UNMAPPED, `description` is what the bounded search
            # sequence's "exact_description" attempt actually searches
            # with -- it must be the true raw extracted text, not
            # `mapped_description` (a machine-bucketed summary from the
            # mapping stage). Live-reproduced: two textually different
            # rows ("R&R Wood fence rail - 2\" x 4\" x 8'" and "Stain -
            # wood fence/gate") both had mapped_description == "Wood
            # fence", so both searched identically -- not "the exact
            # source description" by any reasonable reading. A normal
            # (already-mapped/approved) row is UNCHANGED: mapped_
            # description is a real, reviewed value there and stays
            # preferred.
            description=(
                (row.get("original_description") or row.get("mapped_description") or "")
                if is_unmapped
                else (row.get("mapped_description") or row.get("original_description") or "")
            ),
            category=category,
            selector=selector,
            lookup_strategy=LOOKUP_STRATEGY_TEST_DESCRIPTION_FIRST if is_unmapped else LOOKUP_STRATEGY_REVIEW_APPROVED,
            source_quantity=row.get("quantity"),
            source_unit=row.get("unit"),
            expected_unit=row.get("unit"),
            began_unmapped=is_unmapped,
            normalized_action=row.get("normalized_action") if is_unmapped else None,
            normalized_trade=row.get("normalized_trade") if is_unmapped else None,
            normalized_component=row.get("normalized_component") if is_unmapped else None,
            normalized_material=row.get("normalized_material") if is_unmapped else None,
        )
        tasks.append(task)
        groups[group_id].task_ids.append(task.task_id)

    tasks.sort(key=lambda t: t.source_order)

    first_order: dict[str, int] = {}
    for t in tasks:
        gid = t.section_name or f"__ungrouped__{t.area_name or 'none'}"
        first_order.setdefault(gid, t.source_order)
    ordered_groups = sorted(groups.values(), key=lambda g: first_order.get(g.group_id, 10**9))

    # Phase 5.23 (R&R Stage 1-2): pure, offline, read-only pairing pass
    # over the tasks just built -- never talks to Xactimate, never
    # affects which tasks/groups exist. See coordinated_pairs.py's own
    # module docstring. Naturally scoped to began_unmapped tasks only:
    # an approved (non-unmapped) task's normalized_* fields are always
    # None above, so it can never match ACTION_REMOVE or an install-
    # like action and is never considered a pairing candidate.
    coordinated_pairs: list[CoordinatedPair] = []
    tasks_by_id = {t.task_id: t for t in tasks}
    for detection in detect_coordinated_pairs(tasks):
        if not detection.paired:
            continue
        pid = pair_id_for(detection.remove_task_id, detection.replace_task_id)
        remove_task = tasks_by_id[detection.remove_task_id]
        replace_task = tasks_by_id[detection.replace_task_id]
        remove_task.coordinated_pair_id = pid
        replace_task.coordinated_pair_id = pid
        coordinated_pairs.append(CoordinatedPair(
            pair_id=pid,
            remove_task_id=detection.remove_task_id,
            replace_task_id=detection.replace_task_id,
            activation_task_id=detection.remove_task_id,
            expected_minus_quantity=remove_task.source_quantity,
            expected_minus_unit=remove_task.source_unit,
            expected_plus_quantity=replace_task.source_quantity,
            expected_plus_unit=replace_task.source_unit,
            detection_reason=detection.reason,
        ))

    now = utc_now_iso()
    plan_id = f"plan_{project_slug}_{now.replace(':', '').replace('-', '').replace('.', '').replace('+', '')}"
    return ExecutionPlan(
        plan_id=plan_id,
        project_slug=project_slug,
        source_filename=source_filename,
        created_at=now,
        groups=ordered_groups,
        tasks=tasks,
        coordinated_pairs=coordinated_pairs,
        schema_version=CURRENT_SCHEMA_VERSION,
    )
