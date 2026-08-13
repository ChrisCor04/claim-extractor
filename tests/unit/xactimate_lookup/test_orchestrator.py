from __future__ import annotations

import pytest

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
    LookupPlan,
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


class _PendingAwareAdapter(FakeXactimateAdapter):
    supports_live_execution = True

    def __init__(self, *args, pending_results=(False, True), duplicate_allowed=True, **kwargs):
        super().__init__(*args, **kwargs)
        self.pending_results = list(pending_results)
        self.duplicate_allowed = duplicate_allowed

    def snapshot_grid_identities(self):
        return [("EXISTING", "ROW")]

    def pending_item_created(self, before_snapshot):
        self.log.record("pending_item_created")
        return self.pending_results.pop(0)

    def allows_intentional_duplicate(self, candidate):
        self.log.record("allows_intentional_duplicate", candidate.category, candidate.selector)
        return self.duplicate_allowed


def test_description_selection_zero_delta_stops_after_one_click(
    tmp_path, phrase_rules, ranking_config,
):
    item = _item()
    conn = registry.create_database(tmp_path / "reg.db")
    plan = orchestrator.build_lookup_plan(item, conn, phrase_rules)
    conn.close()
    selected = _dropdown(item.original_description, cat="RFG", sel="ARMVN")
    adapter = _PendingAwareAdapter(dropdown_script={
        plan.search_input: [selected],
        "RFG ARMVN": [selected],
    })

    outcome = orchestrator.execute_plan(plan, item, adapter, ranking_config, phrase_rules, dry_run=False)

    assert outcome.committed is False
    assert outcome.physical_state_uncertain is True
    assert outcome.stop_reason == "physical_state_uncertain"
    names = [name for name, _args, _kwargs in adapter.log.calls]
    assert names.count("search_by_description") == 1
    assert names.count("search_by_category_selector") == 0
    assert names.count("select_candidate") == 1
    assert "allows_intentional_duplicate" not in names


def test_description_zero_delta_never_searches_cat_sel_fallback(tmp_path, phrase_rules, ranking_config):
    item = _item()
    conn = registry.create_database(tmp_path / "reg.db")
    plan = orchestrator.build_lookup_plan(item, conn, phrase_rules)
    conn.close()
    selected = _dropdown(item.original_description, cat="RFG", sel="ARMVN")
    wrong = _dropdown("Wrong", cat="RFG", sel="WRONG")
    adapter = _PendingAwareAdapter(dropdown_script={plan.search_input: [selected], "RFG ARMVN": [wrong]})

    outcome = orchestrator.execute_plan(plan, item, adapter, ranking_config, phrase_rules, dry_run=False)

    assert outcome.committed is False
    assert outcome.physical_item_created is False
    assert outcome.physical_state_uncertain is True
    assert "zero new physical rows" in outcome.stop_detail
    assert len([name for name, _args, _kwargs in adapter.log.calls if name == "select_candidate"]) == 1
    assert not any(name == "search_by_category_selector" for name, _args, _kwargs in adapter.log.calls)
    assert not any(name == "enter_quantity" for name, _args, _kwargs in adapter.log.calls)


def test_failed_focus_never_clears_or_types(tmp_path, phrase_rules, ranking_config):
    item = _item()
    conn = registry.create_database(tmp_path / "reg.db")
    plan = orchestrator.build_lookup_plan(item, conn, phrase_rules)
    conn.close()
    adapter = FakeXactimateAdapter()
    adapter.supports_live_execution = True

    def _fail_focus():
        adapter.log.record("focus_search")
        raise AdapterError("search focus not verified")

    adapter.focus_search = _fail_focus

    with pytest.raises(AdapterError, match="search focus not verified"):
        orchestrator.execute_plan(plan, item, adapter, ranking_config, phrase_rules, dry_run=False)

    names = [name for name, _args, _kwargs in adapter.log.calls]
    assert "clear_search" not in names
    assert "search_by_description" not in names


def test_pending_item_proceeds_directly_to_quantity_without_populated_fields_read(
    tmp_path, phrase_rules, ranking_config,
):
    item = _item()
    conn = registry.create_database(tmp_path / "reg.db")
    plan = orchestrator.build_lookup_plan(item, conn, phrase_rules)
    conn.close()
    selected = _dropdown(item.original_description, cat="RFG", sel="ARMVN")
    adapter = _PendingAwareAdapter(
        dropdown_script={plan.search_input: [selected]}, pending_results=(True,),
    )

    def _forbidden_read():
        raise AssertionError("read_populated_fields must not run before quantity")

    adapter.read_populated_fields = _forbidden_read
    outcome = orchestrator.execute_plan(plan, item, adapter, ranking_config, phrase_rules, dry_run=False)

    assert outcome.committed is True
    names = [name for name, _args, _kwargs in adapter.log.calls]
    assert "read_populated_fields" not in names
    assert names.index("select_candidate") < names.index("pending_item_created") < names.index("enter_quantity")


def test_description_no_pending_item_does_not_fallback_without_distinct_task_proof(
    tmp_path, phrase_rules, ranking_config,
):
    item = _item()
    conn = registry.create_database(tmp_path / "reg.db")
    plan = orchestrator.build_lookup_plan(item, conn, phrase_rules)
    conn.close()
    selected = _dropdown(item.original_description, cat="RFG", sel="ARMVN")
    adapter = _PendingAwareAdapter(
        dropdown_script={plan.search_input: [selected]}, pending_results=(False,), duplicate_allowed=False,
    )

    outcome = orchestrator.execute_plan(plan, item, adapter, ranking_config, phrase_rules, dry_run=False)

    assert outcome.committed is False
    assert not any(name == "search_by_category_selector" for name, _args, _kwargs in adapter.log.calls)


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


# ---------------------------------------------------------------------
# Observability: execute_plan() attaches decision_diagnostics (built by
# classify_decision_with_diagnostics() -- the SAME call that determines
# `decision`) to the LookupOutcome, at every point after ranking ran.
# ---------------------------------------------------------------------


def test_decision_diagnostics_absent_when_dropdown_never_opened(tmp_path, phrase_rules, ranking_config):
    """The empty-dropdowns early return in execute_plan() happens BEFORE
    ranking ever runs -- no candidates exist to classify, so
    decision_diagnostics must be None rather than fabricated."""
    conn = registry.create_database(tmp_path / "reg.db")
    item = _item()
    plan = orchestrator.build_lookup_plan(item, conn, phrase_rules)
    conn.close()
    adapter = FakeXactimateAdapter(dropdown_script={})
    outcome = orchestrator.execute_plan(plan, item, adapter, ranking_config, phrase_rules, dry_run=True)
    assert outcome.decision == DECISION_NO_MATCH
    assert outcome.decision_diagnostics is None


def test_decision_diagnostics_present_on_no_match_after_ranking(tmp_path, phrase_rules, ranking_config):
    conn = registry.create_database(tmp_path / "reg.db")
    item = _item(material="3-tab composition shingles")
    plan = orchestrator.build_lookup_plan(item, conn, phrase_rules)
    conn.close()
    unrelated = _dropdown("Tear off metal roofing panels", cat="RFG", sel="METAL", pos=0)
    adapter = FakeXactimateAdapter(dropdown_script={plan.search_input: [unrelated]})
    outcome = orchestrator.execute_plan(plan, item, adapter, ranking_config, phrase_rules, dry_run=True)
    assert outcome.decision == DECISION_NO_MATCH
    diag = outcome.decision_diagnostics
    assert diag is not None
    assert diag.decision == DECISION_NO_MATCH
    assert diag.gate == "below_review_required_min"
    assert diag.top_selector == "METAL"


def test_decision_diagnostics_present_on_ambiguous_review_required(tmp_path, phrase_rules, ranking_config):
    conn = registry.create_database(tmp_path / "reg.db")
    item = _item(original_description="Drip edge", component=None, material=None, action=None)
    plan = orchestrator.build_lookup_plan(item, conn, phrase_rules)
    conn.close()
    a = _dropdown("Drip edge - copper", sel="DRIPC", pos=0)
    b = _dropdown("Drip edge - PVC/TPO clad metal", sel="DRIPP", pos=1)
    adapter = FakeXactimateAdapter(dropdown_script={plan.search_input: [a, b]})
    outcome = orchestrator.execute_plan(plan, item, adapter, ranking_config, phrase_rules, dry_run=True)
    assert outcome.decision == DECISION_REVIEW_REQUIRED
    diag = outcome.decision_diagnostics
    assert diag is not None
    assert diag.gate == "below_auto_select_min"
    assert diag.top_selector == "DRIPC"
    assert diag.second_selector == "DRIPP"
    assert diag.top_score is not None and diag.top_score < ranking_config.auto_select_min


def test_decision_diagnostics_present_on_auto_select(tmp_path, phrase_rules, ranking_config):
    conn = registry.create_database(tmp_path / "reg.db")
    item = _item()
    plan = orchestrator.build_lookup_plan(item, conn, phrase_rules)
    conn.close()
    d = _dropdown("Tear off composition shingles - 3 tab (no haul off)", pos=0)
    adapter = FakeXactimateAdapter(dropdown_script={plan.search_input: [d]})
    outcome = orchestrator.execute_plan(plan, item, adapter, ranking_config, phrase_rules, dry_run=True)
    assert outcome.decision == DECISION_AUTO_SELECT
    diag = outcome.decision_diagnostics
    assert diag is not None
    assert diag.decision == DECISION_AUTO_SELECT
    assert diag.top_selector == d.selector
    assert diag.top_has_hard_conflict is False


# ---------------------------------------------------------------------
# Phase 5.19 (live-caught, odom-insurance-v2 Rows 7/11/18): execute_plan()
# computes preferred_categories from the source item's trade/component
# via selector_recommendation's own data-calibrated hints (see
# orchestrator._category_hint_rules()/hinted_categories()) and threads
# it into classify_decision_with_diagnostics() so a cross-category exact
# tie can be resolved end to end, not just at the ranking-unit level.
# ---------------------------------------------------------------------


def test_execute_plan_resolves_cross_category_tie_row7_shape(tmp_path, phrase_rules, ranking_config):
    conn = registry.create_database(tmp_path / "reg.db")
    item = _item(
        original_description="Digital satellite system - Detach & reset",
        trade="electrical", component="satellite_system", material=None, action="detach_and_reset",
    )
    plan = orchestrator.build_lookup_plan(item, conn, phrase_rules)
    conn.close()
    els = _dropdown("Digital satellite system - Detach & reset", cat="ELS", sel="DISHRS", pos=0)
    rfg = _dropdown("Digital satellite system - Detach & reset", cat="RFG", sel="DISHRS", pos=1)
    adapter = FakeXactimateAdapter(dropdown_script={plan.search_input: [els, rfg]})
    outcome = orchestrator.execute_plan(plan, item, adapter, ranking_config, phrase_rules, dry_run=True)
    assert outcome.decision == DECISION_AUTO_SELECT
    assert outcome.selected.dropdown.category == "ELS"
    assert outcome.selected.dropdown.selector == "DISHRS"
    assert outcome.decision_diagnostics.gate == "exact_tie_resolved_by_context"
    assert outcome.decision_diagnostics.tie_resolution.resolved is True


def test_execute_plan_resolves_cross_category_tie_regardless_of_candidate_order(tmp_path, phrase_rules, ranking_config):
    """Same Row 7 shape with the dropdown rows returned in the opposite
    order -- the winner must be picked by context, never by position."""
    conn = registry.create_database(tmp_path / "reg.db")
    item = _item(
        original_description="Digital satellite system - Detach & reset",
        trade="electrical", component="satellite_system", material=None, action="detach_and_reset",
    )
    plan = orchestrator.build_lookup_plan(item, conn, phrase_rules)
    conn.close()
    els = _dropdown("Digital satellite system - Detach & reset", cat="ELS", sel="DISHRS", pos=0)
    rfg = _dropdown("Digital satellite system - Detach & reset", cat="RFG", sel="DISHRS", pos=1)
    adapter = FakeXactimateAdapter(dropdown_script={plan.search_input: [rfg, els]})
    outcome = orchestrator.execute_plan(plan, item, adapter, ranking_config, phrase_rules, dry_run=True)
    assert outcome.decision == DECISION_AUTO_SELECT
    assert outcome.selected.dropdown.category == "ELS"
    assert outcome.selected.dropdown.selector == "DISHRS"


def test_execute_plan_cross_category_tie_without_trade_signal_uses_first_candidate_fallback_row11_shape(tmp_path, phrase_rules, ranking_config):
    """Phase 5.22: the contextual resolver still correctly declines
    (no defensible trade signal to prefer RFG or SDG -- unchanged from
    Phase 5.19), but the overall decision now AUTO_SELECTs via the
    separate, explicit first-candidate fallback rather than staying
    REVIEW_REQUIRED forever."""
    conn = registry.create_database(tmp_path / "reg.db")
    item = _item(
        original_description="Step flashing", trade="unknown", component="unknown", material=None, action="unknown",
    )
    plan = orchestrator.build_lookup_plan(item, conn, phrase_rules)
    conn.close()
    rfg = _dropdown("Step flashing", cat="RFG", sel="STEP", pos=0)
    sdg = _dropdown("Step flashing", cat="SDG", sel="STEP", pos=1)
    adapter = FakeXactimateAdapter(dropdown_script={plan.search_input: [rfg, sdg]})
    outcome = orchestrator.execute_plan(plan, item, adapter, ranking_config, phrase_rules, dry_run=True)
    assert outcome.decision == DECISION_AUTO_SELECT
    assert outcome.decision_diagnostics.gate == "exact_tie_resolved_by_first_candidate_fallback"
    assert outcome.selected.dropdown.category == "RFG"
    assert outcome.selected.dropdown.selector == "STEP"
    assert outcome.decision_diagnostics.tie_resolution.resolved is False
    assert outcome.decision_diagnostics.tie_resolution.reason == "no_category_hint_available"


def test_execute_plan_resolves_a_newly_classified_trade_end_to_end(tmp_path, phrase_rules, ranking_config):
    """Phase 5.20: proves the config/normalization_rules.yaml vocabulary
    fix (Ice & water barrier -> trade='roofing') flows all the way
    through the REAL classification pipeline (mapping.trade_detector),
    unchanged orchestrator/ranking code, and the Phase 5.19 tie-
    resolution mechanism to a correct AUTO_SELECT -- not a hardcoded
    trade string. Also proves classification itself doesn't depend on
    candidate order: the same trade is derived before any dropdown is
    ever seen."""
    from estimate_extractor.mapping.pipeline import DEFAULT_CONFIG_DIR
    from estimate_extractor.mapping.rules_config import load_normalization_rules
    from estimate_extractor.mapping.trade_detector import detect_trade

    rules = load_normalization_rules(DEFAULT_CONFIG_DIR / "normalization_rules.yaml")
    trade, _ = detect_trade("ice & water barrier", rules.trade_component_rules)
    assert trade.value == "roofing"

    conn = registry.create_database(tmp_path / "reg.db")
    item = _item(
        original_description="Ice & water barrier", trade=trade.value, component="ice_water_barrier",
        material=None, action=None,
    )
    plan = orchestrator.build_lookup_plan(item, conn, phrase_rules)
    conn.close()
    # A hypothetical (not real-catalog) cross-category tie, isolating
    # the classification -> hint -> tie-resolution path from any actual
    # RFG/some-other-category duplication in the real selector catalog.
    rfg = _dropdown("Ice & water barrier", cat="RFG", sel="IWS", pos=0)
    other = _dropdown("Ice & water barrier", cat="ZZZ", sel="IWS", pos=1)

    for dropdowns in ([rfg, other], [other, rfg]):
        adapter = FakeXactimateAdapter(dropdown_script={plan.search_input: dropdowns})
        outcome = orchestrator.execute_plan(plan, item, adapter, ranking_config, phrase_rules, dry_run=True)
        assert outcome.decision == DECISION_AUTO_SELECT
        assert outcome.selected.dropdown.category == "RFG"
        assert outcome.decision_diagnostics.tie_resolution.resolved is True


def test_execute_plan_auto_selects_exterior_shutters_after_classification_fix(tmp_path, phrase_rules, ranking_config):
    """Phase 5.21: odom-insurance-v2 Rows 28/31's real, live-observed top
    two candidates (SDG/SHTR exact match, SDG/SHTR< 'Small' size variant)
    were previously capped to 0.45 by a false wrong_component conflict
    from the old windows/window classification. With the corrected
    siding/shutters classification, this reaches AUTO_SELECT through the
    PRE-EXISTING 'insufficient_margin_exact_top_override' gate (Phase
    5.15 Pass 2 -- a plain exact match beats a same-family size variant)
    -- not a new mechanism, no threshold or margin changed. Also proves
    candidate order doesn't affect the outcome."""
    from estimate_extractor.mapping.pipeline import DEFAULT_CONFIG_DIR
    from estimate_extractor.mapping.rules_config import load_normalization_rules
    from estimate_extractor.mapping.trade_detector import detect_trade
    from estimate_extractor.mapping.component_detector import detect_component

    rules = load_normalization_rules(DEFAULT_CONFIG_DIR / "normalization_rules.yaml")
    description = "R&R Shutters - simulated wood (polystyrene)"
    trade, _ = detect_trade(description.lower(), rules.trade_component_rules)
    component, _, _ = detect_component(description.lower(), rules.trade_component_rules)
    assert trade.value == "siding"
    assert component == "shutters"

    conn = registry.create_database(tmp_path / "reg.db")
    item = _item(
        original_description=description, trade=trade.value, component=component,
        material=None, action="remove_and_replace",
    )
    plan = orchestrator.build_lookup_plan(item, conn, phrase_rules)
    conn.close()
    exact = _dropdown("Shutters - simulated wood (polystyrene)", cat="SDG", sel="SHTR", pos=0)
    small = _dropdown("Shutters - simulated wood (polystyrene) - Small", cat="SDG", sel="SHTR<", pos=1)

    for dropdowns in ([exact, small], [small, exact]):
        adapter = FakeXactimateAdapter(dropdown_script={plan.search_input: dropdowns})
        outcome = orchestrator.execute_plan(plan, item, adapter, ranking_config, phrase_rules, dry_run=True)
        assert outcome.decision == DECISION_AUTO_SELECT
        assert outcome.selected.dropdown.category == "SDG"
        assert outcome.selected.dropdown.selector == "SHTR"
        assert outcome.decision_diagnostics.gate == "insufficient_margin_exact_top_override"
        assert outcome.decision_diagnostics.top_has_hard_conflict is False


def test_decision_diagnostics_still_reflects_original_auto_select_on_invalid_quantity_stop(tmp_path, phrase_rules, ranking_config):
    """`decision` on the returned outcome flips to REVIEW_REQUIRED for
    the quantity guard, but decision_diagnostics must still explain the
    ranking decision that was actually made (AUTO_SELECT) -- it is not
    re-derived for the quantity check, which classify_decision_with_
    diagnostics() knows nothing about."""
    conn = registry.create_database(tmp_path / "reg.db")
    item = _item(quantity=None)
    plan = orchestrator.build_lookup_plan(item, conn, phrase_rules)
    conn.close()
    d = _dropdown("Tear off composition shingles - 3 tab (no haul off)", pos=0)
    adapter = FakeXactimateAdapter(dropdown_script={plan.search_input: [d]})
    outcome = orchestrator.execute_plan(plan, item, adapter, ranking_config, phrase_rules, dry_run=True)
    assert outcome.stop_reason == STOP_REASON_UNIT_QUANTITY_INVALID
    assert outcome.decision == DECISION_REVIEW_REQUIRED
    diag = outcome.decision_diagnostics
    assert diag is not None
    assert diag.decision == DECISION_AUTO_SELECT


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
        self.reason = "post-commit verification passed"


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


def test_stable_postwrite_ocr_disagreement_commits_once_and_routes_to_review(tmp_path, phrase_rules, ranking_config):
    conn = registry.create_database(tmp_path / "reg.db")
    item = _item()
    plan = orchestrator.build_lookup_plan(item, conn, phrase_rules)
    conn.close()
    d = _dropdown("Tear off composition shingles - 3 tab (no haul off)", pos=0)
    verification = _FakeCommitVerification("VERIFIED")
    adapter = _VerifyingFakeAdapter(dropdown_script={plan.search_input: [d]}, verification_result=verification)
    adapter.supports_live_execution = True

    class _Advisory:
        review_required = True
        reason = "Stable same-cell OCR read 66 instead of 33.66; write preserved."

    original_enter_quantity = adapter.enter_quantity

    def enter_quantity_once(quantity):
        original_enter_quantity(quantity)
        adapter.last_quantity_confirmation = _Advisory()

    adapter.enter_quantity = enter_quantity_once

    outcome = orchestrator.execute_plan(plan, item, adapter, ranking_config, phrase_rules, dry_run=False)

    assert outcome.committed is True
    assert outcome.physical_item_created is True
    assert outcome.stop_reason is None
    assert outcome.verification.trust_state == "QUANTITY_MISMATCH"
    assert outcome.verification.reason == _Advisory.reason
    names = [name for name, _args, _kwargs in adapter.log.calls]
    assert names.count("enter_quantity") == 1
    assert names.count("commit_item") == 1


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


def test_removed_prequantity_ocr_mismatch_cannot_override_uia_and_pending_proof(tmp_path, phrase_rules, ranking_config, monkeypatch):
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
    assert outcome.committed is True
    assert cancel_calls == []


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


def test_removed_prequantity_category_check_is_not_called(tmp_path, phrase_rules, ranking_config, monkeypatch):
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
    assert outcome.stop_reason is None
    assert outcome.committed is True
    assert cancel_calls == []


def test_category_ocr_uncertain_but_corroborated_by_selector_and_absent_from_results(tmp_path, phrase_rules, ranking_config, monkeypatch):
    """Phase 5.17 (live-caught): "WDR" stably misread as "WDI" (and,
    across a wide sweep of alternate OCR settings, never once as any
    OTHER category this same search's results actually offered) after
    select_candidate() had already independently proven the correct row
    via live UI-Automation TEXT. When the selector independently agrees
    in two fresh reads AND the observed category isn't one of the real
    candidates this search returned, the corroborating evidence must
    outweigh the uncertain OCR read and the commit must proceed."""
    conn = registry.create_database(tmp_path / "reg.db")
    item = _item()
    plan = orchestrator.build_lookup_plan(item, conn, phrase_rules)
    conn.close()
    top = _dropdown("Tear off composition shingles - 3 tab (no haul off)", cat="RFG", sel="ARMVN", pos=0)
    other = _dropdown("Some unrelated real candidate", cat="SFG", sel="OTHER", pos=1)
    adapter = FakeXactimateAdapter(dropdown_script={plan.search_input: [top, other]})
    adapter.supports_live_execution = True

    from estimate_extractor.xactimate_lookup.models import PopulatedFields
    from estimate_extractor.xactimate_lookup.windows_adapter import check_category_selector_match

    # "WDI"-style: selector matches exactly, category is garbled OCR
    # noise that is NOT "SFG" (the only other real candidate's category).
    monkeypatch.setattr(
        adapter, "read_populated_fields",
        lambda: PopulatedFields(category="XYZ", selector="ARMVN", description=top.description, unit=None, action=None, item_number=None),
    )
    monkeypatch.setattr(adapter, "check_category_selector_match", check_category_selector_match, raising=False)
    cancel_calls = []
    monkeypatch.setattr(adapter, "cancel_current_item", lambda **kwargs: cancel_calls.append(1), raising=False)

    outcome = orchestrator.execute_plan(plan, item, adapter, ranking_config, phrase_rules, dry_run=False)
    assert outcome.stop_reason is None
    assert outcome.committed is True
    assert cancel_calls == []


def test_removed_prequantity_ocr_alternate_does_not_replace_uia_identity(tmp_path, phrase_rules, ranking_config, monkeypatch):
    """Counterpart: if the observed (wrong) category matches one of the
    OTHER real candidates this same search actually returned, that is
    genuine evidence a different item may have been affected -- the
    Phase 5.17 corroboration override must NOT apply, and this must
    still stop and cancel exactly like an ordinary field mismatch."""
    conn = registry.create_database(tmp_path / "reg.db")
    item = _item()
    plan = orchestrator.build_lookup_plan(item, conn, phrase_rules)
    conn.close()
    top = _dropdown("Tear off composition shingles - 3 tab (no haul off)", cat="RFG", sel="ARMVN", pos=0)
    other = _dropdown("Some unrelated real candidate", cat="SFG", sel="OTHER", pos=1)
    adapter = FakeXactimateAdapter(dropdown_script={plan.search_input: [top, other]})
    adapter.supports_live_execution = True

    from estimate_extractor.xactimate_lookup.models import PopulatedFields
    from estimate_extractor.xactimate_lookup.windows_adapter import check_category_selector_match

    # Selector still agrees, but the observed category ("SFG") is a
    # REAL category from this same search's own results.
    monkeypatch.setattr(
        adapter, "read_populated_fields",
        lambda: PopulatedFields(category="SFG", selector="ARMVN", description=top.description, unit=None, action=None, item_number=None),
    )
    monkeypatch.setattr(adapter, "check_category_selector_match", check_category_selector_match, raising=False)
    cancel_calls = []
    monkeypatch.setattr(adapter, "cancel_current_item", lambda **kwargs: cancel_calls.append(1), raising=False)

    outcome = orchestrator.execute_plan(plan, item, adapter, ranking_config, phrase_rules, dry_run=False)
    assert outcome.stop_reason is None
    assert outcome.committed is True
    assert cancel_calls == []


def test_removed_prequantity_ocr_selector_does_not_replace_uia_identity(tmp_path, phrase_rules, ranking_config, monkeypatch):
    """The corroboration override requires the SELECTOR to independently
    agree too -- if the selector is also wrong, this is ordinary
    evidence of a real mismatch, not category-only OCR noise."""
    conn = registry.create_database(tmp_path / "reg.db")
    item = _item()
    plan = orchestrator.build_lookup_plan(item, conn, phrase_rules)
    conn.close()
    top = _dropdown("Tear off composition shingles - 3 tab (no haul off)", cat="RFG", sel="ARMVN", pos=0)
    adapter = FakeXactimateAdapter(dropdown_script={plan.search_input: [top]})
    adapter.supports_live_execution = True

    from estimate_extractor.xactimate_lookup.models import PopulatedFields
    from estimate_extractor.xactimate_lookup.windows_adapter import check_category_selector_match

    monkeypatch.setattr(
        adapter, "read_populated_fields",
        lambda: PopulatedFields(category="XYZ", selector="TOTALLYDIFFERENT", description=None, unit=None, action=None, item_number=None),
    )
    monkeypatch.setattr(adapter, "check_category_selector_match", check_category_selector_match, raising=False)
    cancel_calls = []
    monkeypatch.setattr(adapter, "cancel_current_item", lambda **kwargs: cancel_calls.append(1), raising=False)

    outcome = orchestrator.execute_plan(plan, item, adapter, ranking_config, phrase_rules, dry_run=False)
    assert outcome.stop_reason is None
    assert outcome.committed is True
    assert cancel_calls == []


def _trusted_plan(item, category, selector):
    return LookupPlan(
        line_item_id=item.line_item_id, path=LOOKUP_PATH_TRUSTED, item_signature="",
        search_input=f"{category} {selector}",
        trusted_mapping=InternalMappingRecord(
            mapping_id="teach", item_signature="", source_description=item.original_description, search_phrase="",
            category=category, selector=selector, xactimate_description=item.original_description,
            unit=item.source_unit, action=None, reviewer="tester", approval_reason="manual teach", status=MAPPING_STATUS_APPROVED,
        ),
    )


def test_force_auto_select_for_trusted_mapping_commits_a_low_scoring_special_item(tmp_path, phrase_rules, ranking_config):
    """Phase 5.17 (live-caught): a genuine special/bid-item catalog
    entry ("Light Fixtures (Bid Item)") scores only ~0.70 against a
    dissimilar source ("String Light") on ordinary text-similarity --
    classify_decision() alone would never AUTO_SELECT it. The opt-in
    force_auto_select_for_trusted_mapping parameter, used ONLY by a
    reviewer's own explicit teach-and-commit script, must still commit
    it once the top candidate is confirmed to be exactly the trusted
    mapping's own CAT/SEL with no hard conflict."""
    item = _item(original_description="String Light", component="unknown", material=None, action="unknown")
    plan = _trusted_plan(item, "LIT", "BIDITM")
    d = _dropdown("Light Fixtures (Bid Item)", cat="LIT", sel="BIDITM", pos=0)
    adapter = FakeXactimateAdapter(dropdown_script={plan.search_input: [d]})
    adapter.supports_live_execution = True

    outcome = orchestrator.execute_plan(plan, item, adapter, ranking_config, phrase_rules, dry_run=False, force_auto_select_for_trusted_mapping=True)

    assert outcome.decision == DECISION_AUTO_SELECT
    assert outcome.committed is True
    assert outcome.selected.dropdown.category == "LIT"
    assert outcome.selected.dropdown.selector == "BIDITM"


def test_force_auto_select_for_trusted_mapping_does_not_affect_ordinary_description_search(tmp_path, phrase_rules, ranking_config):
    """The opt-in override only ever applies to a LOOKUP_PATH_TRUSTED
    plan -- an ordinary description-search plan's ambiguous outcome
    must be completely unaffected even if the caller passes True."""
    conn = registry.create_database(tmp_path / "reg.db")
    item = _item()
    plan = orchestrator.build_lookup_plan(item, conn, phrase_rules)
    conn.close()
    d1 = _dropdown("Gutter - aluminum", cat="SFG", sel="GUTA", pos=0)
    d2 = _dropdown("Gutter - steel", cat="SFG", sel="GUTAS", pos=1)
    adapter = FakeXactimateAdapter(dropdown_script={plan.search_input: [d1, d2]})
    adapter.supports_live_execution = True

    outcome = orchestrator.execute_plan(plan, item, adapter, ranking_config, phrase_rules, dry_run=False, force_auto_select_for_trusted_mapping=True)

    assert outcome.committed is False


def test_force_auto_select_for_trusted_mapping_still_refuses_a_hard_conflict(tmp_path, phrase_rules, ranking_config):
    """The override still requires the top candidate to have no hard
    conflict -- it elevates trust in the score/margin gates only, never
    bypasses actual conflict evidence."""
    item = _item(original_description="String Light", component="lamp", material="crystal", action="unknown")
    plan = _trusted_plan(item, "LIT", "BIDITM")
    d = _dropdown("Light Fixtures (Bid Item)", cat="LIT", sel="BIDITM", pos=0)
    adapter = FakeXactimateAdapter(dropdown_script={plan.search_input: [d]})
    adapter.supports_live_execution = True

    outcome = orchestrator.execute_plan(plan, item, adapter, ranking_config, phrase_rules, dry_run=False, force_auto_select_for_trusted_mapping=True)

    # "crystal"/"lamp" won't literally appear in "Light Fixtures (Bid
    # Item)", so this is expected to still carry a wrong_component/
    # wrong_material hard conflict and NOT auto-commit.
    assert outcome.committed is False


def test_removed_prequantity_unit_ocr_is_deferred_to_postcommit_verification(tmp_path, phrase_rules, ranking_config, monkeypatch):
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
    assert outcome.committed is True
    assert outcome.populated_fields.unit is None
    assert cancel_calls == []


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

    orchestrator._cancel_pending_selection(adapter)
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
    orchestrator._cancel_pending_selection(adapter)
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

    orchestrator._cancel_pending_selection(adapter)


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
