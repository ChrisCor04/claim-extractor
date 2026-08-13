from estimate_extractor.mapping.component_detector import detect_component
from estimate_extractor.mapping.models import TradeType
from estimate_extractor.mapping.pipeline import DEFAULT_CONFIG_DIR
from estimate_extractor.mapping.rules_config import load_normalization_rules
from estimate_extractor.mapping.trade_detector import detect_trade


def _rules():
    return load_normalization_rules(DEFAULT_CONFIG_DIR / "normalization_rules.yaml")


def test_gutter_detected_as_gutters_trade():
    trade, conf = detect_trade('r&r gutter - aluminum - up to 5"', _rules().trade_component_rules)
    assert trade == TradeType.GUTTERS
    assert conf > 0.5


def test_shingle_detected_as_roofing_trade():
    trade, _ = detect_trade("laminated - comp. shingle rfg. - w/out felt", _rules().trade_component_rules)
    assert trade == TradeType.ROOFING


def test_fence_detected_as_fencing_trade():
    trade, _ = detect_trade("stain - wood fence/gate", _rules().trade_component_rules)
    assert trade == TradeType.FENCING


def test_debris_detected_as_debris_removal_trade():
    trade, _ = detect_trade("haul debris - per pickup truck load", _rules().trade_component_rules)
    assert trade == TradeType.DEBRIS_REMOVAL


def test_unrecognized_text_returns_unknown_not_a_guess():
    trade, conf = detect_trade("completely unrecognizable gibberish xyz", _rules().trade_component_rules)
    assert trade == TradeType.UNKNOWN
    assert conf < 0.5


# ---------------------------------------------------------------------
# Phase 5.20 (odom-insurance-v2 Row 11 investigation): trade_detector.py
# is a pure first-match-wins substring lookup over config/normalization_
# rules.yaml -- it never infers from ranking/candidate data, so its
# output is identical no matter what happens downstream. Investigating
# why "Step flashing" (Row 11) classifies as UNKNOWN found a genuine,
# narrow vocabulary gap unrelated to that row: "Ice & water barrier"
# (Row 15 of the same benchmark, and a standard national roofing term --
# "ice and water shield" is the common synonym) has zero cross-trade
# ambiguity -- unlike drip edge/valley metal/ridge cap/starter course,
# which this file already covers, it simply had no rule at all. "Step
# flashing" itself is deliberately NOT added anywhere: this catalog
# carries BOTH an RFG/STEP and an SDG/STEP entry for it (see ranking.py's
# cross-category tie resolution, Phase 5.19), which is concrete evidence
# that classifying it as unambiguously one trade would be a guess, not a
# fact -- exactly what this module's own design ("never invents a
# concept that isn't backed by a rule") already refuses to do.
# ---------------------------------------------------------------------


def test_ice_and_water_barrier_detected_as_roofing_trade():
    """Newly supported evidence: a real, standard roofing underlayment
    term with no other-trade meaning at all."""
    trade, conf = detect_trade("ice & water barrier", _rules().trade_component_rules)
    assert trade == TradeType.ROOFING
    assert conf > 0.5


def test_ice_and_water_shield_synonym_also_detected_as_roofing_trade():
    """The common national-standard synonym ('shield' rather than
    'barrier') must classify the same way -- this is a source-grounded
    vocabulary completion, not an Odom-specific literal string match."""
    trade, conf = detect_trade("ice and water shield - eave", _rules().trade_component_rules)
    assert trade == TradeType.ROOFING
    assert conf > 0.5


def test_step_flashing_remains_unknown_trade():
    """The motivating case, kept as an explicit negative control: this
    catalog has both an RFG and an SDG entry for 'Step flashing' (a
    genuine live-caught cross-category tie -- see test_ranking.py), so
    there is no defensible single-trade evidence for it. Must remain
    UNKNOWN even after the ice & water barrier rule is added elsewhere
    in the same file -- proves the new rule doesn't overreach."""
    trade, conf = detect_trade("step flashing", _rules().trade_component_rules)
    assert trade == TradeType.UNKNOWN
    assert conf < 0.5


def test_ambiguous_evidence_remains_unknown_not_guessed():
    """No rule anywhere in the file recognizes this text at all --
    genuinely insufficient evidence must stay UNKNOWN, never resolved by
    picking whichever rule merely appears first in file order."""
    trade, conf = detect_trade("patio watcher 7-piece wicker conversation set", _rules().trade_component_rules)
    assert trade == TradeType.UNKNOWN
    assert conf < 0.5


def test_existing_roofing_accessory_classifications_are_unchanged():
    """Regression guard: the new ice & water barrier rule sits among
    several other specific roofing-accessory rules (drip edge, valley
    metal, ridge cap, starter course) -- none of their own classifications
    may shift as a result of the addition."""
    rules = _rules().trade_component_rules
    for description, expected_component in [
        ("drip edge", "drip_edge"),
        ("valley metal", "valley_metal"),
        ("hip / ridge cap", "ridge_cap"),
        ("starter course", "starter_course"),
        ("roofing felt - 15 lb.", "roofing_felt"),
        ("r&r flashing - pipe jack", "pipe_flashing"),
    ]:
        trade, _ = detect_trade(description, rules)
        component, _, _ = detect_component(description, rules)
        assert trade == TradeType.ROOFING, description
        assert component == expected_component, description


def test_trade_classification_is_independent_of_any_ranking_candidate_order():
    """trade_detector.py never sees Xactimate dropdown candidates at all
    -- it runs entirely from the source description, before any search
    or ranking happens. Calling it repeatedly must always return the
    same result regardless of anything that could vary between runs
    (this guards against ever accidentally wiring candidate data into
    classification -- see the module's own 'no inference from candidate
    ranking results' constraint)."""
    rules = _rules().trade_component_rules
    results = {detect_trade("ice & water barrier", rules) for _ in range(5)}
    assert results == {(TradeType.ROOFING, 0.9)}


# ---------------------------------------------------------------------
# Phase 5.21 (odom-insurance-v2 Rows 28/31 investigation): "R&R Shutters
# - simulated wood (polystyrene)" was classifying as trade='windows',
# component='window' -- this catalog's real SDG/SHTR entry is a
# VERBATIM match for that source text, but ranking's component_ok check
# (component_words={'window'} vs a candidate description that never
# says "window") flagged a false wrong_component conflict, capping an
# otherwise-perfect match at 0.45. The fix is narrowly scoped to
# catalog-evidenced EXTERIOR-only shutter phrasings (material/style/
# security qualifiers that only ever appear on this catalog's SDG
# entries, never on its WDT "interior window shutters" family) --
# bare/unqualified "shutters" wording is deliberately left unchanged,
# since this catalog genuinely has both an SDG and a WDT family for it
# with no material-based way to tell them apart (see SDG/SHTRRS and
# WDT/SHTRRS, both literally "Shutters - Detach & reset").
# ---------------------------------------------------------------------


def test_simulated_wood_polystyrene_shutters_no_longer_classify_as_window():
    """The exact live-caught case: must no longer produce
    component='window' (the false-conflict trigger)."""
    rules = _rules().trade_component_rules
    trade, _ = detect_trade("r&r shutters - simulated wood (polystyrene)", rules)
    component, _, _ = detect_component("r&r shutters - simulated wood (polystyrene)", rules)
    assert trade == TradeType.SIDING
    assert component == "shutters"


def test_exterior_shutter_material_variants_classify_as_siding():
    """Every catalog-evidenced exterior-only qualifier (aluminum, wood-
    board-&-batten, wood-louvered, security, storm) must classify the
    same way -- this is a generalized vocabulary fix, not one literal
    string match."""
    rules = _rules().trade_component_rules
    cases = [
        "shutters - aluminum",
        "aluminum shutters",
        "shutters - wood - board & batten",
        "shutters - wood - louvered or paneled",
        "security shutter - accordion or folding type",
        "storm shutter - roll-up type",
    ]
    for description in cases:
        trade, _ = detect_trade(description, rules)
        component, _, _ = detect_component(description, rules)
        assert trade == TradeType.SIDING, description
        assert component == "shutters", description


def test_interior_window_shutters_remain_classified_as_windows():
    """The catalog's WDT 'Interior window shutters' family must remain
    distinguishable -- none of the new exterior-only patterns match
    this wording, so it correctly falls through to the pre-existing
    windows/window rule, unchanged."""
    rules = _rules().trade_component_rules
    trade, _ = detect_trade("interior window shutters (set)", rules)
    component, _, _ = detect_component("interior window shutters (set)", rules)
    assert trade == TradeType.WINDOWS
    assert component == "window"


def test_ambiguous_bare_shutters_wording_remains_conservative():
    """No material/security/storm qualifier at all -- this catalog has
    BOTH an exterior (SDG) and interior (WDT) family for exactly this
    bare wording ('Shutters - Detach & reset' exists identically under
    both), so there is no source-side basis to prefer one. Must stay on
    the existing, unchanged default rather than being force-classified
    either way."""
    rules = _rules().trade_component_rules
    trade, _ = detect_trade("shutters - detach & reset", rules)
    component, _, _ = detect_component("shutters - detach & reset", rules)
    assert trade == TradeType.WINDOWS
    assert component == "window"


def test_painting_shutters_wording_unaffected():
    """Real fixture example (Garrety): 'Seal & paint window shutters -
    per set' explicitly says 'window' and matches no new exterior-only
    pattern -- classification must be completely unchanged."""
    rules = _rules().trade_component_rules
    trade, _ = detect_trade("seal & paint window shutters - per set", rules)
    component, _, _ = detect_component("seal & paint window shutters - per set", rules)
    assert trade == TradeType.WINDOWS
    assert component == "window"


def test_unrelated_window_items_unchanged_by_shutter_fix():
    rules = _rules().trade_component_rules
    for description, expected_component in [
        ("r&r window screen, 1 - 9 sf", "window_screen"),
        ("wood window - double hung, 9-12 sf", "window"),
    ]:
        trade, _ = detect_trade(description, rules)
        component, _, _ = detect_component(description, rules)
        assert trade == TradeType.WINDOWS, description
        assert component == expected_component, description


def test_unrelated_siding_items_unchanged_by_shutter_fix():
    rules = _rules().trade_component_rules
    for description in ["r&r siding - vinyl", "metal or vinyl siding - detach & reset", "house wrap (air/moisture barrier)"]:
        trade, _ = detect_trade(description, rules)
        component, _, _ = detect_component(description, rules)
        assert trade == TradeType.SIDING, description
        assert component != "shutters", description


def test_shutter_classification_independent_of_candidate_order():
    """Same guard as the ice & water barrier test above, applied to the
    new shutter rule -- classification runs purely off the source text,
    repeatedly, before any search/ranking exists."""
    rules = _rules().trade_component_rules
    results = {detect_trade("shutters - simulated wood (polystyrene)", rules) for _ in range(5)}
    assert results == {(TradeType.SIDING, 0.9)}


def test_corrected_classification_removes_the_false_wrong_component_conflict():
    """End-to-end proof at the ranking layer: scoring the REAL live-
    observed top candidate (SDG/SHTR, verbatim catalog match) with the
    OLD (windows/window) classification reproduces the false conflict
    that capped it at 0.45; the NEW (siding/shutters) classification
    removes it entirely, without touching any ranking threshold, cap
    value, or hard-conflict rule -- only the input classification
    changed."""
    from estimate_extractor.xactimate_lookup import ranking, phrase_generator
    from estimate_extractor.xactimate_lookup.models import DropdownResult

    phrase_rules = phrase_generator.load_phrase_rules()
    config = ranking.load_ranking_config()
    source = "R&R Shutters - simulated wood (polystyrene)"
    candidate = DropdownResult(
        raw_text="Shutters - simulated wood (polystyrene)", row_position=0, category="SDG", selector="SHTR",
        description="Shutters - simulated wood (polystyrene)", extraction_confidence=1.0,
    )

    before = ranking.score_dropdown_candidate(
        original_description=source, trade="windows", component="window", material=None, action="remove_and_replace",
        unit="EA", size_key=None, grade_key=None, dropdown=candidate, rules=phrase_rules, config=config,
    )
    assert before.has_hard_conflict is True
    assert any("wrong_component" in r for r in before.conflict_reasons)
    assert before.score == 0.45

    trade, _ = detect_trade(source.lower(), _rules().trade_component_rules)
    component, _, _ = detect_component(source.lower(), _rules().trade_component_rules)
    after = ranking.score_dropdown_candidate(
        original_description=source, trade=trade.value, component=component, material=None, action="remove_and_replace",
        unit="EA", size_key=None, grade_key=None, dropdown=candidate, rules=phrase_rules, config=config,
    )
    assert after.has_hard_conflict is False
    assert after.conflict_reasons == []
    assert after.score == 1.0
