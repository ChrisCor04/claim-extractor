"""Unit tests for the group-aware, resumable execution runner (Phase 5.0
Priorities 5/6/7/8). Uses a FakeXactimateAdapter extended with the
duck-typed group (ensure_group/select_group/verify_group) and commit-
verification (snapshot_grid_identities/verify_commit) hooks, exactly as a
real WindowsXactimateAdapter would expose them, so the runner's group and
verification logic is exercised without any real Windows/Xactimate
session."""

from __future__ import annotations

import json

import pytest

from estimate_extractor.xactimate_lookup.adapter import (
    AdapterError,
    FakeXactimateAdapter,
    PhysicalStateUncertainError,
    ProtectedCommittedRowError,
    QuantityConfirmationError,
    TaskLocalRowReconciliationError,
    UnexpectedDialogError,
)
from estimate_extractor.xactimate_lookup.execution_plan import (
    CURRENT_SCHEMA_VERSION,
    CoordinatedPair,
    ExecutionPlan,
    ExecutionTask,
    GROUP_COMPLETED,
    GROUP_FAILED,
    GROUP_PENDING,
    GROUP_VERIFIED,
    GroupExecutionState,
    LOOKUP_STRATEGY_REVIEW_APPROVED,
    LOOKUP_STRATEGY_TEST_DESCRIPTION_FIRST,
    PAIR_UNACTIVATED,
    RUN_STATE_COMPLETED,
    RUN_STATE_PAUSED,
    STOP_REASON_NORMAL_COMPLETION,
    STOP_REASON_COORDINATED_PAIR_EXECUTION_NOT_IMPLEMENTED,
    STOP_REASON_GROUP_SETUP_BLOCKED,
    STOP_REASON_PROTECTED_ROW_REFUSAL,
    STOP_REASON_PROJECT_LEVEL_HARD_STOP,
    TASK_COMPLETED,
    TASK_FAILED,
    TASK_PENDING,
    TASK_REVIEW_REQUIRED,
    TASK_COMMIT_STATE_PHYSICAL_ITEM_CREATED_UNCONFIRMED,
    TASK_SKIPPED,
    is_plan_stale,
    load_execution_plan,
    reset_unfinished_tasks,
    save_execution_plan,
)
from estimate_extractor.xactimate_lookup.execution_runner import (
    OBSERVED_MAPPING_STATE,
    SEARCH_TYPE_GENERIC_CLEANING_FALLBACK,
    SEARCH_TYPE_EXACT_DESCRIPTION,
    SEARCH_TYPE_TRUSTED_OBSERVED_CAT_SEL,
    SEARCH_TYPE_VERIFIED_SEARCH_DESCRIPTION,
    UnsafeLookupRouting,
    _description_first_search_attempts,
    _find_trusted_observed_mapping,
    _find_verified_search_description,
    _observed_mappings_path,
    _task_to_lookup_plan,
    run_execution_plan,
    skip_task,
)
from estimate_extractor.xactimate_lookup.models import (
    LOOKUP_PATH_DESCRIPTION_SEARCH,
    LOOKUP_PATH_TRUSTED,
    DropdownResult,
    STOP_REASON_EXTRACTION_FAILED,
    STOP_REASON_QUANTITY_CONFIRMATION_FAILED,
    STOP_REASON_TASK_LOCAL_ROW_RECONCILIATION,
)


class _FakeCommitVerification:
    def __init__(
        self, trust_state, quantity_observed=None, unit_observed=None,
        category_observed=None, selector_observed=None, description_observed=None,
    ):
        self.trust_state = trust_state
        self.quantity_observed = quantity_observed
        self.unit = _FakeUnitResult(unit_observed) if unit_observed else None
        self.category_observed = category_observed
        self.selector_observed = selector_observed
        self.description_observed = description_observed


class _FakeUnitResult:
    def __init__(self, observed_xactimate_unit):
        self.observed_xactimate_unit = observed_xactimate_unit


class GroupAwareFakeAdapter(FakeXactimateAdapter):
    """Adds the group + commit-verification duck-typed hooks on top of
    the existing FakeXactimateAdapter, with fully controllable behavior
    per test."""

    def __init__(self, *args, verified_groups=None, trust_state="VERIFIED", raise_on_group=None, position_warnings=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.verified_groups = verified_groups if verified_groups is not None else None  # None = verify everything
        self.trust_state = trust_state
        self.raise_on_group = raise_on_group or set()
        #: Phase 5.7: name -> GROUP_POSITION_WARNING string, simulating
        #: ensure_group() creating a group that lands at an unexpected
        #: nesting depth (ancestry is informational-only, never a
        #: reason to stop the group).
        self.position_warnings = position_warnings or {}
        self.ensure_group_calls: list[str] = []
        self.ensure_group_parent_calls: list[str | None] = []
        self.select_group_calls: list[str] = []
        self.verify_group_calls: list[str] = []
        self.snapshot_calls = 0
        self.verify_commit_calls = 0

    def ensure_group(self, name: str, *, parent_group_name: str | None = None) -> str | None:
        # Phase 5.8: recorded into the SAME shared, ordered self.log.calls
        # every task-level call already uses, so a test can assert the
        # exact interleaved order of group setup vs. task execution
        # (see test_group_boundary_ordering_last_task_before_next_group).
        self.log.record("ensure_group", name)
        if name in self.raise_on_group:
            raise AdapterError(f"Simulated failure creating group {name!r}.")
        self.ensure_group_calls.append(name)
        self.ensure_group_parent_calls.append(parent_group_name)
        return self.position_warnings.get(name)

    def select_group(self, name: str) -> None:
        self.log.record("select_group", name)
        self.select_group_calls.append(name)

    def verify_group(self, name: str) -> bool:
        self.log.record("verify_group", name)
        self.verify_group_calls.append(name)
        if self.verified_groups is None:
            return True
        return name in self.verified_groups

    def snapshot_grid_identities(self):
        self.snapshot_calls += 1
        return [("EXISTING", "ROW")]

    def verify_commit(self, before_snapshot, category, selector, expected_quantity, *, source_unit=None, expected_xactimate_unit=None, populated_unit=None):
        self.verify_commit_calls += 1
        # Defaults to "observed exactly what was selected" -- the normal,
        # self-consistent case for a real successful commit. Tests that
        # care about a specific OCR-observed value (Phase 5.5) can still
        # override via self.trust_state's sibling knobs if needed.
        return _FakeCommitVerification(
            self.trust_state, quantity_observed=expected_quantity, unit_observed=expected_xactimate_unit,
            category_observed=category, selector_observed=selector, description_observed=self._selected.description if self._selected else None,
        )


def _task(task_id, line_item_id, section_name, category, selector, source_order, qty=5.0, unit="LF"):
    return ExecutionTask(
        task_id=task_id, line_item_id=line_item_id, source_order=source_order,
        area_name=None, section_name=section_name, description=f"{category}/{selector} description",
        category=category, selector=selector, lookup_strategy=LOOKUP_STRATEGY_REVIEW_APPROVED,
        source_quantity=qty, source_unit=unit, expected_unit=unit,
    )


def _plan_two_groups():
    t1 = _task("task_1", "line_0001", "Dwelling Roof", "SFG", "GUTA", 0)
    t2 = _task("task_2", "line_0002", "Dwelling Roof", "SFG", "GUTC", 1)
    t3 = _task("task_3", "line_0003", "Fence", "FEN", "WOOD6", 2)
    g1 = GroupExecutionState(
        group_id="Dwelling Roof", area_name="Dwelling", section_name="Dwelling Roof",
        xactimate_group_name="Dwelling Roof", group_name_reviewed=True, task_ids=["task_1", "task_2"],
    )
    g2 = GroupExecutionState(
        group_id="Fence", area_name=None, section_name="Fence",
        xactimate_group_name="Fence", group_name_reviewed=True, task_ids=["task_3"],
    )
    return ExecutionPlan(
        plan_id="p1", project_slug="test", source_filename=None, created_at="now",
        groups=[g1, g2], tasks=[t1, t2, t3],
    )


def _dropdown(cat, sel):
    return DropdownResult(raw_text=f"{cat} {sel}", row_position=0, category=cat, selector=sel, description=f"{cat}/{sel}", extraction_confidence=1.0)


def _dropdown_script(*tasks):
    return {f"{t.category} {t.selector}": [_dropdown(t.category, t.selector)] for t in tasks}


# ---------------------------------------------------------------------
# Phase 5.23 (R&R Stage 1-2): the runner-side structural guard -- a
# task belonging to a coordinated pair must never fall through to the
# ordinary independent single-task path. Stage 3 (live coordinated
# execution) does not exist yet, so every such task must be explicitly,
# safely diverted -- never executed, never a fabricated success.
# ---------------------------------------------------------------------


def _plan_with_one_coordinated_pair_and_one_ordinary_task():
    remove_task = _task("task_remove", "line_remove", "Dwelling Roof", "RFG", "REM", 0)
    replace_task = _task("task_replace", "line_replace", "Dwelling Roof", "RFG", "REM", 1)
    ordinary_task = _task("task_ordinary", "line_ordinary", "Dwelling Roof", "SFG", "GUTA", 2)
    pair_id = "pair_task_remove_task_replace"
    remove_task.coordinated_pair_id = pair_id
    replace_task.coordinated_pair_id = pair_id
    group = GroupExecutionState(
        group_id="Dwelling Roof", area_name=None, section_name="Dwelling Roof",
        xactimate_group_name="Dwelling Roof", group_name_reviewed=True,
        task_ids=["task_remove", "task_replace", "task_ordinary"],
    )
    plan = ExecutionPlan(
        plan_id="p1", project_slug="test", source_filename=None, created_at="now",
        groups=[group], tasks=[remove_task, replace_task, ordinary_task],
        coordinated_pairs=[CoordinatedPair(
            pair_id=pair_id, remove_task_id="task_remove", replace_task_id="task_replace",
            pair_state=PAIR_UNACTIVATED, activation_task_id="task_remove",
            expected_minus_quantity=remove_task.source_quantity, expected_minus_unit=remove_task.source_unit,
            expected_plus_quantity=replace_task.source_quantity, expected_plus_unit=replace_task.source_unit,
        )],
    )
    return plan, remove_task, replace_task, ordinary_task


def test_coordinated_pair_members_never_reach_independent_execution(tmp_path, phrase_rules, ranking_config):
    """Phase 5.23 (R&R Stage 4): GroupAwareFakeAdapter does not
    implement the Stage 3 R&R primitives (bind_rr_pair_after_
    activation/write_and_verify_rr_pair_quantities/etc.), so a pair
    reaching a run against it must still fail closed -- never a fake
    success, never independent execution of either member -- exactly
    like the Stage 1-2 guard did, just now scoped to "this adapter
    can't do it" rather than "Stage 3 doesn't exist yet"."""
    plan, remove_task, replace_task, ordinary_task = _plan_with_one_coordinated_pair_and_one_ordinary_task()
    adapter = _adapter_with_test_project(dropdown_script=_dropdown_script(ordinary_task))

    run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False)

    for task in (remove_task, replace_task):
        assert task.state == TASK_REVIEW_REQUIRED
        assert task.stop_reason == STOP_REASON_COORDINATED_PAIR_EXECUTION_NOT_IMPLEMENTED
        assert "task_remove" in task.stop_detail or "pair_" in task.stop_detail

    # Never a fake success, and never any adapter interaction at all
    # for either paired task -- no search, no selection, no commit.
    search_calls = [c for c in adapter.log.calls if c[0] in ("search_by_description", "search_by_category_selector")]
    assert search_calls == [("search_by_category_selector", ("SFG", "GUTA"), {})]

    # The pair's own persisted state is untouched -- an adapter that
    # can't do coordinated execution never creates a physical
    # checkpoint, so a later run/reset with a capable adapter must
    # still see this pair as untouched, not artificially "protected".
    pair = plan.coordinated_pairs[0]
    assert pair.pair_state == PAIR_UNACTIVATED


def test_ordinary_task_alongside_a_coordinated_pair_executes_normally(tmp_path, phrase_rules, ranking_config):
    """The coordinated-pair guard must be completely scoped to paired
    tasks -- an unrelated ordinary task in the SAME group/run must
    execute exactly as it always has."""
    plan, remove_task, replace_task, ordinary_task = _plan_with_one_coordinated_pair_and_one_ordinary_task()
    adapter = _adapter_with_test_project(dropdown_script=_dropdown_script(ordinary_task))

    run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False)

    assert ordinary_task.state in (TASK_COMPLETED, TASK_REVIEW_REQUIRED)
    assert ordinary_task.stop_reason != STOP_REASON_COORDINATED_PAIR_EXECUTION_NOT_IMPLEMENTED
    assert ("search_by_category_selector", ("SFG", "GUTA"), {}) in adapter.log.calls


class QuantityFailureFakeAdapter(GroupAwareFakeAdapter):
    """Physically instantiates the first task, then fails its read-back."""

    def enter_quantity(self, quantity: float) -> None:
        self.log.record("enter_quantity", quantity)
        raise QuantityConfirmationError("expected 1 from SFG/GSG, observed 200 from prior SFG/GUTA")


def test_physical_unconfirmed_quantity_failure_hard_stops_entire_run(
    tmp_path, phrase_rules, ranking_config,
):
    plan = _plan_two_groups()
    adapter = QuantityFailureFakeAdapter(dropdown_script=_dropdown_script(*plan.tasks))
    adapter.supports_live_execution = True

    result = run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False)

    first, second, third = result.tasks
    assert first.commit_state == TASK_COMMIT_STATE_PHYSICAL_ITEM_CREATED_UNCONFIRMED
    assert first.stop_reason == STOP_REASON_QUANTITY_CONFIRMATION_FAILED
    assert first.stop_reason != STOP_REASON_EXTRACTION_FAILED
    assert result.run_state == RUN_STATE_PAUSED
    assert result.stop_reason_category == STOP_REASON_PROJECT_LEVEL_HARD_STOP
    assert second.state == TASK_PENDING and second.attempts == 0
    assert third.state == TASK_PENDING and third.attempts == 0
    assert adapter.ensure_group_calls == ["Dwelling Roof"]
    assert "Fence" not in adapter.select_group_calls
    search_calls = [call for call in adapter.log.calls if call[0] in ("search_by_description", "search_by_category_selector")]
    assert len(search_calls) == 1


class ItemsTabFailureOnceFakeAdapter(GroupAwareFakeAdapter):
    """Raises ItemsTabVerificationError from focus_search() on exactly
    the first call, then behaves normally -- the live-caught Search-
    subview degradation that doesn't recur on every later task."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._focus_search_calls = 0

    def focus_search(self) -> None:
        self._focus_search_calls += 1
        if self._focus_search_calls == 1:
            from estimate_extractor.xactimate_lookup.windows_adapter import ItemsTabVerificationError
            self.log.record("focus_search")
            raise ItemsTabVerificationError(
                "Items tab click did not produce a positively located Search field within the bounded readiness "
                "poll; refusing to continue."
            )
        super().focus_search()


def test_items_tab_verification_error_is_task_local_and_later_tasks_still_run(
    tmp_path, phrase_rules, ranking_config,
):
    """Requirement 7: an ItemsTabVerificationError on one task's
    focus_search() -- the bounded Search-subtab recovery having already
    failed closed inside the adapter -- must remain task-local: the
    task itself fails, but the run continues and later tasks (same
    group and the next group) are still attempted, exactly like any
    other task-level AdapterError. Not a project-level hard stop."""
    plan = _plan_two_groups()
    adapter = ItemsTabFailureOnceFakeAdapter(dropdown_script=_dropdown_script(*plan.tasks))
    adapter.supports_live_execution = True

    result = run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False)

    first, second, third = result.tasks
    assert first.state == TASK_FAILED
    assert "Items tab" in (first.error or "")
    assert result.stop_reason_category != STOP_REASON_PROJECT_LEVEL_HARD_STOP
    # Later tasks were still attempted -- same group and the next group.
    assert second.attempts >= 1
    assert third.attempts >= 1
    assert adapter.ensure_group_calls == ["Dwelling Roof", "Fence"]


def test_physical_state_uncertain_activation_stops_following_tasks(
    tmp_path, phrase_rules, ranking_config,
):
    plan = _plan_two_groups()
    adapter = GroupAwareFakeAdapter(dropdown_script=_dropdown_script(*plan.tasks))
    adapter.supports_live_execution = True

    def uncertain_activation(_before):
        raise PhysicalStateUncertainError("one unexplained physical row appeared")

    adapter.pending_item_created = uncertain_activation
    result = run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False)

    first, second, third = result.tasks
    assert first.state == TASK_REVIEW_REQUIRED
    assert first.commit_state == "not_committed"
    assert first.physical_state_uncertain is True
    assert first.stop_reason == "physical_state_uncertain"
    assert result.run_state == RUN_STATE_PAUSED
    assert result.stop_reason_category == STOP_REASON_PROJECT_LEVEL_HARD_STOP
    assert second.state == TASK_PENDING and second.attempts == 0
    assert third.state == TASK_PENDING and third.attempts == 0
    assert len([call for call in adapter.log.calls if call[0] == "select_candidate"]) == 1


def test_task_local_row_reconciliation_marks_only_that_task_and_continues(
    tmp_path, phrase_rules, ranking_config,
):
    """Policy correction: a row-count/R&R multiset delta mismatch local
    to one task's own candidate activation (e.g. 'added two physical
    rows, but the logical multiset delta was not exactly one R&R -/+
    item') must NOT hard-stop the whole run -- only that task is marked
    REVIEW_REQUIRED, and the run continues with the next task in the
    same group, then the next group. Contrast with the genuinely
    project-level PhysicalStateUncertainError case immediately above,
    which still (correctly) hard-stops everything."""
    plan = _plan_two_groups()
    adapter = GroupAwareFakeAdapter(dropdown_script=_dropdown_script(*plan.tasks))
    adapter.supports_live_execution = True

    def reconciliation_uncertain(_before):
        raise TaskLocalRowReconciliationError(
            "Candidate activation added two physical rows, but the logical multiset delta was not "
            "exactly one R&R -/+ item (before=2, after=4)."
        )

    adapter.pending_item_created = reconciliation_uncertain
    result = run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False)

    first, second, third = result.tasks
    assert first.state == TASK_REVIEW_REQUIRED
    assert first.commit_state == "not_committed"
    assert first.physical_state_uncertain is False
    assert first.stop_reason == STOP_REASON_TASK_LOCAL_ROW_RECONCILIATION
    # The whole point of the fix: sibling task in the SAME group, and the
    # task in the NEXT group, were both still attempted -- neither is
    # left untouched PENDING the way a project-level hard stop leaves them.
    assert second.state == TASK_REVIEW_REQUIRED and second.attempts == 1
    assert third.state == TASK_REVIEW_REQUIRED and third.attempts == 1
    assert result.stop_reason_category != STOP_REASON_PROJECT_LEVEL_HARD_STOP
    assert len([call for call in adapter.log.calls if call[0] == "select_candidate"]) == 3


def test_resumed_physical_unconfirmed_checkpoint_stops_before_any_task_or_later_group(
    tmp_path, phrase_rules, ranking_config,
):
    plan = _plan_two_groups()
    plan.tasks[0].commit_state = TASK_COMMIT_STATE_PHYSICAL_ITEM_CREATED_UNCONFIRMED
    adapter = GroupAwareFakeAdapter(dropdown_script=_dropdown_script(*plan.tasks))
    adapter.supports_live_execution = True

    result = run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False)

    assert result.run_state == RUN_STATE_PAUSED
    assert result.stop_reason_category == STOP_REASON_PROJECT_LEVEL_HARD_STOP
    assert all(task.state == TASK_PENDING and task.attempts == 0 for task in result.tasks)
    assert not any(call[0] in ("search_by_description", "search_by_category_selector") for call in adapter.log.calls)
    assert adapter.ensure_group_calls == []
    assert "Fence" not in adapter.select_group_calls


def test_full_reset_clears_stale_physical_uncertain_so_preloop_guard_does_not_refire(
    tmp_path, phrase_rules, ranking_config,
):
    """Live-caught regression: reset_unfinished_tasks(full_reset=True)
    reset task state back to PENDING but left physical_state_uncertain
    stale from a PRIOR run -- the resumed_physical pre-loop guard above
    treats that identically to a real physical-item-created-unconfirmed
    checkpoint and hard-stops the whole run before task 1 ever runs,
    even on a plan that was just deliberately reset to start over (e.g.
    after the live estimate was manually cleared, so the grid state that
    originally set the flag no longer even exists). A genuine full reset
    must clear it, or "start over" doesn't actually start over."""
    plan = _plan_two_groups()
    plan.tasks[0].state = TASK_REVIEW_REQUIRED
    plan.tasks[0].physical_state_uncertain = True
    plan.tasks[0].stop_reason = "physical_state_uncertain"

    reset_unfinished_tasks(plan, tmp_path, full_reset=True)
    assert plan.tasks[0].state == TASK_PENDING
    assert plan.tasks[0].physical_state_uncertain is False

    adapter = GroupAwareFakeAdapter(dropdown_script=_dropdown_script(*plan.tasks))
    adapter.supports_live_execution = True

    result = run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False)

    # The pre-loop guard must NOT fire -- the run actually proceeds and
    # attempts task 0, rather than hard-stopping before touching anything.
    assert result.stop_reason_category != STOP_REASON_PROJECT_LEVEL_HARD_STOP
    assert result.tasks[0].attempts >= 1
    assert adapter.ensure_group_calls != []


def test_full_reset_sanitizes_an_already_pending_task_so_preloop_guard_does_not_refire(
    tmp_path, phrase_rules, ranking_config,
):
    """Second edge of the same live-caught bug: task_line_0002 persisted
    with state ALREADY == pending (no transition needed) but physical_
    state_uncertain still True from an earlier interrupted attempt --
    reset_unfinished_tasks()'s own "already pending -> no-op" early-exit
    used to mean full_reset=True never sanitized it either, so this
    EXACT persisted shape kept re-triggering the pre-loop hard stop on
    every subsequent "start over" attempt."""
    plan = _plan_two_groups()
    plan.tasks[0].state = TASK_PENDING  # already pending -- no state transition happens
    plan.tasks[0].physical_state_uncertain = True

    reset_unfinished_tasks(plan, tmp_path, full_reset=True)
    assert plan.tasks[0].state == TASK_PENDING
    assert plan.tasks[0].physical_state_uncertain is False

    adapter = GroupAwareFakeAdapter(dropdown_script=_dropdown_script(*plan.tasks))
    adapter.supports_live_execution = True

    result = run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False)

    assert result.stop_reason_category != STOP_REASON_PROJECT_LEVEL_HARD_STOP
    assert result.tasks[0].attempts >= 1
    assert adapter.ensure_group_calls != []


def test_happy_path_all_groups_verified_all_tasks_completed(tmp_path, phrase_rules, ranking_config):
    plan = _plan_two_groups()
    adapter = GroupAwareFakeAdapter(dropdown_script=_dropdown_script(*plan.tasks))
    adapter.supports_live_execution = True

    result = run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False)

    assert result.run_state == RUN_STATE_COMPLETED
    assert all(t.state == TASK_COMPLETED for t in result.tasks)
    assert all(g.state == GROUP_COMPLETED for g in result.groups)
    assert adapter.ensure_group_calls == ["Dwelling Roof", "Fence"]
    assert adapter.verify_commit_calls == 3

    reports_dir = tmp_path / "execution" / "reports"
    assert (reports_dir / "execution_report.json").exists()
    assert (reports_dir / "execution_report.csv").exists()
    assert (reports_dir / "unresolved_row_summary.json").exists()
    assert (reports_dir / "structured_audit.json").exists()


def test_group_boundary_ordering_last_task_before_next_group(tmp_path, phrase_rules, ranking_config):
    """Phase 5.8 Stage 6/7 regression: live-reported symptom was that a
    group's LAST task sometimes appeared skipped, as if the runner
    began setting up the next group before the current group's final
    task had actually completed. Group A: 3 tasks, Group B: 2 tasks --
    every one of group A's tasks (including A3, the last) must be
    searched, and A3 must reach a terminal state, strictly BEFORE any
    of group B's setup calls (ensure_group/select_group/verify_group)
    happen. GroupAwareFakeAdapter records group setup into the SAME
    shared, ordered call log task-level search calls already use, so
    the full interleaved order can be asserted exactly, not just
    inferred from final state."""
    tA1 = _task("A1", "line_a1", "Group A", "AAA", "SEL1", 0)
    tA2 = _task("A2", "line_a2", "Group A", "AAA", "SEL2", 1)
    tA3 = _task("A3", "line_a3", "Group A", "AAA", "SEL3", 2)
    tB1 = _task("B1", "line_b1", "Group B", "BBB", "SEL1", 3)
    tB2 = _task("B2", "line_b2", "Group B", "BBB", "SEL2", 4)
    gA = GroupExecutionState(
        group_id="Group A", area_name=None, section_name="Group A",
        xactimate_group_name="Group A", group_name_reviewed=True, task_ids=["A1", "A2", "A3"],
    )
    gB = GroupExecutionState(
        group_id="Group B", area_name=None, section_name="Group B",
        xactimate_group_name="Group B", group_name_reviewed=True, task_ids=["B1", "B2"],
    )
    plan = ExecutionPlan(
        plan_id="p1", project_slug="test", source_filename=None, created_at="now",
        groups=[gA, gB], tasks=[tA1, tA2, tA3, tB1, tB2],
    )
    adapter = GroupAwareFakeAdapter(dropdown_script=_dropdown_script(tA1, tA2, tA3, tB1, tB2))
    adapter.supports_live_execution = True

    result = run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False)

    assert all(t.state == TASK_COMPLETED for t in result.tasks)  # every task, including A3, actually ran

    trace = []
    for name, args, _kwargs in adapter.log.calls:
        if name in ("ensure_group", "select_group", "verify_group"):
            trace.append(f"{name}:{args[0]}")
        elif name == "search_by_category_selector":
            trace.append(f"search:{args[0]}{args[1]}")

    assert trace == [
        "ensure_group:Group A", "select_group:Group A", "verify_group:Group A",
        "search:AAASEL1", "search:AAASEL2", "search:AAASEL3",
        "ensure_group:Group B", "select_group:Group B", "verify_group:Group B",
        "search:BBBSEL1", "search:BBBSEL2",
    ]


def test_group_position_warning_is_recorded_but_never_blocks_the_group(tmp_path, phrase_rules, ranking_config):
    """Phase 5.7 product-requirement change: a group whose ensure_group()
    call returns a GROUP_POSITION_WARNING (created successfully, but
    landed at an unexpected nesting depth) must still select, verify,
    and execute its tasks completely normally -- ancestry is
    informational-only. The warning is recorded on the group for the
    final report/UI, but never turns into a task-level stop reason or a
    group-level failure."""
    plan = _plan_two_groups()
    adapter = GroupAwareFakeAdapter(
        dropdown_script=_dropdown_script(*plan.tasks),
        position_warnings={"Fence": "GROUP_POSITION_WARNING: 'Fence' nested under 'Dwelling Roof' instead of the root."},
    )
    adapter.supports_live_execution = True

    result = run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False)

    assert result.run_state == RUN_STATE_COMPLETED
    assert all(t.state == TASK_COMPLETED for t in result.tasks)
    fence_group = result.group_by_id("Fence")
    roof_group = result.group_by_id("Dwelling Roof")
    assert fence_group.state == GROUP_COMPLETED
    assert fence_group.position_warning is not None
    assert "GROUP_POSITION_WARNING" in fence_group.position_warning
    assert roof_group.state == GROUP_COMPLETED
    assert roof_group.position_warning is None  # no warning for the correctly-placed group
    # tasks still landed in (and only in) their own intended named group
    fence_tasks = [t for t in result.tasks if t.section_name == "Fence"]
    assert len(fence_tasks) >= 1
    assert adapter.select_group_calls.count("Fence") >= 1


def test_group_verification_failure_marks_only_that_groups_tasks_review_required(tmp_path, phrase_rules, ranking_config):
    plan = _plan_two_groups()
    # "Fence" never verifies -- "Dwelling Roof" does.
    adapter = GroupAwareFakeAdapter(dropdown_script=_dropdown_script(*plan.tasks), verified_groups={"Dwelling Roof"})
    adapter.supports_live_execution = True

    result = run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False)

    roof_tasks = [t for t in result.tasks if t.section_name == "Dwelling Roof"]
    fence_tasks = [t for t in result.tasks if t.section_name == "Fence"]
    assert all(t.state == TASK_COMPLETED for t in roof_tasks)
    assert all(t.state == TASK_REVIEW_REQUIRED for t in fence_tasks)
    assert all(t.stop_reason == STOP_REASON_GROUP_SETUP_BLOCKED for t in fence_tasks)
    assert all(t.attempts == 0 for t in fence_tasks)
    assert all(not t.search_attempts for t in fence_tasks)
    assert result.group_by_id("Fence").state == GROUP_FAILED
    assert result.group_by_id("Dwelling Roof").state == GROUP_COMPLETED
    # never silently used whatever group happened to be active -- Fence's
    # task never even reached a real search/select/commit call; only the
    # 2 Dwelling Roof tasks (which DID verify) actually committed
    commit_calls = [c for c in adapter.log.calls if c[0] == "commit_item"]
    assert len(commit_calls) == len(roof_tasks) == 2
    search_calls = [c for c in adapter.log.calls if c[0] == "search_by_category_selector"]
    assert ("FEN", "WOOD6") not in [(a[0], a[1]) for _n, a, _k in search_calls]


def test_adapter_without_group_support_marks_everything_review_required(tmp_path, phrase_rules, ranking_config):
    plan = _plan_two_groups()
    adapter = FakeXactimateAdapter(dropdown_script=_dropdown_script(*plan.tasks))  # plain Fake, no group hooks
    adapter.supports_live_execution = True

    result = run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False)

    assert all(t.state == TASK_REVIEW_REQUIRED for t in result.tasks)
    assert all(g.state == GROUP_FAILED for g in result.groups)


def test_non_verified_trust_state_is_review_required_not_completed(tmp_path, phrase_rules, ranking_config):
    plan = _plan_two_groups()
    adapter = GroupAwareFakeAdapter(dropdown_script=_dropdown_script(*plan.tasks), trust_state="UNIT_MISMATCH")
    adapter.supports_live_execution = True

    result = run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False)

    assert all(t.state == TASK_REVIEW_REQUIRED for t in result.tasks)
    assert all(t.trust_state == "UNIT_MISMATCH" for t in result.tasks)
    assert len([call for call in adapter.log.calls if call[0] == "commit_item"]) == len(result.tasks)
    queue = json.loads(
        (tmp_path / "execution" / "reports" / "review_queue.json").read_text(encoding="utf-8")
    )["items"]
    assert [item["task_id"] for item in queue] == [task.task_id for task in result.tasks]


def test_committed_without_verification_support_is_review_required_never_completed(tmp_path, phrase_rules, ranking_config):
    """An adapter that can commit but cannot verify_commit must never
    have its tasks silently marked COMPLETED -- never claim success
    without evidence."""
    plan = _plan_two_groups()

    class GroupOnlyAdapter(FakeXactimateAdapter):
        def ensure_group(self, name, *, parent_group_name=None):
            pass

        def select_group(self, name):
            pass

        def verify_group(self, name):
            return True

    adapter = GroupOnlyAdapter(dropdown_script=_dropdown_script(*plan.tasks))
    adapter.supports_live_execution = True

    result = run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False)

    assert all(t.state == TASK_REVIEW_REQUIRED for t in result.tasks)


def test_dry_run_never_mutates_task_state_or_calls_group_methods(tmp_path, phrase_rules, ranking_config):
    plan = _plan_two_groups()
    adapter = GroupAwareFakeAdapter(dropdown_script=_dropdown_script(*plan.tasks))
    adapter.supports_live_execution = True

    result = run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=True)

    assert all(t.state == TASK_PENDING for t in result.tasks)
    assert adapter.ensure_group_calls == []
    assert not (tmp_path / "execution" / "execution_plan.json").exists()


def test_task_level_exception_does_not_abort_the_whole_run(tmp_path, phrase_rules, ranking_config):
    plan = _plan_two_groups()
    adapter = GroupAwareFakeAdapter(dropdown_script=_dropdown_script(*plan.tasks))
    adapter.supports_live_execution = True

    original_select = adapter.select_candidate

    def failing_select(candidate):
        if candidate.selector == "GUTA":
            raise RuntimeError("simulated unexpected crash")
        return original_select(candidate)

    adapter.select_candidate = failing_select

    result = run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False)

    task1 = result.task_by_id("task_1")
    task2 = result.task_by_id("task_2")
    task3 = result.task_by_id("task_3")
    assert task1.state == TASK_FAILED
    assert "simulated unexpected crash" in task1.error
    assert task1.recovery_outcome == "recovered"
    assert task2.state == TASK_COMPLETED  # same group, later task, still ran
    assert task3.state == TASK_COMPLETED  # different group, unaffected


def test_task_level_exception_records_recovery_failure_when_recover_itself_raises(tmp_path, phrase_rules, ranking_config):
    plan = _plan_two_groups()
    adapter = GroupAwareFakeAdapter(dropdown_script=_dropdown_script(*plan.tasks))
    adapter.supports_live_execution = True

    def failing_select(candidate):
        raise RuntimeError("simulated unexpected crash")

    def failing_recover():
        raise RuntimeError("simulated recovery failure")

    adapter.select_candidate = failing_select
    adapter.recover = failing_recover

    result = run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False)

    task1 = result.task_by_id("task_1")
    assert task1.state == TASK_FAILED
    assert task1.recovery_outcome == "recovery_failed"


def test_application_unverified_pauses_without_touching_any_task(tmp_path, phrase_rules, ranking_config):
    plan = _plan_two_groups()
    adapter = GroupAwareFakeAdapter(dropdown_script=_dropdown_script(*plan.tasks), application_verified=False)
    adapter.supports_live_execution = True

    result = run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False)

    assert result.run_state == RUN_STATE_PAUSED
    assert all(t.state == TASK_PENDING for t in result.tasks)


def test_resume_skips_already_terminal_tasks_and_completes_the_rest(tmp_path, phrase_rules, ranking_config):
    plan = _plan_two_groups()
    # Simulate a prior partial run: task_1 already completed.
    plan.tasks[0].state = TASK_COMPLETED
    save_execution_plan(plan, tmp_path)

    reloaded = load_execution_plan(tmp_path)
    adapter = GroupAwareFakeAdapter(dropdown_script=_dropdown_script(*reloaded.tasks))
    adapter.supports_live_execution = True

    result = run_execution_plan(reloaded, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False)

    assert result.run_state == RUN_STATE_COMPLETED
    assert all(t.state == TASK_COMPLETED for t in result.tasks)
    # task_1 was never re-executed -- only task_2 and task_3 went through search/select
    select_calls = [c for c in adapter.log.calls if c[0] == "select_candidate"]
    assert len(select_calls) == 2


def test_skip_task_marks_skipped_and_persists(tmp_path, phrase_rules, ranking_config):
    plan = _plan_two_groups()
    save_execution_plan(plan, tmp_path)

    skip_task(plan, "task_2", "Reviewer decided to exclude this item from this pass.", tmp_path)

    assert plan.task_by_id("task_2").state == TASK_SKIPPED
    reloaded = load_execution_plan(tmp_path)
    assert reloaded.task_by_id("task_2").state == TASK_SKIPPED
    assert "exclude" in reloaded.task_by_id("task_2").stop_detail


# ---------------------------------------------------------------------
# Phase 5.2 Stage 6: exactly the recoverable-vs-hard-stop classification
# the build spec names. Each "recoverable" condition must route its own
# task to REVIEW_REQUIRED/FAILED and let the run continue to every
# other task/group; each "hard stop" condition must pause the WHOLE run
# and leave not-yet-reached tasks untouched.
# ---------------------------------------------------------------------


def test_no_match_is_recoverable_run_continues_to_other_tasks(tmp_path, phrase_rules, ranking_config):
    """NO_MATCH (empty dropdown for one task's CAT/SEL) must fail only
    that task -- every other task, including later ones in the SAME
    group, must still run."""
    plan = _plan_two_groups()
    script = _dropdown_script(*plan.tasks)
    del script["SFG GUTA"]  # task_1's CAT/SEL now returns no dropdown results at all
    adapter = GroupAwareFakeAdapter(dropdown_script=script)
    adapter.supports_live_execution = True

    result = run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False)

    assert result.run_state == RUN_STATE_COMPLETED
    assert result.task_by_id("task_1").state == TASK_FAILED
    assert result.task_by_id("task_1").stop_reason == "no_results"
    assert result.task_by_id("task_2").state == TASK_COMPLETED  # same group, later task -- still ran
    assert result.task_by_id("task_3").state == TASK_COMPLETED  # different group -- unaffected


def test_ambiguous_ranking_is_recoverable_run_continues_to_other_tasks(tmp_path, phrase_rules, ranking_config):
    """A REVIEW_REQUIRED ranking outcome (multiple similarly-plausible
    candidates, no clear margin -- live-reproduced in Phase 5.2 Stage 3
    with a real RFG/ARMVN search) must fail only that task."""
    plan = _plan_two_groups()
    script = _dropdown_script(*plan.tasks)
    # Two candidates for task_1's query -- ambiguous, no single clear winner.
    script["SFG GUTA"] = [
        DropdownResult(raw_text="Gutter A", row_position=0, category="SFG", selector="GUTA", description="Gutter - aluminum", extraction_confidence=1.0),
        DropdownResult(raw_text="Gutter A steel", row_position=1, category="SFG", selector="GUTAS", description="Gutter - steel", extraction_confidence=1.0),
    ]
    adapter = GroupAwareFakeAdapter(dropdown_script=script)
    adapter.supports_live_execution = True

    result = run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False)

    assert result.run_state == RUN_STATE_COMPLETED
    assert result.task_by_id("task_1").state == TASK_REVIEW_REQUIRED
    assert result.task_by_id("task_1").stop_reason == "ambiguous_candidates"
    assert result.task_by_id("task_2").state == TASK_COMPLETED
    assert result.task_by_id("task_3").state == TASK_COMPLETED


def test_missing_quantity_is_recoverable_run_continues_to_other_tasks(tmp_path, phrase_rules, ranking_config):
    """A task with no usable quantity (source_quantity<=0, and no
    entered_quantity override) must fail only that task -- never abort
    the run, never guess a quantity."""
    plan = _plan_two_groups()
    plan.tasks[0].source_quantity = 0
    adapter = GroupAwareFakeAdapter(dropdown_script=_dropdown_script(*plan.tasks))
    adapter.supports_live_execution = True

    result = run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False)

    assert result.run_state == RUN_STATE_COMPLETED
    assert result.task_by_id("task_1").state == TASK_REVIEW_REQUIRED
    assert result.task_by_id("task_1").stop_reason == "unit_or_quantity_invalid"
    assert result.task_by_id("task_2").state == TASK_COMPLETED
    assert result.task_by_id("task_3").state == TASK_COMPLETED


def test_project_lost_between_groups_is_a_hard_stop_for_the_whole_run(tmp_path, phrase_rules, ranking_config):
    """Losing the verified project identity partway through (Xactimate
    closes, wrong project becomes active, etc.) must PAUSE the entire
    run before the next group -- never proceed to execute against an
    unverified context, even though the FIRST group already succeeded."""
    plan = _plan_two_groups()
    adapter = GroupAwareFakeAdapter(dropdown_script=_dropdown_script(*plan.tasks))
    adapter.supports_live_execution = True

    call_count = {"n": 0}

    def flaky_verify_project():
        call_count["n"] += 1
        # verify_project() is called at the run's initial check, before
        # each group, AND inside orchestrator.execute_plan() for each
        # individual task -- succeed through all of "Dwelling Roof"'s
        # checks (initial + group-entry + its 2 tasks' own internal
        # checks = 4 calls), then report project identity lost from the
        # "Fence" group-entry check onward.
        return call_count["n"] <= 4

    adapter.verify_project = flaky_verify_project

    result = run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False)

    assert result.run_state == RUN_STATE_PAUSED
    assert result.task_by_id("task_1").state == TASK_COMPLETED
    assert result.task_by_id("task_2").state == TASK_COMPLETED
    # "Fence" was never reached -- the whole run stopped before it, not
    # just that one group's tasks routed to review.
    assert result.task_by_id("task_3").state == TASK_PENDING
    assert result.group_by_id("Fence").state == GROUP_PENDING


# ---------------------------------------------------------------------
# Phase 5.5: TEST-only description-first execution of rows that began
# unmapped (no CAT/SEL at plan-build time -- see execution_plan.py's
# include_unmapped_rows). The lookup/ranking/safety-stop machinery
# itself is entirely orchestrator.execute_plan(), unchanged; this
# module's own job is just: build the right kind of LookupPlan, gate on
# the live project being exactly "TEST", and record what was observed.
# ---------------------------------------------------------------------

_FELT_PHRASE = "roofing felt 15"  # what generate_search_phrase() produces for the description/context below (Phase 5.6: "15" now recognized as a size term, see phrase_generator.py's weight-unit pattern)
_FELT_FULL_DESCRIPTION = "Roofing felt - 15 lb."


def _unmapped_task(task_id, line_item_id, section_name, source_order, qty=33.66, unit="SQ"):
    return ExecutionTask(
        task_id=task_id, line_item_id=line_item_id, source_order=source_order,
        area_name="Dwelling", section_name=section_name, description="Roofing felt - 15 lb.",
        category=None, selector=None, lookup_strategy=LOOKUP_STRATEGY_TEST_DESCRIPTION_FIRST,
        source_quantity=qty, source_unit=unit, expected_unit=unit,
        began_unmapped=True, normalized_action="install", normalized_trade="roofing",
        normalized_component="roofing_felt", normalized_material="roofing felt",
    )


def _plan_mapped_and_unmapped():
    t_mapped = _task("task_mapped", "line_mapped", "Dwelling Roof", "RFG", "3TAB", 0, qty=10.0, unit="SQ")
    t_unmapped = _unmapped_task("task_unmapped", "line_unmapped", "Dwelling Roof", 1)
    g1 = GroupExecutionState(
        group_id="Dwelling Roof", area_name="Dwelling", section_name="Dwelling Roof",
        xactimate_group_name="Dwelling Roof", group_name_reviewed=True, task_ids=["task_mapped", "task_unmapped"],
    )
    return ExecutionPlan(
        plan_id="p1", project_slug="test", source_filename=None, created_at="now",
        groups=[g1], tasks=[t_mapped, t_unmapped],
    )


def _felt_dropdown():
    return DropdownResult(
        raw_text="RFG FELT15", row_position=0, category="RFG", selector="FELT15",
        description="Roofing felt - 15 lb.", extraction_confidence=1.0,
    )


def _adapter_with_test_project(**kwargs):
    adapter = GroupAwareFakeAdapter(**kwargs)
    adapter.supports_live_execution = True
    adapter.expected_project_name = "TEST"
    return adapter


def test_unmapped_task_uses_description_first_and_mapped_task_uses_cat_sel(tmp_path, phrase_rules, ranking_config):
    """Requirements 8 & 9: a task without CAT/SEL is searched by
    description -- Phase 5.5D Stage 7's bounded attempt sequence means
    the FIRST attempt is the exact source description (not the compact
    generated phrase); a task WITH CAT/SEL still goes through the
    unchanged trusted-lookup path in the SAME run, and CAT/SEL is never
    tried for the unmapped task at all."""
    plan = _plan_mapped_and_unmapped()
    script = {
        "RFG 3TAB": [_dropdown("RFG", "3TAB")],
        "Roofing felt - 15 lb.": [_felt_dropdown()],  # exact source description -- attempt 1 succeeds immediately
    }
    adapter = _adapter_with_test_project(dropdown_script=script)

    result = run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False)

    assert result.task_by_id("task_mapped").state == TASK_COMPLETED
    assert result.task_by_id("task_unmapped").state == TASK_COMPLETED
    search_desc_calls = [c for c in adapter.log.calls if c[0] == "search_by_description"]
    search_cs_calls = [c for c in adapter.log.calls if c[0] == "search_by_category_selector"]
    # Exactly ONE description search -- the exact source description --
    # never the compact phrase, since attempt 1 already found a match.
    assert search_desc_calls == [("search_by_description", ("Roofing felt - 15 lb.",), {})]
    assert search_cs_calls == [("search_by_category_selector", ("RFG", "3TAB"), {})]
    unmapped_task = result.task_by_id("task_unmapped")
    assert [a["search_type"] for a in unmapped_task.search_attempts] == ["exact_description"]


def test_unmapped_task_ambiguous_ranking_is_still_a_safe_stop(tmp_path, phrase_rules, ranking_config):
    """Requirement 10: ranking and safety-stop behavior are unchanged
    for an unmapped task -- an ambiguous dropdown still routes to
    REVIEW_REQUIRED instead of guessing, exactly like the existing
    CAT/SEL path already does."""
    plan = _plan_mapped_and_unmapped()
    script = {
        "RFG 3TAB": [_dropdown("RFG", "3TAB")],
        # Phase 5.6: two candidates that are EQUALLY distant from the
        # source (neither is the "15 lb" exact/near match Stage 5's
        # weight-unit size fix now correctly disambiguates) -- genuinely
        # ambiguous, tied fuzzy scores by construction.
        _FELT_FULL_DESCRIPTION: [
            DropdownResult(raw_text="Felt A", row_position=0, category="RFG", selector="FELTX", description="Roofing felt product Alpha", extraction_confidence=1.0),
            DropdownResult(raw_text="Felt B", row_position=1, category="RFG", selector="FELTY", description="Roofing felt product Omega", extraction_confidence=1.0),
        ],
    }
    adapter = _adapter_with_test_project(dropdown_script=script)

    result = run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False)

    assert result.task_by_id("task_unmapped").state == TASK_REVIEW_REQUIRED
    # Either safe-stop reason is fine here -- the property under test is
    # "never guesses among equally-weak candidates", not which specific
    # ranking rule caught it (this pair also mismatches the source's
    # "15" size term, which Stage 5's own size check now correctly
    # flags as a hard conflict rather than leaving it to the margin
    # check alone).
    assert result.task_by_id("task_unmapped").stop_reason in ("ambiguous_candidates", "hard_conflict")
    # never selected/committed anything for the ambiguous task
    select_calls = [c for c in adapter.log.calls if c[0] == "select_candidate"]
    assert len(select_calls) == 1  # only task_mapped's unambiguous selection


def test_uia_observed_cat_sel_recorded_after_successful_unmapped_commit(tmp_path, phrase_rules, ranking_config):
    """Requirement 11: UIA-observed CAT/SEL and description are
    recorded on the task after a successful commit of an
    originally-unmapped row, and a proposal is written to the separate
    observed_mappings.json file. Activity remains unknown because the
    removed pre-quantity grid OCR was its only source."""
    plan = _plan_mapped_and_unmapped()
    script = {"RFG 3TAB": [_dropdown("RFG", "3TAB")], _FELT_FULL_DESCRIPTION: [_felt_dropdown()]}
    adapter = _adapter_with_test_project(dropdown_script=script)

    result = run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False)

    task = result.task_by_id("task_unmapped")
    assert task.state == TASK_COMPLETED
    assert task.observed_category == "RFG"
    assert task.observed_selector == "FELT15"
    assert task.observed_description == "Roofing felt - 15 lb."
    assert task.observed_activity is None
    # the mapped task never gets these populated -- unchanged behavior
    assert result.task_by_id("task_mapped").observed_category is None

    proposals_path = _observed_mappings_path(tmp_path)
    assert proposals_path.exists()
    proposals = json.loads(proposals_path.read_text(encoding="utf-8"))
    assert "line_unmapped" in proposals
    proposal = proposals["line_unmapped"]
    assert proposal["observed_category"] == "RFG"
    assert proposal["observed_selector"] == "FELT15"
    assert proposal["search_phrase"] == _FELT_FULL_DESCRIPTION
    assert "line_mapped" not in proposals  # never written for the normal CAT/SEL path


def test_observed_mapping_proposal_uses_a_distinct_non_approved_state(tmp_path, phrase_rules, ranking_config):
    """Requirement 12: the observed proposal is never labeled
    human-approved -- it carries its own, clearly distinct state."""
    plan = _plan_mapped_and_unmapped()
    script = {"RFG 3TAB": [_dropdown("RFG", "3TAB")], _FELT_FULL_DESCRIPTION: [_felt_dropdown()]}
    adapter = _adapter_with_test_project(dropdown_script=script)

    run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False)

    proposals = json.loads(_observed_mappings_path(tmp_path).read_text(encoding="utf-8"))
    assert proposals["line_unmapped"]["state"] == OBSERVED_MAPPING_STATE
    assert OBSERVED_MAPPING_STATE != "approved"
    assert "approved" not in OBSERVED_MAPPING_STATE.lower().split("_from_")[0]


@pytest.mark.parametrize(
    "trust_state", ["REVIEW_REQUIRED", "UNIT_MISMATCH", "QUANTITY_MISMATCH", "CONFLICTING_ROW", "VERIFICATION_FAILED"],
)
def test_observed_mapping_proposal_verified_flag_false_for_any_non_verified_trust_state(
    tmp_path, phrase_rules, ranking_config, trust_state,
):
    """Phase 5.6 Stage 6: persisted learned mappings must be reusable
    ONLY from a genuinely VERIFIED commit -- a merely-committed row
    whose independent post-commit verification landed on ANY other
    trust_state (review-required, a unit mismatch, a quantity
    mismatch, a conflicting row, or an outright verification failure)
    must be recorded with verified=False, never eligible for reuse."""
    plan = _plan_mapped_and_unmapped()
    if trust_state == "VERIFICATION_FAILED":
        # Structural failure hard-stops the run, so make the unmapped task
        # the first/only task to verify its own proposal is still persisted.
        unmapped = plan.task_by_id("task_unmapped")
        plan.tasks = [unmapped]
        plan.groups[0].task_ids = [unmapped.task_id]
    script = {"RFG 3TAB": [_dropdown("RFG", "3TAB")], _FELT_FULL_DESCRIPTION: [_felt_dropdown()]}
    adapter = _adapter_with_test_project(dropdown_script=script, trust_state=trust_state)

    run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False)

    proposals = json.loads(_observed_mappings_path(tmp_path).read_text(encoding="utf-8"))
    assert proposals["line_unmapped"]["verified"] is False


def test_observed_mapping_proposal_verified_flag_true_only_for_verified_trust_state(
    tmp_path, phrase_rules, ranking_config,
):
    """Companion to the test above: the positive case -- a commit whose
    independent post-commit verification reaches VERIFIED is recorded
    with verified=True, the one and only signal
    _find_trusted_observed_mapping() will ever trust."""
    plan = _plan_mapped_and_unmapped()
    script = {"RFG 3TAB": [_dropdown("RFG", "3TAB")], _FELT_FULL_DESCRIPTION: [_felt_dropdown()]}
    adapter = _adapter_with_test_project(dropdown_script=script, trust_state="VERIFIED")

    run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False)

    proposals = json.loads(_observed_mappings_path(tmp_path).read_text(encoding="utf-8"))
    assert proposals["line_unmapped"]["verified"] is True


# ---------------------------------------------------------------------
# Phase 5.9 (live-caught): a committed-but-imperfectly-verified row must
# never be treated as "unfinished" -- see execution_plan.task_has_
# committed_row()/commit_state_from_trust_state(). A live run showed 13
# of 14 real committed rows carried a non-VERIFIED trust_state; without
# this, "Reset unfinished TEST execution" would re-search and duplicate
# every one of them.
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "trust_state", ["VERIFIED", "REVIEW_REQUIRED", "UNIT_MISMATCH", "QUANTITY_MISMATCH", "CONFLICTING_ROW"],
)
def test_commit_state_is_committed_for_every_trust_state_except_verification_failed(
    tmp_path, phrase_rules, ranking_config, trust_state,
):
    """Every trust_state EXCEPT VERIFICATION_FAILED means the row-count
    delta was nonzero -- i.e. something structurally landed in the
    grid -- so commit_state must be COMMITTED for all of them, not just
    the fully-VERIFIED case."""
    from estimate_extractor.xactimate_lookup.execution_plan import TASK_COMMIT_STATE_COMMITTED

    plan = _plan_mapped_and_unmapped()
    script = {"RFG 3TAB": [_dropdown("RFG", "3TAB")], _FELT_FULL_DESCRIPTION: [_felt_dropdown()]}
    adapter = _adapter_with_test_project(dropdown_script=script, trust_state=trust_state)

    result = run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False)

    assert result.task_by_id("task_unmapped").commit_state == TASK_COMMIT_STATE_COMMITTED


def test_verification_failed_preserves_physical_checkpoint_and_stops(tmp_path, phrase_rules, ranking_config):
    """A proven activation followed by zero commit delta is divergent.

    The runner must retain the physical-created checkpoint and stop before
    the following task instead of pretending the prior physical row vanished.
    """
    plan = _plan_mapped_and_unmapped()
    script = {"RFG 3TAB": [_dropdown("RFG", "3TAB")], _FELT_FULL_DESCRIPTION: [_felt_dropdown()]}
    adapter = _adapter_with_test_project(dropdown_script=script, trust_state="VERIFICATION_FAILED")

    result = run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False)

    mapped = result.task_by_id("task_mapped")
    unmapped = result.task_by_id("task_unmapped")
    assert mapped.commit_state == TASK_COMMIT_STATE_PHYSICAL_ITEM_CREATED_UNCONFIRMED
    assert mapped.physical_state_uncertain is True
    assert mapped.state == TASK_REVIEW_REQUIRED
    assert unmapped.state == TASK_PENDING and unmapped.attempts == 0
    assert result.run_state == RUN_STATE_PAUSED
    assert result.stop_reason_category == STOP_REASON_PROJECT_LEVEL_HARD_STOP


def test_commit_state_is_not_committed_when_nothing_ever_committed(tmp_path, phrase_rules, ranking_config):
    plan = _plan_mapped_and_unmapped()
    adapter = _adapter_with_test_project(dropdown_script={})  # no candidates for anything -- NO_MATCH

    result = run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False)

    from estimate_extractor.xactimate_lookup.execution_plan import TASK_COMMIT_STATE_NOT_COMMITTED
    assert result.task_by_id("task_unmapped").commit_state == TASK_COMMIT_STATE_NOT_COMMITTED
    assert result.task_by_id("task_mapped").commit_state == TASK_COMMIT_STATE_NOT_COMMITTED


def test_physical_created_unconfirmed_is_not_blindly_retryable(tmp_path, phrase_rules, ranking_config):
    plan = _plan_mapped_and_unmapped()
    # Run only the mapped task in this focused test.
    plan.groups[0].task_ids = ["task_mapped"]
    plan.tasks = [plan.task_by_id("task_mapped")]
    task = plan.tasks[0]
    adapter = _adapter_with_test_project(dropdown_script={"RFG 3TAB": [_dropdown("RFG", "3TAB")]})
    adapter.pending_item_created = lambda before: True

    def fail_after_creation(_quantity):
        # The checkpoint is both in memory and on disk before quantity
        # processing begins, rather than only being inferred later from
        # the returned outcome.
        assert task.commit_state == TASK_COMMIT_STATE_PHYSICAL_ITEM_CREATED_UNCONFIRMED
        persisted = load_execution_plan(tmp_path)
        assert persisted.task_by_id(task.task_id).commit_state == TASK_COMMIT_STATE_PHYSICAL_ITEM_CREATED_UNCONFIRMED
        raise AdapterError("simulated failure after the physical row appeared")

    adapter.enter_quantity = fail_after_creation
    result = run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False)

    assert result.tasks[0].commit_state == TASK_COMMIT_STATE_PHYSICAL_ITEM_CREATED_UNCONFIRMED
    # A second run must hit the independent retry gate before any search.
    result.tasks[0].state = TASK_PENDING
    searches_before = len([c for c in adapter.log.calls if c[0] == "search_by_category_selector"])
    rerun = run_execution_plan(result, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False)
    searches_after = len([c for c in adapter.log.calls if c[0] == "search_by_category_selector"])
    assert searches_after == searches_before
    assert "RETRY BLOCKED" in rerun.tasks[0].stop_detail


def test_already_committed_task_is_never_re_executed_even_if_pending(tmp_path, phrase_rules, ranking_config):
    """Defense-in-depth: a task that somehow reaches TASK_PENDING while
    still carrying commit evidence (trust_state from a prior real
    commit) must be refused, not re-searched/re-committed -- this must
    hold independent of reset_unfinished_tasks()'s own protection."""
    plan = _plan_mapped_and_unmapped()
    task = plan.task_by_id("task_unmapped")
    task.state = TASK_PENDING
    task.trust_state = "QUANTITY_MISMATCH"  # a prior real commit, low confidence

    script = {"RFG 3TAB": [_dropdown("RFG", "3TAB")], _FELT_FULL_DESCRIPTION: [_felt_dropdown()]}
    adapter = _adapter_with_test_project(dropdown_script=script)
    search_calls = []
    original_search = adapter.search_by_description

    def _tracking_search(text):
        search_calls.append(text)
        return original_search(text)

    adapter.search_by_description = _tracking_search

    result = run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False)

    result_task = result.task_by_id("task_unmapped")
    assert result_task.state == TASK_REVIEW_REQUIRED
    assert "ALREADY_COMMITTED" in result_task.stop_detail
    assert "RETRY BLOCKED" in result_task.stop_detail
    assert search_calls == []  # never re-searched


def test_find_trusted_observed_mapping_refuses_unverified_and_missing_entries(tmp_path):
    """Phase 5.6 Stage 6: _find_trusted_observed_mapping() -- the only
    path by which a PRIOR run's observed CAT/SEL can be reused as a
    later task's search fallback -- must refuse an entry whose
    verified flag is false or absent, and must refuse a line_item_id
    it has no entry for at all. Only an entry with verified=True and a
    resolvable category/selector is ever returned."""
    path = _observed_mappings_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "line_unverified": {
                    "verified": False,
                    "observed_category": "RFG", "observed_selector": "FELT15",
                },
                "line_verified": {
                    "verified": True,
                    "observed_category": "RFG", "observed_selector": "FELT15",
                },
                "line_verified_no_cat_sel": {
                    "verified": True,
                    "observed_category": None, "observed_selector": None,
                },
            }
        ),
        encoding="utf-8",
    )

    assert _find_trusted_observed_mapping(tmp_path, "line_unverified") is None
    assert _find_trusted_observed_mapping(tmp_path, "line_verified") == ("RFG", "FELT15")
    assert _find_trusted_observed_mapping(tmp_path, "line_verified_no_cat_sel") is None


# ---------------------------------------------------------------------
# Phase 5.7A: DESCRIPTION = what we search for, CAT/SEL = what we
# learn/select after Xactimate returns results. A live incident
# (SFG/GUTA -- the group-verification probe's own hardcoded disposable
# test item, unrelated to any PDF row -- mistaken for an unmapped row's
# search input) prompted tracing description_first_search_attempts()
# end-to-end and locking its ordering down with these tests.
# ---------------------------------------------------------------------


def test_find_verified_search_description_refuses_unverified_and_missing_entries(tmp_path):
    """Companion to _find_trusted_observed_mapping()'s own test: the
    DESCRIPTION-evidence reader must apply the exact same verified-only
    gate, and must refuse an entry with no description recorded."""
    path = _observed_mappings_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "line_unverified": {
                    "verified": False,
                    "verified_search_description": "Gutter / downspout - aluminum - up to 5\"",
                },
                "line_verified": {
                    "verified": True,
                    "verified_search_description": "Gutter / downspout - aluminum - up to 5\"",
                },
                "line_verified_no_description": {
                    "verified": True,
                    "verified_search_description": None,
                },
            }
        ),
        encoding="utf-8",
    )

    assert _find_verified_search_description(tmp_path, "line_unverified") is None
    assert _find_verified_search_description(tmp_path, "line_verified") == "Gutter / downspout - aluminum - up to 5\""
    assert _find_verified_search_description(tmp_path, "line_verified_no_description") is None
    assert _find_verified_search_description(tmp_path, "line_never_seen") is None


def test_unmapped_task_keeps_cat_sel_as_retrieval_only_last_resort(tmp_path, phrase_rules):
    path = _observed_mappings_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({
            "line_0001": {
                "verified": True,
                "observed_category": "SFG", "observed_selector": "GUTA",
                "verified_search_description": None,  # no learned description yet, only CAT/SEL
            },
        }),
        encoding="utf-8",
    )
    task = _unmapped_task("task_line_0001", "line_0001", "Exterior", 0, qty=200.0, unit="LF")
    task.description = 'R&R Gutter - aluminum - up to 5"'

    attempts = _description_first_search_attempts(task, phrase_rules, tmp_path)

    assert attempts[0][0] == SEARCH_TYPE_EXACT_DESCRIPTION
    assert attempts[0][1].search_input == 'R&R Gutter - aluminum - up to 5"'
    assert attempts[0][1].path == "description_search"
    assert attempts[-1][0] == SEARCH_TYPE_TRUSTED_OBSERVED_CAT_SEL
    assert attempts[-1][1].search_input == "SFG GUTA"


def test_verified_search_description_is_after_full_source_description(tmp_path, phrase_rules):
    path = _observed_mappings_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({
            "line_0001": {
                "verified": True,
                "observed_category": "SFG", "observed_selector": "GUTA",
                "verified_search_description": "Gutter / downspout - aluminum - up to 5\"",
            },
        }),
        encoding="utf-8",
    )
    task = _unmapped_task("task_line_0001", "line_0001", "Exterior", 0, qty=200.0, unit="LF")
    task.description = 'R&R Gutter - aluminum - up to 5"'

    attempts = _description_first_search_attempts(task, phrase_rules, tmp_path)

    assert attempts[0][0] == SEARCH_TYPE_EXACT_DESCRIPTION
    assert attempts[0][1].search_input == 'R&R Gutter - aluminum - up to 5"'
    assert attempts[0][1].path == "description_search"
    assert attempts[1][0] == SEARCH_TYPE_VERIFIED_SEARCH_DESCRIPTION
    assert attempts[1][1].search_input == "Gutter / downspout - aluminum - up to 5\""


def test_true_zero_results_can_reach_trusted_cat_sel_last_resort(tmp_path, phrase_rules, ranking_config):
    path = _observed_mappings_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({
            "line_unmapped": {
                "verified": True,
                "observed_category": "RFG", "observed_selector": "FELT15",
                "verified_search_description": None,
            },
        }),
        encoding="utf-8",
    )
    plan = _plan_mapped_and_unmapped()
    script = {
        "RFG 3TAB": [_dropdown("RFG", "3TAB")],
        "RFG FELT15": [_felt_dropdown()],
    }
    adapter = _adapter_with_test_project(dropdown_script=script)

    result = run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False)

    assert result.task_by_id("task_unmapped").state == TASK_COMPLETED
    search_desc_calls = [c for c in adapter.log.calls if c[0] == "search_by_description"]
    search_cs_calls = [c for c in adapter.log.calls if c[0] == "search_by_category_selector"]
    assert search_desc_calls[0] == ("search_by_description", ("Roofing felt - 15 lb.",), {})
    assert len(search_desc_calls) >= 2
    assert ("search_by_category_selector", ("RFG", "FELT15"), {}) in search_cs_calls
    assert result.task_by_id("task_unmapped").search_attempts[0]["advanced_to_next_attempt"] is True


def test_full_description_failure_never_searches_an_alternative(tmp_path, phrase_rules, ranking_config):
    """Regression: one failed interaction must not try alternate wording or add again."""
    plan = _plan_mapped_and_unmapped()
    adapter = _adapter_with_test_project(
        dropdown_script={"RFG 3TAB": [_dropdown("RFG", "3TAB")]},
        raise_unexpected_dialog_for={_FELT_FULL_DESCRIPTION},
    )

    result = run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False)

    task = result.task_by_id("task_unmapped")
    assert [
        call for call in adapter.log.calls if call[0] == "search_by_description"
    ] == [("search_by_description", (_FELT_FULL_DESCRIPTION,), {})]
    assert len(task.search_attempts) == 1
    assert task.search_attempts[0]["search_type"] == SEARCH_TYPE_EXACT_DESCRIPTION
    assert task.search_attempts[0]["advanced_to_next_attempt"] is False
    assert ("search_by_category_selector", ("RFG", "FELT15"), {}) not in adapter.log.calls


def test_exact_description_same_category_tie_resolves_via_first_candidate_fallback(tmp_path, phrase_rules, ranking_config):
    """Phase 5.22: a genuine same-category exact tie (two selectors,
    identical description text, no discriminating context) used to stay
    REVIEW_REQUIRED/ambiguous_candidates forever -- it now resolves via
    the explicit first-candidate fallback and commits FELT15A (position
    0). Still exactly one search attempt -- the fallback doesn't change
    the bounded-attempt/no-needless-requery behavior this test also
    guards."""
    plan = _plan_mapped_and_unmapped()
    tied = [
        DropdownResult(
            raw_text="RFG FELT15A", row_position=0, category="RFG", selector="FELT15A",
            description=_FELT_FULL_DESCRIPTION, extraction_confidence=1.0,
        ),
        DropdownResult(
            raw_text="RFG FELT15B", row_position=1, category="RFG", selector="FELT15B",
            description=_FELT_FULL_DESCRIPTION, extraction_confidence=1.0,
        ),
    ]
    adapter = _adapter_with_test_project(dropdown_script={
        "RFG 3TAB": [_dropdown("RFG", "3TAB")],
        _FELT_FULL_DESCRIPTION: tied,
    })

    result = run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False)

    task = result.task_by_id("task_unmapped")
    assert task.commit_state == "committed"
    assert task.observed_category == "RFG"
    assert task.observed_selector == "FELT15A"
    assert [call for call in adapter.log.calls if call[0] == "search_by_description"] == [
        ("search_by_description", (_FELT_FULL_DESCRIPTION,), {}),
    ]
    assert task.search_attempts[0]["advanced_to_next_attempt"] is False
    assert task.search_attempts[0]["decision_diagnostics"]["gate"] == "exact_tie_resolved_by_first_candidate_fallback"


def test_exact_description_hard_conflict_is_terminal(tmp_path, phrase_rules, ranking_config):
    plan = _plan_mapped_and_unmapped()
    conflict = DropdownResult(
        raw_text="RFG FELT30", row_position=0, category="RFG", selector="FELT30",
        description="Roofing felt - 30 lb.", extraction_confidence=1.0,
    )
    adapter = _adapter_with_test_project(dropdown_script={
        "RFG 3TAB": [_dropdown("RFG", "3TAB")],
        _FELT_FULL_DESCRIPTION: [conflict],
    })

    result = run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False)

    task = result.task_by_id("task_unmapped")
    assert task.stop_reason == "hard_conflict"
    assert len(task.search_attempts) == 1
    assert task.search_attempts[0]["advanced_to_next_attempt"] is False


# ---------------------------------------------------------------------
# Observability: each recorded search_attempts entry carries a
# "decision_diagnostics" dict (outcome.decision_diagnostics.to_dict(),
# built by classify_decision_with_diagnostics() -- the same call that
# determines "decision" above it), None only when ranking never ran.
# ---------------------------------------------------------------------


def test_search_attempt_decision_diagnostics_present_on_hard_conflict(tmp_path, phrase_rules, ranking_config):
    plan = _plan_mapped_and_unmapped()
    conflict = DropdownResult(
        raw_text="RFG FELT30", row_position=0, category="RFG", selector="FELT30",
        description="Roofing felt - 30 lb.", extraction_confidence=1.0,
    )
    adapter = _adapter_with_test_project(dropdown_script={
        "RFG 3TAB": [_dropdown("RFG", "3TAB")],
        _FELT_FULL_DESCRIPTION: [conflict],
    })

    result = run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False)

    task = result.task_by_id("task_unmapped")
    diag = task.search_attempts[0]["decision_diagnostics"]
    assert diag is not None
    assert diag["decision"] == "REVIEW_REQUIRED"
    # wrong_material caps the score well below auto_select_min, so the
    # below_auto_select_min gate fires before the hard_conflict gate is
    # ever reached -- top_has_hard_conflict/top_conflict_reasons below
    # still independently record the conflict regardless of which gate
    # produced the decision.
    assert diag["gate"] == "below_auto_select_min"
    assert diag["top_category"] == "RFG"
    assert diag["top_selector"] == "FELT30"
    assert diag["top_has_hard_conflict"] is True
    assert any("wrong_size" in r for r in diag["top_conflict_reasons"])
    assert diag["auto_select_min"] == ranking_config.auto_select_min
    assert diag["auto_select_margin"] == ranking_config.auto_select_margin
    assert diag["min_extraction_confidence"] == ranking_config.min_extraction_confidence


def test_search_attempt_decision_diagnostics_present_on_auto_select(tmp_path, phrase_rules, ranking_config):
    plan = _plan_mapped_and_unmapped()
    adapter = _adapter_with_test_project(dropdown_script={
        "RFG 3TAB": [_dropdown("RFG", "3TAB")],
        _FELT_FULL_DESCRIPTION: [_felt_dropdown()],
    })

    result = run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False)

    task = result.task_by_id("task_unmapped")
    assert task.state == TASK_COMPLETED
    diag = task.search_attempts[-1]["decision_diagnostics"]
    assert diag is not None
    assert diag["decision"] == "AUTO_SELECT"
    assert diag["top_category"] == "RFG"
    assert diag["top_selector"] == "FELT15"
    assert diag["top_has_hard_conflict"] is False


def test_candidate_click_then_duplicate_dialog_is_terminal(tmp_path, phrase_rules, ranking_config):
    plan = _plan_mapped_and_unmapped()
    adapter = _adapter_with_test_project(dropdown_script={
        "RFG 3TAB": [_dropdown("RFG", "3TAB")],
        _FELT_FULL_DESCRIPTION: [_felt_dropdown()],
    })
    original_select = adapter.select_candidate

    def duplicate_dialog(candidate):
        original_select(candidate)
        if candidate.selector == "FELT15":
            raise UnexpectedDialogError("Duplicate Item(s)")

    adapter.select_candidate = duplicate_dialog

    result = run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False)

    task = result.task_by_id("task_unmapped")
    assert task.stop_reason == "unexpected_dialog"
    assert task.physical_state_uncertain is True
    assert result.run_state == RUN_STATE_PAUSED
    assert result.stop_reason_category == STOP_REASON_PROJECT_LEVEL_HARD_STOP
    assert len(task.search_attempts) == 1
    assert task.search_attempts[0]["advanced_to_next_attempt"] is False


def test_pending_physical_checkpoint_prohibits_later_search(tmp_path, phrase_rules, ranking_config):
    plan = _plan_mapped_and_unmapped()
    adapter = _adapter_with_test_project(dropdown_script={
        "RFG 3TAB": [_dropdown("RFG", "3TAB")],
        _FELT_FULL_DESCRIPTION: [_felt_dropdown()],
    })
    original_enter = adapter.enter_quantity

    def fail_after_pending(quantity):
        if adapter._selected and adapter._selected.selector == "FELT15":
            raise AdapterError("quantity control unavailable")
        return original_enter(quantity)

    adapter.enter_quantity = fail_after_pending

    result = run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False)

    task = result.task_by_id("task_unmapped")
    assert task.commit_state == TASK_COMMIT_STATE_PHYSICAL_ITEM_CREATED_UNCONFIRMED
    assert len(task.search_attempts) == 1
    assert task.search_attempts[0]["advanced_to_next_attempt"] is False


def test_dropdown_capture_failure_can_advance_when_state_is_clean(tmp_path, phrase_rules, ranking_config):
    plan = _plan_mapped_and_unmapped()
    attempts = _description_first_search_attempts(plan.task_by_id("task_unmapped"), phrase_rules, tmp_path)
    fallback_text = attempts[1][1].search_input
    adapter = _adapter_with_test_project(
        dropdown_script={
            "RFG 3TAB": [_dropdown("RFG", "3TAB")],
            fallback_text: [_felt_dropdown()],
        },
        fail_on_capture_for={_FELT_FULL_DESCRIPTION},
    )

    result = run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False)

    task = result.task_by_id("task_unmapped")
    assert task.state == TASK_COMPLETED
    assert [attempt["search_text"] for attempt in task.search_attempts] == [
        _FELT_FULL_DESCRIPTION, fallback_text,
    ]
    assert task.search_attempts[0]["advanced_to_next_attempt"] is True


def test_retrieval_fallback_is_blocked_when_adapter_state_is_dirty(tmp_path, phrase_rules, ranking_config):
    plan = _plan_mapped_and_unmapped()
    adapter = _adapter_with_test_project(dropdown_script={"RFG 3TAB": [_dropdown("RFG", "3TAB")]})
    adapter.verify_search_fallback_state = lambda baseline: (False, "pending physical item exists")

    result = run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False)

    task = result.task_by_id("task_unmapped")
    assert [call for call in adapter.log.calls if call[0] == "search_by_description"] == [
        ("search_by_description", (_FELT_FULL_DESCRIPTION,), {}),
    ]
    assert task.search_attempts[0]["advanced_to_next_attempt"] is False
    assert "pending physical item" in task.search_attempts[0]["advance_reason"]


def test_generic_cleaning_fallback_remains_available_after_clean_zero_results(
    tmp_path, phrase_rules, ranking_config,
):
    plan = _plan_mapped_and_unmapped()
    task = plan.task_by_id("task_unmapped")
    task.description = "Clean Fence"
    task.normalized_trade = "fencing"
    task.normalized_component = "fence"
    task.normalized_material = "wood"
    task.normalized_action = "clean"
    attempts = _description_first_search_attempts(task, phrase_rules, tmp_path)
    assert attempts[0][1].search_input == "Clean Fence"
    assert any(kind == SEARCH_TYPE_GENERIC_CLEANING_FALLBACK for kind, _plan in attempts)
    adapter = _adapter_with_test_project(dropdown_script={
        "RFG 3TAB": [_dropdown("RFG", "3TAB")],
        "clean": [DropdownResult(
            raw_text="CLN AV", row_position=0, category="CLN", selector="AV",
            description="Clean {V}", extraction_confidence=1.0,
        )],
    })

    result = run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False)

    executed = result.task_by_id("task_unmapped").search_attempts
    assert any(attempt["search_type"] == SEARCH_TYPE_GENERIC_CLEANING_FALLBACK for attempt in executed)
    assert ("search_by_description", ("clean",), {}) in adapter.log.calls


def test_review_approved_task_still_uses_trusted_cat_sel_directly(phrase_rules):
    """Requirement 5: a normal already-mapped/approved row (NOT
    began_unmapped) is completely unchanged by Phase 5.7A -- it still
    routes straight to the trusted CAT/SEL path via
    _task_to_lookup_plan(), never through the description-first
    sequence at all."""
    task = _task("task_mapped", "line_mapped", "Dwelling Roof", "RFG", "3TAB", 0, qty=10.0, unit="SQ")

    lookup_plan, actual_strategy, reason = _task_to_lookup_plan(task, phrase_rules)

    assert lookup_plan.path == LOOKUP_PATH_TRUSTED
    assert lookup_plan.search_input == "RFG 3TAB"
    assert actual_strategy == LOOKUP_PATH_TRUSTED


def test_gutter_and_downspout_each_keep_their_own_full_description(tmp_path, phrase_rules):
    """Distinct source rows remain distinct even when evidence shares a catalog description."""
    gutter = _unmapped_task("task_line_0001", "line_0001", "Exterior", 0, qty=200.0, unit="LF")
    gutter.description = 'R&R Gutter - aluminum - up to 5"'
    downspout = _unmapped_task("task_line_0032", "line_0032", "Rear Elevation", 1, qty=24.0, unit="LF")
    downspout.description = 'R&R Downspout - aluminum - up to 5"'

    gutter_attempts = _description_first_search_attempts(gutter, phrase_rules, tmp_path)
    downspout_attempts = _description_first_search_attempts(downspout, phrase_rules, tmp_path)

    # distinct tasks, distinct own-description search attempts
    assert gutter_attempts[0][1].search_input == 'R&R Gutter - aluminum - up to 5"'
    assert downspout_attempts[0][1].search_input == 'R&R Downspout - aluminum - up to 5"'
    assert gutter_attempts[0][1].search_input != downspout_attempts[0][1].search_input

    # after gutter's run is verified with a learned catalog description...
    path = _observed_mappings_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({
            "line_0001": {
                "verified": True,
                "observed_category": "SFG", "observed_selector": "GUTA",
                "verified_search_description": "Gutter / downspout - aluminum - up to 5\"",
            },
        }),
        encoding="utf-8",
    )
    gutter_attempts_2 = _description_first_search_attempts(gutter, phrase_rules, tmp_path)
    downspout_attempts_2 = _description_first_search_attempts(downspout, phrase_rules, tmp_path)

    assert gutter_attempts_2[0][0] == SEARCH_TYPE_EXACT_DESCRIPTION
    assert gutter_attempts_2[0][1].search_input == 'R&R Gutter - aluminum - up to 5"'
    assert gutter_attempts_2[1][0] == SEARCH_TYPE_VERIFIED_SEARCH_DESCRIPTION
    assert downspout_attempts_2[0][0] == SEARCH_TYPE_EXACT_DESCRIPTION
    assert downspout_attempts_2[0][1].search_input == 'R&R Downspout - aluminum - up to 5"'
    assert _find_trusted_observed_mapping(tmp_path, "line_never_seen") is None


def test_observed_mapping_proposal_never_touches_review_service_state(tmp_path, phrase_rules, ranking_config):
    """Requirement 13: existing reviewed CAT/SEL (or any review_service
    state at all) is never overwritten -- the observed-mapping-proposal
    writer doesn't import or touch review_service/review_state.json."""
    project_dir = tmp_path
    (project_dir / "review").mkdir(parents=True, exist_ok=True)
    review_state_path = project_dir / "review" / "review_state.json"
    review_state_path.write_text(json.dumps({
        "line_unmapped": {"status": "unreviewed", "overrides": {}, "reviewer_note": "", "activity_required_waived": False, "updated_at": "t", "reviewer": ""}
    }), encoding="utf-8")
    before = review_state_path.read_text(encoding="utf-8")

    plan = _plan_mapped_and_unmapped()
    script = {"RFG 3TAB": [_dropdown("RFG", "3TAB")], _FELT_FULL_DESCRIPTION: [_felt_dropdown()]}
    adapter = _adapter_with_test_project(dropdown_script=script)

    run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False)

    after = review_state_path.read_text(encoding="utf-8")
    assert after == before  # byte-for-byte unchanged


def test_test_only_task_refused_when_live_project_is_not_exactly_test(tmp_path, phrase_rules, ranking_config):
    """Hard scope boundary: a description-first (unmapped) task refuses
    to run when the live adapter isn't positively verified on exactly
    'TEST' -- independent of, and even if, build_execution_plan()'s own
    plan-build-time check was somehow bypassed. Never abandons the
    whole run -- only this one task is refused; a normal CAT/SEL task
    in the same run is unaffected."""
    plan = _plan_mapped_and_unmapped()
    script = {"RFG 3TAB": [_dropdown("RFG", "3TAB")], _FELT_FULL_DESCRIPTION: [_felt_dropdown()]}
    adapter = GroupAwareFakeAdapter(dropdown_script=script)
    adapter.supports_live_execution = True
    adapter.expected_project_name = "some-production-claim"  # NOT "TEST"

    result = run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False)

    unmapped = result.task_by_id("task_unmapped")
    assert unmapped.state == TASK_REVIEW_REQUIRED
    assert "TEST" in unmapped.stop_detail
    assert result.task_by_id("task_mapped").state == TASK_COMPLETED  # unaffected
    # never even attempted a search for the refused task
    search_desc_calls = [c for c in adapter.log.calls if c[0] == "search_by_description"]
    assert search_desc_calls == []


# ---------------------------------------------------------------------
# Phase 5.5B: description-first routing enforcement (Objective 1) and
# group ancestry verification (Objective 2). See execution_runner.py's
# _task_to_lookup_plan()/UnsafeLookupRouting and windows_adapter.py's
# ensure_group() docstrings for the live incident these guard against.
# ---------------------------------------------------------------------


def test_began_unmapped_task_always_starts_description_first(phrase_rules):
    """Requirement 1: a task whose lookup_strategy is test_description_
    first is ALWAYS routed to the description-search path -- proven
    directly against _task_to_lookup_plan(), not inferred from a full
    run."""
    task = _unmapped_task("task_unmapped", "line_unmapped", "Dwelling Roof", 0)
    lookup_plan, actual_strategy, reason = _task_to_lookup_plan(task, phrase_rules)

    assert actual_strategy == LOOKUP_PATH_DESCRIPTION_SEARCH
    assert lookup_plan.path == LOOKUP_PATH_DESCRIPTION_SEARCH
    assert lookup_plan.search_input == _FELT_PHRASE
    assert "test_description_first" in reason


def test_stale_cat_sel_values_do_not_override_test_description_first(phrase_rules):
    """Requirement 2: even if category/selector end up populated on a
    task whose lookup_strategy is test_description_first (e.g. a stale
    reload, a placeholder mapping, a partially-populated field from an
    earlier failed attempt) -- exactly the live incident this phase
    fixes -- routing must NEVER silently use them. Refuses with a safe,
    catchable exception instead of building a CAT/SEL search."""
    task = _unmapped_task("task_unmapped", "line_unmapped", "Dwelling Roof", 0)
    task.category = "RFG"  # stale/unexpected -- began_unmapped tasks never legitimately have both
    task.selector = "FELT15"

    with pytest.raises(UnsafeLookupRouting, match="test_description_first"):
        _task_to_lookup_plan(task, phrase_rules)


def test_review_approved_strategy_uses_trusted_cat_sel_path(phrase_rules):
    """Requirement 3: the one currently-implemented form of a verified,
    trusted mapping -- LOOKUP_STRATEGY_REVIEW_APPROVED, a human-approved
    CAT/SEL carried into the plan -- correctly uses the trusted path.
    (A "began_unmapped task reusing a previously-observed, verified
    mapping" is intentionally NOT built by this phase -- see execution_
    runner.py's OBSERVED_MAPPING_STATE, deliberately never auto-reused
    -- so this is the only currently-legitimate route to CAT/SEL.)"""
    task = _task("task_mapped", "line_mapped", "Dwelling Roof", "RFG", "3TAB", 0)
    lookup_plan, actual_strategy, reason = _task_to_lookup_plan(task, phrase_rules)

    assert actual_strategy == LOOKUP_PATH_TRUSTED
    assert lookup_plan.path == LOOKUP_PATH_TRUSTED
    assert lookup_plan.search_input == "RFG 3TAB"
    assert "review_approved_cat_sel" in reason


def test_requested_and_actual_lookup_strategies_are_reported(tmp_path, phrase_rules, ranking_config):
    """Requirement 4: requested (task.lookup_strategy, fixed at plan-
    build time) and actual (task.actual_lookup_strategy, set fresh at
    execution time) strategies are both independently visible on the
    persisted task -- the exact audit trail that would have caught the
    live "None None" CAT/SEL incident."""
    plan = _plan_mapped_and_unmapped()
    script = {"RFG 3TAB": [_dropdown("RFG", "3TAB")], _FELT_FULL_DESCRIPTION: [_felt_dropdown()]}
    adapter = _adapter_with_test_project(dropdown_script=script)

    result = run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False)

    mapped = result.task_by_id("task_mapped")
    assert mapped.lookup_strategy == LOOKUP_STRATEGY_REVIEW_APPROVED  # requested
    assert mapped.actual_lookup_strategy == LOOKUP_PATH_TRUSTED  # actual

    unmapped = result.task_by_id("task_unmapped")
    assert unmapped.lookup_strategy == LOOKUP_STRATEGY_TEST_DESCRIPTION_FIRST  # requested
    assert unmapped.actual_lookup_strategy == LOOKUP_PATH_DESCRIPTION_SEARCH  # actual
    assert unmapped.lookup_strategy_reason  # non-empty audit reason


def test_ancestry_mismatch_blocks_task_execution(tmp_path, phrase_rules, ranking_config):
    """Requirement 7: when a group cannot be created/verified (e.g. its
    ensure_group() call fails -- the same signal a real ancestry-
    verification failure produces), its tasks are marked REVIEW_REQUIRED
    and NEVER reach a live search -- the safety net that closes the
    malformed-nested-tree incident at the execution-runner layer too."""
    plan = _plan_two_groups()
    adapter = GroupAwareFakeAdapter(dropdown_script=_dropdown_script(*plan.tasks), raise_on_group={"Dwelling Roof"})
    adapter.supports_live_execution = True

    result = run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False)

    roof_tasks = [t for t in result.tasks if t.section_name == "Dwelling Roof"]
    assert all(t.state == TASK_REVIEW_REQUIRED for t in roof_tasks)
    assert result.group_by_id("Dwelling Roof").state == GROUP_FAILED
    search_calls = [c for c in adapter.log.calls if c[0] in ("search_by_category_selector", "search_by_description")]
    assert not any(a[0] == "SFG" for _n, a, _k in search_calls)  # Dwelling Roof's tasks never searched


def test_runner_continues_to_later_sibling_groups_safely_after_ancestry_failure(tmp_path, phrase_rules, ranking_config):
    """Requirement 8: one group's ancestry/creation failure never
    aborts the whole run -- later sibling groups still get their own
    fresh, independent ensure_group()/select_group()/verify_group()
    attempt and complete normally."""
    plan = _plan_two_groups()
    adapter = GroupAwareFakeAdapter(dropdown_script=_dropdown_script(*plan.tasks), raise_on_group={"Dwelling Roof"})
    adapter.supports_live_execution = True

    result = run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False)

    assert result.group_by_id("Dwelling Roof").state == GROUP_FAILED
    assert result.group_by_id("Fence").state == GROUP_COMPLETED
    fence_tasks = [t for t in result.tasks if t.section_name == "Fence"]
    assert all(t.state == TASK_COMPLETED for t in fence_tasks)
    assert result.run_state == RUN_STATE_COMPLETED


# ---------------------------------------------------------------------
# Phase 5.5D: no automatic baseline restoration, committed rows survive
# the run, an unresolved later group can't delete an earlier group's
# committed row, stale plans are rejected, and stop_reason_category is
# reported accurately. See destructive_audit.py / windows_adapter.py's
# cancel_current_item() for the actual protection mechanism -- these
# tests exercise it through run_execution_plan()'s own call path.
# ---------------------------------------------------------------------


class ProtectionAwareFakeAdapter(GroupAwareFakeAdapter):
    """Adds just enough of the Phase 5.5D protection surface
    (set_execution_context/record_protected_commit/cancel_current_item)
    to prove run_execution_plan() reacts correctly to a
    ProtectedCommittedRowError raised from inside group verification --
    without needing any real win32/OCR API."""

    def __init__(self, *args, raise_protected_error_on_group=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.raise_protected_error_on_group = raise_protected_error_on_group or set()
        self.protected_commits: list[tuple[str | None, str, str]] = []  # (group, category, selector)
        self.context_group = None

    def set_execution_context(self, *, run_id=None, task_id=None, source_row=None, group=None):
        if group is not None:
            self.context_group = group

    def record_protected_commit(self, *, category, selector, description=None, quantity=None, unit=None, xactimate_item_number=None, verification_state=None):
        self.protected_commits.append((self.context_group, category, selector))

    def verify_group(self, name: str) -> bool:
        if name in self.raise_protected_error_on_group:
            raise ProtectedCommittedRowError(
                f"simulated: cleaning up {name!r} would have deleted a protected row from an earlier group."
            )
        return super().verify_group(name)


def test_execute_never_automatically_restores_the_baseline_or_reverts_committed_tasks(tmp_path, phrase_rules, ranking_config):
    """Requirements 1 & 2: nothing in run_execution_plan()'s own call
    path restores a baseline or reverts an already-COMPLETED task --
    confirmed by running a full plan and checking every committed task
    is still COMPLETED in the value run_execution_plan() itself
    returns (not just what was persisted mid-run)."""
    plan = _plan_two_groups()
    adapter = ProtectionAwareFakeAdapter(dropdown_script=_dropdown_script(*plan.tasks))
    adapter.supports_live_execution = True

    result = run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False)

    assert all(t.state == TASK_COMPLETED for t in result.tasks)
    assert result.run_state == RUN_STATE_COMPLETED
    assert result.stop_reason_category == STOP_REASON_NORMAL_COMPLETION
    # Protected-commit hook fired once per successful commit.
    assert len(adapter.protected_commits) == 3


def test_protected_row_error_in_one_group_hard_stops_without_touching_earlier_completed_tasks(tmp_path, phrase_rules, ranking_config):
    """Requirement 3: a ProtectedCommittedRowError raised while
    verifying a LATER group (simulating group-probe cleanup about to
    delete an earlier group's committed row) hard-stops the whole run
    -- the EARLIER group's already-completed tasks are left exactly as
    they were, never marked failed or reset, and the run is clearly
    flagged as a protected-row refusal, not an ordinary group failure."""
    plan = _plan_two_groups()  # "Dwelling Roof" (2 tasks) then "Fence" (1 task)
    adapter = ProtectionAwareFakeAdapter(
        dropdown_script=_dropdown_script(*plan.tasks), raise_protected_error_on_group={"Fence"},
    )
    adapter.supports_live_execution = True

    result = run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False)

    roof_tasks = [t for t in result.tasks if t.section_name == "Dwelling Roof"]
    fence_tasks = [t for t in result.tasks if t.section_name == "Fence"]
    assert all(t.state == TASK_COMPLETED for t in roof_tasks)  # untouched
    assert result.run_state == RUN_STATE_PAUSED
    assert result.stop_reason_category == STOP_REASON_PROTECTED_ROW_REFUSAL
    assert result.group_by_id("Fence").state == GROUP_FAILED
    assert all(t.state != TASK_COMPLETED for t in fence_tasks)  # never silently completed either


def test_stale_plan_schema_is_rejected_before_any_task_runs(tmp_path, phrase_rules, ranking_config):
    """Requirement 7: a plan whose schema_version predates
    CURRENT_SCHEMA_VERSION (the on-disk-legacy-JSON case -- see
    ExecutionPlan.from_dict()'s own data.get("schema_version", 1))
    must be refused outright, before touching the adapter or any
    group/task at all."""
    plan = _plan_two_groups()
    plan.schema_version = CURRENT_SCHEMA_VERSION - 1
    assert is_plan_stale(plan)
    adapter = GroupAwareFakeAdapter(dropdown_script=_dropdown_script(*plan.tasks))
    adapter.supports_live_execution = True

    result = run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False)

    assert all(t.state == TASK_PENDING for t in result.tasks)  # nothing was attempted
    assert adapter.ensure_group_calls == []  # never even reached group setup
    assert result.run_state == RUN_STATE_PAUSED


def test_current_schema_plan_is_never_treated_as_stale(tmp_path, phrase_rules, ranking_config):
    plan = _plan_two_groups()
    assert plan.schema_version == CURRENT_SCHEMA_VERSION
    assert not is_plan_stale(plan)
