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

from estimate_extractor.xactimate_lookup.adapter import AdapterError, FakeXactimateAdapter, ProtectedCommittedRowError
from estimate_extractor.xactimate_lookup.execution_plan import (
    CURRENT_SCHEMA_VERSION,
    ExecutionPlan,
    ExecutionTask,
    GROUP_COMPLETED,
    GROUP_FAILED,
    GROUP_PENDING,
    GROUP_VERIFIED,
    GroupExecutionState,
    LOOKUP_STRATEGY_REVIEW_APPROVED,
    LOOKUP_STRATEGY_TEST_DESCRIPTION_FIRST,
    RUN_STATE_COMPLETED,
    RUN_STATE_PAUSED,
    STOP_REASON_NORMAL_COMPLETION,
    STOP_REASON_PROTECTED_ROW_REFUSAL,
    TASK_COMPLETED,
    TASK_FAILED,
    TASK_PENDING,
    TASK_REVIEW_REQUIRED,
    TASK_SKIPPED,
    is_plan_stale,
    load_execution_plan,
    save_execution_plan,
)
from estimate_extractor.xactimate_lookup.execution_runner import (
    OBSERVED_MAPPING_STATE,
    UnsafeLookupRouting,
    _observed_mappings_path,
    _task_to_lookup_plan,
    run_execution_plan,
    skip_task,
)
from estimate_extractor.xactimate_lookup.models import (
    LOOKUP_PATH_DESCRIPTION_SEARCH,
    LOOKUP_PATH_TRUSTED,
    DropdownResult,
    PopulatedFields,
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

    def __init__(self, *args, verified_groups=None, trust_state="VERIFIED", raise_on_group=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.verified_groups = verified_groups if verified_groups is not None else None  # None = verify everything
        self.trust_state = trust_state
        self.raise_on_group = raise_on_group or set()
        self.ensure_group_calls: list[str] = []
        self.ensure_group_parent_calls: list[str | None] = []
        self.select_group_calls: list[str] = []
        self.verify_group_calls: list[str] = []
        self.snapshot_calls = 0
        self.verify_commit_calls = 0

    def ensure_group(self, name: str, *, parent_group_name: str | None = None) -> None:
        if name in self.raise_on_group:
            raise AdapterError(f"Simulated failure creating group {name!r}.")
        self.ensure_group_calls.append(name)
        self.ensure_group_parent_calls.append(parent_group_name)

    def select_group(self, name: str) -> None:
        self.select_group_calls.append(name)

    def verify_group(self, name: str) -> bool:
        self.verify_group_calls.append(name)
        if self.verified_groups is None:
            return True
        return name in self.verified_groups

    def snapshot_grid_identities(self):
        self.snapshot_calls += 1
        return [("EXISTING", "ROW")]

    def verify_commit(self, before_snapshot, category, selector, expected_quantity, *, source_unit=None, expected_xactimate_unit=None):
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

_FELT_PHRASE = "roofing felt"  # what generate_search_phrase() produces for the description/context below


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
        _FELT_PHRASE: [
            DropdownResult(raw_text="Felt A", row_position=0, category="RFG", selector="FELT15", description="Roofing felt - 15 lb.", extraction_confidence=1.0),
            DropdownResult(raw_text="Felt B", row_position=1, category="RFG", selector="FELT30", description="Roofing felt - 30 lb.", extraction_confidence=1.0),
        ],
    }
    adapter = _adapter_with_test_project(dropdown_script=script)

    result = run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False)

    assert result.task_by_id("task_unmapped").state == TASK_REVIEW_REQUIRED
    assert result.task_by_id("task_unmapped").stop_reason == "ambiguous_candidates"
    # never selected/committed anything for the ambiguous task
    select_calls = [c for c in adapter.log.calls if c[0] == "select_candidate"]
    assert len(select_calls) == 1  # only task_mapped's unambiguous selection


def test_observed_cat_sel_recorded_after_successful_unmapped_commit(tmp_path, phrase_rules, ranking_config):
    """Requirement 11: observed CAT/SEL (and description/activity) are
    recorded on the task after a successful commit of an
    originally-unmapped row, and a proposal is written to the separate
    observed_mappings.json file."""
    plan = _plan_mapped_and_unmapped()
    script = {"RFG 3TAB": [_dropdown("RFG", "3TAB")], _FELT_PHRASE: [_felt_dropdown()]}
    adapter = _adapter_with_test_project(dropdown_script=script)
    adapter.read_populated_fields = lambda: PopulatedFields(
        category="RFG", selector="FELT15", description="Roofing felt - 15 lb.", unit="SQ", action="install", item_number=None
    )

    result = run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False)

    task = result.task_by_id("task_unmapped")
    assert task.state == TASK_COMPLETED
    assert task.observed_category == "RFG"
    assert task.observed_selector == "FELT15"
    assert task.observed_description == "Roofing felt - 15 lb."
    assert task.observed_activity == "install"
    # the mapped task never gets these populated -- unchanged behavior
    assert result.task_by_id("task_mapped").observed_category is None

    proposals_path = _observed_mappings_path(tmp_path)
    assert proposals_path.exists()
    proposals = json.loads(proposals_path.read_text(encoding="utf-8"))
    assert "line_unmapped" in proposals
    proposal = proposals["line_unmapped"]
    assert proposal["observed_category"] == "RFG"
    assert proposal["observed_selector"] == "FELT15"
    assert proposal["search_phrase"] == _FELT_PHRASE
    assert "line_mapped" not in proposals  # never written for the normal CAT/SEL path


def test_observed_mapping_proposal_uses_a_distinct_non_approved_state(tmp_path, phrase_rules, ranking_config):
    """Requirement 12: the observed proposal is never labeled
    human-approved -- it carries its own, clearly distinct state."""
    plan = _plan_mapped_and_unmapped()
    script = {"RFG 3TAB": [_dropdown("RFG", "3TAB")], _FELT_PHRASE: [_felt_dropdown()]}
    adapter = _adapter_with_test_project(dropdown_script=script)

    run_execution_plan(plan, adapter, ranking_config, phrase_rules, tmp_path, dry_run=False)

    proposals = json.loads(_observed_mappings_path(tmp_path).read_text(encoding="utf-8"))
    assert proposals["line_unmapped"]["state"] == OBSERVED_MAPPING_STATE
    assert OBSERVED_MAPPING_STATE != "approved"
    assert "approved" not in OBSERVED_MAPPING_STATE.lower().split("_from_")[0]


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
    script = {"RFG 3TAB": [_dropdown("RFG", "3TAB")], _FELT_PHRASE: [_felt_dropdown()]}
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
    script = {"RFG 3TAB": [_dropdown("RFG", "3TAB")], _FELT_PHRASE: [_felt_dropdown()]}
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
    script = {"RFG 3TAB": [_dropdown("RFG", "3TAB")], _FELT_PHRASE: [_felt_dropdown()]}
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
