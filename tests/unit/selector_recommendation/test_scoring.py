from __future__ import annotations

from pathlib import Path

from estimate_extractor.selector_catalog.models import SelectorRecord, normalize_description
from estimate_extractor.selector_recommendation import scoring
from estimate_extractor.selector_recommendation.models import RecommendationInput


def _rec(description, category="RFG", selector="X", needs_review=False, ocr_confidence=0.95):
    return SelectorRecord(
        category=category,
        selector=selector,
        description_original=description,
        description_normalized=normalize_description(description),
        needs_review=needs_review,
        ocr_confidence=ocr_confidence,
    )


def test_load_scoring_config_weights_sum_to_one():
    config = scoring.load_scoring_config()
    assert abs(sum(config.weights.values()) - 1.0) < 1e-6


def test_score_candidate_high_for_near_exact_description_match(rules):
    config = scoring.load_scoring_config()
    # Mirrors the context a real Phase 2 normalized item actually carries
    # (component/material populated, not just a bare trade) -- see
    # service.build_recommendation_input.
    item = RecommendationInput(
        line_item_id="x",
        trade="roofing",
        component="composition_shingles",
        material="composition shingles",
        original_description="Tear off composition shingles",
    )
    record = _rec("Tear off composition shingles", selector="ARMVN")
    scored = scoring.score_candidate(item, record, ["RFG"], rules, config)
    assert scored.score >= config.strong_candidate_min
    assert scored.match_reasons  # explainable


def test_score_candidate_low_for_unrelated_description(rules):
    config = scoring.load_scoring_config()
    item = RecommendationInput(line_item_id="x", trade="roofing", original_description="Tear off composition shingles")
    record = _rec("Clean floor lamp - crystal", category="CLM", selector="LAMP")
    scored = scoring.score_candidate(item, record, ["RFG"], rules, config)
    assert scored.score < config.possible_candidate_min


def test_category_conflict_caps_score_even_with_strong_text_match(rules):
    config = scoring.load_scoring_config()
    item = RecommendationInput(line_item_id="x", trade="roofing", original_description="Tear off composition shingles")
    # Exact text match, but from a category outside the hinted set.
    record = _rec("Tear off composition shingles", category="DOR", selector="X")
    scored = scoring.score_candidate(item, record, ["RFG"], rules, config)
    assert scored.score <= config.conflict_caps["category_incompatible"]
    assert any("outside the hinted categories" in p for p in scored.penalties)


def test_action_conflict_caps_score(rules):
    config = scoring.load_scoring_config()
    item = RecommendationInput(line_item_id="x", action="install", original_description="Install composition shingles")
    record = _rec("Tear off composition shingles")  # implies remove, conflicts with install
    scored = scoring.score_candidate(item, record, [], rules, config)
    assert scored.score <= config.conflict_caps["action_conflict"]


def test_ocr_confidence_penalty_reduces_score(rules):
    config = scoring.load_scoring_config()
    item = RecommendationInput(line_item_id="x", original_description="Tear off composition shingles")
    high_conf = _rec("Tear off composition shingles", ocr_confidence=0.95, selector="A")
    low_conf = _rec("Tear off composition shingles", ocr_confidence=0.30, selector="B")
    scored_high = scoring.score_candidate(item, high_conf, [], rules, config)
    scored_low = scoring.score_candidate(item, low_conf, [], rules, config)
    assert scored_low.score < scored_high.score
    assert any("OCR confidence" in p for p in scored_low.penalties)


def test_uncertain_source_penalty_applied_only_when_include_uncertain(rules):
    config = scoring.load_scoring_config()
    item = RecommendationInput(line_item_id="x", original_description="Tear off composition shingles")
    record = _rec("Tear off composition shingles", needs_review=True)

    scored_default = scoring.score_candidate(item, record, [], rules, config, include_uncertain=False)
    scored_uncertain = scoring.score_candidate(item, record, [], rules, config, include_uncertain=True)
    assert scored_default.penalties == []  # never penalized/labeled when not surfaced at all
    assert any("needs_review" in p for p in scored_uncertain.penalties)
    assert scored_uncertain.score < scored_default.score


def test_verified_evidence_flag_recorded_and_explained(rules):
    config = scoring.load_scoring_config()
    item = RecommendationInput(line_item_id="x", original_description="Tear off composition shingles")
    record = _rec("Tear off composition shingles")
    scored = scoring.score_candidate(item, record, [], rules, config, verified_evidence=True)
    assert scored.verified_evidence is True
    assert any("human-verified" in r for r in scored.match_reasons)


def test_missing_context_reduces_confidence_without_crashing(rules):
    config = scoring.load_scoring_config()
    item = RecommendationInput(line_item_id="x")  # everything else missing
    record = _rec("Tear off composition shingles")
    scored = scoring.score_candidate(item, record, [], rules, config)
    assert 0.0 <= scored.score <= 1.0
    assert scored.score < config.possible_candidate_min
