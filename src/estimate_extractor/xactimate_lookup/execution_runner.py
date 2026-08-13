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

Phase 5.5: a task built with ``LOOKUP_STRATEGY_TEST_DESCRIPTION_FIRST``
(see execution_plan.py's ``include_unmapped_rows``) has no CAT/SEL --
``_task_to_lookup_plan()`` builds a description-search ``LookupPlan`` for
it instead of a trusted one, reusing ``phrase_generator.generate_search_
phrase()`` exactly as ``orchestrator.build_lookup_plan()``'s own
description-first path already does. Still routed through the exact
same ``orchestrator.execute_plan()`` -- ranking, safety stops, quantity/
unit handling, and commit verification are completely unchanged. This
module ALSO independently re-verifies, immediately before running any
such task, that the live adapter is positively confirmed on the exact
project ``execution_plan.TEST_ONLY_PROJECT_NAME`` -- a second,
authoritative gate beyond the cheap string check ``build_execution_
plan()`` already did at plan-build time.
"""

from __future__ import annotations

import json
from pathlib import Path

from estimate_extractor.xactimate_lookup import orchestrator
from estimate_extractor.xactimate_lookup.adapter import (
    AdapterError,
    PhysicalStateUncertainError,
    ProtectedCommittedRowError,
    QuantityConfirmationError,
    TaskLocalRowReconciliationError,
    UnexpectedDialogError,
    XactimateAdapter,
)
from estimate_extractor.xactimate_lookup.execution_plan import (
    CoordinatedPair,
    ExecutionPlan,
    ExecutionTask,
    GROUP_COMPLETED,
    GROUP_FAILED,
    GROUP_IN_PROGRESS,
    GROUP_PENDING,
    GROUP_SELECTED,
    GROUP_VERIFIED,
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
    RUN_STATE_COMPLETED,
    RUN_STATE_IN_PROGRESS,
    RUN_STATE_PAUSED,
    STOP_REASON_GROUP_VERIFICATION_FAILURE,
    STOP_REASON_GROUP_SETUP_BLOCKED,
    STOP_REASON_NORMAL_COMPLETION,
    STOP_REASON_PROJECT_LEVEL_HARD_STOP,
    STOP_REASON_PROJECT_VERIFICATION_FAILURE,
    STOP_REASON_PROTECTED_ROW_REFUSAL,
    STOP_REASON_TASK_LEVEL_STOPS,
    STOP_REASON_COORDINATED_PAIR_EXECUTION_NOT_IMPLEMENTED,
    TASK_COMMIT_STATE_COMMITTED,
    TASK_COMMIT_STATE_NOT_COMMITTED,
    TASK_COMMIT_STATE_PHYSICAL_ITEM_CREATED_UNCONFIRMED,
    TASK_COMPLETED,
    TASK_FAILED,
    TASK_PENDING,
    TASK_REVIEW_REQUIRED,
    TASK_SKIPPED,
    TEST_ONLY_PROJECT_NAME,
    commit_state_from_trust_state,
    is_plan_stale,
    pair_has_physical_activity,
    save_execution_plan,
    task_has_committed_row,
    utc_now_iso,
)
from estimate_extractor.xactimate_lookup.execution_reports import write_all_execution_reports
from estimate_extractor.xactimate_lookup.models import (
    DECISION_AUTO_SELECT,
    DECISION_NO_MATCH,
    InternalMappingRecord,
    LOOKUP_PATH_DESCRIPTION_SEARCH,
    LOOKUP_PATH_TRUSTED,
    LookupPlan,
    MAPPING_STATUS_APPROVED,
    RecommendationInput,
    STOP_REASON_ADAPTER_ERROR,
    STOP_REASON_EXTRACTION_FAILED,
    STOP_REASON_NO_RESULTS,
    STOP_REASON_PHYSICAL_STATE_UNCERTAIN,
    STOP_REASON_QUANTITY_CONFIRMATION_FAILED,
    STOP_REASON_TASK_LOCAL_ROW_RECONCILIATION,
    STOP_REASON_UNEXPECTED_DIALOG,
)
from estimate_extractor.xactimate_lookup.phrase_generator import PhraseRules, generate_search_phrase
from estimate_extractor.xactimate_lookup.ranking import RankingConfig
from estimate_extractor.xactimate_lookup.signature import compute_normalized_description

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


#: Phase 5.5B: audit reasons recorded on ExecutionTask.lookup_strategy_
#: reason -- an explicit trail proving what _task_to_lookup_plan()
#: actually decided and why, so a began_unmapped task can never be
#: silently routed to CAT/SEL without that being visible in the report.
_REASON_REVIEW_APPROVED = "review_approved_cat_sel: human-approved mapping, unchanged trusted path."
_REASON_DESCRIPTION_FIRST = (
    "test_description_first: no trusted CAT/SEL and no verified reusable observed mapping -- searched by description."
)


class UnsafeLookupRouting(Exception):
    """Phase 5.5B: raised by _task_to_lookup_plan() when a task whose
    lookup_strategy is LOOKUP_STRATEGY_TEST_DESCRIPTION_FIRST would
    otherwise be routed to the trusted CAT/SEL path with no explicit,
    verified reason -- caught by run_execution_plan() and turned into a
    safe TASK_FAILED for that one task, never silently trusted. See
    _task_to_lookup_plan()'s docstring for the live incident this
    guards against."""


def _task_to_lookup_plan(task: ExecutionTask, phrase_rules: PhraseRules) -> tuple[LookupPlan, str, str]:
    """Returns (plan, actual_strategy, reason) -- `actual_strategy` is
    always LOOKUP_PATH_TRUSTED or LOOKUP_PATH_DESCRIPTION_SEARCH.

    Phase 5.5B: routing is driven EXCLUSIVELY by `task.lookup_strategy`
    (set once, at plan-build time, by execution_plan.py -- see its
    module docstring) -- never re-derived from whether task.category/
    task.selector happen to be populated. Live-caught: an earlier
    version routed on category/selector PRESENCE alone (`if task.
    category and task.selector`), which a stale, pre-Phase-5.5 process
    reproduced live as a literal "None None" CAT/SEL search for
    originally-unmapped rows (search_input built from two None values
    formatted into a string) -- the row was never actually searched by
    description at all. A LOOKUP_STRATEGY_REVIEW_APPROVED task carries
    a human-approved CAT/SEL (see execution_plan.py's
    LOOKUP_STRATEGY_REVIEW_APPROVED) -- wrapped as a transient
    InternalMappingRecord (never persisted to the registry, exactly
    like orchestrator._verified_catalog_trusted_mapping() does for
    verified-catalog matches) so it flows through the SAME trusted-path
    code in orchestrator.execute_plan() as a registry-sourced mapping.
    Every other task (today, only LOOKUP_STRATEGY_TEST_DESCRIPTION_
    FIRST) is ALWAYS searched by description, built with the SAME
    `generate_search_phrase()` call orchestrator.build_lookup_plan()'s
    own description-first path already uses -- nothing about phrase
    generation, ranking, or the safety-stop rules downstream in
    orchestrator.execute_plan() is different for it. The only way such
    a task could ever legitimately use CAT/SEL instead is a future,
    explicit, verified-and-persisted reusable observed mapping (not
    built by this phase -- see execution_runner.py's OBSERVED_MAPPING_
    STATE, which is deliberately never auto-reused); absent that, a
    task somehow reaching this function with lookup_strategy=test_
    description_first AND both category and selector populated is an
    unexpected, unsafe state -- raises UnsafeLookupRouting rather than
    silently trusting it."""
    if task.lookup_strategy == LOOKUP_STRATEGY_REVIEW_APPROVED:
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
        plan = LookupPlan(
            line_item_id=task.line_item_id,
            path=LOOKUP_PATH_TRUSTED,
            item_signature="",
            search_input=f"{task.category} {task.selector}",
            trusted_mapping=trusted,
        )
        return plan, LOOKUP_PATH_TRUSTED, _REASON_REVIEW_APPROVED

    if task.category and task.selector:
        raise UnsafeLookupRouting(
            f"{task.task_id}: lookup_strategy={task.lookup_strategy!r} but category/selector "
            f"({task.category!r}/{task.selector!r}) are both populated with no verified reusable observed "
            f"mapping -- refusing to silently use the trusted CAT/SEL path for a task that must be "
            f"searched by description."
        )

    phrase_result = generate_search_phrase(
        task.description, task.normalized_component, task.normalized_material, task.normalized_action, phrase_rules
    )
    plan = LookupPlan(
        line_item_id=task.line_item_id,
        path=LOOKUP_PATH_DESCRIPTION_SEARCH,
        item_signature="",
        search_input=phrase_result.phrase,
        phrase_result=phrase_result,
    )
    return plan, LOOKUP_PATH_DESCRIPTION_SEARCH, _REASON_DESCRIPTION_FIRST


def _task_to_recommendation_input(task: ExecutionTask) -> RecommendationInput:
    return RecommendationInput(
        line_item_id=task.line_item_id,
        original_description=task.description,
        quantity=task.entered_quantity if task.entered_quantity is not None else task.source_quantity,
        source_unit=task.source_unit,
        action=task.normalized_action,
        trade=task.normalized_trade,
        component=task.normalized_component,
        material=task.normalized_material,
    )


def _ensure_select_verify_group(adapter, group: GroupExecutionState) -> tuple[bool, str | None]:
    """Returns (verified, error_detail). Never raises for an ordinary
    AdapterError -- a group that can't be verified means every task
    inside it is marked REVIEW_REQUIRED, not that the whole run stops
    (Priority 8). ProtectedCommittedRowError (Phase 5.5D) is the one
    deliberate exception to that: it means group verification's own
    probe-cleanup would have deleted a row Execute already successfully
    committed, in some OTHER group this run. That is never a "this one
    group failed" condition -- it is let through so run_execution_plan()
    can hard-stop the whole run instead of silently continuing to the
    next group as if nothing happened."""
    if not _supports_group_operations(adapter):
        return False, "Adapter does not support group operations (ensure_group/select_group/verify_group)."

    target = group.xactimate_group_name or group.section_name
    if not target:
        return False, "No resolvable Xactimate group name for this section."

    try:
        # Phase 5.7: ensure_group() now returns a GROUP_POSITION_WARNING
        # string (never raises for this) when the group was created
        # successfully but landed at an unexpected nesting depth --
        # ancestry is informational-only, kept on the group for the
        # final report/UI, never a reason to stop this group.
        group.position_warning = adapter.ensure_group(target, parent_group_name=group.parent_group_name)
        group.state = GROUP_SELECTED
        adapter.select_group(target)
        verified = adapter.verify_group(target)
    except ProtectedCommittedRowError:
        raise
    except AdapterError as exc:
        return False, str(exc)

    if not verified:
        return False, f"verify_group({target!r}) returned False -- refusing to trust the active group."
    group.state = GROUP_VERIFIED
    return True, None


def _record_terminal(adapter, task: ExecutionTask) -> None:
    """Phase 5.9: marks this task's row-lifecycle ledger entry TERMINAL
    -- called at every exit point of the per-task loop in
    run_execution_plan(), regardless of which outcome/exception path
    was taken, so the ledger always has a closing event to compare
    against an independent post-run grid re-inventory (Stage 11: task
    status is never, by itself, proof the row is still there)."""
    if hasattr(adapter, "record_lifecycle_event"):
        try:
            adapter.record_lifecycle_event("TERMINAL", task_state=task.state, stop_reason=task.stop_reason)
        except Exception:
            pass


def _apply_outcome_to_task(task: ExecutionTask, outcome, dry_run: bool) -> None:
    task.attempts += 1
    task.completed_at = utc_now_iso()
    task.stop_reason = outcome.stop_reason
    task.stop_detail = outcome.stop_detail
    task.evidence_path = outcome.evidence_reference
    task.physical_state_uncertain = bool(
        getattr(outcome, "physical_state_uncertain", False)
        or outcome.stop_reason == STOP_REASON_UNEXPECTED_DIALOG
    )
    selected = getattr(outcome, "selected", None)
    selected_dropdown = getattr(selected, "dropdown", None)
    if selected_dropdown is not None:
        task.selected_category = selected_dropdown.category
        task.selected_selector = selected_dropdown.selector
        task.selected_description = selected_dropdown.description

    if dry_run:
        # Nothing was actually executed -- record what WOULD happen
        # without claiming any state transition occurred.
        task.stop_detail = outcome.stop_detail or f"dry_run: decision={outcome.decision}"
        return

    if not outcome.committed:
        task.commit_state = (
            TASK_COMMIT_STATE_PHYSICAL_ITEM_CREATED_UNCONFIRMED
            if getattr(outcome, "physical_item_created", False)
            else TASK_COMMIT_STATE_NOT_COMMITTED
        )
        task.state = TASK_FAILED if outcome.decision == DECISION_NO_MATCH else TASK_REVIEW_REQUIRED
        return

    verification = outcome.verification
    if verification is None:
        # Committed, but the adapter cannot independently confirm it.
        # Never claim success without evidence -- route to review. Still
        # a real commit_item() call succeeded, though -- conservatively
        # treated as committed (see commit_state_from_trust_state()'s
        # docstring) so it is never silently retried/duplicated.
        task.commit_state = TASK_COMMIT_STATE_COMMITTED
        task.state = TASK_REVIEW_REQUIRED
        task.review_reason = "Committed, but the adapter does not support commit verification."
        task.stop_detail = (task.stop_detail or "") + f" {task.review_reason}"
        return

    task.trust_state = getattr(verification, "trust_state", None)
    task.commit_state = commit_state_from_trust_state(task.trust_state)
    if task.trust_state == "VERIFICATION_FAILED" and getattr(outcome, "physical_item_created", False):
        task.commit_state = TASK_COMMIT_STATE_PHYSICAL_ITEM_CREATED_UNCONFIRMED
        task.physical_state_uncertain = True
    task.observed_quantity = getattr(verification, "quantity_observed", None)
    observed_unit = getattr(verification, "unit", None)
    task.observed_unit = getattr(observed_unit, "observed_xactimate_unit", None) if observed_unit is not None else None
    task.entered_quantity = task.entered_quantity if task.entered_quantity is not None else task.source_quantity

    # Phase 5.5: OCR-observed CAT/SEL/description/activity, recorded
    # ONLY for a row that began unmapped -- purely informational
    # (never labeled human-approved, never written back over an
    # existing reviewed CAT/SEL in review_service's own state; see
    # _record_observed_mapping_proposal()). Captured regardless of
    # trust_state, since even a QUANTITY_MISMATCH/REVIEW_REQUIRED
    # commit still legitimately observed a real CAT/SEL at the
    # structurally-identified row.
    if task.began_unmapped:
        task.observed_category = getattr(verification, "category_observed", None)
        task.observed_selector = getattr(verification, "selector_observed", None)
        task.observed_description = getattr(verification, "description_observed", None)
        populated_fields = getattr(outcome, "populated_fields", None)
        task.observed_activity = getattr(populated_fields, "action", None) if populated_fields is not None else None

    if task.trust_state == _VERIFIED_TRUST_STATE:
        task.state = TASK_COMPLETED
        task.review_reason = None
    else:
        task.state = TASK_REVIEW_REQUIRED
        task.review_reason = getattr(verification, "reason", None) or task.trust_state


#: Phase 5.5: the state label on a proposal saved by
#: _record_observed_mapping_proposal() -- deliberately distinct from
#: any review_service status (STATUS_APPROVED, etc.) so nothing here
#: can ever be confused with a human review decision.
OBSERVED_MAPPING_STATE = "observed_from_test_execution"


def _observed_mappings_path(project_dir: Path) -> Path:
    return project_dir / "execution" / "observed_mappings.json"


def _record_observed_mapping_proposal(project_dir: Path, task: ExecutionTask, outcome) -> None:
    """Phase 5.5: best-effort, additive-only record of what an
    originally-unmapped row's live description-first search actually
    found -- written to its OWN file, completely separate from
    review_service's review_state.json, so this can NEVER overwrite an
    existing reviewed CAT/SEL or silently change a row's stored
    approval status (review_service is not imported here at all).
    Never raises -- a failure to persist this proposal must not affect
    the task's own already-decided outcome."""
    if not task.began_unmapped or not outcome.committed:
        return
    try:
        path = _observed_mappings_path(project_dir)
        data = {}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                data = {}
        selected = outcome.selected
        dropdown = selected.dropdown if selected is not None else None
        data[task.line_item_id] = {
            "line_item_id": task.line_item_id,
            "task_id": task.task_id,
            "state": OBSERVED_MAPPING_STATE,
            "observed_category": task.observed_category,
            "observed_selector": task.observed_selector,
            "observed_description": task.observed_description,
            "observed_activity": task.observed_activity,
            "observed_unit": task.observed_unit,
            "observed_quantity": task.observed_quantity,
            "source_description": task.description,
            "search_phrase": outcome.plan.search_input if outcome.plan is not None else None,
            #: Phase 5.7A: the exact search text that WON -- the
            #: description-search input that actually produced this
            #: commit (identical to "search_phrase" above; kept as its
            #: own explicitly-named field so learned-mapping consumers
            #: don't have to know "search_phrase" doubles as this).
            #: DESCRIPTION evidence for future searches, distinct from
            #: observed_category/observed_selector below, which are
            #: CAT/SEL evidence -- never conflate the two (see
            #: _description_first_search_attempts()'s docstring).
            "verified_search_description": outcome.plan.search_input if outcome.plan is not None else None,
            "selected_candidate_category": dropdown.category if dropdown is not None else None,
            "selected_candidate_selector": dropdown.selector if dropdown is not None else None,
            "selected_candidate_description": dropdown.description if dropdown is not None else None,
            "selected_candidate_score": selected.score if selected is not None else None,
            "match_reasons": list(selected.match_reasons) if selected is not None else [],
            "source_row": task.row_label,
            "group": task.section_name,
            "quantity": task.source_quantity,
            "evidence_path": task.evidence_path,
            "observed_at": utc_now_iso(),
            #: Phase 5.5D Stage 7: True only when THIS commit's own
            #: independent post-commit verification reached VERIFIED --
            #: the one and only signal _find_trusted_observed_mapping()
            #: will ever treat as safe to reuse as a later task's
            #: attempt-4 CAT/SEL fallback. A merely-committed-but-
            #: unverified row (trust_state != VERIFIED) is never
            #: eligible, no matter how recent.
            "verified": task.trust_state == _VERIFIED_TRUST_STATE,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    except Exception:
        pass


def _find_trusted_observed_mapping(project_dir: Path, line_item_id: str) -> tuple[str, str] | None:
    """Phase 5.5D Stage 7: returns (category, selector) ONLY if a
    PREVIOUS run's execution of this exact line_item_id both committed
    AND was independently verified (trust_state == VERIFIED) -- read
    from observed_mappings.json (see _record_observed_mapping_
    proposal()'s "verified" field). Read-only, best-effort: any error
    (missing file, malformed JSON, missing fields) returns None rather
    than raising, matching every other observed-mapping helper here."""
    try:
        path = _observed_mappings_path(project_dir)
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        entry = data.get(line_item_id)
        if not entry or not entry.get("verified"):
            return None
        category = entry.get("observed_category") or entry.get("selected_candidate_category")
        selector = entry.get("observed_selector") or entry.get("selected_candidate_selector")
        if not category or not selector:
            return None
        return category, selector
    except Exception:
        return None


def _find_verified_search_description(project_dir: Path, line_item_id: str) -> str | None:
    """Phase 5.7A: returns the DESCRIPTION text (never CAT/SEL) that
    won a PREVIOUS run's execution of this exact line_item_id, only
    when that commit was independently verified (trust_state ==
    VERIFIED) -- same "verified" gate as _find_trusted_observed_
    mapping(), same file, but returns search-input evidence instead of
    a CAT/SEL shortcut. This is what _description_first_search_
    attempts() tries FIRST for a repeat/resumed run of the same row --
    CAT/SEL remains a separate, later fallback (see that function's
    docstring). Read-only, best-effort: any error returns None rather
    than raising."""
    try:
        path = _observed_mappings_path(project_dir)
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        entry = data.get(line_item_id)
        if not entry or not entry.get("verified"):
            return None
        description = entry.get("verified_search_description")
        return description if description and description.strip() else None
    except Exception:
        return None


#: Ordered search-attempt labels recorded in ExecutionTask.search_attempts.
#: The complete source description is always first; later types are
#: retrieval-only fallbacks and are never ranking/execution retries.
SEARCH_TYPE_VERIFIED_SEARCH_DESCRIPTION = "verified_search_description"
SEARCH_TYPE_EXACT_DESCRIPTION = "exact_description"
SEARCH_TYPE_NORMALIZED_DESCRIPTION = "normalized_description"
SEARCH_TYPE_COMPACT_GENERATED_PHRASE = "compact_generated_phrase"
SEARCH_TYPE_GENERIC_CLEANING_FALLBACK = "generic_cleaning_fallback"
SEARCH_TYPE_TRUSTED_OBSERVED_CAT_SEL = "trusted_observed_cat_sel"

_RETRIEVAL_FAILURE_STOP_REASONS = frozenset({STOP_REASON_NO_RESULTS, STOP_REASON_EXTRACTION_FAILED})

def _description_first_search_attempts(
    task: ExecutionTask, phrase_rules: PhraseRules, project_dir: Path,
) -> list[tuple[str, LookupPlan]]:
    """Build exact-description-first retrieval attempts.

    Attempt 1 is always the complete original source description.
    Learned, normalized, compact, generic-cleaning, and trusted CAT/SEL
    plans remain available only to the runner's clean retrieval-failure
    transition. Duplicate text is skipped.
    """
    attempts: list[tuple[str, LookupPlan]] = []
    seen_texts: set[str] = set()

    def _add(search_type: str, value: str | None, *, phrase_result=None) -> None:
        if not value or not value.strip():
            return
        text = value.strip()
        if text.casefold() in seen_texts:
            return
        seen_texts.add(text.casefold())
        attempts.append((search_type, LookupPlan(
            line_item_id=task.line_item_id,
            path=LOOKUP_PATH_DESCRIPTION_SEARCH,
            item_signature="",
            search_input=text,
            phrase_result=phrase_result,
        )))

    _add(SEARCH_TYPE_EXACT_DESCRIPTION, task.description)
    _add(
        SEARCH_TYPE_VERIFIED_SEARCH_DESCRIPTION,
        _find_verified_search_description(project_dir, task.line_item_id),
    )
    _add(SEARCH_TYPE_NORMALIZED_DESCRIPTION, compute_normalized_description(
        task.normalized_trade, task.normalized_component, task.normalized_material, task.normalized_action,
    ))
    phrase_result = generate_search_phrase(
        task.description, task.normalized_component, task.normalized_material, task.normalized_action, phrase_rules,
    )
    _add(SEARCH_TYPE_COMPACT_GENERATED_PHRASE, phrase_result.phrase, phrase_result=phrase_result)

    action_term = phrase_rules.action_search_terms.get(task.normalized_action or "")
    if action_term == "clean":
        _add(SEARCH_TYPE_GENERIC_CLEANING_FALLBACK, action_term)

    trusted = _find_trusted_observed_mapping(project_dir, task.line_item_id)
    if trusted is not None:
        category, selector = trusted
        attempts.append((SEARCH_TYPE_TRUSTED_OBSERVED_CAT_SEL, LookupPlan(
            line_item_id=task.line_item_id,
            path=LOOKUP_PATH_TRUSTED,
            item_signature="",
            search_input=f"{category} {selector}",
            trusted_mapping=InternalMappingRecord(
                mapping_id=f"observed_mapping:{task.line_item_id}",
                item_signature="",
                source_description=task.description,
                search_phrase="",
                category=category,
                selector=selector,
                xactimate_description=task.description,
                unit=task.expected_unit,
                action=None,
                reviewer="",
                approval_reason="Previous VERIFIED commit of this exact source task.",
                status=MAPPING_STATUS_APPROVED,
            ),
        )))
    return attempts


def _is_clean_retrieval_failure(outcome) -> bool:
    """True only when lookup ended before ranking or physical interaction."""
    return (
        not outcome.candidates
        and outcome.selected is None
        and not outcome.physical_item_created
        and not outcome.committed
        and outcome.stop_reason in _RETRIEVAL_FAILURE_STOP_REASONS
    )


def _run_description_first_task(
    task: ExecutionTask, item: RecommendationInput, adapter, ranking_config: RankingConfig,
    phrase_rules: PhraseRules, project_dir: Path, dry_run: bool,
):
    """Run fallbacks only across positively clean retrieval failures.

    Any candidate-bearing ranking decision or physical interaction is
    terminal. Before every later query, both persisted task state and
    the adapter's read-only selection/dialog/grid checkpoint must prove
    the task is still clean.
    """
    attempts = _description_first_search_attempts(task, phrase_rules, project_dir)
    if not attempts:
        raise UnsafeLookupRouting(
            f"{task.task_id}: no search attempt could be built (empty full description) -- "
            f"refusing to guess."
        )
    fallback_baseline = None
    fallback_baseline_error = None
    if len(attempts) > 1:
        try:
            fallback_baseline = adapter.snapshot_search_fallback_state()
        except Exception as exc:
            fallback_baseline_error = str(exc)

    outcome = None
    for attempt_number, (search_type, attempt_plan) in enumerate(attempts, start=1):
        outcome = orchestrator.execute_plan(
            attempt_plan, item, adapter, ranking_config, phrase_rules, dry_run=dry_run,
        )
        is_last_attempt = attempt_number == len(attempts)
        retrieval_failure = _is_clean_retrieval_failure(outcome)
        fallback_blocked_reason = None
        should_advance = False

        if retrieval_failure and not is_last_attempt:
            if task.commit_state == TASK_COMMIT_STATE_PHYSICAL_ITEM_CREATED_UNCONFIRMED:
                fallback_blocked_reason = "physical_item_created_unconfirmed checkpoint exists"
            elif fallback_baseline_error is not None:
                fallback_blocked_reason = f"clean fallback baseline unavailable: {fallback_baseline_error}"
            else:
                try:
                    clean, detail = adapter.verify_search_fallback_state(fallback_baseline)
                except Exception as exc:
                    clean, detail = False, f"clean-state verification failed: {exc}"
                if clean:
                    should_advance = True
                else:
                    fallback_blocked_reason = detail

        if should_advance:
            advance_reason = "clean retrieval failure; adapter/task state verified clean"
        elif fallback_blocked_reason:
            advance_reason = f"fallback blocked: {fallback_blocked_reason}"
        elif not retrieval_failure:
            advance_reason = "terminal candidate/ranking/execution outcome; fallback prohibited"
        else:
            advance_reason = "final retrieval attempt"

        top = outcome.candidates[0] if outcome.candidates else None
        diagnostics = outcome.decision_diagnostics
        task.search_attempts.append({
            "attempt_number": attempt_number,
            "search_type": search_type,
            "search_text": attempt_plan.search_input,
            "result_count": len(outcome.candidates),
            "top_candidate_category": top.dropdown.category if top is not None else None,
            "top_candidate_selector": top.dropdown.selector if top is not None else None,
            "top_candidate_score": top.score if top is not None else None,
            "decision": outcome.decision,
            "stop_reason": outcome.stop_reason,
            "advanced_to_next_attempt": should_advance,
            "advance_reason": advance_reason,
            # classify_decision_with_diagnostics()'s own record of every
            # value it consulted (top/second candidate, score, margin,
            # extraction confidence, hard-conflict/conflict reasons, the
            # applicable thresholds) and which exact branch ("gate")
            # produced `decision` above -- None only when ranking was
            # never reached (e.g. an empty dropdown popup). See
            # DecisionDiagnostics in ranking.py.
            "decision_diagnostics": diagnostics.to_dict() if hasattr(diagnostics, "to_dict") else None,
        })
        if not should_advance:
            break

    reason = (
        f"test_description_first: attempt {len(task.search_attempts)}/{len(attempts)} "
        f"({task.search_attempts[-1]['search_type']}) -- {task.search_attempts[-1]['advance_reason']}."
    )
    actual_strategy = LOOKUP_PATH_TRUSTED if outcome.plan.path == LOOKUP_PATH_TRUSTED else LOOKUP_PATH_DESCRIPTION_SEARCH
    return outcome, actual_strategy, reason


# ---------------------------------------------------------------------
# Phase 5.23 (R&R Stage 4): coordinated remove/replace pair execution.
#
# Replaces the Stage 1-2 guard (STOP_REASON_COORDINATED_PAIR_EXECUTION_
# NOT_IMPLEMENTED unconditionally, for every paired task) with real
# execution: one search/rank/decide + one candidate activation for the
# WHOLE pair, dual-target binding and dual quantity write/verify via
# windows_adapter.py's Stage 3 primitives, then one shared commit_item()
# + verify_commit() -- reusing orchestrator._search_rank_and_decide()
# and every existing single-row adapter primitive (select_candidate,
# snapshot_grid_identities_for_activation, record_protected_commit,
# commit_item, verify_commit, capture_evidence) exactly as the ordinary
# per-task path does. Never a second, forked search/ranking/candidate-
# selection implementation -- see _run_coordinated_pair()'s own
# docstring for the full flow and crash-safe resume shape.
#
# The constant is still named STOP_REASON_COORDINATED_PAIR_EXECUTION_
# NOT_IMPLEMENTED (unchanged label) but is now produced only when THIS
# adapter specifically cannot do coordinated execution (see _supports_
# rr_pair_operations()) -- e.g. the plain FakeXactimateAdapter, or any
# future adapter that hasn't implemented the Stage 3 primitives -- not
# "Stage 3 doesn't exist yet" (it does now).
# ---------------------------------------------------------------------


def _supports_rr_pair_operations(adapter) -> bool:
    return (
        hasattr(adapter, "bind_rr_pair_after_activation")
        and hasattr(adapter, "write_and_verify_rr_pair_quantities")
        and hasattr(adapter, "locate_existing_rr_pair")
        and hasattr(adapter, "verify_existing_rr_pair_half_quantity")
        and hasattr(adapter, "write_and_verify_existing_rr_pair_half_quantity")
    )


def _mark_pair_members_pre_activation(
    pair: CoordinatedPair, remove_task: ExecutionTask, replace_task: ExecutionTask,
    decision: str, stop_reason: str | None, stop_detail: str | None, dry_run: bool,
) -> None:
    """Applies ONE shared search/rank/decide outcome to BOTH member
    tasks identically -- mirrors _apply_outcome_to_task()'s own "not
    committed" branch, generalized to two tasks sharing one decision.
    Never touches pair.pair_state or any binding field: nothing
    adapter-side has happened yet (no select_candidate() call), so
    this is never a physical checkpoint -- see run_execution_plan()'s
    own docstring requirement that pre-activation ambiguity must leave
    NO physical checkpoint behind, exactly like an ordinary task that
    never reaches activation."""
    for task in (remove_task, replace_task):
        task.attempts += 1
        task.completed_at = utc_now_iso()
        task.stop_reason = stop_reason
        task.stop_detail = stop_detail
        if dry_run:
            continue
        task.state = TASK_FAILED if decision == DECISION_NO_MATCH else TASK_REVIEW_REQUIRED
        task.commit_state = TASK_COMMIT_STATE_NOT_COMMITTED


def _mark_pair_physical_state_uncertain(
    pair: CoordinatedPair, remove_task: ExecutionTask, replace_task: ExecutionTask,
    stop_reason: str, detail: str,
) -> None:
    """Genuine project-level hard stop -- mirrors an ordinary task's
    own outcome.physical_state_uncertain=True handling, applied to
    BOTH member tasks so the EXISTING generic per-task/pre-loop resume
    guards in run_execution_plan() (which key off task.physical_state_
    uncertain, not anything pair-specific) protect this pair from ever
    being silently retried -- no pair-aware resume special-casing
    needed there at all."""
    pair.pair_state = PAIR_PHYSICAL_STATE_UNCERTAIN
    pair.uncertainty_reason = detail
    for task in (remove_task, replace_task):
        task.attempts += 1
        task.completed_at = utc_now_iso()
        task.physical_state_uncertain = True
        task.state = TASK_REVIEW_REQUIRED
        task.stop_reason = stop_reason
        task.stop_detail = detail


def _mark_pair_task_local_review(
    pair: CoordinatedPair, remove_task: ExecutionTask, replace_task: ExecutionTask,
    stop_reason: str, detail: str, *, review_reason: str | None = None,
) -> None:
    """Task-local failure -- deliberately never sets physical_state_
    uncertain (mirrors TaskLocalRowReconciliationError/Quantity
    ConfirmationError's own existing severity: confined to this pair's
    own commit, never evidence the wider grid/group is unsafe). The
    run continues with the next task/group afterward."""
    pair.pair_state = PAIR_REVIEW_REQUIRED
    pair.review_reason = review_reason or detail
    for task in (remove_task, replace_task):
        task.attempts += 1
        task.completed_at = utc_now_iso()
        task.state = TASK_REVIEW_REQUIRED
        task.stop_reason = stop_reason
        task.stop_detail = detail


def _binding_dict(identity: tuple[str, str, str], activity: str) -> dict:
    category, selector, description = identity
    return {"category": category, "selector": selector, "description": description, "activity": activity}


def _finalize_pair_task(
    task: ExecutionTask, confirmation, structural_trust_state: str, expected_quantity: float | None,
) -> None:
    """Per-task terminal state from ITS OWN quantity confirmation plus
    the ONE shared structural commit_verification -- mirrors _apply_
    outcome_to_task()'s VERIFIED-trust_state bar (TASK_COMPLETED only
    when both the shared structural check AND this task's own same-
    cell confirmation are fully confirmed) so neither task is ever
    reported committed merely because its partner's evidence looked
    good; each task's own affirmative evidence is what is checked
    here. Mirrors execute_plan()'s own "structural VERIFIED but the
    same-cell OCR confirmation itself was uncertain -> QUANTITY_
    MISMATCH" downgrade, evaluated independently per half."""
    task.entered_quantity = expected_quantity
    task.observed_quantity = getattr(confirmation, "observed", None)
    task.commit_state = TASK_COMMIT_STATE_COMMITTED
    confirmed = getattr(confirmation, "confidence", None) == "CONFIRMED"
    if structural_trust_state == _VERIFIED_TRUST_STATE and confirmed:
        task.trust_state = _VERIFIED_TRUST_STATE
        task.state = TASK_COMPLETED
        task.review_reason = None
    else:
        task.trust_state = "QUANTITY_MISMATCH" if structural_trust_state == _VERIFIED_TRUST_STATE else structural_trust_state
        task.state = TASK_REVIEW_REQUIRED
        task.review_reason = (
            getattr(confirmation, "reason", None) if not confirmed
            else f"Structural commit verification: {structural_trust_state}"
        )


def _write_verify_and_finalize_rr_pair(
    pair: CoordinatedPair, remove_task: ExecutionTask, replace_task: ExecutionTask,
    pair_target, before_snapshot, populated_unit: str | None,
    adapter, plan: ExecutionPlan, project_dir: Path,
) -> bool:
    """Shared tail for EVERY path that reaches "both halves physically
    bound, ready to write/verify quantities and finish" -- a fresh
    activation, and every resume shape that still needs a real write.
    Returns True on a genuine hard stop (structural VERIFICATION_
    FAILED -- the commit could not be detected in the grid at all,
    mirroring an ordinary task's own identical treatment), False
    otherwise (satisfied or task-locally reviewed)."""
    def _checkpoint_minus_verified(confirmation) -> None:
        pair.minus_written = True
        pair.minus_verified_ok = getattr(confirmation, "confidence", None) == "CONFIRMED"
        pair.pair_state = PAIR_MINUS_VERIFIED
        save_execution_plan(plan, project_dir)

    try:
        result = adapter.write_and_verify_rr_pair_quantities(
            pair_target, pair.expected_minus_quantity, pair.expected_plus_quantity,
            on_minus_verified=_checkpoint_minus_verified,
        )
    except QuantityConfirmationError as exc:
        side = getattr(exc, "side", None)
        minus_confirmation = getattr(exc, "minus_confirmation", None)
        if side == "minus":
            _mark_pair_task_local_review(
                pair, remove_task, replace_task, STOP_REASON_QUANTITY_CONFIRMATION_FAILED, str(exc),
            )
        else:
            # Plus failed after minus succeeded -- the evidence that
            # minus was already verified was ALREADY persisted by
            # _checkpoint_minus_verified() above (pair.pair_state ==
            # PAIR_MINUS_VERIFIED, pair.minus_written/verified_ok set)
            # before the plus write was even attempted; never lost,
            # never re-derived from this exception. Neither task is
            # marked COMPLETED here: commit_item() -- the one shared
            # save for BOTH physical rows -- is never reached when the
            # plus write fails, so nothing is actually saved yet.
            detail = str(exc)
            if minus_confirmation is not None:
                detail += f" (minus side already verified: observed={getattr(minus_confirmation, 'observed', None)!r})."
            _mark_pair_task_local_review(
                pair, remove_task, replace_task, STOP_REASON_QUANTITY_CONFIRMATION_FAILED, detail,
                review_reason="Plus-side quantity entry failed after the minus side was already verified.",
            )
        save_execution_plan(plan, project_dir)
        return False

    pair.minus_written = True
    pair.plus_written = True
    pair.minus_verified_ok = result.minus_confirmation.confidence == "CONFIRMED"
    pair.plus_verified_ok = result.plus_confirmation.confidence == "CONFIRMED"
    pair.pair_state = PAIR_PLUS_VERIFIED
    save_execution_plan(plan, project_dir)

    return _commit_and_finalize_rr_pair(
        pair, remove_task, replace_task, result.minus_confirmation, result.plus_confirmation,
        before_snapshot, populated_unit, adapter, plan, project_dir,
    )


def _commit_and_finalize_rr_pair(
    pair: CoordinatedPair, remove_task: ExecutionTask, replace_task: ExecutionTask,
    minus_confirmation, plus_confirmation, before_snapshot, populated_unit: str | None,
    adapter, plan: ExecutionPlan, project_dir: Path,
) -> bool:
    """The ONE shared save (commit_item()) and ONE shared structural
    reconciliation (verify_commit(), reused completely unmodified --
    it already natively proves "one corroborated R&R -/+ pair", the
    exact same primitive the legacy single-target R&R path has always
    used) for the whole pair -- Xactimate saves both physical rows
    together; there is no such thing as committing only one half.
    Called both from a fresh write and from every resume path once
    both halves are (re-)verified.

    `before_snapshot` is None for every RESUME path (see _resume_rr_
    pair()) -- deliberately, not an oversight: verify_commit()'s whole
    mechanism is detecting a ROW-COUNT DELTA between a "before" and
    "after" grid snapshot to prove exactly one new logical item
    appeared. On a resume, the physical rows already exist from a
    PRIOR session -- commit_item() here is a bare re-save, no new row
    is expected to appear, so a delta-based check would see delta==0
    and misreport a legitimate resume completion as trust_state==
    "VERIFICATION_FAILED". The pair's own structural "exactly one
    clean -/+ pair" proof already happened ONCE, at ORIGINAL bind time
    (_pending_rr_pair_targets_from_delta()'s multiset proof, whether
    that bind happened in this session or a prior one); a resume's own
    confidence instead comes entirely from Stage 3's read-only re-
    identification affirmations (verify_existing_rr_pair_half_
    quantity()) already performed by the caller before this function
    is ever reached. structural_trust_state therefore stays at its
    _VERIFIED_TRUST_STATE default for every resume path -- each task's
    OWN confirmation confidence (see _finalize_pair_task()) is still
    independently what decides TASK_COMPLETED vs TASK_REVIEW_REQUIRED,
    so this never fabricates success beyond what was actually
    reaffirmed."""
    plus_binding = pair.plus_binding or {}
    category = plus_binding.get("category")
    selector = plus_binding.get("selector")
    description = plus_binding.get("description")

    if hasattr(adapter, "record_protected_commit"):
        try:
            adapter.record_protected_commit(
                category=category, selector=selector, description=description,
                quantity=pair.expected_plus_quantity, unit=pair.expected_plus_unit,
            )
        except Exception:
            pass

    try:
        adapter.commit_item()
    except AdapterError as exc:
        adapter.recover()
        _mark_pair_task_local_review(pair, remove_task, replace_task, STOP_REASON_ADAPTER_ERROR, str(exc))
        save_execution_plan(plan, project_dir)
        return False

    evidence_reference = adapter.capture_evidence() if hasattr(adapter, "capture_evidence") else None

    structural_trust_state = _VERIFIED_TRUST_STATE
    verification = None
    if before_snapshot is not None and hasattr(adapter, "verify_commit"):
        verification = adapter.verify_commit(
            before_snapshot, category, selector, pair.expected_plus_quantity,
            source_unit=pair.expected_plus_unit, expected_xactimate_unit=pair.expected_plus_unit,
            populated_unit=populated_unit,
        )
        structural_trust_state = getattr(verification, "trust_state", None)

    if structural_trust_state == "VERIFICATION_FAILED":
        # Mirrors _apply_outcome_to_task()'s own identical special
        # case: the adapter's own physical_item_created evidence is
        # positive (both halves were bound and written), but the
        # structural row-count delta was never independently detected
        # -- genuine physical-state uncertainty, not a guess either way.
        _mark_pair_physical_state_uncertain(
            pair, remove_task, replace_task, STOP_REASON_PHYSICAL_STATE_UNCERTAIN,
            f"Commit could not be independently verified in the grid: {getattr(verification, 'reason', None)!r}.",
        )
        save_execution_plan(plan, project_dir)
        return True

    remove_task.evidence_path = evidence_reference
    replace_task.evidence_path = evidence_reference
    remove_task.selected_category = category
    remove_task.selected_selector = selector
    remove_task.selected_description = description
    replace_task.selected_category = category
    replace_task.selected_selector = selector
    replace_task.selected_description = description

    _finalize_pair_task(remove_task, minus_confirmation, structural_trust_state, pair.expected_minus_quantity)
    _finalize_pair_task(replace_task, plus_confirmation, structural_trust_state, pair.expected_plus_quantity)

    pair.pair_state = (
        PAIR_SATISFIED if remove_task.state == TASK_COMPLETED and replace_task.state == TASK_COMPLETED
        else PAIR_BOTH_VERIFIED
    )
    save_execution_plan(plan, project_dir)
    return False


def _resume_rr_pair(
    pair: CoordinatedPair, remove_task: ExecutionTask, replace_task: ExecutionTask,
    adapter, plan: ExecutionPlan, project_dir: Path,
) -> bool:
    """Crash-safe resume -- NEVER re-searches, re-ranks, or re-
    activates a candidate; only Stage 3's read-only re-identification
    primitives are used to recover physical state. Dispatches purely
    on pair.pair_state, the single source of truth for "how far did
    the prior attempt get":

    * PAIR_BOTH_BOUND: neither quantity verified yet -- re-identify
      BOTH halves (locate_existing_rr_pair()) and write both, reusing
      the exact same _write_verify_and_finalize_rr_pair() tail a fresh
      activation uses.
    * PAIR_MINUS_VERIFIED: re-affirm minus read-only (never rewritten
      -- see write_and_verify_existing_rr_pair_half_quantity()'s own
      docstring for why the OTHER, already-verified side must never be
      touched again), then write ONLY the plus side.
    * PAIR_PLUS_VERIFIED / PAIR_BOTH_VERIFIED: both sides already have
      a real write/verify from a prior attempt but the shared commit/
      task-persistence step never completed -- re-affirm both read-
      only (no write at all) and go straight to the shared commit/
      finalize tail.

    Any other persisted pair_state reaching this function (PAIR_
    ACTIVATED_PENDING_BINDING -- never itself persisted by this Stage,
    kept only for schema completeness; or PAIR_SATISFIED/PAIR_REVIEW_
    REQUIRED/PAIR_PHYSICAL_STATE_UNCERTAIN, which should already leave
    both member tasks terminal and therefore unreachable via the
    runner's own PENDING-task loop) is treated as unrecoverable
    physical-state uncertainty -- fails closed rather than guessing."""
    plus_binding = pair.plus_binding or {}
    minus_binding = pair.minus_binding or {}
    category = plus_binding.get("category") or minus_binding.get("category")
    selector = plus_binding.get("selector") or minus_binding.get("selector")
    description = plus_binding.get("description") or minus_binding.get("description")
    if not (category and selector and description):
        _mark_pair_physical_state_uncertain(
            pair, remove_task, replace_task, STOP_REASON_PHYSICAL_STATE_UNCERTAIN,
            f"Coordinated pair {pair.pair_id!r} has pair_state={pair.pair_state!r} but no usable persisted "
            f"minus/plus binding identity -- refusing to guess at physical state on resume.",
        )
        save_execution_plan(plan, project_dir)
        return True

    if pair.pair_state == PAIR_BOTH_BOUND:
        pair_target = adapter.locate_existing_rr_pair(category=category, selector=selector, description=description)
        if pair_target is None:
            _mark_pair_physical_state_uncertain(
                pair, remove_task, replace_task, STOP_REASON_PHYSICAL_STATE_UNCERTAIN,
                f"Coordinated pair {pair.pair_id!r} was previously bound but its physical -/+ rows could not "
                f"be uniquely re-identified on resume; refusing to reactivate the candidate.",
            )
            save_execution_plan(plan, project_dir)
            return True
        return _write_verify_and_finalize_rr_pair(
            pair, remove_task, replace_task, pair_target, None, None, adapter, plan, project_dir,
        )

    if pair.pair_state == PAIR_MINUS_VERIFIED:
        affirm = adapter.verify_existing_rr_pair_half_quantity(
            category=category, selector=selector, description=description, activity="-",
            expected_quantity=pair.expected_minus_quantity,
        )
        if affirm is None:
            _mark_pair_physical_state_uncertain(
                pair, remove_task, replace_task, STOP_REASON_PHYSICAL_STATE_UNCERTAIN,
                f"Coordinated pair {pair.pair_id!r}'s previously-verified minus half could not be "
                f"re-identified on resume; refusing to guess at physical state.",
            )
            save_execution_plan(plan, project_dir)
            return True
        try:
            plus_confirmation = adapter.write_and_verify_existing_rr_pair_half_quantity(
                category=category, selector=selector, description=description, activity="+",
                quantity=pair.expected_plus_quantity,
            )
        except QuantityConfirmationError as exc:
            _mark_pair_task_local_review(
                pair, remove_task, replace_task, STOP_REASON_QUANTITY_CONFIRMATION_FAILED, str(exc),
                review_reason="Plus-side quantity entry failed while resuming a pair whose minus side was already verified.",
            )
            save_execution_plan(plan, project_dir)
            return False
        pair.plus_written = True
        pair.plus_verified_ok = plus_confirmation.confidence == "CONFIRMED"
        pair.pair_state = PAIR_PLUS_VERIFIED
        save_execution_plan(plan, project_dir)
        return _commit_and_finalize_rr_pair(
            pair, remove_task, replace_task, affirm, plus_confirmation, None, None, adapter, plan, project_dir,
        )

    if pair.pair_state in (PAIR_PLUS_VERIFIED, PAIR_BOTH_VERIFIED):
        minus_affirm = adapter.verify_existing_rr_pair_half_quantity(
            category=category, selector=selector, description=description, activity="-",
            expected_quantity=pair.expected_minus_quantity,
        )
        plus_affirm = adapter.verify_existing_rr_pair_half_quantity(
            category=category, selector=selector, description=description, activity="+",
            expected_quantity=pair.expected_plus_quantity,
        )
        if minus_affirm is None or plus_affirm is None:
            _mark_pair_physical_state_uncertain(
                pair, remove_task, replace_task, STOP_REASON_PHYSICAL_STATE_UNCERTAIN,
                f"Coordinated pair {pair.pair_id!r}'s previously-verified halves could not be re-identified "
                f"on resume; refusing to guess at physical state.",
            )
            save_execution_plan(plan, project_dir)
            return True
        return _commit_and_finalize_rr_pair(
            pair, remove_task, replace_task, minus_affirm, plus_affirm, None, None, adapter, plan, project_dir,
        )

    _mark_pair_physical_state_uncertain(
        pair, remove_task, replace_task, STOP_REASON_PHYSICAL_STATE_UNCERTAIN,
        f"Coordinated pair {pair.pair_id!r} has an unrecoverable persisted pair_state={pair.pair_state!r} on "
        f"resume; refusing to guess at physical state.",
    )
    save_execution_plan(plan, project_dir)
    return True


def _run_coordinated_pair(
    pair: CoordinatedPair, plan: ExecutionPlan, adapter, ranking_config: RankingConfig,
    phrase_rules: PhraseRules, project_dir: Path, dry_run: bool,
) -> bool:
    """Executes (or safely resumes) one coordinated remove/replace pair
    as ONE atomic unit of work -- see run_execution_plan()'s own
    coordinated-pair routing branch, which calls this for ANY task
    carrying task.coordinated_pair_id instead of the ordinary per-task
    path. Marks BOTH pair.remove_task_id/replace_task_id tasks
    terminal in this one call (or leaves them PENDING for dry_run,
    mirroring ordinary tasks) -- so the runner's per-task loop skips a
    task whose state was already advanced by its OWN pair partner (see
    the `if task.state != TASK_PENDING: continue` guard added there).

    Returns True if the whole run must hard-stop (genuine project-
    level physical-state uncertainty -- mirrors an ordinary task's own
    physical_state_uncertain hard-stop), False otherwise (normal
    continuation, whether satisfied or task-locally reviewed)."""
    remove_task = plan.task_by_id(pair.remove_task_id)
    replace_task = plan.task_by_id(pair.replace_task_id)
    activation_task = plan.task_by_id(pair.activation_task_id) or remove_task

    if not dry_run and not _supports_rr_pair_operations(adapter):
        # Deliberately never touches pair.pair_state -- nothing adapter-
        # side happens here at all (not even a search), so this must
        # leave the pair exactly as re-runnable as ordinary pre-
        # activation ambiguity does: a LATER run with a capable adapter
        # (or, if the pair was already active from a prior session,
        # this run simply defers to that later run) must not find this
        # pair artificially "protected" by pair_has_physical_activity()
        # from a genuine, deliberate reset.
        detail = (
            f"Adapter {type(adapter).__name__!r} does not support coordinated R&R pair execution (missing "
            f"one or more Stage 3 primitives) -- refusing to execute pair {pair.pair_id!r}."
        )
        for task in (remove_task, replace_task):
            task.attempts += 1
            task.completed_at = utc_now_iso()
            task.state = TASK_REVIEW_REQUIRED
            task.stop_reason = STOP_REASON_COORDINATED_PAIR_EXECUTION_NOT_IMPLEMENTED
            task.stop_detail = detail
        save_execution_plan(plan, project_dir)
        return False

    try:
        lookup_plan, actual_strategy, reason = _task_to_lookup_plan(activation_task, phrase_rules)
    except UnsafeLookupRouting as exc:
        for task in (remove_task, replace_task):
            task.attempts += 1
            task.completed_at = utc_now_iso()
            task.error = str(exc)
            if not dry_run:
                task.state = TASK_FAILED
        if not dry_run:
            save_execution_plan(plan, project_dir)
        return False
    activation_task.actual_lookup_strategy = actual_strategy
    activation_task.lookup_strategy_reason = reason
    item = _task_to_recommendation_input(activation_task)

    resumed = pair_has_physical_activity(pair)

    if hasattr(adapter, "set_execution_context"):
        adapter.set_execution_context(task_id=activation_task.task_id, source_row=activation_task.row_label)
    if hasattr(adapter, "record_lifecycle_event"):
        # Phase 5.23 (R&R Stage 4): the one lifecycle event that names
        # BOTH logical task IDs and the pair ID together -- everything
        # after this point is recorded per-task exactly like an
        # ordinary single task (PLANNED/CANDIDATE_SELECTED/QUANTITY_
        # ENTERED/COMMIT_STARTED/COMMIT_RETURNED/VERIFIED/TERMINAL, via
        # the SAME _record_lifecycle()-driven calls windows_adapter.py
        # already makes from inside select_candidate()/commit_item()/
        # etc. where it supports them), so the ledger can reconstruct
        # "pair ID, both task IDs, one candidate activation, ...,
        # whether execution was fresh or resumed" without any adapter-
        # side R&R-specific ledger logic.
        try:
            adapter.record_lifecycle_event(
                "COORDINATED_PAIR_STARTED", pair_id=pair.pair_id,
                remove_task_id=pair.remove_task_id, replace_task_id=pair.replace_task_id,
                resumed=resumed, pair_state=pair.pair_state,
            )
        except Exception:
            pass

    if resumed:
        if dry_run:
            # dry_run must NEVER cause a real adapter call, resumed pair
            # or not -- mirrors ordinary tasks' own dry_run contract
            # (preview only; task.state is left completely untouched).
            return False
        # A resumed pair must NEVER search/rank/decide/activate again
        # -- the candidate already physically exists. Route straight
        # to resume handling, entirely via Stage 3's read-only
        # re-identification primitives.
        return _resume_rr_pair(pair, remove_task, replace_task, adapter, plan, project_dir)

    outcome = orchestrator._search_rank_and_decide(
        lookup_plan, item, adapter, ranking_config, phrase_rules, dry_run=dry_run,
    )
    if dry_run or outcome.decision != DECISION_AUTO_SELECT:
        _mark_pair_members_pre_activation(
            pair, remove_task, replace_task, outcome.decision, outcome.stop_reason, outcome.stop_detail, dry_run,
        )
        if not dry_run:
            save_execution_plan(plan, project_dir)
        return False

    top = outcome.selected
    if hasattr(adapter, "snapshot_grid_identities_for_activation"):
        before_snapshot = adapter.snapshot_grid_identities_for_activation()
    elif hasattr(adapter, "snapshot_grid_identities"):
        before_snapshot = adapter.snapshot_grid_identities()
    else:
        before_snapshot = None

    try:
        adapter.select_candidate(top.dropdown)
        pair_target = adapter.bind_rr_pair_after_activation(before_snapshot or [])
    except UnexpectedDialogError as exc:
        _mark_pair_physical_state_uncertain(
            pair, remove_task, replace_task, STOP_REASON_UNEXPECTED_DIALOG, str(exc),
        )
        save_execution_plan(plan, project_dir)
        return True
    except PhysicalStateUncertainError as exc:
        _mark_pair_physical_state_uncertain(
            pair, remove_task, replace_task, STOP_REASON_PHYSICAL_STATE_UNCERTAIN, str(exc),
        )
        save_execution_plan(plan, project_dir)
        return True
    except TaskLocalRowReconciliationError as exc:
        adapter.recover()
        _mark_pair_task_local_review(pair, remove_task, replace_task, STOP_REASON_TASK_LOCAL_ROW_RECONCILIATION, str(exc))
        save_execution_plan(plan, project_dir)
        return False
    except AdapterError as exc:
        adapter.recover()
        _mark_pair_task_local_review(pair, remove_task, replace_task, STOP_REASON_ADAPTER_ERROR, str(exc))
        save_execution_plan(plan, project_dir)
        return False

    # Bind checkpoint -- persisted BEFORE any quantity mutation, exactly
    # once the physical -/+ pair is positively, uniquely proven (Stage 3
    # binding is atomic: either fully bound or an exception above).
    pair.minus_binding = _binding_dict(pair_target.minus_target.identity, "-")
    pair.plus_binding = _binding_dict(pair_target.plus_target.identity, "+")
    pair.pair_state = PAIR_BOTH_BOUND
    save_execution_plan(plan, project_dir)

    populated_unit = None  # No populated_fields OCR read is taken for the pair path (mirrors ordinary tasks' own deliberate omission before quantity entry).
    return _write_verify_and_finalize_rr_pair(
        pair, remove_task, replace_task, pair_target, before_snapshot, populated_unit, adapter, plan, project_dir,
    )


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
    exactly what stops the whole run versus one task.

    Phase 5.5D: refuses outright (no group/task is touched) if `plan`
    is stale (see execution_plan.is_plan_stale()) -- the UI's own
    "Build / refresh" / "Rebuild TEST plan" actions always produce a
    current-schema plan; this is the defense-in-depth check for any
    other caller. Also never silently continues past a
    ProtectedCommittedRowError (Phase 5.5D) -- that means some
    destructive cleanup call would have deleted a row THIS run already
    successfully committed, in some other group; the whole run hard-
    stops rather than treating it as an ordinary per-group failure."""
    if not dry_run and is_plan_stale(plan):
        plan.run_state = RUN_STATE_PAUSED
        plan.stop_reason_category = STOP_REASON_PROJECT_LEVEL_HARD_STOP
        save_execution_plan(plan, project_dir)
        return plan

    # Phase 5.5B, Objective 3: recorded BEFORE anything else runs, so
    # "how many tasks did this call skip because they were already
    # terminal (resume)" survives in the persisted plan even if the
    # run pauses immediately after.
    plan.last_run_skipped_already_terminal = sum(1 for t in plan.tasks if t.state != TASK_PENDING)

    run_id = f"run_{utc_now_iso().replace(':', '').replace('-', '').replace('.', '').replace('+', '')}"
    if hasattr(adapter, "set_execution_context"):
        adapter.set_execution_context(run_id=run_id)

    if not adapter.verify_application():
        plan.run_state = RUN_STATE_PAUSED
        plan.stop_reason_category = STOP_REASON_PROJECT_VERIFICATION_FAILURE
        save_execution_plan(plan, project_dir)
        return plan
    if not adapter.verify_project():
        plan.run_state = RUN_STATE_PAUSED
        plan.stop_reason_category = STOP_REASON_PROJECT_VERIFICATION_FAILURE
        save_execution_plan(plan, project_dir)
        return plan

    resumed_physical = next(
        (
            task for task in plan.tasks
            if task.state == TASK_PENDING
            and (
                task.commit_state == TASK_COMMIT_STATE_PHYSICAL_ITEM_CREATED_UNCONFIRMED
                or task.physical_state_uncertain
            )
        ),
        None,
    )
    if not dry_run and resumed_physical is not None:
        plan.run_state = RUN_STATE_PAUSED
        plan.stop_reason_category = STOP_REASON_PROJECT_LEVEL_HARD_STOP
        plan.resume_cursor = plan.tasks.index(resumed_physical)
        prior_detail = (resumed_physical.stop_detail or "").strip()
        retry_detail = (
            "RETRY BLOCKED: physical_item_created_unconfirmed checkpoint exists; "
            "reconcile the live row before resuming."
        )
        if retry_detail not in prior_detail:
            resumed_physical.stop_detail = f"{prior_detail} {retry_detail}".strip()
        save_execution_plan(plan, project_dir)
        write_all_execution_reports(plan, project_dir)
        return plan

    plan.run_state = RUN_STATE_IN_PROGRESS
    tasks_by_id = {t.task_id: t for t in plan.tasks}

    for group in plan.groups:
        group_tasks = [tasks_by_id[tid] for tid in group.task_ids if tid in tasks_by_id]
        pending_tasks = [t for t in group_tasks if t.state == TASK_PENDING]
        if not pending_tasks:
            continue

        if hasattr(adapter, "set_execution_context"):
            adapter.set_execution_context(group=group.xactimate_group_name or group.section_name or group.group_id)

        if not dry_run:
            # Re-verify the application/project are still in a sane state
            # before EACH group -- if Xactimate crashed or the wrong
            # project got activated mid-run, stop the whole run rather
            # than risk executing against the wrong estimate.
            if not (adapter.verify_application() and adapter.verify_project()):
                plan.run_state = RUN_STATE_PAUSED
                plan.stop_reason_category = STOP_REASON_PROJECT_LEVEL_HARD_STOP
                save_execution_plan(plan, project_dir)
                write_all_execution_reports(plan, project_dir)
                return plan

            group.state = GROUP_IN_PROGRESS
            try:
                verified, detail = _ensure_select_verify_group(adapter, group)
            except ProtectedCommittedRowError as exc:
                # Never treated as "this group failed" -- a committed
                # row from this run was about to be deleted. Hard stop.
                plan.run_state = RUN_STATE_PAUSED
                plan.stop_reason_category = STOP_REASON_PROTECTED_ROW_REFUSAL
                group.state = GROUP_FAILED
                group.error = str(exc)
                save_execution_plan(plan, project_dir)
                write_all_execution_reports(plan, project_dir)
                return plan
            if not verified:
                group.state = GROUP_FAILED
                group.error = detail
                for task in pending_tasks:
                    task.state = TASK_REVIEW_REQUIRED
                    task.stop_reason = STOP_REASON_GROUP_SETUP_BLOCKED
                    task.stop_detail = f"Group not verified: {detail}"
                    task.completed_at = utc_now_iso()
                save_execution_plan(plan, project_dir)
                continue

        for task in pending_tasks:
            # Phase 5.23 (R&R Stage 4): `pending_tasks` is a snapshot
            # taken before this loop started. Processing a coordinated
            # pair's FIRST member (below) also resolves its partner's
            # state in the SAME call -- when the loop later reaches
            # that partner via this same stale snapshot, it must be
            # skipped outright rather than re-processed. Unreachable
            # for every ordinary task, whose state only ever changes
            # within its own iteration of this exact loop.
            if task.state != TASK_PENDING:
                continue

            # A crash can leave the immediate physical-created
            # checkpoint persisted while the task itself is still
            # PENDING.  Treat that resumed shape exactly like an
            # in-process post-creation failure: stop before touching
            # this task, any sibling task, or any later group.
            if not dry_run and (
                task.commit_state == TASK_COMMIT_STATE_PHYSICAL_ITEM_CREATED_UNCONFIRMED
                or task.physical_state_uncertain
            ):
                plan.run_state = RUN_STATE_PAUSED
                plan.stop_reason_category = STOP_REASON_PROJECT_LEVEL_HARD_STOP
                plan.resume_cursor = plan.tasks.index(task)
                save_execution_plan(plan, project_dir)
                write_all_execution_reports(plan, project_dir)
                return plan

            # Phase 5.9: defense-in-depth, independent of reset_
            # unfinished_tasks()'s own protection -- a task should never
            # legitimately reach TASK_PENDING with commit evidence still
            # attached (that function now clears commit_state/trust_
            # state on genuine reset), so this should be unreachable in
            # normal operation. It exists for the same reason
            # ProtectedCommittedRowError does: a second, structurally
            # independent gate against duplicating a real committed row,
            # in case some OTHER path (a manual JSON edit, a future bug)
            # ever puts a committed task back in TASK_PENDING.
            if not dry_run and task_has_committed_row(task):
                task.state = TASK_REVIEW_REQUIRED
                task.stop_detail = (
                    "ALREADY_COMMITTED — RETRY BLOCKED: this task has evidence of a prior real commit "
                    "(commit_state/trust_state indicates a row landed) -- automatic retry refused. Reconcile "
                    "against the live Xactimate grid before ever re-executing this row."
                )
                task.completed_at = utc_now_iso()
                _record_terminal(adapter, task)
                if not dry_run:
                    plan.resume_cursor = plan.tasks.index(task) + 1
                    save_execution_plan(plan, project_dir)
                continue

            # Phase 5.23 (R&R Stage 4): a task belonging to a
            # coordinated remove/replace pair NEVER falls through to
            # the ordinary independent single-task path below -- doing
            # so could search/select/commit its own candidate while its
            # partner is untouched, exactly the duplicate-activation
            # risk coordinated pairs exist to prevent. _run_coordinated_
            # pair() executes (or safely resumes) the WHOLE pair as one
            # atomic unit and marks BOTH member tasks terminal itself
            # (see the `task.state != TASK_PENDING` guard above, which
            # is what lets the partner's own later loop iteration skip
            # cleanly) -- never a fabricated success, never a silent
            # independent execution of either member.
            if task.coordinated_pair_id:
                pair = plan.pair_by_id(task.coordinated_pair_id)
                if pair is None:
                    # Structurally impossible in a well-formed plan --
                    # fail this one task closed rather than guess.
                    task.state = TASK_REVIEW_REQUIRED
                    task.stop_reason = STOP_REASON_COORDINATED_PAIR_EXECUTION_NOT_IMPLEMENTED
                    task.stop_detail = f"coordinated_pair_id {task.coordinated_pair_id!r} has no matching CoordinatedPair record."
                    task.completed_at = utc_now_iso()
                    _record_terminal(adapter, task)
                    if not dry_run:
                        plan.resume_cursor = plan.tasks.index(task) + 1
                        save_execution_plan(plan, project_dir)
                    continue
                hard_stop = _run_coordinated_pair(pair, plan, adapter, ranking_config, phrase_rules, project_dir, dry_run)
                remove_task = plan.task_by_id(pair.remove_task_id)
                replace_task = plan.task_by_id(pair.replace_task_id)
                for member in (remove_task, replace_task):
                    if member is not None:
                        _record_terminal(adapter, member)
                if hard_stop:
                    plan.run_state = RUN_STATE_PAUSED
                    plan.stop_reason_category = STOP_REASON_PROJECT_LEVEL_HARD_STOP
                    plan.resume_cursor = plan.tasks.index(task)
                    save_execution_plan(plan, project_dir)
                    write_all_execution_reports(plan, project_dir)
                    return plan
                if not dry_run:
                    plan.resume_cursor = plan.tasks.index(task) + 1
                    save_execution_plan(plan, project_dir)
                continue

            task.started_at = task.started_at or utc_now_iso()
            if hasattr(adapter, "set_execution_context"):
                adapter.set_execution_context(task_id=task.task_id, source_row=task.row_label)
            if hasattr(adapter, "record_lifecycle_event"):
                try:
                    adapter.record_lifecycle_event("PLANNED", description=task.description)
                except Exception:
                    pass

            # Phase 5.5: a second, authoritative gate for unmapped-row
            # description-first tasks, independent of the cheap string
            # check build_execution_plan() already did at plan-build
            # time -- re-verified against the LIVE adapter immediately
            # before this specific task runs. Never aborts the whole
            # run; only this one task is refused.
            if task.lookup_strategy == LOOKUP_STRATEGY_TEST_DESCRIPTION_FIRST:
                expected_name = getattr(adapter, "expected_project_name", None)
                project_ok = expected_name == TEST_ONLY_PROJECT_NAME and adapter.verify_project()
                if not project_ok:
                    task.state = TASK_REVIEW_REQUIRED
                    task.completed_at = utc_now_iso()
                    task.stop_detail = (
                        f"TEST-only unmapped-row execution refused: the live Xactimate project is not "
                        f"positively verified as exactly {TEST_ONLY_PROJECT_NAME!r} "
                        f"(adapter expects {expected_name!r})."
                    )
                    if not dry_run:
                        plan.resume_cursor = plan.tasks.index(task) + 1
                        save_execution_plan(plan, project_dir)
                    _record_terminal(adapter, task)
                    continue

            item = _task_to_recommendation_input(task)
            try:
                # A began_unmapped task starts with the complete source
                # description. Retrieval-only fallbacks require a clean
                # adapter/task checkpoint; candidate-bearing outcomes
                # are terminal -- see
                # _run_description_first_task()'s docstring. Every other
                # (today, only review_approved) task is completely
                # unchanged: one _task_to_lookup_plan() call, one
                # orchestrator.execute_plan() call, exactly as before
                # this phase.
                def _checkpoint_physical_item_created() -> None:
                    task.commit_state = TASK_COMMIT_STATE_PHYSICAL_ITEM_CREATED_UNCONFIRMED
                    save_execution_plan(plan, project_dir)

                adapter.set_physical_item_created_callback(
                    None if dry_run else _checkpoint_physical_item_created
                )
                try:
                    if task.lookup_strategy == LOOKUP_STRATEGY_TEST_DESCRIPTION_FIRST:
                        outcome, actual_strategy, reason = _run_description_first_task(
                            task, item, adapter, ranking_config, phrase_rules, project_dir, dry_run,
                        )
                    else:
                        lookup_plan, actual_strategy, reason = _task_to_lookup_plan(task, phrase_rules)
                        outcome = orchestrator.execute_plan(lookup_plan, item, adapter, ranking_config, phrase_rules, dry_run=dry_run)
                finally:
                    adapter.set_physical_item_created_callback(None)

                task.actual_lookup_strategy = actual_strategy
                task.lookup_strategy_reason = reason
                _apply_outcome_to_task(task, outcome, dry_run)
                if not dry_run:
                    _record_observed_mapping_proposal(project_dir, task, outcome)
            except UnsafeLookupRouting as exc:
                # Phase 5.5B: routing itself refused, before any live
                # adapter interaction happened for this task -- a safe
                # task-level failure, never a whole-run abort, and never
                # a silent fall-through to CAT/SEL. See _task_to_lookup_
                # plan()'s docstring.
                task.state = TASK_FAILED
                task.actual_lookup_strategy = None
                task.lookup_strategy_reason = str(exc)
                task.error = str(exc)
                task.completed_at = utc_now_iso()
                if not dry_run:
                    plan.resume_cursor = plan.tasks.index(task) + 1
                    save_execution_plan(plan, project_dir)
                _record_terminal(adapter, task)
                continue
            except ProtectedCommittedRowError as exc:
                # Never treated as "this task failed" -- a committed row
                # from this run was about to be deleted. Hard stop.
                plan.run_state = RUN_STATE_PAUSED
                plan.stop_reason_category = STOP_REASON_PROTECTED_ROW_REFUSAL
                task.state = TASK_FAILED
                task.error = str(exc)
                task.completed_at = utc_now_iso()
                if not dry_run:
                    plan.resume_cursor = plan.tasks.index(task) + 1
                    save_execution_plan(plan, project_dir)
                    write_all_execution_reports(plan, project_dir)
                _record_terminal(adapter, task)
                return plan
            except Exception as exc:  # noqa: BLE001 -- one task's unexpected failure must never abort the run
                task.state = TASK_FAILED
                task.error = repr(exc)
                task.completed_at = utc_now_iso()
                try:
                    adapter.recover()
                    task.recovery_outcome = "recovered"
                except Exception:
                    task.recovery_outcome = "recovery_failed"

            _record_terminal(adapter, task)
            if not dry_run:
                # A physical row exists but this task did not reach a
                # reconciled commit.  Continuing would make every later
                # task/group operate on dirty, divergent state.  Leave
                # all untouched work PENDING with attempts=0 and pause
                # at this task until a human reconciles the physical row.
                if (
                    task.commit_state == TASK_COMMIT_STATE_PHYSICAL_ITEM_CREATED_UNCONFIRMED
                    or task.physical_state_uncertain
                ):
                    plan.run_state = RUN_STATE_PAUSED
                    plan.stop_reason_category = STOP_REASON_PROJECT_LEVEL_HARD_STOP
                    plan.resume_cursor = plan.tasks.index(task)
                    save_execution_plan(plan, project_dir)
                    write_all_execution_reports(plan, project_dir)
                    return plan
                plan.resume_cursor = plan.tasks.index(task) + 1
                save_execution_plan(plan, project_dir)

        if not dry_run:
            group.state = GROUP_COMPLETED if all(t.state != TASK_PENDING for t in group_tasks) else group.state

    if not dry_run:
        all_terminal = all(t.state != TASK_PENDING for t in plan.tasks)
        plan.run_state = RUN_STATE_COMPLETED if all_terminal else RUN_STATE_PAUSED
        if all_terminal:
            plan.stop_reason_category = STOP_REASON_NORMAL_COMPLETION
        elif any(g.state == GROUP_FAILED for g in plan.groups):
            plan.stop_reason_category = STOP_REASON_GROUP_VERIFICATION_FAILURE
        else:
            plan.stop_reason_category = STOP_REASON_TASK_LEVEL_STOPS
        save_execution_plan(plan, project_dir)
        write_all_execution_reports(plan, project_dir)

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
