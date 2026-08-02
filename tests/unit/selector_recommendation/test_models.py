from __future__ import annotations

from estimate_extractor.selector_recommendation.models import (
    RECOMMENDATION_STATE_STRONG,
    Candidate,
    RecommendationInput,
    RecommendationResult,
)


def test_candidate_round_trips_through_dict():
    candidate = Candidate(
        category="RFG",
        selector="ARMVN",
        description="Tear off composition shingles - 3 tab",
        source_needs_review=False,
        score=0.91,
        rank=1,
        match_reasons=["description contains ridge vent"],
        penalties=[],
        source_image="Screenshots_By_CAT/RFG/RFG_012.png",
        ocr_confidence=0.97,
    )
    restored = Candidate.from_dict(candidate.to_dict())
    assert restored == candidate


def test_candidate_preserves_selector_value_exactly_through_round_trip():
    # Real malformed-looking-but-valid selectors carry punctuation that
    # must never be collapsed or altered (see selector_catalog QA phase).
    candidate = Candidate(category="RFG", selector="ARMVN>>", description="x", source_needs_review=False, score=0.5, rank=1)
    restored = Candidate.from_dict(candidate.to_dict())
    assert restored.selector == "ARMVN>>"


def test_recommendation_result_round_trips_through_dict():
    candidate = Candidate(category="RFG", selector="A", description="x", source_needs_review=False, score=0.9, rank=1)
    result = RecommendationResult(line_item_id="line_0001", state=RECOMMENDATION_STATE_STRONG, candidates=[candidate])
    restored = RecommendationResult.from_dict(result.to_dict())
    assert restored.line_item_id == "line_0001"
    assert restored.state == RECOMMENDATION_STATE_STRONG
    assert len(restored.candidates) == 1
    assert restored.candidates[0].selector == "A"


def test_recommendation_input_tolerates_all_optional_fields_missing():
    item = RecommendationInput(line_item_id="line_0001")
    assert item.trade is None
    assert item.attributes == {}
