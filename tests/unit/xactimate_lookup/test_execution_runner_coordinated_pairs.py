"""Unit tests for Phase 5.23 R&R Stage 4: coordinated remove/replace
pair execution wired into the group-aware execution runner.

Kept in its own file (mirroring test_coordinated_pairs.py's own "stay
fast and fully isolated" precedent) rather than growing
test_execution_runner.py further -- this exercises a genuinely
different adapter surface (the Stage 3 dual-target R&R primitives)
than that file's ordinary single-row GroupAwareFakeAdapter tests.

RRPairFakeAdapter below is a FakeXactimateAdapter extended with BOTH
the group/commit-verification hooks (same shape as test_execution_
runner.GroupAwareFakeAdapter) AND fully scripted Stage 3 R&R pair
primitives (bind_rr_pair_after_activation/write_and_verify_rr_pair_
quantities/locate_existing_rr_pair/verify_existing_rr_pair_half_
quantity/write_and_verify_existing_rr_pair_half_quantity), so
execution_runner._run_coordinated_pair()'s fresh-activation and every
resume shape can be exercised without any real Windows/Xactimate
session -- exactly this whole package's established testing
convention (see windows_adapter.py's own module docstring)."""

from __future__ import annotations

import pytest

from estimate_extractor.xactimate_lookup.adapter import (
    AdapterError,
    FakeXactimateAdapter,
    PhysicalStateUncertainError,
    QuantityConfirmationError,
    TaskLocalRowReconciliationError,
    UnexpectedDialogError,
)
from estimate_extractor.xactimate_lookup.execution_plan import (
    CoordinatedPair,
    ExecutionPlan,
    ExecutionPlanOverwriteRefused,
    ExecutionTask,
    GroupExecutionState,
    LOOKUP_STRATEGY_REVIEW_APPROVED,
    LOOKUP_STRATEGY_TEST_DESCRIPTION_FIRST,
    PAIR_BOTH_BOUND,
    PAIR_BOTH_VERIFIED,
    PAIR_MINUS_VERIFIED,
    PAIR_PHYSICAL_STATE_UNCERTAIN,
    PAIR_PLUS_VERIFIED,
    PAIR_REVIEW_REQUIRED,
    PAIR_SATISFIED,
    PAIR_UNACTIVATED,
    RUN_STATE_PAUSED,
    STOP_REASON_COORDINATED_PAIR_EXECUTION_NOT_IMPLEMENTED,
    STOP_REASON_PROJECT_LEVEL_HARD_STOP,
    TASK_COMPLETED,
    TASK_FAILED,
    TASK_PENDING,
    TASK_REVIEW_REQUIRED,
    load_execution_plan,
    reset_unfinished_tasks,
    restricted_plan_path,
    restricted_reports_dir,
    save_execution_plan,
)
from estimate_extractor.xactimate_lookup.execution_runner import (
    _bounded_description_first_decision,
    _description_first_search_attempts,
    run_execution_plan,
)
from estimate_extractor.xactimate_lookup.models import DropdownResult
from estimate_extractor.xactimate_lookup.ranking import rank_dropdown_results


# ---------------------------------------------------------------------
# Fake result/target shapes -- deliberately duck-typed, never importing
# windows_adapter's real PendingRRPairTarget/RRPairQuantityResult/
# QuantityEntryConfirmation types, exactly matching how execution_
# runner.py itself only ever accesses these opaquely (adapter-agnostic
# boundary; see adapter.py's own module docstring).
# ---------------------------------------------------------------------


class _FakeConfirmation:
    def __init__(self, expected, observed, *, confidence="CONFIRMED", reason="simulated"):
        self.expected = expected
        self.observed = observed
        self.confidence = confidence
        self.review_required = confidence != "CONFIRMED"
        self.reason = reason


class _FakeRRPairQuantityResult:
    def __init__(self, minus_confirmation, plus_confirmation):
        self.minus_confirmation = minus_confirmation
        self.plus_confirmation = plus_confirmation


class _FakeRRPairQuantityError(QuantityConfirmationError):
    def __init__(self, message, *, side, minus_confirmation=None):
        super().__init__(message)
        self.side = side
        self.minus_confirmation = minus_confirmation


class _FakePendingQuantityTarget:
    def __init__(self, identity, activity):
        self.identity = identity
        self.activity = activity


class _FakePendingRRPairTarget:
    def __init__(self, identity):
        self.minus_target = _FakePendingQuantityTarget(identity, "-")
        self.plus_target = _FakePendingQuantityTarget(identity, "+")
        self.identity = identity


class _FakeCommitVerification:
    def __init__(self, trust_state, quantity_observed=None, unit_observed=None, category_observed=None, selector_observed=None):
        self.trust_state = trust_state
        self.quantity_observed = quantity_observed
        self.unit = _FakeUnitResult(unit_observed) if unit_observed else None
        self.category_observed = category_observed
        self.selector_observed = selector_observed
        self.description_observed = None
        self.reason = f"simulated trust_state={trust_state}"


class _FakeUnitResult:
    def __init__(self, observed_xactimate_unit):
        self.observed_xactimate_unit = observed_xactimate_unit


_PAIR_IDENTITY = ("RFG", "REM", "RFG/REM description")


class RRPairFakeAdapter(FakeXactimateAdapter):
    """See module docstring. Every R&R knob below defaults to a clean,
    fully-successful pair execution; individual tests override exactly
    the one behavior they're exercising, either via a constructor
    kwarg or by subclassing (mirroring test_execution_runner.py's own
    QuantityFailureFakeAdapter-style convention)."""

    def __init__(self, *args, verified_groups=None, trust_state="VERIFIED", **kwargs):
        super().__init__(*args, **kwargs)
        self.verified_groups = verified_groups
        self.trust_state = trust_state
        self.ensure_group_calls: list[str] = []
        self.select_group_calls: list[str] = []
        self.verify_group_calls: list[str] = []
        self.verify_commit_calls = 0
        self.bind_calls = 0
        self.select_candidate_calls = 0
        self.write_pair_calls = 0
        self.locate_pair_calls: list[tuple] = []
        self.commit_item_calls = 0
        #: Controls: see each method below for the values each knob accepts.
        self.bind_outcome = "success"
        self.minus_write_outcome = "success"
        self.plus_write_outcome = "success"
        self.locate_pair_outcome = "success"
        self.verify_half_outcome = {"-": "success", "+": "success"}
        self.write_half_outcome = {"-": "success", "+": "success"}

    # -- group hooks (same shape as GroupAwareFakeAdapter) --------------
    def ensure_group(self, name: str, *, parent_group_name: str | None = None) -> str | None:
        self.ensure_group_calls.append(name)
        return None

    def select_group(self, name: str) -> None:
        self.select_group_calls.append(name)

    def verify_group(self, name: str) -> bool:
        self.verify_group_calls.append(name)
        if self.verified_groups is None:
            return True
        return name in self.verified_groups

    def snapshot_grid_identities(self):
        return [("EXISTING", "ROW")]

    def verify_commit(self, before_snapshot, category, selector, expected_quantity, *, source_unit=None, expected_xactimate_unit=None, populated_unit=None):
        self.verify_commit_calls += 1
        return _FakeCommitVerification(
            self.trust_state, quantity_observed=expected_quantity, unit_observed=expected_xactimate_unit,
            category_observed=category, selector_observed=selector,
        )

    def select_candidate(self, candidate) -> None:
        self.select_candidate_calls += 1
        super().select_candidate(candidate)

    # -- Stage 3 R&R pair primitives -------------------------------------
    def bind_rr_pair_after_activation(self, before_snapshot):
        self.bind_calls += 1
        if self.bind_outcome == "dialog":
            raise UnexpectedDialogError("simulated dialog during pair binding")
        if self.bind_outcome == "physical_uncertain":
            raise PhysicalStateUncertainError("simulated physical state uncertain during binding")
        if self.bind_outcome == "task_local":
            raise TaskLocalRowReconciliationError("simulated malformed activation pair")
        if self.bind_outcome == "adapter_error":
            raise AdapterError("simulated adapter error during binding")
        return _FakePendingRRPairTarget(_PAIR_IDENTITY)

    def write_and_verify_rr_pair_quantities(self, pair_target, minus_quantity, plus_quantity, *, on_minus_verified=None):
        self.write_pair_calls += 1
        if self.minus_write_outcome == "fail":
            raise _FakeRRPairQuantityError("simulated minus write failure", side="minus")
        minus_confirmation = _FakeConfirmation(
            minus_quantity, minus_quantity,
            confidence="CONFIRMED" if self.minus_write_outcome == "success" else "LOW_CONFIDENCE",
        )
        if on_minus_verified is not None:
            on_minus_verified(minus_confirmation)
        if self.plus_write_outcome == "fail":
            raise _FakeRRPairQuantityError(
                "simulated plus write failure", side="plus", minus_confirmation=minus_confirmation,
            )
        plus_confirmation = _FakeConfirmation(
            plus_quantity, plus_quantity,
            confidence="CONFIRMED" if self.plus_write_outcome == "success" else "LOW_CONFIDENCE",
        )
        return _FakeRRPairQuantityResult(minus_confirmation, plus_confirmation)

    def locate_existing_rr_pair(self, *, category, selector, description):
        self.locate_pair_calls.append((category, selector, description))
        if self.locate_pair_outcome != "success":
            return None
        return _FakePendingRRPairTarget((category, selector, description))

    def verify_existing_rr_pair_half_quantity(self, *, category, selector, description, activity, expected_quantity):
        outcome = self.verify_half_outcome.get(activity, "success")
        if outcome == "missing":
            return None
        return _FakeConfirmation(
            expected_quantity, expected_quantity,
            confidence="CONFIRMED" if outcome == "success" else "LOW_CONFIDENCE",
        )

    def write_and_verify_existing_rr_pair_half_quantity(self, *, category, selector, description, activity, quantity):
        outcome = self.write_half_outcome.get(activity, "success")
        if outcome == "fail":
            side = "minus" if activity == "-" else "plus"
            raise _FakeRRPairQuantityError("simulated resumed write failure", side=side)
        return _FakeConfirmation(quantity, quantity, confidence="CONFIRMED" if outcome == "success" else "LOW_CONFIDENCE")

    def commit_item(self) -> None:
        self.commit_item_calls += 1
        super().commit_item()


def _rr_adapter(**kwargs) -> RRPairFakeAdapter:
    adapter = RRPairFakeAdapter(**kwargs)
    adapter.supports_live_execution = True
    return adapter


def _task(task_id, line_item_id, section_name, category, selector, source_order, qty=5.0, unit="LF"):
    return ExecutionTask(
        task_id=task_id, line_item_id=line_item_id, source_order=source_order,
        area_name=None, section_name=section_name, description=f"{category}/{selector} description",
        category=category, selector=selector, lookup_strategy=LOOKUP_STRATEGY_REVIEW_APPROVED,
        source_quantity=qty, source_unit=unit, expected_unit=unit,
    )


def _dropdown(cat, sel):
    return DropdownResult(raw_text=f"{cat} {sel}", row_position=0, category=cat, selector=sel, description=f"{cat}/{sel}", extraction_confidence=1.0)


def _plan_with_one_pair(*, minus_qty=10.0, plus_qty=10.0, unit="SQ", extra_ordinary_task=False):
    remove_task = _task("task_remove", "line_remove", "Dwelling Roof", "RFG", "REM", 0, qty=minus_qty, unit=unit)
    replace_task = _task("task_replace", "line_replace", "Dwelling Roof", "RFG", "REM", 1, qty=plus_qty, unit=unit)
    pair_id = "pair_task_remove_task_replace"
    remove_task.coordinated_pair_id = pair_id
    replace_task.coordinated_pair_id = pair_id
    tasks = [remove_task, replace_task]
    task_ids = ["task_remove", "task_replace"]
    ordinary_task = None
    if extra_ordinary_task:
        ordinary_task = _task("task_ordinary", "line_ordinary", "Dwelling Roof", "SFG", "GUTA", 2)
        tasks.append(ordinary_task)
        task_ids.append("task_ordinary")
    group = GroupExecutionState(
        group_id="Dwelling Roof", area_name=None, section_name="Dwelling Roof",
        xactimate_group_name="Dwelling Roof", group_name_reviewed=True, task_ids=task_ids,
    )
    pair = CoordinatedPair(
        pair_id=pair_id, remove_task_id="task_remove", replace_task_id="task_replace",
        pair_state=PAIR_UNACTIVATED, activation_task_id="task_remove",
        expected_minus_quantity=minus_qty, expected_minus_unit=unit,
        expected_plus_quantity=plus_qty, expected_plus_unit=unit,
    )
    plan = ExecutionPlan(
        plan_id="p1", project_slug="test", source_filename=None, created_at="now",
        groups=[group], tasks=tasks, coordinated_pairs=[pair],
    )
    return plan, remove_task, replace_task, pair, ordinary_task


def _dropdown_script_for_pair():
    return {"RFG REM": [_dropdown("RFG", "REM")]}


# ---------------------------------------------------------------------
# Fresh execution
# ---------------------------------------------------------------------


def test_valid_pair_executes_one_search_and_one_activation(tmp_path, phrase_rules, ranking_config):
    plan, remove_task, replace_task, pair, _ = _plan_with_one_pair()
    adapter = _rr_adapter(dropdown_script=_dropdown_script_for_pair())

    run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False)

    search_calls = [c for c in adapter.log.calls if c[0] == "search_by_category_selector"]
    assert search_calls == [("search_by_category_selector", ("RFG", "REM"), {})]
    assert adapter.select_candidate_calls == 1
    assert adapter.bind_calls == 1
    assert adapter.write_pair_calls == 1
    assert adapter.commit_item_calls == 1


def test_remove_source_quantity_goes_to_minus(tmp_path, phrase_rules, ranking_config):
    plan, remove_task, replace_task, pair, _ = _plan_with_one_pair(minus_qty=7.0, plus_qty=9.0)
    adapter = _rr_adapter(dropdown_script=_dropdown_script_for_pair())
    seen = {}
    original = adapter.write_and_verify_rr_pair_quantities

    def spy(pair_target, minus_quantity, plus_quantity, *, on_minus_verified=None):
        seen["minus"] = minus_quantity
        seen["plus"] = plus_quantity
        return original(pair_target, minus_quantity, plus_quantity, on_minus_verified=on_minus_verified)

    adapter.write_and_verify_rr_pair_quantities = spy

    run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False)

    assert seen["minus"] == 7.0
    assert seen["plus"] == 9.0
    assert remove_task.entered_quantity == 7.0
    assert replace_task.entered_quantity == 9.0


def test_differing_quantities_map_correctly_to_each_task(tmp_path, phrase_rules, ranking_config):
    plan, remove_task, replace_task, pair, _ = _plan_with_one_pair(minus_qty=3.0, plus_qty=15.0)
    adapter = _rr_adapter(dropdown_script=_dropdown_script_for_pair())

    run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False)

    assert remove_task.state == TASK_COMPLETED
    assert replace_task.state == TASK_COMPLETED
    assert remove_task.observed_quantity == 3.0
    assert replace_task.observed_quantity == 15.0


def test_equal_quantities_still_verify_both_physical_halves(tmp_path, phrase_rules, ranking_config):
    plan, remove_task, replace_task, pair, _ = _plan_with_one_pair(minus_qty=10.0, plus_qty=10.0)
    adapter = _rr_adapter(dropdown_script=_dropdown_script_for_pair())

    run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False)

    assert adapter.write_pair_calls == 1
    assert pair.minus_written is True
    assert pair.plus_written is True
    assert pair.minus_verified_ok is True
    assert pair.plus_verified_ok is True


def test_both_logical_tasks_close_from_one_coordinated_operation(tmp_path, phrase_rules, ranking_config):
    plan, remove_task, replace_task, pair, _ = _plan_with_one_pair()
    adapter = _rr_adapter(dropdown_script=_dropdown_script_for_pair())

    run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False)

    assert remove_task.state == TASK_COMPLETED
    assert replace_task.state == TASK_COMPLETED
    assert pair.pair_state == PAIR_SATISFIED
    assert adapter.select_candidate_calls == 1
    assert adapter.commit_item_calls == 1


def test_second_member_never_independently_executed(tmp_path, phrase_rules, ranking_config):
    plan, remove_task, replace_task, pair, _ = _plan_with_one_pair()
    adapter = _rr_adapter(dropdown_script=_dropdown_script_for_pair())

    run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False)

    # Exactly one search/select/bind/write/commit for the WHOLE pair --
    # never two independent activations.
    assert adapter.select_candidate_calls == 1
    assert adapter.bind_calls == 1
    assert adapter.write_pair_calls == 1
    assert adapter.commit_item_calls == 1


def test_ordinary_unpaired_tasks_are_unchanged(tmp_path, phrase_rules, ranking_config):
    plan, remove_task, replace_task, pair, ordinary_task = _plan_with_one_pair(extra_ordinary_task=True)
    script = _dropdown_script_for_pair()
    script["SFG GUTA"] = [_dropdown("SFG", "GUTA")]
    adapter = _rr_adapter(dropdown_script=script)

    run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False)

    assert ordinary_task.state == TASK_COMPLETED
    assert ("search_by_category_selector", ("SFG", "GUTA"), {}) in adapter.log.calls


def test_pre_activation_ambiguity_creates_no_physical_checkpoint(tmp_path, phrase_rules, ranking_config):
    """No dropdown candidate at all -> NO_MATCH before any activation."""
    plan, remove_task, replace_task, pair, _ = _plan_with_one_pair()
    adapter = _rr_adapter(dropdown_script={})  # empty candidates -> NO_MATCH

    run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False)

    assert pair.pair_state == PAIR_UNACTIVATED
    assert pair.minus_binding is None
    assert pair.plus_binding is None
    assert adapter.select_candidate_calls == 0
    assert adapter.bind_calls == 0
    for task in (remove_task, replace_task):
        assert task.state == TASK_REVIEW_REQUIRED or task.state.startswith("failed")


def test_dry_run_never_activates_and_leaves_tasks_pending(tmp_path, phrase_rules, ranking_config):
    """Mirrors ordinary tasks' own dry_run contract: search/rank/decide
    only, no adapter selection/activation/commit, and both tasks stay
    PENDING (never marked terminal) -- exactly like an ordinary task's
    own dry_run outcome leaves task.state untouched."""
    plan, remove_task, replace_task, pair, _ = _plan_with_one_pair()
    adapter = _rr_adapter(dropdown_script=_dropdown_script_for_pair())

    run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=True)

    assert adapter.select_candidate_calls == 0
    assert adapter.bind_calls == 0
    assert adapter.write_pair_calls == 0
    assert adapter.commit_item_calls == 0
    assert remove_task.state == TASK_PENDING
    assert replace_task.state == TASK_PENDING
    assert pair.pair_state == PAIR_UNACTIVATED


def test_dry_run_on_a_resumed_pair_never_touches_the_adapter(tmp_path, phrase_rules, ranking_config):
    """A pair already activated in a PRIOR real run must still never
    cause a real adapter call during a dry_run preview -- dry_run's
    contract is "no adapter side effects", resumed or not."""
    plan, remove_task, replace_task, pair, _ = _plan_with_one_pair()
    pair.pair_state = PAIR_BOTH_BOUND
    pair.minus_binding = {"category": "RFG", "selector": "REM", "description": "RFG/REM description", "activity": "-"}
    pair.plus_binding = {"category": "RFG", "selector": "REM", "description": "RFG/REM description", "activity": "+"}
    remove_task.state = TASK_PENDING
    replace_task.state = TASK_PENDING
    save_execution_plan(plan, tmp_path)
    adapter = _rr_adapter(dropdown_script=_dropdown_script_for_pair())

    run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=True)

    assert adapter.select_candidate_calls == 0
    assert adapter.bind_calls == 0
    assert adapter.write_pair_calls == 0
    assert adapter.commit_item_calls == 0
    assert len(adapter.locate_pair_calls) == 0
    assert pair.pair_state == PAIR_BOTH_BOUND
    assert remove_task.state == TASK_PENDING
    assert replace_task.state == TASK_PENDING


def test_malformed_activation_pair_fails_closed(tmp_path, phrase_rules, ranking_config):
    plan, remove_task, replace_task, pair, _ = _plan_with_one_pair()
    adapter = _rr_adapter(dropdown_script=_dropdown_script_for_pair())
    adapter.bind_outcome = "task_local"

    run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False)

    assert pair.pair_state == PAIR_REVIEW_REQUIRED
    assert remove_task.state == TASK_REVIEW_REQUIRED
    assert replace_task.state == TASK_REVIEW_REQUIRED
    assert remove_task.physical_state_uncertain is False
    assert replace_task.physical_state_uncertain is False
    # Task-local -- the run continues normally, never a project-level pause.
    plan_after = load_execution_plan(tmp_path)
    assert plan_after.run_state != RUN_STATE_PAUSED or plan_after.stop_reason_category != STOP_REASON_PROJECT_LEVEL_HARD_STOP


def test_activation_dialog_is_project_level_hard_stop(tmp_path, phrase_rules, ranking_config):
    plan, remove_task, replace_task, pair, _ = _plan_with_one_pair()
    adapter = _rr_adapter(dropdown_script=_dropdown_script_for_pair())
    adapter.bind_outcome = "dialog"

    result_plan = run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False)

    assert pair.pair_state == PAIR_PHYSICAL_STATE_UNCERTAIN
    assert remove_task.physical_state_uncertain is True
    assert replace_task.physical_state_uncertain is True
    assert result_plan.run_state == RUN_STATE_PAUSED
    assert result_plan.stop_reason_category == STOP_REASON_PROJECT_LEVEL_HARD_STOP


def test_activation_physical_state_uncertain_is_project_level_hard_stop(tmp_path, phrase_rules, ranking_config):
    plan, remove_task, replace_task, pair, _ = _plan_with_one_pair()
    adapter = _rr_adapter(dropdown_script=_dropdown_script_for_pair())
    adapter.bind_outcome = "physical_uncertain"

    result_plan = run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False)

    assert pair.pair_state == PAIR_PHYSICAL_STATE_UNCERTAIN
    assert remove_task.physical_state_uncertain is True
    assert replace_task.physical_state_uncertain is True
    assert result_plan.run_state == RUN_STATE_PAUSED
    assert result_plan.stop_reason_category == STOP_REASON_PROJECT_LEVEL_HARD_STOP


def test_activation_adapter_error_is_task_local_not_hard_stop(tmp_path, phrase_rules, ranking_config):
    plan, remove_task, replace_task, pair, _ = _plan_with_one_pair()
    adapter = _rr_adapter(dropdown_script=_dropdown_script_for_pair())
    adapter.bind_outcome = "adapter_error"

    result_plan = run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False)

    assert pair.pair_state == PAIR_REVIEW_REQUIRED
    assert remove_task.physical_state_uncertain is False
    assert replace_task.physical_state_uncertain is False
    assert result_plan.run_state != RUN_STATE_PAUSED or result_plan.stop_reason_category != STOP_REASON_PROJECT_LEVEL_HARD_STOP


def test_commit_item_failure_is_task_local_review(tmp_path, phrase_rules, ranking_config):
    class CommitFailureAdapter(RRPairFakeAdapter):
        def commit_item(self) -> None:
            raise AdapterError("simulated commit_item failure")

    plan, remove_task, replace_task, pair, _ = _plan_with_one_pair()
    adapter = CommitFailureAdapter(dropdown_script=_dropdown_script_for_pair())
    adapter.supports_live_execution = True

    result_plan = run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False)

    assert pair.pair_state == PAIR_REVIEW_REQUIRED
    assert remove_task.state == TASK_REVIEW_REQUIRED
    assert replace_task.state == TASK_REVIEW_REQUIRED
    assert remove_task.physical_state_uncertain is False
    assert result_plan.run_state != RUN_STATE_PAUSED or result_plan.stop_reason_category != STOP_REASON_PROJECT_LEVEL_HARD_STOP


def test_minus_write_failure_does_not_attempt_plus(tmp_path, phrase_rules, ranking_config):
    plan, remove_task, replace_task, pair, _ = _plan_with_one_pair()
    adapter = _rr_adapter(dropdown_script=_dropdown_script_for_pair())
    adapter.minus_write_outcome = "fail"

    run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False)

    assert remove_task.state == TASK_REVIEW_REQUIRED
    assert replace_task.state == TASK_REVIEW_REQUIRED
    assert remove_task.state != TASK_COMPLETED
    assert replace_task.state != TASK_COMPLETED
    assert adapter.commit_item_calls == 0
    assert pair.pair_state == PAIR_REVIEW_REQUIRED
    assert pair.minus_written is False


def test_plus_failure_preserves_verified_minus_checkpoint(tmp_path, phrase_rules, ranking_config):
    plan, remove_task, replace_task, pair, _ = _plan_with_one_pair()
    adapter = _rr_adapter(dropdown_script=_dropdown_script_for_pair())
    adapter.plus_write_outcome = "fail"

    run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False)

    assert pair.minus_written is True
    assert pair.minus_verified_ok is True
    assert pair.plus_written is False
    assert adapter.commit_item_calls == 0
    # Neither task independently marked completed/committed -- nothing
    # was actually saved (commit_item() never reached).
    assert remove_task.state == TASK_REVIEW_REQUIRED
    assert replace_task.state == TASK_REVIEW_REQUIRED
    assert remove_task.commit_state != "committed"


# ---------------------------------------------------------------------
# Crash-safe resume
# ---------------------------------------------------------------------


def test_restart_after_activation_does_not_reactivate(tmp_path, phrase_rules, ranking_config):
    plan, remove_task, replace_task, pair, _ = _plan_with_one_pair()
    pair.pair_state = PAIR_BOTH_BOUND
    pair.minus_binding = {"category": "RFG", "selector": "REM", "description": "RFG/REM description", "activity": "-"}
    pair.plus_binding = {"category": "RFG", "selector": "REM", "description": "RFG/REM description", "activity": "+"}
    save_execution_plan(plan, tmp_path)
    adapter = _rr_adapter(dropdown_script=_dropdown_script_for_pair())

    run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False)

    assert adapter.select_candidate_calls == 0
    assert adapter.bind_calls == 0
    assert len(adapter.locate_pair_calls) == 1
    assert remove_task.state == TASK_COMPLETED
    assert replace_task.state == TASK_COMPLETED
    assert pair.pair_state == PAIR_SATISFIED


def test_restart_after_minus_verification_writes_only_unfinished_plus(tmp_path, phrase_rules, ranking_config):
    plan, remove_task, replace_task, pair, _ = _plan_with_one_pair()
    pair.pair_state = PAIR_MINUS_VERIFIED
    pair.minus_binding = {"category": "RFG", "selector": "REM", "description": "RFG/REM description", "activity": "-"}
    pair.plus_binding = {"category": "RFG", "selector": "REM", "description": "RFG/REM description", "activity": "+"}
    pair.minus_written = True
    pair.minus_verified_ok = True
    save_execution_plan(plan, tmp_path)
    adapter = _rr_adapter(dropdown_script=_dropdown_script_for_pair())

    run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False)

    assert adapter.select_candidate_calls == 0
    assert adapter.bind_calls == 0
    assert adapter.write_pair_calls == 0  # never the dual-write -- only the single resumed plus write
    assert remove_task.state == TASK_COMPLETED
    assert replace_task.state == TASK_COMPLETED
    assert pair.pair_state == PAIR_SATISFIED


def test_restart_after_both_verifications_performs_persistence_only(tmp_path, phrase_rules, ranking_config):
    plan, remove_task, replace_task, pair, _ = _plan_with_one_pair()
    pair.pair_state = PAIR_BOTH_VERIFIED
    pair.minus_binding = {"category": "RFG", "selector": "REM", "description": "RFG/REM description", "activity": "-"}
    pair.plus_binding = {"category": "RFG", "selector": "REM", "description": "RFG/REM description", "activity": "+"}
    pair.minus_written = True
    pair.plus_written = True
    pair.minus_verified_ok = True
    pair.plus_verified_ok = True
    save_execution_plan(plan, tmp_path)
    adapter = _rr_adapter(dropdown_script=_dropdown_script_for_pair())

    run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False)

    assert adapter.select_candidate_calls == 0
    assert adapter.bind_calls == 0
    assert adapter.write_pair_calls == 0
    assert adapter.commit_item_calls == 1
    assert remove_task.state == TASK_COMPLETED
    assert replace_task.state == TASK_COMPLETED
    assert pair.pair_state == PAIR_SATISFIED


def test_missing_persisted_physical_half_on_resume_fails_closed(tmp_path, phrase_rules, ranking_config):
    plan, remove_task, replace_task, pair, _ = _plan_with_one_pair()
    pair.pair_state = PAIR_BOTH_BOUND
    pair.minus_binding = {"category": "RFG", "selector": "REM", "description": "RFG/REM description", "activity": "-"}
    pair.plus_binding = {"category": "RFG", "selector": "REM", "description": "RFG/REM description", "activity": "+"}
    save_execution_plan(plan, tmp_path)
    adapter = _rr_adapter(dropdown_script=_dropdown_script_for_pair())
    adapter.locate_pair_outcome = "missing"

    result_plan = run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False)

    assert adapter.select_candidate_calls == 0
    assert adapter.bind_calls == 0
    assert pair.pair_state == PAIR_PHYSICAL_STATE_UNCERTAIN
    assert remove_task.physical_state_uncertain is True
    assert replace_task.physical_state_uncertain is True
    assert result_plan.run_state == RUN_STATE_PAUSED


def test_unexpected_grid_mutation_propagates_physical_state_uncertain(tmp_path, phrase_rules, ranking_config):
    plan, remove_task, replace_task, pair, _ = _plan_with_one_pair()
    adapter = _rr_adapter(dropdown_script=_dropdown_script_for_pair(), trust_state="VERIFICATION_FAILED")

    result_plan = run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False)

    assert pair.pair_state == PAIR_PHYSICAL_STATE_UNCERTAIN
    assert remove_task.physical_state_uncertain is True
    assert replace_task.physical_state_uncertain is True
    assert result_plan.run_state == RUN_STATE_PAUSED
    assert result_plan.stop_reason_category == STOP_REASON_PROJECT_LEVEL_HARD_STOP
    # Both write/verify calls already happened -- never destructively
    # cleaned up or retried.
    assert adapter.write_pair_calls == 1


def test_pair_state_persists_and_reloads_correctly(tmp_path, phrase_rules, ranking_config):
    plan, remove_task, replace_task, pair, _ = _plan_with_one_pair()
    adapter = _rr_adapter(dropdown_script=_dropdown_script_for_pair())

    run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False)

    reloaded = load_execution_plan(tmp_path)
    reloaded_pair = reloaded.pair_by_id(pair.pair_id)
    assert reloaded_pair.pair_state == PAIR_SATISFIED
    assert reloaded_pair.minus_binding == pair.minus_binding
    assert reloaded_pair.plus_binding == pair.plus_binding
    assert reloaded_pair.minus_verified_ok is True
    assert reloaded_pair.plus_verified_ok is True


# ---------------------------------------------------------------------
# Reset behavior
# ---------------------------------------------------------------------


def test_reset_protects_incomplete_coordinated_pair_with_physical_activity(tmp_path, phrase_rules, ranking_config):
    plan, remove_task, replace_task, pair, _ = _plan_with_one_pair()
    pair.pair_state = PAIR_MINUS_VERIFIED
    pair.minus_binding = {"category": "RFG", "selector": "REM", "description": "RFG/REM description", "activity": "-"}
    pair.plus_binding = {"category": "RFG", "selector": "REM", "description": "RFG/REM description", "activity": "+"}
    pair.minus_written = True
    pair.minus_verified_ok = True
    remove_task.state = TASK_PENDING
    replace_task.state = TASK_PENDING
    save_execution_plan(plan, tmp_path)

    reset_unfinished_tasks(plan, tmp_path)

    assert pair.pair_state == PAIR_MINUS_VERIFIED
    assert pair.minus_binding is not None
    assert remove_task.state == TASK_PENDING
    assert replace_task.state == TASK_PENDING


def test_reset_clears_coordinated_pair_with_no_physical_activity(tmp_path, phrase_rules, ranking_config):
    plan, remove_task, replace_task, pair, _ = _plan_with_one_pair()
    remove_task.state = TASK_REVIEW_REQUIRED
    replace_task.state = TASK_REVIEW_REQUIRED
    remove_task.stop_reason = STOP_REASON_COORDINATED_PAIR_EXECUTION_NOT_IMPLEMENTED
    replace_task.stop_reason = STOP_REASON_COORDINATED_PAIR_EXECUTION_NOT_IMPLEMENTED
    save_execution_plan(plan, tmp_path)

    reset_unfinished_tasks(plan, tmp_path)

    assert pair.pair_state == PAIR_UNACTIVATED
    assert remove_task.state == TASK_PENDING
    assert replace_task.state == TASK_PENDING


def test_completed_coordinated_pair_remains_completed_across_reset(tmp_path, phrase_rules, ranking_config):
    plan, remove_task, replace_task, pair, _ = _plan_with_one_pair()
    adapter = _rr_adapter(dropdown_script=_dropdown_script_for_pair())
    run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False)
    assert pair.pair_state == PAIR_SATISFIED

    reset_unfinished_tasks(plan, tmp_path)

    assert pair.pair_state == PAIR_SATISFIED
    assert remove_task.state == TASK_COMPLETED
    assert replace_task.state == TASK_COMPLETED

    full_reset_count = reset_unfinished_tasks(plan, tmp_path, full_reset=True)
    assert pair.pair_state == PAIR_UNACTIVATED
    assert remove_task.state == TASK_PENDING
    assert replace_task.state == TASK_PENDING
    assert full_reset_count == 2


# ---------------------------------------------------------------------
# Lifecycle ledger
# ---------------------------------------------------------------------


def test_lifecycle_ledger_contains_both_task_ids_and_verification_results(tmp_path, phrase_rules, ranking_config):
    """"Ledger" here is the persisted plan itself (Phase 5.9's existing
    convention: task.commit_state/trust_state/observed_quantity ARE
    the row-lifecycle evidence -- see task_has_committed_row()'s own
    docstring). After a run, the PERSISTED, RELOADED plan must let a
    caller reconstruct: both logical task IDs, both physical
    verification results (independently, per half), and whether this
    run was fresh."""
    class LedgerFakeAdapter(RRPairFakeAdapter):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.events: list[tuple] = []

        def set_execution_context(self, **kwargs):
            self.events.append(("CONTEXT", kwargs))

        def record_lifecycle_event(self, event, **detail):
            self.events.append((event, detail))

    plan, remove_task, replace_task, pair, _ = _plan_with_one_pair(minus_qty=4.0, plus_qty=6.0)
    adapter = LedgerFakeAdapter(dropdown_script=_dropdown_script_for_pair())
    adapter.supports_live_execution = True

    run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False)

    task_ids_seen = {kwargs.get("task_id") for name, kwargs in adapter.events if name == "CONTEXT" and "task_id" in kwargs}
    assert "task_remove" in task_ids_seen

    reloaded = load_execution_plan(tmp_path)
    reloaded_pair = reloaded.pair_by_id(pair.pair_id)
    reloaded_remove = reloaded.task_by_id("task_remove")
    reloaded_replace = reloaded.task_by_id("task_replace")

    # Both logical task IDs.
    assert reloaded_pair.remove_task_id == "task_remove"
    assert reloaded_pair.replace_task_id == "task_replace"
    # Both physical verification results, independently, per half.
    assert reloaded_pair.minus_verified_ok is True
    assert reloaded_pair.plus_verified_ok is True
    assert reloaded_remove.observed_quantity == 4.0
    assert reloaded_replace.observed_quantity == 6.0
    assert reloaded_remove.commit_state == "committed"
    assert reloaded_replace.commit_state == "committed"
    assert reloaded_remove.trust_state == "VERIFIED"
    assert reloaded_replace.trust_state == "VERIFIED"


# ---------------------------------------------------------------------
# Ranking/search behavior byte-for-byte unchanged
# ---------------------------------------------------------------------


def test_ranking_behavior_unaffected_by_coordinated_pair_reuse(phrase_rules, ranking_config):
    """rank_dropdown_results() itself is called through the EXACT same
    orchestrator._search_rank_and_decide() helper for both an ordinary
    task and a coordinated pair's activation task -- there is no
    second, forked ranking path, so the same inputs produce the same
    ranked output regardless of caller."""
    dropdowns = [_dropdown("RFG", "REM")]
    candidates_direct = rank_dropdown_results(
        original_description="RFG/REM description", trade=None, component=None, material=None, action=None,
        unit="SQ", size_key=None, grade_key=None, dropdowns=dropdowns, rules=phrase_rules, config=ranking_config,
    )
    assert len(candidates_direct) == 1
    assert candidates_direct[0].dropdown.category == "RFG"
    assert candidates_direct[0].dropdown.selector == "REM"


# ---------------------------------------------------------------------
# Phase 5.24 Part A: a restricted execution plan must checkpoint/resume
# independently of, and never overwrite/shrink/corrupt, the canonical
# project-wide plan -- see execution_plan.restricted_plan_path()'s own
# tests for the pure save/load-level proof; this exercises the SAME
# mechanism end-to-end through run_execution_plan() with a real
# coordinated-pair run.
# ---------------------------------------------------------------------


def test_restricted_execution_does_not_alter_canonical_plan(tmp_path, phrase_rules, ranking_config):
    canonical = ExecutionPlan(
        plan_id="canonical", project_slug="test", source_filename=None, created_at="now",
        groups=[GroupExecutionState(
            group_id="Other", area_name=None, section_name="Other", xactimate_group_name="Other",
            group_name_reviewed=True, task_ids=[f"other_{i}" for i in range(35)],
        )],
        tasks=[
            _task(f"other_{i}", f"line_other_{i}", "Other", "XXX", "YYY", i, qty=1.0)
            for i in range(35)
        ],
    )
    for t in canonical.tasks:
        t.state = TASK_COMPLETED
    save_execution_plan(canonical, tmp_path)

    plan, remove_task, replace_task, pair, _ = _plan_with_one_pair()
    path = restricted_plan_path(tmp_path, "six-task-validation")
    adapter = _rr_adapter(dropdown_script=_dropdown_script_for_pair())

    run_execution_plan(
        plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False, plan_path=path,
    )

    assert remove_task.state == TASK_COMPLETED
    assert replace_task.state == TASK_COMPLETED

    canonical_reloaded = load_execution_plan(tmp_path)
    assert canonical_reloaded.plan_id == "canonical"
    assert len(canonical_reloaded.tasks) == 35
    assert all(t.state == TASK_COMPLETED for t in canonical_reloaded.tasks)

    restricted_reloaded = load_execution_plan(tmp_path, plan_path=path)
    assert restricted_reloaded.plan_id == "p1"
    assert len(restricted_reloaded.tasks) == 2


def test_restricted_execution_would_have_raised_without_plan_path(tmp_path, phrase_rules, ranking_config):
    """Proves the blocker this whole mechanism fixes was real: the
    SAME 2-task plan, saved WITHOUT plan_path against a pre-existing
    larger canonical plan, raises ExecutionPlanOverwriteRefused."""
    canonical = ExecutionPlan(
        plan_id="canonical", project_slug="test", source_filename=None, created_at="now",
        tasks=[_task(f"other_{i}", f"line_other_{i}", "Other", "XXX", "YYY", i) for i in range(35)],
    )
    save_execution_plan(canonical, tmp_path)

    plan, _remove_task, _replace_task, _pair, _ = _plan_with_one_pair()
    with pytest.raises(ExecutionPlanOverwriteRefused):
        save_execution_plan(plan, tmp_path)


def test_restricted_execution_checkpoints_and_resumes(tmp_path, phrase_rules, ranking_config):
    plan, remove_task, replace_task, pair, _ = _plan_with_one_pair()
    path = restricted_plan_path(tmp_path, "resume-validation")
    adapter = _rr_adapter(dropdown_script=_dropdown_script_for_pair())
    adapter.plus_write_outcome = "fail"  # stop mid-way, leaving a real checkpoint

    run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False, plan_path=path)
    assert pair.minus_written is True
    assert pair.plus_written is False
    assert pair.pair_state == PAIR_REVIEW_REQUIRED

    reloaded = load_execution_plan(tmp_path, plan_path=path)
    assert reloaded is not None
    reloaded_pair = reloaded.coordinated_pairs[0]
    assert reloaded_pair.minus_written is True
    assert reloaded_pair.minus_binding is not None


def test_normal_full_plan_persistence_is_unchanged_by_plan_path_addition(tmp_path, phrase_rules, ranking_config):
    """Omitting plan_path entirely (every pre-existing caller) behaves
    exactly as before this addition -- persists to the canonical path."""
    plan, remove_task, replace_task, pair, _ = _plan_with_one_pair()
    adapter = _rr_adapter(dropdown_script=_dropdown_script_for_pair())

    run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False)

    reloaded = load_execution_plan(tmp_path)
    assert reloaded is not None
    assert reloaded.plan_id == "p1"
    assert reloaded.pair_by_id(pair.pair_id).pair_state == PAIR_SATISFIED


# ---------------------------------------------------------------------
# Phase 5.24 Part B: a coordinated pair's activation task, when it is
# LOOKUP_STRATEGY_TEST_DESCRIPTION_FIRST, must get the SAME bounded,
# multi-attempt description-first search policy an ordinary unmapped
# task gets (execution_runner._bounded_description_first_decision(),
# shared with _run_description_first_task()) -- never the single,
# direct search Stage 4 originally fell back to. Exactly one physical
# candidate activation must still happen per pair, regardless of how
# many search attempts ran first.
# ---------------------------------------------------------------------


def _matching_dropdown(task, cat="RFG", sel="3TAB"):
    """A single, unambiguous dropdown result that reliably scores
    AUTO_SELECT against `task`'s own original description -- mirrors
    test_execution_runner.py's own `_felt_dropdown()`/`_FELT_FULL_
    DESCRIPTION` precedent: an exact description echo is what
    rank_dropdown_results() needs to score confidently, independent
    of any real catalog data."""
    return DropdownResult(
        raw_text=f"{cat} {sel}", row_position=0, category=cat, selector=sel,
        description=task.description, extraction_confidence=1.0,
    )


def _unmapped_task(
    task_id, line_item_id, section_name, source_order, description, *,
    qty=10.0, unit="SQ", action="remove", trade="roofing", component="composition_shingles", material="3-tab",
):
    return ExecutionTask(
        task_id=task_id, line_item_id=line_item_id, source_order=source_order,
        area_name=None, section_name=section_name, description=description,
        category=None, selector=None, lookup_strategy=LOOKUP_STRATEGY_TEST_DESCRIPTION_FIRST,
        source_quantity=qty, source_unit=unit, expected_unit=unit,
        began_unmapped=True, normalized_action=action, normalized_trade=trade,
        normalized_component=component, normalized_material=material,
    )


def _plan_with_one_unmapped_pair(*, minus_qty=10.0, plus_qty=12.0, unit="SQ"):
    remove_task = _unmapped_task(
        "task_remove", "line_remove", "Dwelling Roof", 0, "Remove old shingle roofing",
        qty=minus_qty, unit=unit, action="remove",
    )
    replace_task = _unmapped_task(
        "task_replace", "line_replace", "Dwelling Roof", 1, "New shingle roofing installed",
        qty=plus_qty, unit=unit, action="unknown",
    )
    pair_id = "pair_task_remove_task_replace"
    remove_task.coordinated_pair_id = pair_id
    replace_task.coordinated_pair_id = pair_id
    group = GroupExecutionState(
        group_id="Dwelling Roof", area_name=None, section_name="Dwelling Roof",
        xactimate_group_name="Dwelling Roof", group_name_reviewed=True,
        task_ids=["task_remove", "task_replace"],
    )
    pair = CoordinatedPair(
        pair_id=pair_id, remove_task_id="task_remove", replace_task_id="task_replace",
        pair_state=PAIR_UNACTIVATED, activation_task_id="task_remove",
        expected_minus_quantity=minus_qty, expected_minus_unit=unit,
        expected_plus_quantity=plus_qty, expected_plus_unit=unit,
    )
    plan = ExecutionPlan(
        plan_id="p1", project_slug="test", source_filename=None, created_at="now",
        groups=[group], tasks=[remove_task, replace_task], coordinated_pairs=[pair],
    )
    return plan, remove_task, replace_task, pair


def test_paired_description_first_task_gets_bounded_multi_attempt_search(tmp_path, phrase_rules, ranking_config):
    """First attempt (the exact source description) yields a clean
    NO_MATCH; the bounded sequence must advance to a later attempt --
    the SAME policy _run_description_first_task() gives an ordinary
    unmapped task, proven here by reusing _description_first_search_
    attempts() directly to discover the real fallback text."""
    plan, remove_task, replace_task, pair = _plan_with_one_unmapped_pair()
    attempts = _description_first_search_attempts(remove_task, phrase_rules, tmp_path)
    assert len(attempts) >= 2, "fixture must produce at least 2 distinct search attempts"
    fallback_text = attempts[1][1].search_input

    adapter = _rr_adapter(dropdown_script={
        remove_task.description: [],  # attempt 1: clean retrieval failure
        fallback_text: [_matching_dropdown(remove_task)],  # attempt 2: succeeds
    })

    run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False)

    assert len(remove_task.search_attempts) == 2
    assert remove_task.search_attempts[0]["advanced_to_next_attempt"] is True
    assert remove_task.search_attempts[1]["search_text"] == fallback_text
    assert remove_task.state == TASK_COMPLETED
    assert replace_task.state == TASK_COMPLETED


def test_failed_search_attempts_cause_zero_physical_activation(tmp_path, phrase_rules, ranking_config):
    plan, remove_task, replace_task, pair = _plan_with_one_unmapped_pair()
    adapter = _rr_adapter(dropdown_script={})  # every attempt returns empty -> NO_MATCH throughout

    run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False)

    assert adapter.select_candidate_calls == 0
    assert adapter.bind_calls == 0
    assert adapter.write_pair_calls == 0
    assert adapter.commit_item_calls == 0
    assert pair.pair_state == PAIR_UNACTIVATED
    assert pair.minus_binding is None
    assert len(remove_task.search_attempts) >= 1


def test_successful_pair_execution_after_multiple_attempts_still_activates_once(tmp_path, phrase_rules, ranking_config):
    plan, remove_task, replace_task, pair = _plan_with_one_unmapped_pair()
    attempts = _description_first_search_attempts(remove_task, phrase_rules, tmp_path)
    fallback_text = attempts[1][1].search_input
    adapter = _rr_adapter(dropdown_script={
        remove_task.description: [],
        fallback_text: [_matching_dropdown(remove_task)],
    })

    run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False)

    assert len(remove_task.search_attempts) == 2  # two search ATTEMPTS
    assert adapter.select_candidate_calls == 1  # exactly one ACTIVATION
    assert adapter.bind_calls == 1
    assert adapter.write_pair_calls == 1
    assert adapter.commit_item_calls == 1
    assert pair.pair_state == PAIR_SATISFIED


def test_partner_task_never_independently_executes_with_description_first_pair(tmp_path, phrase_rules, ranking_config):
    plan, remove_task, replace_task, pair = _plan_with_one_unmapped_pair()
    attempts = _description_first_search_attempts(remove_task, phrase_rules, tmp_path)
    fallback_text = attempts[1][1].search_input
    adapter = _rr_adapter(dropdown_script={
        remove_task.description: [],
        fallback_text: [_matching_dropdown(remove_task)],
    })

    run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False)

    searched_texts = [call[1][0] for call in adapter.log.calls if call[0] == "search_by_description"]
    assert replace_task.description not in searched_texts
    assert replace_task.actual_lookup_strategy is None
    assert replace_task.state == TASK_COMPLETED  # closed via the pair, never independently


def test_pair_resume_protection_after_description_first_activation(tmp_path, phrase_rules, ranking_config):
    """Mirrors test_restart_after_activation_does_not_reactivate() but
    for a pair whose activation task is description-first -- resume
    must still never re-search or re-activate."""
    plan, remove_task, replace_task, pair = _plan_with_one_unmapped_pair()
    pair.pair_state = PAIR_BOTH_BOUND
    pair.minus_binding = {"category": "RFG", "selector": "3TAB", "description": "RFG/3TAB description", "activity": "-"}
    pair.plus_binding = {"category": "RFG", "selector": "3TAB", "description": "RFG/3TAB description", "activity": "+"}
    save_execution_plan(plan, tmp_path)
    adapter = _rr_adapter(dropdown_script={remove_task.description: [_dropdown("RFG", "3TAB")]})

    run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False)

    assert adapter.select_candidate_calls == 0
    assert adapter.bind_calls == 0
    assert len(adapter.locate_pair_calls) == 1
    assert remove_task.state == TASK_COMPLETED
    assert replace_task.state == TASK_COMPLETED
    assert pair.pair_state == PAIR_SATISFIED
    assert remove_task.search_attempts == []  # resume never re-enters the search-attempt loop at all


def test_bounded_description_first_decision_reuses_same_attempt_list_as_ordinary_tasks(tmp_path, phrase_rules, ranking_config):
    """Direct, unit-level proof that the pair path and the ordinary
    unmapped-task path build search attempts through the exact same,
    unforked function."""
    plan, remove_task, replace_task, pair = _plan_with_one_unmapped_pair()
    adapter = _rr_adapter(dropdown_script={})

    outcome, actual_strategy, reason = _bounded_description_first_decision(
        remove_task, _task_to_recommendation_input_for_test(remove_task), adapter, ranking_config, phrase_rules,
        tmp_path, False,
    )

    direct_attempts = _description_first_search_attempts(remove_task, phrase_rules, tmp_path)
    assert len(remove_task.search_attempts) == len(direct_attempts)
    assert outcome.committed is False  # decide-only -- never activates


def _task_to_recommendation_input_for_test(task):
    from estimate_extractor.xactimate_lookup.models import RecommendationInput
    return RecommendationInput(
        line_item_id=task.line_item_id, original_description=task.description,
        quantity=task.source_quantity, source_unit=task.source_unit,
        action=task.normalized_action, trade=task.normalized_trade,
        component=task.normalized_component, material=task.normalized_material,
    )


def test_unsafe_lookup_routing_from_description_first_branch_fails_both_tasks_not_the_run(
    tmp_path, phrase_rules, ranking_config,
):
    """_bounded_description_first_decision() raises UnsafeLookupRouting
    when the activation task's description is empty -- this must be a
    safe, task-level failure for BOTH pair members (never a whole-run
    crash), exactly like the ordinary per-task loop's own identical
    guard around _run_description_first_task()."""
    plan, remove_task, replace_task, pair = _plan_with_one_unmapped_pair()
    remove_task.description = "   "  # empty after strip() -> no attempts can be built
    adapter = _rr_adapter(dropdown_script={})

    result_plan = run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False)

    assert remove_task.state == TASK_FAILED
    assert replace_task.state == TASK_FAILED
    assert adapter.select_candidate_calls == 0
    assert result_plan.run_state != RUN_STATE_PAUSED or result_plan.stop_reason_category != STOP_REASON_PROJECT_LEVEL_HARD_STOP
