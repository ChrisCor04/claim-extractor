"""Unit tests for the group-aware, resumable execution runner (Phase 5.0
Priorities 5/6/7/8). Uses a FakeXactimateAdapter extended with the
duck-typed group (ensure_group/select_group/verify_group) and commit-
verification (snapshot_grid_identities/verify_commit) hooks, exactly as a
real WindowsXactimateAdapter would expose them, so the runner's group and
verification logic is exercised without any real Windows/Xactimate
session."""

from __future__ import annotations

from estimate_extractor.xactimate_lookup.adapter import AdapterError, FakeXactimateAdapter
from estimate_extractor.xactimate_lookup.execution_plan import (
    ExecutionPlan,
    ExecutionTask,
    GROUP_COMPLETED,
    GROUP_FAILED,
    GROUP_VERIFIED,
    GroupExecutionState,
    LOOKUP_STRATEGY_REVIEW_APPROVED,
    RUN_STATE_COMPLETED,
    RUN_STATE_PAUSED,
    TASK_COMPLETED,
    TASK_FAILED,
    TASK_PENDING,
    TASK_REVIEW_REQUIRED,
    TASK_SKIPPED,
    load_execution_plan,
    save_execution_plan,
)
from estimate_extractor.xactimate_lookup.execution_runner import run_execution_plan, skip_task
from estimate_extractor.xactimate_lookup.models import DropdownResult


class _FakeCommitVerification:
    def __init__(self, trust_state, quantity_observed=None, unit_observed=None):
        self.trust_state = trust_state
        self.quantity_observed = quantity_observed
        self.unit = _FakeUnitResult(unit_observed) if unit_observed else None


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
        self.select_group_calls: list[str] = []
        self.verify_group_calls: list[str] = []
        self.snapshot_calls = 0
        self.verify_commit_calls = 0

    def ensure_group(self, name: str) -> None:
        if name in self.raise_on_group:
            raise AdapterError(f"Simulated failure creating group {name!r}.")
        self.ensure_group_calls.append(name)

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
        return _FakeCommitVerification(self.trust_state, quantity_observed=expected_quantity, unit_observed=expected_xactimate_unit)


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
        def ensure_group(self, name):
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
    assert task2.state == TASK_COMPLETED  # same group, later task, still ran
    assert task3.state == TASK_COMPLETED  # different group, unaffected


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
