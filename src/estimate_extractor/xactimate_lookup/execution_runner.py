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
from estimate_extractor.xactimate_lookup.adapter import AdapterError, ProtectedCommittedRowError, XactimateAdapter
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
    LOOKUP_STRATEGY_REVIEW_APPROVED,
    LOOKUP_STRATEGY_TEST_DESCRIPTION_FIRST,
    RUN_STATE_COMPLETED,
    RUN_STATE_IN_PROGRESS,
    RUN_STATE_PAUSED,
    STOP_REASON_GROUP_VERIFICATION_FAILURE,
    STOP_REASON_NORMAL_COMPLETION,
    STOP_REASON_PROJECT_LEVEL_HARD_STOP,
    STOP_REASON_PROJECT_VERIFICATION_FAILURE,
    STOP_REASON_PROTECTED_ROW_REFUSAL,
    STOP_REASON_TASK_LEVEL_STOPS,
    TASK_COMMIT_STATE_COMMITTED,
    TASK_COMMIT_STATE_NOT_COMMITTED,
    TASK_COMPLETED,
    TASK_FAILED,
    TASK_PENDING,
    TASK_REVIEW_REQUIRED,
    TASK_SKIPPED,
    TEST_ONLY_PROJECT_NAME,
    commit_state_from_trust_state,
    is_plan_stale,
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
    STOP_REASON_EXTRACTION_FAILED,
    STOP_REASON_NO_RESULTS,
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

    if dry_run:
        # Nothing was actually executed -- record what WOULD happen
        # without claiming any state transition occurred.
        task.stop_detail = outcome.stop_detail or f"dry_run: decision={outcome.decision}"
        return

    if not outcome.committed:
        task.commit_state = TASK_COMMIT_STATE_NOT_COMMITTED
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
        task.stop_detail = (task.stop_detail or "") + " Committed, but the adapter does not support commit verification."
        return

    task.trust_state = getattr(verification, "trust_state", None)
    task.commit_state = commit_state_from_trust_state(task.trust_state)
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
    else:
        task.state = TASK_REVIEW_REQUIRED


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


#: Phase 5.5D Stage 7: the fixed, ordered attempt-type labels a began_
#: unmapped task's search sequence can produce -- recorded on each
#: ExecutionTask.search_attempts entry's "search_type" field.
SEARCH_TYPE_VERIFIED_SEARCH_DESCRIPTION = "verified_search_description"
SEARCH_TYPE_EXACT_DESCRIPTION = "exact_description"
SEARCH_TYPE_NORMALIZED_DESCRIPTION = "normalized_description"
SEARCH_TYPE_COMPACT_GENERATED_PHRASE = "compact_generated_phrase"
SEARCH_TYPE_TRUSTED_OBSERVED_CAT_SEL = "trusted_observed_cat_sel"

#: Decision/stop_reason combinations that mean "try the next attempt" --
#: everything else means "stop here, this attempt produced a defensible
#: result" (an AUTO_SELECT, or a REVIEW_REQUIRED for any reason OTHER
#: than these two -- e.g. an ambiguous ranking IS defensible: a human
#: should look at THAT candidate, not have the row silently re-searched
#: with different text).
_ADVANCE_STOP_REASONS = frozenset({STOP_REASON_NO_RESULTS, STOP_REASON_EXTRACTION_FAILED, STOP_REASON_UNEXPECTED_DIALOG})


def _description_first_search_attempts(
    task: ExecutionTask, phrase_rules: PhraseRules, project_dir: Path,
) -> list[tuple[str, LookupPlan]]:
    """Builds the bounded, ordered attempt sequence for a began_unmapped
    task (Phase 5.5D Stage 7, extended Phase 5.7A): a previously
    VERIFIED search description for this exact line item (if this row
    was already executed and verified in an earlier run -- e.g. a
    resume/retry), then the exact source description, normalized
    description, the existing compact/generated phrase, then -- only if
    a previous VERIFIED commit of this exact line item produced one --
    a trusted CAT/SEL as the FINAL fallback. Never starts with CAT/SEL,
    and a learned CAT/SEL NEVER substitutes for a description attempt
    -- see docs/build-estimate.md Phase 5.7A: a live incident where
    SFG/GUTA (the group-verification probe's own hardcoded disposable
    test item, unrelated to any PDF row -- see WindowsXactimateAdapter.
    _verify_group_once()) was mistaken for an unmapped row's search
    input prompted tracing this whole sequence end-to-end; the trace
    confirmed description-first ordering was already correct, and this
    phase adds the verified-description-reuse attempt plus locks the
    ordering down with explicit regression tests. Reuses generate_
    search_phrase()/compute_normalized_description() exactly as they
    already exist elsewhere in this codebase -- no global phrase-
    generation or ranking change. Skips a candidate attempt whose text
    is empty or exactly duplicates an earlier attempt's text (nothing
    new to learn from re-running an identical search)."""
    attempts: list[tuple[str, LookupPlan]] = []
    seen_texts: set[str] = set()

    def _add(search_type: str, text: str | None, *, phrase_result=None) -> None:
        if not text or not text.strip():
            return
        normalized_text = text.strip()
        if normalized_text.lower() in seen_texts:
            return
        seen_texts.add(normalized_text.lower())
        attempts.append((
            search_type,
            LookupPlan(
                line_item_id=task.line_item_id, path=LOOKUP_PATH_DESCRIPTION_SEARCH,
                item_signature="", search_input=normalized_text, phrase_result=phrase_result,
            ),
        ))

    # Phase 5.7A: a DESCRIPTION, not a CAT/SEL shortcut -- reused only
    # when a previous run of this EXACT row was independently verified.
    # Still a description-search attempt like any other, ranked and
    # safety-stopped identically; the only thing "learned" here is
    # which wording to type first, never which candidate to pick.
    _add(SEARCH_TYPE_VERIFIED_SEARCH_DESCRIPTION, _find_verified_search_description(project_dir, task.line_item_id))

    _add(SEARCH_TYPE_EXACT_DESCRIPTION, task.description)
    _add(SEARCH_TYPE_NORMALIZED_DESCRIPTION, compute_normalized_description(
        task.normalized_trade, task.normalized_component, task.normalized_material, task.normalized_action,
    ))
    phrase_result = generate_search_phrase(
        task.description, task.normalized_component, task.normalized_material, task.normalized_action, phrase_rules,
    )
    _add(SEARCH_TYPE_COMPACT_GENERATED_PHRASE, phrase_result.phrase, phrase_result=phrase_result)

    # Phase 5.7A: CAT/SEL remains the LAST resort -- every description
    # attempt above is tried and must fail (no_results/extraction-
    # failed/unexpected-dialog) before this is ever reached. Learned
    # CAT/SEL knowledge is candidate/result metadata, never a reason to
    # skip searching by description first.
    trusted = _find_trusted_observed_mapping(project_dir, task.line_item_id)
    if trusted is not None:
        category, selector = trusted
        trusted_mapping = InternalMappingRecord(
            mapping_id=f"observed_mapping:{task.line_item_id}",
            item_signature="", source_description=task.description, search_phrase="",
            category=category, selector=selector, xactimate_description=task.description,
            unit=task.expected_unit, action=None, reviewer="",
            approval_reason="Reused from a previous VERIFIED commit of this exact line item (Phase 5.5D Stage 7).",
            status=MAPPING_STATUS_APPROVED,
        )
        attempts.append((
            SEARCH_TYPE_TRUSTED_OBSERVED_CAT_SEL,
            LookupPlan(
                line_item_id=task.line_item_id, path=LOOKUP_PATH_TRUSTED, item_signature="",
                search_input=f"{category} {selector}", trusted_mapping=trusted_mapping,
            ),
        ))
    return attempts


def _run_description_first_task(
    task: ExecutionTask, item: RecommendationInput, adapter, ranking_config: RankingConfig,
    phrase_rules: PhraseRules, project_dir: Path, dry_run: bool,
):
    """Phase 5.5D Stage 7: runs a began_unmapped task through the
    bounded attempt sequence from _description_first_search_attempts(),
    stopping at the FIRST attempt that produces a defensible result
    (anything other than no-results/extraction-failure/unexpected-
    dialog) -- reusing orchestrator.execute_plan() UNCHANGED for every
    individual attempt, so ranking/safety-stop/commit/verify behavior
    is identical to any other lookup. Records the full attempt trail on
    `task.search_attempts` regardless of outcome. Returns the winning
    (or final) LookupOutcome, and (actual_strategy, reason) exactly
    like _task_to_lookup_plan() -- kept in that same 3-tuple shape so
    the caller's existing task.actual_lookup_strategy/lookup_strategy_
    reason assignment doesn't need special-casing."""
    attempts = _description_first_search_attempts(task, phrase_rules, project_dir)
    if not attempts:
        raise UnsafeLookupRouting(
            f"{task.task_id}: no search attempt could be built (empty description and no fallback) -- "
            f"refusing to guess."
        )

    outcome = None
    for attempt_number, (search_type, attempt_plan) in enumerate(attempts, start=1):
        outcome = orchestrator.execute_plan(attempt_plan, item, adapter, ranking_config, phrase_rules, dry_run=dry_run)
        is_last_attempt = attempt_number == len(attempts)
        should_advance = (
            not is_last_attempt
            and (outcome.decision == DECISION_NO_MATCH or outcome.stop_reason in _ADVANCE_STOP_REASONS)
        )
        top = outcome.candidates[0] if outcome.candidates else None
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
            "advance_reason": (
                "no defensible candidate -- trying the next attempt" if should_advance
                else ("final attempt in the sequence" if is_last_attempt else "defensible result found -- stopping here")
            ),
        })
        if not should_advance:
            break

    reason = (
        f"test_description_first: attempt {len(task.search_attempts)}/{len(attempts)} "
        f"({task.search_attempts[-1]['search_type']}) -- {task.search_attempts[-1]['advance_reason']}."
    )
    actual_strategy = LOOKUP_PATH_TRUSTED if outcome.plan.path == LOOKUP_PATH_TRUSTED else LOOKUP_PATH_DESCRIPTION_SEARCH
    return outcome, actual_strategy, reason


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
                    task.stop_detail = f"Group not verified: {detail}"
                    task.completed_at = utc_now_iso()
                save_execution_plan(plan, project_dir)
                continue

        for task in pending_tasks:
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
                # Phase 5.5D Stage 7: a began_unmapped task runs through
                # the bounded exact-description-first attempt sequence
                # instead of a single generated-phrase search -- see
                # _run_description_first_task()'s docstring. Every other
                # (today, only review_approved) task is completely
                # unchanged: one _task_to_lookup_plan() call, one
                # orchestrator.execute_plan() call, exactly as before
                # this phase.
                if task.lookup_strategy == LOOKUP_STRATEGY_TEST_DESCRIPTION_FIRST:
                    outcome, actual_strategy, reason = _run_description_first_task(
                        task, item, adapter, ranking_config, phrase_rules, project_dir, dry_run,
                    )
                else:
                    lookup_plan, actual_strategy, reason = _task_to_lookup_plan(task, phrase_rules)
                    outcome = orchestrator.execute_plan(lookup_plan, item, adapter, ranking_config, phrase_rules, dry_run=dry_run)

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
