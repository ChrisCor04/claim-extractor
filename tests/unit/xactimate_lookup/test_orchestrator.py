from __future__ import annotations

from estimate_extractor.xactimate_lookup import orchestrator, registry
from estimate_extractor.xactimate_lookup.adapter import AdapterError, FakeXactimateAdapter
from estimate_extractor.xactimate_lookup.models import (
    DECISION_AUTO_SELECT,
    DECISION_NO_MATCH,
    DECISION_REVIEW_REQUIRED,
    LOOKUP_PATH_DESCRIPTION_SEARCH,
    LOOKUP_PATH_TRUSTED,
    STOP_REASON_AMBIGUOUS,
    STOP_REASON_CONTEXT_UNVERIFIED,
    STOP_REASON_EXTRACTION_FAILED,
    STOP_REASON_FIELD_MISMATCH,
    STOP_REASON_NO_RESULTS,
    STOP_REASON_UNEXPECTED_DIALOG,
    STOP_REASON_UNIT_MISMATCH,
    STOP_REASON_UNIT_QUANTITY_INVALID,
    STOP_REASON_UNSUPPORTED_ADAPTER,
    DropdownResult,
    InternalMappingRecord,
    MAPPING_STATUS_APPROVED,
    RecommendationInput,
)


def _item(**overrides):
    defaults = dict(
        line_item_id="line_0001", original_description="Tear off composition shingles - 3 tab (no haul off)",
        trade="roofing", component="composition_shingles", material="3-tab composition shingles",
        action="remove", source_unit="SQ", quantity=10.0,
    )
    defaults.update(overrides)
    return RecommendationInput(**defaults)


def _dropdown(text, cat="RFG", sel="ARMVN", desc=None, pos=0, conf=0.97):
    return DropdownResult(raw_text=text, row_position=pos, category=cat, selector=sel, description=desc or text, extraction_confidence=conf)


def test_build_lookup_plan_uses_description_search_when_no_trusted_mapping(tmp_path, phrase_rules):
    conn = registry.create_database(tmp_path / "reg.db")
    plan = orchestrator.build_lookup_plan(_item(), conn, phrase_rules)
    conn.close()
    assert plan.path == LOOKUP_PATH_DESCRIPTION_SEARCH
    assert plan.search_input  # a real phrase, not empty
    assert plan.trusted_mapping is None


def test_build_lookup_plan_uses_trusted_path_when_registry_has_approved_mapping(tmp_path, phrase_rules):
    conn = registry.create_database(tmp_path / "reg.db")
    item = _item()
    sig = orchestrator.signature_mod.compute_item_signature(item.trade, item.component, item.material, item.action, item.source_unit, item.original_description, phrase_rules)
    registry.save_record(conn, InternalMappingRecord(
        mapping_id="m1", item_signature=sig, source_description=item.original_description, search_phrase="x",
        category="RFG", selector="ARMVN", xactimate_description="Tear off composition shingles - 3 tab",
        unit="SQ", action="remove", reviewer="tester", approval_reason="matches", status=MAPPING_STATUS_APPROVED,
    ))
    plan = orchestrator.build_lookup_plan(item, conn, phrase_rules)
    conn.close()
    assert plan.path == LOOKUP_PATH_TRUSTED
    assert plan.search_input == "RFG ARMVN"
    assert plan.trusted_mapping.mapping_id == "m1"


def test_execute_plan_stops_when_application_unverified(tmp_path, phrase_rules, ranking_config):
    conn = registry.create_database(tmp_path / "reg.db")
    item = _item()
    plan = orchestrator.build_lookup_plan(item, conn, phrase_rules)
    conn.close()
    adapter = FakeXactimateAdapter(application_verified=False)
    outcome = orchestrator.execute_plan(plan, item, adapter, ranking_config, phrase_rules, dry_run=True)
    assert outcome.stop_reason == STOP_REASON_CONTEXT_UNVERIFIED
    assert outcome.decision == DECISION_REVIEW_REQUIRED
    assert not any(name == "search_by_description" for name, _a, _k in adapter.log.calls)


def test_execute_plan_stops_when_project_unverified(tmp_path, phrase_rules, ranking_config):
    conn = registry.create_database(tmp_path / "reg.db")
    item = _item()
    plan = orchestrator.build_lookup_plan(item, conn, phrase_rules)
    conn.close()
    adapter = FakeXactimateAdapter(project_verified=False)
    outcome = orchestrator.execute_plan(plan, item, adapter, ranking_config, phrase_rules, dry_run=True)
    assert outcome.stop_reason == STOP_REASON_CONTEXT_UNVERIFIED
    assert not any(name == "search_by_description" for name, _a, _k in adapter.log.calls)


def test_execute_plan_stops_on_no_results(tmp_path, phrase_rules, ranking_config):
    conn = registry.create_database(tmp_path / "reg.db")
    item = _item()
    plan = orchestrator.build_lookup_plan(item, conn, phrase_rules)
    conn.close()
    adapter = FakeXactimateAdapter(dropdown_script={})
    outcome = orchestrator.execute_plan(plan, item, adapter, ranking_config, phrase_rules, dry_run=True)
    assert outcome.decision == DECISION_NO_MATCH
    assert outcome.stop_reason == STOP_REASON_NO_RESULTS
    # Phase 5.8A (live-caught): a NO_MATCH/REVIEW_REQUIRED decision never
    # calls select_candidate(), so nothing ever closes the results
    # popup the way a real commit naturally does -- recover() must be
    # called explicitly, or the popup is still open when execution
    # moves on to the next task/group (reproduced live: it visibly
    # stayed open and was still there when the next group's setup
    # began interacting with the window).
    assert adapter.log.calls[-1][0] == "recover"


def test_execute_plan_stops_on_no_results_after_ranking_scores_too_low(tmp_path, phrase_rules, ranking_config):
    """Companion to the case above: real candidates WERE captured (the
    popup genuinely opened and was read -- matches the live-caught
    "Clean Fence" case, real dropdown rows present but nothing scored
    high enough even for review), but every one scored below
    review_required_min -- a DIFFERENT code path (post-ranking, not the
    empty-dropdown early return) reaching the SAME DECISION_NO_MATCH
    outcome, and it must ALSO dismiss the popup."""
    conn = registry.create_database(tmp_path / "reg.db")
    item = _item(material="3-tab composition shingles")
    plan = orchestrator.build_lookup_plan(item, conn, phrase_rules)
    conn.close()
    unrelated = _dropdown("Tear off metal roofing panels", cat="RFG", sel="METAL", pos=0)
    adapter = FakeXactimateAdapter(dropdown_script={plan.search_input: [unrelated]})
    outcome = orchestrator.execute_plan(plan, item, adapter, ranking_config, phrase_rules, dry_run=True)
    assert outcome.decision == DECISION_NO_MATCH
    assert outcome.stop_reason == STOP_REASON_NO_RESULTS
    assert adapter.log.calls[-1][0] == "recover"


def test_execute_plan_stops_on_extraction_failure(tmp_path, phrase_rules, ranking_config):
    conn = registry.create_database(tmp_path / "reg.db")
    item = _item()
    plan = orchestrator.build_lookup_plan(item, conn, phrase_rules)
    conn.close()
    adapter = FakeXactimateAdapter(dropdown_script={plan.search_input: []}, fail_on_capture_for={plan.search_input})
    outcome = orchestrator.execute_plan(plan, item, adapter, ranking_config, phrase_rules, dry_run=True)
    assert outcome.stop_reason == STOP_REASON_EXTRACTION_FAILED
    assert outcome.decision == DECISION_REVIEW_REQUIRED
    assert adapter.log.calls[-1][0] == "recover"


def test_execute_plan_stops_on_unexpected_dialog(tmp_path, phrase_rules, ranking_config):
    conn = registry.create_database(tmp_path / "reg.db")
    item = _item()
    plan = orchestrator.build_lookup_plan(item, conn, phrase_rules)
    conn.close()
    adapter = FakeXactimateAdapter(dropdown_script={plan.search_input: []}, raise_unexpected_dialog_for={plan.search_input})
    outcome = orchestrator.execute_plan(plan, item, adapter, ranking_config, phrase_rules, dry_run=True)
    assert outcome.stop_reason == STOP_REASON_UNEXPECTED_DIALOG
    assert outcome.decision == DECISION_REVIEW_REQUIRED
    assert adapter.log.calls[-1][0] == "recover"


def test_execute_plan_stops_on_ambiguous_candidates(tmp_path, phrase_rules, ranking_config):
    """Phase 5.6: neither candidate is an exact match to the source
    (both add a different unmentioned qualifier) -- genuinely
    ambiguous, unlike the plain-vs-superset case ranking.py now
    resolves cleanly (see test_ranking.py's exact-match-beats-superset
    coverage)."""
    conn = registry.create_database(tmp_path / "reg.db")
    item = _item(original_description="Drip edge", component=None, material=None, action=None)
    plan = orchestrator.build_lookup_plan(item, conn, phrase_rules)
    conn.close()
    a = _dropdown("Drip edge - copper", sel="DRIPC", pos=0)
    b = _dropdown("Drip edge - PVC/TPO clad metal", sel="DRIPP", pos=1)
    adapter = FakeXactimateAdapter(dropdown_script={plan.search_input: [a, b]})
    outcome = orchestrator.execute_plan(plan, item, adapter, ranking_config, phrase_rules, dry_run=True)
    assert outcome.decision == DECISION_REVIEW_REQUIRED
    assert outcome.stop_reason == STOP_REASON_AMBIGUOUS
    # Phase 5.8A (live-caught): see test_execute_plan_stops_on_no_results's
    # comment -- REVIEW_REQUIRED never clicks a candidate either, so the
    # popup must be explicitly dismissed here too.
    assert adapter.log.calls[-1][0] == "recover"

    # Note: recover() is called BEFORE the top.has_hard_conflict branch
    # inside this same `decision == DECISION_REVIEW_REQUIRED` block (see
    # execute_plan()), so this one assertion structurally covers BOTH
    # REVIEW_REQUIRED sub-paths (hard-conflict and margin/ambiguity),
    # not just the margin/ambiguity one exercised above.


def test_execute_plan_stops_on_invalid_quantity(tmp_path, phrase_rules, ranking_config):
    conn = registry.create_database(tmp_path / "reg.db")
    item = _item(quantity=None)
    plan = orchestrator.build_lookup_plan(item, conn, phrase_rules)
    conn.close()
    d = _dropdown("Tear off composition shingles - 3 tab (no haul off)", pos=0)
    adapter = FakeXactimateAdapter(dropdown_script={plan.search_input: [d]})
    outcome = orchestrator.execute_plan(plan, item, adapter, ranking_config, phrase_rules, dry_run=True)
    assert outcome.stop_reason == STOP_REASON_UNIT_QUANTITY_INVALID
    assert outcome.decision == DECISION_REVIEW_REQUIRED


def test_dry_run_never_calls_select_or_commit_even_on_auto_select(tmp_path, phrase_rules, ranking_config):
    conn = registry.create_database(tmp_path / "reg.db")
    item = _item()
    plan = orchestrator.build_lookup_plan(item, conn, phrase_rules)
    conn.close()
    d = _dropdown("Tear off composition shingles - 3 tab (no haul off)", pos=0)
    adapter = FakeXactimateAdapter(dropdown_script={plan.search_input: [d]})
    outcome = orchestrator.execute_plan(plan, item, adapter, ranking_config, phrase_rules, dry_run=True)
    assert outcome.decision == DECISION_AUTO_SELECT
    assert outcome.committed is False
    names = [name for name, _a, _k in adapter.log.calls]
    assert "select_candidate" not in names
    assert "commit_item" not in names
    assert "enter_quantity" not in names


def test_dry_run_ignores_supports_live_execution(tmp_path, phrase_rules, ranking_config):
    """dry_run=True never commits even against an adapter that DOES
    declare live-execution support -- the unsupported_adapter guard is
    only relevant for dry_run=False."""
    conn = registry.create_database(tmp_path / "reg.db")
    item = _item()
    plan = orchestrator.build_lookup_plan(item, conn, phrase_rules)
    conn.close()
    d = _dropdown("Tear off composition shingles - 3 tab (no haul off)", pos=0)
    adapter = FakeXactimateAdapter(dropdown_script={plan.search_input: [d]})
    adapter.supports_live_execution = True
    outcome = orchestrator.execute_plan(plan, item, adapter, ranking_config, phrase_rules, dry_run=True)
    assert outcome.committed is False
    assert not any(name == "commit_item" for name, _a, _k in adapter.log.calls)


def test_live_run_refused_against_adapter_without_live_support(tmp_path, phrase_rules, ranking_config):
    """The default FakeXactimateAdapter never declares live-execution
    support -- see build spec 'Do not fabricate successful automation.'"""
    conn = registry.create_database(tmp_path / "reg.db")
    item = _item()
    plan = orchestrator.build_lookup_plan(item, conn, phrase_rules)
    conn.close()
    d = _dropdown("Tear off composition shingles - 3 tab (no haul off)", pos=0)
    adapter = FakeXactimateAdapter(dropdown_script={plan.search_input: [d]})
    outcome = orchestrator.execute_plan(plan, item, adapter, ranking_config, phrase_rules, dry_run=False)
    assert outcome.stop_reason == STOP_REASON_UNSUPPORTED_ADAPTER
    assert outcome.committed is False
    assert adapter.log.calls == []  # refused before touching the adapter at all


def test_live_run_commits_on_auto_select_with_matching_fields(tmp_path, phrase_rules, ranking_config):
    conn = registry.create_database(tmp_path / "reg.db")
    item = _item()
    plan = orchestrator.build_lookup_plan(item, conn, phrase_rules)
    conn.close()
    d = _dropdown("Tear off composition shingles - 3 tab (no haul off)", pos=0)
    adapter = FakeXactimateAdapter(dropdown_script={plan.search_input: [d]})
    adapter.supports_live_execution = True  # test-only opt-in; production adapters set this in __init__
    outcome = orchestrator.execute_plan(plan, item, adapter, ranking_config, phrase_rules, dry_run=False)
    assert outcome.decision == DECISION_AUTO_SELECT
    assert outcome.committed is True
    assert outcome.evidence_reference is not None


class _VerifyingFakeAdapter(FakeXactimateAdapter):
    """A FakeXactimateAdapter extended with the Phase 4.8 duck-typed
    verification hooks (snapshot_grid_identities/verify_commit), so
    execute_plan()'s wiring to them can be tested without a real Windows
    adapter -- see orchestrator.execute_plan()'s before_snapshot logic."""

    def __init__(self, *args, verification_result=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.verification_result = verification_result
        self.snapshot_calls = 0
        self.verify_commit_calls: list[tuple] = []

    def snapshot_grid_identities(self):
        self.snapshot_calls += 1
        return [("EXISTING", "ROW")]

    def verify_commit(self, before_snapshot, category, selector, expected_quantity, *, source_unit=None, expected_xactimate_unit=None, populated_unit=None):
        self.verify_commit_calls.append((before_snapshot, category, selector, expected_quantity, source_unit, expected_xactimate_unit, populated_unit))
        return self.verification_result


class _FakeCommitVerification:
    def __init__(self, trust_state):
        self.trust_state = trust_state


def test_verify_commit_called_on_live_commit_when_adapter_supports_it(tmp_path, phrase_rules, ranking_config):
    conn = registry.create_database(tmp_path / "reg.db")
    item = _item()
    plan = orchestrator.build_lookup_plan(item, conn, phrase_rules)
    conn.close()
    d = _dropdown("Tear off composition shingles - 3 tab (no haul off)", pos=0)
    verification = _FakeCommitVerification("VERIFIED")
    adapter = _VerifyingFakeAdapter(dropdown_script={plan.search_input: [d]}, verification_result=verification)
    adapter.supports_live_execution = True

    outcome = orchestrator.execute_plan(plan, item, adapter, ranking_config, phrase_rules, dry_run=False)

    assert outcome.committed is True
    assert adapter.snapshot_calls == 1
    assert len(adapter.verify_commit_calls) == 1
    before_snapshot, category, selector, quantity, source_unit, expected_unit, populated_unit = adapter.verify_commit_calls[0]
    assert before_snapshot == [("EXISTING", "ROW")]
    assert (category, selector) == (d.category, d.selector)
    assert quantity == item.quantity
    assert source_unit == item.source_unit
    assert outcome.verification is verification
    assert outcome.to_dict()["verification_trust_state"] == "VERIFIED"


def test_verify_commit_not_called_on_dry_run(tmp_path, phrase_rules, ranking_config):
    conn = registry.create_database(tmp_path / "reg.db")
    item = _item()
    plan = orchestrator.build_lookup_plan(item, conn, phrase_rules)
    conn.close()
    d = _dropdown("Tear off composition shingles - 3 tab (no haul off)", pos=0)
    adapter = _VerifyingFakeAdapter(dropdown_script={plan.search_input: [d]})
    adapter.supports_live_execution = True

    outcome = orchestrator.execute_plan(plan, item, adapter, ranking_config, phrase_rules, dry_run=True)

    assert outcome.committed is False
    assert adapter.snapshot_calls == 0
    assert adapter.verify_commit_calls == []
    assert outcome.verification is None


def test_verify_commit_not_called_when_adapter_does_not_support_it(tmp_path, phrase_rules, ranking_config):
    conn = registry.create_database(tmp_path / "reg.db")
    item = _item()
    plan = orchestrator.build_lookup_plan(item, conn, phrase_rules)
    conn.close()
    d = _dropdown("Tear off composition shingles - 3 tab (no haul off)", pos=0)
    adapter = FakeXactimateAdapter(dropdown_script={plan.search_input: [d]})  # plain Fake, no verify_commit
    adapter.supports_live_execution = True

    outcome = orchestrator.execute_plan(plan, item, adapter, ranking_config, phrase_rules, dry_run=False)

    assert outcome.committed is True
    assert outcome.verification is None
    assert outcome.to_dict()["verification_trust_state"] is None


def test_field_mismatch_after_selection_stops_and_does_not_commit(tmp_path, phrase_rules, ranking_config, monkeypatch):
    conn = registry.create_database(tmp_path / "reg.db")
    item = _item()
    plan = orchestrator.build_lookup_plan(item, conn, phrase_rules)
    conn.close()
    d = _dropdown("Tear off composition shingles - 3 tab (no haul off)", pos=0)
    adapter = FakeXactimateAdapter(dropdown_script={plan.search_input: [d]})
    adapter.supports_live_execution = True

    from estimate_extractor.xactimate_lookup.models import PopulatedFields

    monkeypatch.setattr(adapter, "read_populated_fields", lambda: PopulatedFields(category="WRONG", selector="MISMATCH", description=None, unit=None, action=None, item_number=None))
    cancel_calls = []
    monkeypatch.setattr(adapter, "cancel_current_item", lambda **kwargs: cancel_calls.append(1), raising=False)

    outcome = orchestrator.execute_plan(plan, item, adapter, ranking_config, phrase_rules, dry_run=False)
    assert outcome.decision == DECISION_REVIEW_REQUIRED
    assert outcome.stop_reason == STOP_REASON_FIELD_MISMATCH
    assert outcome.committed is False
    # Phase 5.3: select_candidate() already put a pending row in the
    # grid -- it must be cancelled here, or a LATER unrelated
    # commit_item() call (e.g. verify_group()'s own probe cycle) can
    # silently persist it with default/wrong data, never having gone
    # through this module's own commit path at all (live-reproduced).
    assert cancel_calls == [1]


def test_category_selector_match_duck_typed_check_prevents_a_false_cancellation(tmp_path, phrase_rules, ranking_config, monkeypatch):
    """Phase 5.12 (live-caught): a truncated/noisy OCR read of the
    populated CAT/SEL (here simulating the live-reproduced 'WDR' ->
    'WD' truncation) must NOT cancel an otherwise-correct, already
    UI-Automation-verified selection when the adapter implements
    check_category_selector_match() -- proves the orchestrator-level
    wiring, not just the pure function in isolation."""
    conn = registry.create_database(tmp_path / "reg.db")
    item = _item()
    plan = orchestrator.build_lookup_plan(item, conn, phrase_rules)
    conn.close()
    d = _dropdown("Tear off composition shingles - 3 tab (no haul off)", cat="RFG", sel="ARMV", pos=0)
    adapter = FakeXactimateAdapter(dropdown_script={plan.search_input: [d]})
    adapter.supports_live_execution = True

    from estimate_extractor.xactimate_lookup.models import PopulatedFields
    from estimate_extractor.xactimate_lookup.windows_adapter import check_category_selector_match

    # Truncated category ("RF" instead of "RFG"), matching the live-
    # reproduced "WDR" -> "WD" case exactly.
    monkeypatch.setattr(
        adapter, "read_populated_fields",
        lambda: PopulatedFields(category="RF", selector="ARMV", description=d.description, unit=None, action=None, item_number=None),
    )
    monkeypatch.setattr(adapter, "check_category_selector_match", check_category_selector_match, raising=False)

    outcome = orchestrator.execute_plan(plan, item, adapter, ranking_config, phrase_rules, dry_run=False)
    assert outcome.stop_reason is None
    assert outcome.committed is True


def test_category_selector_match_duck_typed_check_still_catches_a_real_mismatch(tmp_path, phrase_rules, ranking_config, monkeypatch):
    """Counterpart: a genuinely different, unrelated selector must still
    stop and cancel even when check_category_selector_match() is
    available -- the tolerant comparison must never become a rubber
    stamp."""
    conn = registry.create_database(tmp_path / "reg.db")
    item = _item()
    plan = orchestrator.build_lookup_plan(item, conn, phrase_rules)
    conn.close()
    d = _dropdown("Tear off composition shingles - 3 tab (no haul off)", cat="RFG", sel="ARMV", pos=0)
    adapter = FakeXactimateAdapter(dropdown_script={plan.search_input: [d]})
    adapter.supports_live_execution = True

    from estimate_extractor.xactimate_lookup.models import PopulatedFields
    from estimate_extractor.xactimate_lookup.windows_adapter import check_category_selector_match

    monkeypatch.setattr(
        adapter, "read_populated_fields",
        lambda: PopulatedFields(category="WRONG", selector="MISMATCH", description=None, unit=None, action=None, item_number=None),
    )
    monkeypatch.setattr(adapter, "check_category_selector_match", check_category_selector_match, raising=False)
    cancel_calls = []
    monkeypatch.setattr(adapter, "cancel_current_item", lambda **kwargs: cancel_calls.append(1), raising=False)

    outcome = orchestrator.execute_plan(plan, item, adapter, ranking_config, phrase_rules, dry_run=False)
    assert outcome.stop_reason == STOP_REASON_FIELD_MISMATCH
    assert outcome.committed is False
    assert cancel_calls == [1]


def test_unit_mismatch_after_selection_stops_and_does_not_commit(tmp_path, phrase_rules, ranking_config, monkeypatch):
    conn = registry.create_database(tmp_path / "reg.db")
    item = _item(source_unit="SQ")
    plan = orchestrator.build_lookup_plan(item, conn, phrase_rules)
    conn.close()
    d = _dropdown("Tear off composition shingles - 3 tab (no haul off)", pos=0)
    adapter = FakeXactimateAdapter(dropdown_script={plan.search_input: [d]}, populated_unit="LF")
    adapter.supports_live_execution = True
    cancel_calls = []
    monkeypatch.setattr(adapter, "cancel_current_item", lambda **kwargs: cancel_calls.append(1), raising=False)

    outcome = orchestrator.execute_plan(plan, item, adapter, ranking_config, phrase_rules, dry_run=False)
    assert outcome.decision == DECISION_REVIEW_REQUIRED
    assert outcome.stop_reason == STOP_REASON_UNIT_MISMATCH
    # outcome.committed is the authoritative "was anything actually
    # committed" signal -- NOT whether commit_item() was ever called.
    # Phase 5.4 added a save-after-cancel step (commit_item() persists
    # the "nothing here" state so the project doesn't sit as "Unsaved
    # changes" after a safe stop), which legitimately calls
    # commit_item() without committing a line item.
    assert outcome.committed is False
    assert cancel_calls == [1]  # Phase 5.3: pending row must be cancelled, not left dangling


def test_cancel_pending_selection_retries_when_the_first_attempt_fails(tmp_path, phrase_rules, ranking_config, monkeypatch):
    """Phase 5.4 (critical, live-caught): a single cancel_current_item()
    call is not reliable enough on its own -- the real adapter's first
    attempt can fail with "row count did not decrease" even though an
    immediate second attempt succeeds. Live-reproduced consequence of
    NOT retrying: a real $330.31 row survived a field-mismatch stop.
    This proves the retry closes it without ever raising out of
    execute_plan()."""
    conn = registry.create_database(tmp_path / "reg.db")
    item = _item(source_unit="SQ")
    plan = orchestrator.build_lookup_plan(item, conn, phrase_rules)
    conn.close()
    d = _dropdown("Tear off composition shingles - 3 tab (no haul off)", pos=0)
    adapter = FakeXactimateAdapter(dropdown_script={plan.search_input: [d]}, populated_unit="LF")
    adapter.supports_live_execution = True

    attempts = []

    def _flaky_cancel(**kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise AdapterError("cancel_current_item(): row count did not decrease (before=1, after=1).")

    monkeypatch.setattr(adapter, "cancel_current_item", _flaky_cancel, raising=False)

    outcome = orchestrator.execute_plan(plan, item, adapter, ranking_config, phrase_rules, dry_run=False)

    assert outcome.stop_reason == STOP_REASON_UNIT_MISMATCH
    assert len(attempts) == 2  # first attempt failed, second succeeded -- no residue left behind


def test_cancel_pending_selection_gives_up_after_bounded_retries_without_raising(tmp_path, phrase_rules, ranking_config, monkeypatch):
    conn = registry.create_database(tmp_path / "reg.db")
    item = _item(source_unit="SQ")
    plan = orchestrator.build_lookup_plan(item, conn, phrase_rules)
    conn.close()
    d = _dropdown("Tear off composition shingles - 3 tab (no haul off)", pos=0)
    adapter = FakeXactimateAdapter(dropdown_script={plan.search_input: [d]}, populated_unit="LF")
    adapter.supports_live_execution = True

    attempts = []

    def _always_fails(**kwargs):
        attempts.append(1)
        raise AdapterError("simulated persistent failure")

    monkeypatch.setattr(adapter, "cancel_current_item", _always_fails, raising=False)

    # Must not raise -- a cleanup failure must never mask the original stop reason.
    outcome = orchestrator.execute_plan(plan, item, adapter, ranking_config, phrase_rules, dry_run=False)
    assert outcome.stop_reason == STOP_REASON_UNIT_MISMATCH
    assert len(attempts) == 3  # bounded -- never unbounded


def test_cancel_pending_selection_is_a_no_op_when_adapter_lacks_the_method(tmp_path, phrase_rules, ranking_config):
    """Adapters that don't implement cancel_current_item (e.g. a bare
    FakeXactimateAdapter) must not be broken by this cleanup -- it's
    duck-typed, like every other Windows-only capability."""
    conn = registry.create_database(tmp_path / "reg.db")
    item = _item(source_unit="SQ")
    plan = orchestrator.build_lookup_plan(item, conn, phrase_rules)
    conn.close()
    d = _dropdown("Tear off composition shingles - 3 tab (no haul off)", pos=0)
    adapter = FakeXactimateAdapter(dropdown_script={plan.search_input: [d]}, populated_unit="LF")
    adapter.supports_live_execution = True
    assert not hasattr(adapter, "cancel_current_item")

    outcome = orchestrator.execute_plan(plan, item, adapter, ranking_config, phrase_rules, dry_run=False)
    assert outcome.stop_reason == STOP_REASON_UNIT_MISMATCH


def test_matching_populated_unit_does_not_block_commit(tmp_path, phrase_rules, ranking_config):
    conn = registry.create_database(tmp_path / "reg.db")
    item = _item(source_unit="SQ")
    plan = orchestrator.build_lookup_plan(item, conn, phrase_rules)
    conn.close()
    d = _dropdown("Tear off composition shingles - 3 tab (no haul off)", pos=0)
    adapter = FakeXactimateAdapter(dropdown_script={plan.search_input: [d]}, populated_unit="sq")  # case-insensitive match
    adapter.supports_live_execution = True

    outcome = orchestrator.execute_plan(plan, item, adapter, ranking_config, phrase_rules, dry_run=False)
    assert outcome.committed is True


def test_no_populated_unit_signal_does_not_block_commit(tmp_path, phrase_rules, ranking_config):
    """No structured unit is available from the adapter (the common
    case) -- this must never block a commit on its own."""
    conn = registry.create_database(tmp_path / "reg.db")
    item = _item(source_unit="SQ")
    plan = orchestrator.build_lookup_plan(item, conn, phrase_rules)
    conn.close()
    d = _dropdown("Tear off composition shingles - 3 tab (no haul off)", pos=0)
    adapter = FakeXactimateAdapter(dropdown_script={plan.search_input: [d]})  # populated_unit=None
    adapter.supports_live_execution = True

    outcome = orchestrator.execute_plan(plan, item, adapter, ranking_config, phrase_rules, dry_run=False)
    assert outcome.committed is True


def test_trusted_path_searches_by_category_selector_not_description(tmp_path, phrase_rules, ranking_config):
    conn = registry.create_database(tmp_path / "reg.db")
    item = _item()
    sig = orchestrator.signature_mod.compute_item_signature(item.trade, item.component, item.material, item.action, item.source_unit, item.original_description, phrase_rules)
    registry.save_record(conn, InternalMappingRecord(
        mapping_id="m1", item_signature=sig, source_description=item.original_description, search_phrase="x",
        category="RFG", selector="ARMVN", xactimate_description="d", unit="SQ", action="remove",
        reviewer="tester", approval_reason="matches", status=MAPPING_STATUS_APPROVED,
    ))
    plan = orchestrator.build_lookup_plan(item, conn, phrase_rules)
    conn.close()
    d = _dropdown("Tear off composition shingles - 3 tab (no haul off)", pos=0)
    adapter = FakeXactimateAdapter(dropdown_script={"RFG ARMVN": [d]})
    orchestrator.execute_plan(plan, item, adapter, ranking_config, phrase_rules, dry_run=True)
    names = [name for name, _a, _k in adapter.log.calls]
    assert "search_by_category_selector" in names
    assert "search_by_description" not in names
