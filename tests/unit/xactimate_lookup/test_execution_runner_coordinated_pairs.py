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
    ExecutionTask,
    GroupExecutionState,
    LOOKUP_STRATEGY_REVIEW_APPROVED,
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
    TASK_PENDING,
    TASK_REVIEW_REQUIRED,
    load_execution_plan,
    reset_unfinished_tasks,
    save_execution_plan,
)
from estimate_extractor.xactimate_lookup.execution_runner import run_execution_plan
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
