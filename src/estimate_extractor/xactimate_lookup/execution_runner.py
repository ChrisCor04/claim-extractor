"""Group-aware, resumable execution of an ExecutionPlan against a real (or
fake) XactimateAdapter (Phase 5.0 Priorities 5/6/7/8).

Reuses ``orchestrator.execute_plan()`` UNCHANGED for the actual search/
select/enter-quantity/commit/verify sequence -- every ``ExecutionTask``'s
category/selector is a human-approved value (see execution_plan.py), so
it's wrapped as a transient trusted ``LookupPlan`` and handed to the exact
same orchestration code every other lookup path already uses. This module
adds the layer that didn't exist: GROUP handling (a group must be ensured
to exist, selected, and independently VERIFIED before any task inside it
runs -- never inferred, never "whatever's currently active"), persisted
resumability (the plan is saved after every single task, so a crash or
deliberate pause loses at most the one in-flight task), and continue-on-
error semantics (one bad task or group never aborts the whole run; only a
failure to verify the application/project itself does, since nothing
downstream can be trusted at that point).

Group operations (``ensure_group``/``select_group``/``verify_group``) are
duck-typed exactly like ``verify_commit()`` in orchestrator.py: an adapter
that doesn't implement them (the Fake adapter, or any future non-Windows
adapter) never gets a task silently executed against an unverified group --
every task in that group is marked REVIEW_REQUIRED instead. See
docs/build-estimate.md.
"""

from __future__ import annotations

from pathlib import Path

from estimate_extractor.xactimate_lookup import orchestrator
from estimate_extractor.xactimate_lookup.adapter import AdapterError, XactimateAdapter
from estimate_extractor.xactimate_lookup.execution_plan import (
    ExecutionPlan,
    ExecutionTask,
    GROUP_COMPLETED,
    GROUP_FAILED,
    GROUP_IN_PROGRESS,
    GROUP_PENDING,
    GROUP_SELECTED,
    GROUP_VERIFIED,
    GroupExecutionState,
    RUN_STATE_COMPLETED,
    RUN_STATE_IN_PROGRESS,
    RUN_STATE_PAUSED,
    TASK_COMPLETED,
    TASK_FAILED,
    TASK_PENDING,
    TASK_REVIEW_REQUIRED,
    TASK_SKIPPED,
    save_execution_plan,
    utc_now_iso,
)
from estimate_extractor.xactimate_lookup.models import (
    DECISION_AUTO_SELECT,
    DECISION_NO_MATCH,
    InternalMappingRecord,
    LOOKUP_PATH_TRUSTED,
    LookupPlan,
    MAPPING_STATUS_APPROVED,
    RecommendationInput,
)
from estimate_extractor.xactimate_lookup.phrase_generator import PhraseRules
from estimate_extractor.xactimate_lookup.ranking import RankingConfig

# Trust states verify_commit() (Phase 4.8) can return that still count as a
# safely-completed commit -- ONLY "VERIFIED" does. Everything else means
# the adapter could not independently confirm the row, so the task is
# routed to human review rather than silently marked complete (Priority
# 8: "never claim success without evidence").
_VERIFIED_TRUST_STATE = "VERIFIED"


def _supports_group_operations(adapter) -> bool:
    return hasattr(adapter, "ensure_group") and hasattr(adapter, "select_group") and hasattr(adapter, "verify_group")


def _supports_commit_verification(adapter) -> bool:
    return hasattr(adapter, "snapshot_grid_identities") and hasattr(adapter, "verify_commit")


def _task_to_lookup_plan(task: ExecutionTask) -> LookupPlan:
    """Every ExecutionTask carries a human-approved CAT/SEL (see
    execution_plan.py's LOOKUP_STRATEGY_REVIEW_APPROVED) -- wrapped as a
    transient InternalMappingRecord (never persisted to the registry,
    exactly like orchestrator._verified_catalog_trusted_mapping() does for
    verified-catalog matches) so it flows through the SAME trusted-path
    code in orchestrator.execute_plan() as a registry-sourced mapping."""
    trusted = InternalMappingRecord(
        mapping_id=f"execution_plan:{task.task_id}",
        item_signature="",
        source_description=task.description,
        search_phrase="",
        category=task.category,
        selector=task.selector,
        xactimate_description=task.description,
        unit=task.expected_unit,
        action=None,
        reviewer="",
        approval_reason="Human-approved during Mapping Review, carried into the execution plan.",
        status=MAPPING_STATUS_APPROVED,
    )
    return LookupPlan(
        line_item_id=task.line_item_id,
        path=LOOKUP_PATH_TRUSTED,
        item_signature="",
        search_input=f"{task.category} {task.selector}",
        trusted_mapping=trusted,
    )


def _task_to_recommendation_input(task: ExecutionTask) -> RecommendationInput:
    return RecommendationInput(
        line_item_id=task.line_item_id,
        original_description=task.description,
        quantity=task.entered_quantity if task.entered_quantity is not None else task.source_quantity,
        source_unit=task.source_unit,
    )


def _ensure_select_verify_group(adapter, group: GroupExecutionState) -> tuple[bool, str | None]:
    """Returns (verified, error_detail). Never raises -- a group that
    can't be verified means every task inside it is marked
    REVIEW_REQUIRED, not that the whole run stops (Priority 8)."""
    if not _supports_group_operations(adapter):
        return False, "Adapter does not support group operations (ensure_group/select_group/verify_group)."

    target = group.xactimate_group_name or group.section_name
    if not target:
        return False, "No resolvable Xactimate group name for this section."

    try:
        adapter.ensure_group(target)
        group.state = GROUP_SELECTED
        adapter.select_group(target)
        verified = adapter.verify_group(target)
    except AdapterError as exc:
        return False, str(exc)

    if not verified:
        return False, f"verify_group({target!r}) returned False -- refusing to trust the active group."
    group.state = GROUP_VERIFIED
    return True, None


def _apply_outcome_to_task(task: ExecutionTask, outcome, dry_run: bool) -> None:
    task.attempts += 1
    task.completed_at = utc_now_iso()
    task.stop_reason = outcome.stop_reason
    task.stop_detail = outcome.stop_detail
    task.evidence_path = outcome.evidence_reference

    if dry_run:
        # Nothing was actually executed -- record what WOULD happen
        # without claiming any state transition occurred.
        task.stop_detail = outcome.stop_detail or f"dry_run: decision={outcome.decision}"
        return

    if not outcome.committed:
        task.state = TASK_FAILED if outcome.decision == DECISION_NO_MATCH else TASK_REVIEW_REQUIRED
        return

    verification = outcome.verification
    if verification is None:
        # Committed, but the adapter cannot independently confirm it.
        # Never claim success without evidence -- route to review.
        task.state = TASK_REVIEW_REQUIRED
        task.stop_detail = (task.stop_detail or "") + " Committed, but the adapter does not support commit verification."
        return

    task.trust_state = getattr(verification, "trust_state", None)
    task.observed_quantity = getattr(verification, "quantity_observed", None)
    observed_unit = getattr(verification, "unit", None)
    task.observed_unit = getattr(observed_unit, "observed_xactimate_unit", None) if observed_unit is not None else None
    task.entered_quantity = task.entered_quantity if task.entered_quantity is not None else task.source_quantity

    if task.trust_state == _VERIFIED_TRUST_STATE:
        task.state = TASK_COMPLETED
    else:
        task.state = TASK_REVIEW_REQUIRED


def run_execution_plan(
    plan: ExecutionPlan,
    adapter: XactimateAdapter,
    ranking_config: RankingConfig,
    phrase_rules: PhraseRules,
    project_dir: Path,
    *,
    dry_run: bool = True,
) -> ExecutionPlan:
    """Executes every PENDING task in `plan`, group by group, in source
    order, persisting progress after each task so an interrupted run can
    be resumed later (Priority 6) by calling this again with the SAME
    plan (reloaded via execution_plan.load_execution_plan()). Never
    raises on a task- or group-level failure -- see module docstring for
    exactly what stops the whole run versus one task."""
    if not adapter.verify_application():
        plan.run_state = RUN_STATE_PAUSED
        save_execution_plan(plan, project_dir)
        return plan
    if not adapter.verify_project():
        plan.run_state = RUN_STATE_PAUSED
        save_execution_plan(plan, project_dir)
        return plan

    plan.run_state = RUN_STATE_IN_PROGRESS
    tasks_by_id = {t.task_id: t for t in plan.tasks}

    for group in plan.groups:
        group_tasks = [tasks_by_id[tid] for tid in group.task_ids if tid in tasks_by_id]
        pending_tasks = [t for t in group_tasks if t.state == TASK_PENDING]
        if not pending_tasks:
            continue

        if not dry_run:
            # Re-verify the application/project are still in a sane state
            # before EACH group -- if Xactimate crashed or the wrong
            # project got activated mid-run, stop the whole run rather
            # than risk executing against the wrong estimate.
            if not (adapter.verify_application() and adapter.verify_project()):
                plan.run_state = RUN_STATE_PAUSED
                save_execution_plan(plan, project_dir)
                return plan

            group.state = GROUP_IN_PROGRESS
            verified, detail = _ensure_select_verify_group(adapter, group)
            if not verified:
                group.state = GROUP_FAILED
                group.error = detail
                for task in pending_tasks:
                    task.state = TASK_REVIEW_REQUIRED
                    task.stop_detail = f"Group not verified: {detail}"
                    task.completed_at = utc_now_iso()
                save_execution_plan(plan, project_dir)
                continue

        for task in pending_tasks:
            task.started_at = task.started_at or utc_now_iso()
            item = _task_to_recommendation_input(task)
            lookup_plan = _task_to_lookup_plan(task)
            try:
                outcome = orchestrator.execute_plan(lookup_plan, item, adapter, ranking_config, phrase_rules, dry_run=dry_run)
                _apply_outcome_to_task(task, outcome, dry_run)
            except Exception as exc:  # noqa: BLE001 -- one task's unexpected failure must never abort the run
                task.state = TASK_FAILED
                task.error = repr(exc)
                task.completed_at = utc_now_iso()
                try:
                    adapter.recover()
                except Exception:
                    pass

            if not dry_run:
                plan.resume_cursor = plan.tasks.index(task) + 1
                save_execution_plan(plan, project_dir)

        if not dry_run:
            group.state = GROUP_COMPLETED if all(t.state != TASK_PENDING for t in group_tasks) else group.state

    if not dry_run:
        plan.run_state = RUN_STATE_COMPLETED if all(t.state != TASK_PENDING for t in plan.tasks) else RUN_STATE_PAUSED
        save_execution_plan(plan, project_dir)

    return plan


def skip_task(plan: ExecutionPlan, task_id: str, reason: str, project_dir: Path) -> ExecutionTask:
    """Marks one task SKIPPED without executing it -- e.g. a reviewer
    decides mid-run a specific approved item should not actually be sent
    to Xactimate this pass. Persists immediately."""
    task = plan.task_by_id(task_id)
    if task is None:
        raise ValueError(f"Unknown task_id {task_id!r}.")
    task.state = TASK_SKIPPED
    task.stop_detail = reason
    task.completed_at = utc_now_iso()
    save_execution_plan(plan, project_dir)
    return task
