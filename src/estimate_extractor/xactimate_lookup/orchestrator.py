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


def execute_plan(
    plan: LookupPlan,
    item: RecommendationInput,
    adapter: XactimateAdapter,
    ranking_config: RankingConfig,
    phrase_rules: PhraseRules,
    *,
    dry_run: bool = True,
) -> LookupOutcome:
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

    adapter.focus_search()
    adapter.clear_search()
    if plan.path == LOOKUP_PATH_TRUSTED:
        adapter.search_by_category_selector(plan.trusted_mapping.category, plan.trusted_mapping.selector)
    else:
        adapter.search_by_description(plan.search_input)

    try:
        raw = adapter.capture_dropdown()
        dropdowns = adapter.parse_dropdown(raw)
    except UnexpectedDialogError as exc:
        adapter.recover()
        return _stop(item.line_item_id, plan, DECISION_REVIEW_REQUIRED, STOP_REASON_UNEXPECTED_DIALOG, str(exc))
    except AdapterError as exc:
        adapter.recover()
        return _stop(item.line_item_id, plan, DECISION_REVIEW_REQUIRED, STOP_REASON_EXTRACTION_FAILED, str(exc))

    if not dropdowns:
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

    if decision == DECISION_NO_MATCH:
        return _stop(item.line_item_id, plan, DECISION_NO_MATCH, STOP_REASON_NO_RESULTS, "No candidate scored above the review-required threshold.", candidates=candidates)

    if decision == DECISION_REVIEW_REQUIRED:
        top = candidates[0]
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
    before_snapshot = adapter.snapshot_grid_identities() if hasattr(adapter, "snapshot_grid_identities") else None

    try:
        adapter.select_candidate(top.dropdown)
        populated = adapter.read_populated_fields()
    except UnexpectedDialogError as exc:
        adapter.recover()
        return _stop(item.line_item_id, plan, DECISION_REVIEW_REQUIRED, STOP_REASON_UNEXPECTED_DIALOG, str(exc), candidates=candidates, selected=top)
    except AdapterError as exc:
        adapter.recover()
        return _stop(item.line_item_id, plan, DECISION_REVIEW_REQUIRED, STOP_REASON_ADAPTER_ERROR, str(exc), candidates=candidates, selected=top)

    outcome.populated_fields = populated

    if (populated.category, populated.selector) != (top.dropdown.category, top.dropdown.selector):
        _cancel_pending_selection(adapter)
        return _stop(
            item.line_item_id, plan, DECISION_REVIEW_REQUIRED, STOP_REASON_FIELD_MISMATCH,
            f"Populated fields ({populated.category}/{populated.selector}) differ from the selected "
            f"candidate ({top.dropdown.category}/{top.dropdown.selector}).",
            candidates=candidates, selected=top,
        )

    # Phase 5.6 Stage 4 (live-caught): the naive strict-equality check
    # this replaced treated OCR garbage ("sa", "')_|'", etc.) from the
    # freshly-populated Quick Entry unit field as equal-quality evidence
    # to a real unit mismatch -- reproduced live, blocking an otherwise
    # correct selection on unreadable OCR alone. `check_unit_
    # compatibility()` (duck-typed, like verify_commit/snapshot_grid_
    # identities -- only WindowsXactimateAdapter implements it today)
    # applies the SAME evidence-aware synonym/OCR-confusion logic
    # verify_commit()'s post-commit check already uses. Only a
    # CONFIRMED incompatible unit (both readable AND genuinely
    # different, e.g. EA vs LF) stops here pre-commit; an unreadable or
    # missing observed unit is not treated as evidence of a mismatch --
    # the commit proceeds and the more thorough post-commit verify_
    # commit() (which polls/retries) makes the final call. Adapters
    # without this method (e.g. FakeXactimateAdapter in tests) keep the
    # original strict-equality behavior unchanged.
    if item.source_unit and populated.unit:
        if hasattr(adapter, "check_unit_compatibility"):
            unit_result = adapter.check_unit_compatibility(item.source_unit, item.source_unit, populated.unit)
            if unit_result.unit_match_state == "incompatible":
                _cancel_pending_selection(adapter)
                return _stop(
                    item.line_item_id, plan, DECISION_REVIEW_REQUIRED, STOP_REASON_UNIT_MISMATCH,
                    f"Populated unit ({populated.unit!r}) is incompatible with the source line item's unit "
                    f"({item.source_unit!r}): {unit_result.unit_match_reason}",
                    candidates=candidates, selected=top,
                )
        elif item.source_unit.strip().upper() != populated.unit.strip().upper():
            _cancel_pending_selection(adapter)
            return _stop(
                item.line_item_id, plan, DECISION_REVIEW_REQUIRED, STOP_REASON_UNIT_MISMATCH,
                f"Populated unit ({populated.unit!r}) differs from the source line item's unit ({item.source_unit!r}).",
                candidates=candidates, selected=top,
            )

    try:
        adapter.enter_quantity(item.quantity)
        adapter.commit_item()
    except AdapterError as exc:
        adapter.recover()
        return _stop(item.line_item_id, plan, DECISION_REVIEW_REQUIRED, STOP_REASON_EXTRACTION_FAILED, str(exc), candidates=candidates, selected=top)

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

    outcome.evidence_reference = adapter.capture_evidence()
    if before_snapshot is not None and hasattr(adapter, "verify_commit"):
        outcome.verification = adapter.verify_commit(
            before_snapshot, top.dropdown.category, top.dropdown.selector, item.quantity,
            source_unit=item.source_unit, expected_xactimate_unit=item.source_unit,
            populated_unit=populated.unit,
        )
    return outcome
