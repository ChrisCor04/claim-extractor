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
