from estimate_extractor.mapping.catalog import CatalogEntry, XactimateInfo
from estimate_extractor.mapping.matcher import match_line_item, score_candidate
from estimate_extractor.mapping.models import ActionType, Normalized, TradeType
from estimate_extractor.mapping.scorer import ScoringConfig

CONFIG = ScoringConfig(
    weights={
        "component_match": 0.35,
        "trade_match": 0.20,
        "action_compatibility": 0.15,
        "unit_compatibility": 0.15,
        "material_similarity": 0.10,
        "context_compatibility": 0.05,
    },
    mapped_min=0.92,
    partially_mapped_min=0.80,
    needs_review_min=0.60,
    missing_selector_forces_review=True,
    tie_margin=0.03,
    fuzzy_enabled=True,
    fuzzy_min_score=0.55,
)


def _entry(**overrides) -> CatalogEntry:
    defaults = dict(
        mapping_id="test_entry",
        canonical_terms=("laminated composition shingles",),
        trade="roofing",
        component="composition_shingles",
        allowed_actions=("install", "replace", "remove_and_replace"),
        allowed_units=("SQ",),
        xactimate=XactimateInfo(category="RFG", selector=None, activity="install", description="test"),
        confidence_base=0.9,
        requires_review=True,
    )
    defaults.update(overrides)
    return CatalogEntry(**defaults)


def _normalized(**overrides) -> Normalized:
    defaults = dict(
        action=ActionType.REMOVE_AND_REPLACE,
        trade=TradeType.ROOFING,
        component="composition_shingles",
        material="laminated composition shingles",
        attributes={},
        quantity=35.33,
        unit_of_measure="SQ",
    )
    defaults.update(overrides)
    return Normalized(**defaults)


def test_exact_match_scores_high():
    scored = score_candidate(_normalized(), _entry(), CONFIG)
    assert scored.score >= 0.9
    assert scored.conflict_reasons == ()


def test_fuzzy_material_match_scores_reasonably():
    normalized = _normalized(material="laminated composition shingle")  # near-identical, not exact substring both ways
    scored = score_candidate(normalized, _entry(), CONFIG)
    assert scored.score > 0.5


def test_unit_mismatch_caps_score():
    normalized = _normalized(unit_of_measure="EA")
    scored = score_candidate(normalized, _entry(), CONFIG)
    assert "unit_incompatible" in scored.conflict_reasons
    assert scored.score <= 0.5


def test_action_mismatch_caps_score():
    normalized = _normalized(action=ActionType.PAINT)
    scored = score_candidate(normalized, _entry(), CONFIG)
    assert "action_incompatible" in scored.conflict_reasons
    assert scored.score <= 0.5


def test_component_unknown_caps_score():
    normalized = _normalized(component="unknown")
    scored = score_candidate(normalized, _entry(), CONFIG)
    assert scored.score <= 0.5


def test_trade_mismatch_caps_score_lower():
    normalized = _normalized(trade=TradeType.GUTTERS)
    scored = score_candidate(normalized, _entry(), CONFIG)
    assert "trade_mismatch" in scored.conflict_reasons
    assert scored.score <= 0.4


def test_attribute_conflict_caps_score():
    entry = _entry(required_attributes={"felt_included": False})
    normalized = _normalized(attributes={"felt_included": True})
    scored = score_candidate(normalized, entry, CONFIG)
    assert "attribute_conflict" in scored.conflict_reasons
    assert scored.score <= 0.5


def test_missing_selector_forces_needs_review():
    outcome = match_line_item(_normalized(), [_entry()], CONFIG)
    assert outcome.mapping.needs_review is True
    assert "missing_selector" in outcome.mapping.review_reasons


def test_tied_candidates_force_needs_review():
    entry_a = _entry(mapping_id="a")
    entry_b = _entry(mapping_id="b")  # identical scoring inputs -> tied
    outcome = match_line_item(_normalized(), [entry_a, entry_b], CONFIG)
    assert outcome.mapping.status.value == "needs_review"
    assert "tied_candidates" in outcome.mapping.review_reasons


def test_no_catalog_match_when_component_unknown():
    outcome = match_line_item(_normalized(component="unknown"), [_entry()], CONFIG)
    assert outcome.mapping.status.value == "unmapped"
    assert outcome.mapping.best_match is None


def test_confidence_threshold_partially_mapped():
    # Score high enough for partially_mapped (>=0.80) but selector is
    # missing, so it can never reach "mapped" (>=0.92 AND selector present).
    outcome = match_line_item(_normalized(), [_entry()], CONFIG)
    assert outcome.mapping.status.value in ("partially_mapped", "needs_review")
    assert outcome.mapping.status.value != "mapped"


def test_low_score_is_unmapped():
    normalized = _normalized(
        trade=TradeType.GUTTERS, component="gutter", material="aluminum gutter", unit_of_measure="EA", action=ActionType.PAINT
    )
    outcome = match_line_item(normalized, [_entry()], CONFIG)
    assert outcome.mapping.status.value == "unmapped"


def test_no_extracted_line_item_disappears_even_when_unmapped():
    # A totally unmatched item still gets exactly one MappingOutcome, never
    # None / an exception.
    outcome = match_line_item(_normalized(component="unknown", trade=TradeType.UNKNOWN), [], CONFIG)
    assert outcome.mapping is not None
    assert outcome.mapping.status.value == "unmapped"
