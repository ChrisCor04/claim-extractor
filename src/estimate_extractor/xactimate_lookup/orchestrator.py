"""Lookup decision flow: trusted-mapping-first, description-search-
second, with the safety-stop rules from the build spec. This module is
the only place that calls into an XactimateAdapter -- ranking.py and
registry.py know nothing about adapters, and adapter.py knows nothing
about ranking. See docs/xactimate-lookup.md "Lookup decision flow".

``execute_plan(..., dry_run=True)`` (the CLI/UI default) runs the entire
pipeline -- search, capture, rank, decide -- but never calls
``select_candidate`` / ``enter_quantity`` / ``commit_item`` on the
adapter, regardless of the decision. An explicit ``dry_run=False`` call
can commit something ONLY when the adapter declares
``supports_live_execution = True`` -- see build spec "Do not fabricate
successful automation."  -- this module never constructs a live adapter
itself.
"""

from __future__ import annotations

import sqlite3

from estimate_extractor.xactimate_lookup import registry, signature as signature_mod
from estimate_extractor.xactimate_lookup.adapter import (
    AdapterError,
    ProtectedCommittedRowError,
    UnexpectedDialogError,
    XactimateAdapter,
)
from estimate_extractor.xactimate_lookup.models import (
    DECISION_AUTO_SELECT,
    DECISION_NO_MATCH,
    DECISION_REVIEW_REQUIRED,
    MAPPING_STATUS_APPROVED,
    LOOKUP_PATH_DESCRIPTION_SEARCH,
    LOOKUP_PATH_TRUSTED,
    STOP_REASON_ADAPTER_ERROR,
    STOP_REASON_AMBIGUOUS,
    STOP_REASON_CONTEXT_UNVERIFIED,
    STOP_REASON_EXTRACTION_FAILED,
    STOP_REASON_FIELD_MISMATCH,
    STOP_REASON_HARD_CONFLICT,
    STOP_REASON_NO_RESULTS,
    STOP_REASON_UNEXPECTED_DIALOG,
    STOP_REASON_UNIT_MISMATCH,
    STOP_REASON_UNIT_QUANTITY_INVALID,
    STOP_REASON_UNSUPPORTED_ADAPTER,
    InternalMappingRecord,
    LookupOutcome,
    LookupPlan,
    PopulatedFields,
    RecommendationInput,
)
from estimate_extractor.xactimate_lookup.phrase_generator import PhraseRules, generate_search_phrase
from estimate_extractor.xactimate_lookup.ranking import RankingConfig, classify_decision, rank_dropdown_results


def _verified_catalog_trusted_mapping(item: RecommendationInput, verified_records: list, item_signature: str) -> InternalMappingRecord | None:
    """A Phase 3.5 human-verified catalog rule counts as trusted too --
    see build spec step 1 'trusted mapping for that item or item
    signature'. Constructed transiently (never persisted to the
    registry) so orchestrator.py has one uniform 'trusted' shape to read
    from regardless of which store it came from."""
    if not verified_records:
        return None
    from estimate_extractor.ui import verified_catalog_service as vcs

    row_shape = {
        "normalized_trade": item.trade,
        "normalized_component": item.component,
        "unit": item.source_unit,
        "normalized_action": item.action,
        "original_description": item.original_description,
    }
    matches = [
        m for m in vcs.find_verified_matches(row_shape, verified_records)
        if m.record.verification_status == vcs.VERIFICATION_STATUS_HUMAN_VERIFIED
    ]
    if not matches:
        return None
    v = matches[0].record
    return InternalMappingRecord(
        mapping_id=f"verified_catalog:{v.catalog_record_id}",
        item_signature=item_signature,
        source_description=item.original_description or "",
        search_phrase="",
        category=v.category,
        selector=v.selector,
        xactimate_description=v.description,
        unit=v.unit,
        action=item.action,
        reviewer=v.verified_by or "",
        approval_reason="Phase 3.5 human-verified catalog rule",
        status=MAPPING_STATUS_APPROVED,
    )


def build_lookup_plan(
    item: RecommendationInput,
    registry_conn: sqlite3.Connection,
    phrase_rules: PhraseRules,
    verified_records: list | None = None,
) -> LookupPlan:
    item_signature = signature_mod.compute_item_signature(
        item.trade, item.component, item.material, item.action, item.source_unit, item.original_description or "", phrase_rules
    )

    trusted = registry.find_reusable_mapping(registry_conn, item_signature)
    if trusted is None:
        trusted = _verified_catalog_trusted_mapping(item, verified_records or [], item_signature)

    if trusted is not None:
        return LookupPlan(
            line_item_id=item.line_item_id,
            path=LOOKUP_PATH_TRUSTED,
            item_signature=item_signature,
            search_input=f"{trusted.category} {trusted.selector}",
            trusted_mapping=trusted,
        )

    phrase_result = generate_search_phrase(item.original_description or "", item.component, item.material, item.action, phrase_rules)
    return LookupPlan(
        line_item_id=item.line_item_id,
        path=LOOKUP_PATH_DESCRIPTION_SEARCH,
        item_signature=item_signature,
        search_input=phrase_result.phrase,
        phrase_result=phrase_result,
    )


def _stop(line_item_id: str, plan: LookupPlan, decision: str, reason: str, detail: str, candidates=None, selected=None) -> LookupOutcome:
    return LookupOutcome(
        line_item_id=line_item_id, decision=decision, plan=plan, candidates=candidates or [], selected=selected,
        stop_reason=reason, stop_detail=detail,
    )


def _cancel_pending_selection(adapter: XactimateAdapter) -> None:
    """Live-caught (Phase 5.3): `select_candidate()` already puts a
    PENDING row in the grid before this function ever decides whether
    to commit (see the `before_snapshot` comment above) -- if a
    post-selection safety check (field mismatch, unit mismatch) stops
    the task here, that pending row is left behind uncancelled. It is
    never explicitly committed by THIS task, but a real live run
    reproduced it being silently persisted anyway: a LATER, unrelated
    `commit_item()` call (from a different task, or `verify_group()`'s
    own probe-and-cleanup cycle) saves the estimate's current on-screen
    state wholesale, which includes this stale pending row -- with
    whatever default quantity Xactimate assigned it, not the source
    line item's real quantity. That is a real wrong-data commit that
    never went through this module's own commit path at all. Not part
    of the abstract adapter contract (duck-typed, like every other
    Windows-only capability here) -- best-effort and silent on failure,
    since a cleanup failure must never mask the original stop reason.

    Live-caught (Phase 5.4): a SINGLE `cancel_current_item()` call is
    not reliable enough on its own -- confirmed live, reproducibly,
    that the very first attempt can fail with "row count did not
    decrease" even though a second immediate attempt succeeds (the
    same flakiness `_cleanup_probe_item()`/`ensure_group()` already
    retry around elsewhere in this codebase). A single try-and-swallow
    here silently left real financial residue behind (confirmed live:
    a $330.31 row survived a field-mismatch stop). Bounded retry --
    never unbounded -- closes this without weakening the "never let
    cleanup mask the original stop reason" contract: it still never
    raises -- EXCEPT for ProtectedCommittedRowError (Phase 5.5D), which
    is never a transient flakiness signal to retry past: it means this
    specific cancel would have deleted a row Execute already
    successfully committed. That is a hard stop for the whole run, not
    a per-task condition to swallow -- propagated deliberately."""
    if not hasattr(adapter, "cancel_current_item"):
        return
    cancelled = False
    for _attempt in range(3):
        try:
            adapter.cancel_current_item(reason="pending_uncommitted_selection", caller="_cancel_pending_selection")
            cancelled = True
            break
        except ProtectedCommittedRowError:
            raise
        except Exception:
            continue
    # Live-caught (Phase 5.4): cancelling the pending row does NOT by
    # itself return the project to a "Saved" state -- it left "Unsaved
    # changes" behind on every trial even when the cancel itself
    # succeeded and no financial residue remained. Explicitly saving
    # afterward (matching `_cleanup_probe_item()`'s own established
    # cancel-then-commit pattern) closes that gap. Best-effort: a save
    # failure here must not mask the original stop reason either.
    if cancelled and hasattr(adapter, "commit_item"):
        try:
            adapter.commit_item()
        except Exception:
            pass


def _record_lifecycle(adapter: XactimateAdapter, event: str, **detail) -> None:
    """Phase 5.9: duck-typed, best-effort row-lifecycle logging -- see
    execution_diagnostics.py. A FakeXactimateAdapter/non-Windows
    adapter without record_lifecycle_event() is completely unaffected;
    never raises, never affects control flow."""
    if hasattr(adapter, "record_lifecycle_event"):
        try:
            adapter.record_lifecycle_event(event, **detail)
        except Exception:
            pass


def _activate_description_selected_candidate(
    adapter: XactimateAdapter,
    plan: LookupPlan,
    candidate,
    before_snapshot,
) -> bool:
    """Activate the description-ranked candidate and prove a pending
    row exists.  Some Xactimate builds silently ignore a duplicate item
    clicked from description results, while exact CAT/SEL search opens
    the intentional-duplicate flow.  Description remains the sole
    decision/ranking path; CAT/SEL is used only to instantiate that
    already-chosen identity, and only after the first activation was
    positively observed to create nothing.

    Adapters without the two narrow hooks retain their prior behavior.
    The Windows adapter implements both and fails closed unless its
    existing distinct-task duplicate ledger authorizes the fallback.
    """
    adapter.select_candidate(candidate)
    if not hasattr(adapter, "pending_item_created"):
        return True
    if adapter.pending_item_created(before_snapshot):
        return True

    allowed = (
        plan.path == LOOKUP_PATH_DESCRIPTION_SEARCH
        and hasattr(adapter, "allows_intentional_duplicate")
        and adapter.allows_intentional_duplicate(candidate)
    )
    if not allowed:
        return False

    _record_lifecycle(
        adapter, "DESCRIPTION_SELECTION_CREATED_NO_PENDING_ITEM",
        category=candidate.category, selector=candidate.selector,
    )
    adapter.focus_search()
    adapter.clear_search()
    adapter.search_by_category_selector(candidate.category, candidate.selector)
    raw = adapter.capture_dropdown()
    exact_results = [
        result for result in adapter.parse_dropdown(raw)
        if (result.category, result.selector) == (candidate.category, candidate.selector)
    ]
    if not exact_results:
        raise AdapterError(
            "CAT/SEL instantiation fallback returned no exact match for the "
            f"description-selected candidate {candidate.category}/{candidate.selector}."
        )
    adapter.select_candidate(exact_results[0])
    _record_lifecycle(
        adapter, "CAT_SEL_INSTANTIATION_FALLBACK_SELECTED",
        category=candidate.category, selector=candidate.selector,
    )
    return bool(adapter.pending_item_created(before_snapshot))


def _stop_existing(outcome: LookupOutcome, reason: str, detail: str) -> LookupOutcome:
    """Stop after activation without discarding physical-row evidence."""
    outcome.decision = DECISION_REVIEW_REQUIRED
    outcome.stop_reason = reason
    outcome.stop_detail = detail
    return outcome


def execute_plan(
    plan: LookupPlan,
    item: RecommendationInput,
    adapter: XactimateAdapter,
    ranking_config: RankingConfig,
    phrase_rules: PhraseRules,
    *,
    dry_run: bool = True,
    force_auto_select_for_trusted_mapping: bool = False,
) -> LookupOutcome:
    """`force_auto_select_for_trusted_mapping` (Phase 5.17, default
    False -- every existing/production call site is unaffected): for a
    LOOKUP_PATH_TRUSTED plan ONLY, treats the top candidate as
    AUTO_SELECT once it's confirmed to be exactly the trusted mapping's
    own category/selector with no hard conflict, bypassing classify_
    decision()'s ordinary score/margin gates. Reserved for a human
    reviewer's own explicit, one-time confirmation of a special/bid-
    item mapping (a "LEARNED_SPECIAL_ITEM" teach-and-commit) -- NOT
    wired into classify_decision() itself, which stays exactly as it
    was before this phase. Live-caught: a genuine bid-item catalog
    entry ("Light Fixtures (Bid Item)" for "String Light") scores only
    ~0.70 on ordinary text-similarity dimensions despite being the
    human-confirmed correct answer, because nothing about its terse
    catalog description resembles the source wording -- exactly the
    class of item classify_decision()'s ordinary gates are not equipped
    to recognize, and exactly why a trusted mapping exists as a
    separate path in the first place. An EARLIER attempt to fold this
    into classify_decision() itself (treating ANY prior_verified_
    mapping-backed candidate as AUTO_SELECT) was reverted after it
    proved too broad: ordinary review-approved/pre-mapped tasks ALSO
    route through LOOKUP_PATH_TRUSTED and legitimately need their own
    ambiguous-candidate cases caught -- this parameter's caller-must-
    opt-in shape is what keeps that regression from recurring."""
    if not dry_run and not adapter.supports_live_execution:
        return _stop(
            item.line_item_id, plan, DECISION_REVIEW_REQUIRED, STOP_REASON_UNSUPPORTED_ADAPTER,
            f"Adapter {type(adapter).__name__!r} does not declare supports_live_execution=True; "
            f"refusing to commit anything live (see build spec 'Do not fabricate successful automation.').",
        )

    if not adapter.verify_application():
        return _stop(item.line_item_id, plan, DECISION_REVIEW_REQUIRED, STOP_REASON_CONTEXT_UNVERIFIED, "Adapter could not verify the Xactimate application is running.")
    if not adapter.verify_project():
        return _stop(item.line_item_id, plan, DECISION_REVIEW_REQUIRED, STOP_REASON_CONTEXT_UNVERIFIED, "Adapter could not verify the active Xactimate project/estimate context.")

    _record_lifecycle(adapter, "SEARCH_STARTED", search_input=plan.search_input, path=plan.path)
    adapter.focus_search()
    adapter.clear_search()
    if plan.path == LOOKUP_PATH_TRUSTED:
        adapter.search_by_category_selector(plan.trusted_mapping.category, plan.trusted_mapping.selector)
    else:
        adapter.search_by_description(plan.search_input)

    try:
        raw = adapter.capture_dropdown()
        dropdowns = adapter.parse_dropdown(raw)
        _record_lifecycle(adapter, "POPUP_OBSERVED")
        _record_lifecycle(adapter, "CANDIDATES_CAPTURED", count=len(dropdowns))
    except UnexpectedDialogError as exc:
        adapter.recover()
        return _stop(item.line_item_id, plan, DECISION_REVIEW_REQUIRED, STOP_REASON_UNEXPECTED_DIALOG, str(exc))
    except AdapterError as exc:
        adapter.recover()
        return _stop(item.line_item_id, plan, DECISION_REVIEW_REQUIRED, STOP_REASON_EXTRACTION_FAILED, str(exc))

    if not dropdowns:
        # Phase 5.8A (live-caught): a NO_MATCH/REVIEW_REQUIRED decision
        # never calls select_candidate() -- nothing ever clicks the
        # results popup closed the way an AUTO_SELECT commit naturally
        # does. Live-verified: the popup stays open after this return
        # unless explicitly dismissed here, exactly like the exception
        # paths above already do. Left open, it was still on screen
        # when the NEXT group's setup (ensure_group()'s Components-tab
        # click, in particular) started interacting with the window --
        # the reproduced "final item's dropdown is still open when the
        # automation moves on to the next group" defect. recover() only
        # touches adapter-internal popup bookkeeping and presses
        # Escape; it never touches the `candidates`/outcome data already
        # computed for this return.
        adapter.recover()
        return _stop(item.line_item_id, plan, DECISION_NO_MATCH, STOP_REASON_NO_RESULTS, f"No dropdown results for {plan.search_input!r}.")

    size_key = signature_mod.compute_size_key(item.original_description or "")
    grade_key = signature_mod.compute_grade_key(item.original_description or "", phrase_rules)
    candidates = rank_dropdown_results(
        original_description=item.original_description or "",
        trade=item.trade, component=item.component, material=item.material, action=item.action,
        unit=item.source_unit, size_key=size_key, grade_key=grade_key, dropdowns=dropdowns,
        rules=phrase_rules, config=ranking_config, prior_verified_mapping=(plan.path == LOOKUP_PATH_TRUSTED),
    )
    decision = classify_decision(candidates, ranking_config)
    _record_lifecycle(adapter, "DECISION_MADE", decision=decision)

    if (
        force_auto_select_for_trusted_mapping
        and decision != DECISION_AUTO_SELECT
        and plan.path == LOOKUP_PATH_TRUSTED
        and candidates
        and not candidates[0].has_hard_conflict
        and candidates[0].dropdown.category == plan.trusted_mapping.category
        and candidates[0].dropdown.selector == plan.trusted_mapping.selector
    ):
        decision = DECISION_AUTO_SELECT
        _record_lifecycle(adapter, "DECISION_OVERRIDDEN_TRUSTED_MAPPING_REVIEWER_CONFIRMED", original_score=candidates[0].score)

    if decision == DECISION_NO_MATCH:
        adapter.recover()  # Phase 5.8A: same popup-left-open fix as above
        return _stop(item.line_item_id, plan, DECISION_NO_MATCH, STOP_REASON_NO_RESULTS, "No candidate scored above the review-required threshold.", candidates=candidates)

    if decision == DECISION_REVIEW_REQUIRED:
        top = candidates[0]
        adapter.recover()  # Phase 5.8A: same popup-left-open fix as above
        if top.has_hard_conflict:
            return _stop(item.line_item_id, plan, DECISION_REVIEW_REQUIRED, STOP_REASON_HARD_CONFLICT, "; ".join(top.conflict_reasons), candidates=candidates, selected=top)
        return _stop(
            item.line_item_id, plan, DECISION_REVIEW_REQUIRED, STOP_REASON_AMBIGUOUS,
            "Top candidate lacks a clear margin, sufficient score, or reliable extraction confidence.",
            candidates=candidates, selected=top,
        )

    # DECISION_AUTO_SELECT
    top = candidates[0]

    if item.quantity is None or item.quantity <= 0:
        return _stop(item.line_item_id, plan, DECISION_REVIEW_REQUIRED, STOP_REASON_UNIT_QUANTITY_INVALID, f"Invalid quantity for commit: {item.quantity!r}.", candidates=candidates, selected=top)

    outcome = LookupOutcome(line_item_id=item.line_item_id, decision=decision, plan=plan, candidates=candidates, selected=top)

    if dry_run:
        outcome.stop_detail = "dry_run: plan only, adapter selection/commit not executed."
        return outcome

    # Phase 4.8: a before-commit grid snapshot must be taken BEFORE
    # select_candidate() -- the pending row is already present in the
    # grid as soon as a candidate is selected, well before commit_item()
    # (see windows_adapter.py's verify_commit() docstring). Taken
    # unconditionally here, before we know whether this candidate will
    # actually reach a commit, since that's the only point in this
    # function where "before" is still true. Duck-typed: only adapters
    # that implement snapshot_grid_identities()/verify_commit() (today,
    # WindowsXactimateAdapter) get independent post-commit verification;
    # everything else behaves exactly as before this change.
    if hasattr(adapter, "snapshot_grid_identities_for_activation"):
        before_snapshot = adapter.snapshot_grid_identities_for_activation()
    else:
        before_snapshot = adapter.snapshot_grid_identities() if hasattr(adapter, "snapshot_grid_identities") else None

    try:
        pending_item_created = _activate_description_selected_candidate(
            adapter, plan, top.dropdown, before_snapshot,
        )
        if not pending_item_created:
            raise AdapterError(
                "Candidate activation created no pending physical item; "
                "CAT/SEL instantiation fallback was unavailable or also created nothing."
            )
        outcome.physical_item_created = True
        # Persist the physical-created checkpoint before quantity entry.
        # This is deliberately separate from the eventual outcome apply:
        # a crash or later failure must not make a resume instantiate the
        # same task a second time.
        adapter.record_physical_item_created()
        _record_lifecycle(adapter, "CANDIDATE_SELECTED", category=top.dropdown.category, selector=top.dropdown.selector)
    except UnexpectedDialogError as exc:
        adapter.recover()
        outcome.decision = DECISION_REVIEW_REQUIRED
        outcome.stop_reason = STOP_REASON_UNEXPECTED_DIALOG
        outcome.stop_detail = str(exc)
        return outcome
    except AdapterError as exc:
        adapter.recover()
        outcome.decision = DECISION_REVIEW_REQUIRED
        outcome.stop_reason = STOP_REASON_ADAPTER_ERROR
        outcome.stop_detail = str(exc)
        return outcome

    # The exact live UIA match in select_candidate(), combined with the
    # positive physical delta above, is the pre-quantity instantiation
    # proof.  Do not call read_populated_fields() here: its grid OCR may
    # scroll/reset the whole right pane before quantity entry.  Preserve
    # the UIA-proven identity on the outcome without claiming fields that
    # were not independently observed.
    outcome.populated_fields = PopulatedFields(
        category=top.dropdown.category,
        selector=top.dropdown.selector,
        description=top.dropdown.description,
        unit=None,
        action=None,
        item_number=top.dropdown.item_number,
    )

    try:
        adapter.enter_quantity(item.quantity)
        _record_lifecycle(adapter, "QUANTITY_ENTERED", quantity=item.quantity)
        _record_lifecycle(adapter, "COMMIT_STARTED")
        adapter.commit_item()
        _record_lifecycle(adapter, "COMMIT_RETURNED")
    except AdapterError as exc:
        adapter.recover()
        outcome.decision = DECISION_REVIEW_REQUIRED
        outcome.stop_reason = STOP_REASON_EXTRACTION_FAILED
        outcome.stop_detail = str(exc)
        return outcome

    outcome.committed = True

    # Phase 5.5D: protect this row as soon as commit_item() has
    # completed successfully -- deliberately BEFORE verify_commit()
    # below, which can itself legitimately fail/mismatch after a real
    # row already landed; the row must stay protected regardless of
    # that outcome. Best-effort/duck-typed (only WindowsXactimateAdapter
    # implements it) and never raises -- see record_protected_commit()'s
    # own docstring.
    if hasattr(adapter, "record_protected_commit"):
        adapter.record_protected_commit(
            category=top.dropdown.category, selector=top.dropdown.selector,
            description=item.original_description, quantity=item.quantity, unit=item.source_unit,
        )
    _record_lifecycle(adapter, "ROW_OBSERVED_IN_GRID", category=top.dropdown.category, selector=top.dropdown.selector)

    outcome.evidence_reference = adapter.capture_evidence()
    if before_snapshot is not None and hasattr(adapter, "verify_commit"):
        outcome.verification = adapter.verify_commit(
            before_snapshot, top.dropdown.category, top.dropdown.selector, item.quantity,
            source_unit=item.source_unit, expected_xactimate_unit=item.source_unit,
            populated_unit=outcome.populated_fields.unit,
        )
        _record_lifecycle(adapter, "VERIFIED", trust_state=getattr(outcome.verification, "trust_state", None))
    return outcome
