from __future__ import annotations

from estimate_extractor.selector_catalog.models import SelectorRecord, normalize_description
from estimate_extractor.selector_recommendation import ranker, scoring
from estimate_extractor.selector_recommendation.models import (
    CANDIDATE_SOURCE_PLACEHOLDER_MAPPING,
    CANDIDATE_SOURCE_SELECTOR_CATALOG,
    CANDIDATE_SOURCE_VERIFIED_CATALOG,
    RECOMMENDATION_STATE_NONE,
    RECOMMENDATION_STATE_POSSIBLE,
    RECOMMENDATION_STATE_STRONG,
    RECOMMENDATION_STATE_WEAK,
    Candidate,
)
from estimate_extractor.selector_recommendation.scoring import ScoredCandidate


def _rec(selector, description="x", category="RFG"):
    return SelectorRecord(category=category, selector=selector, description_original=description, description_normalized=normalize_description(description))


def _config():
    return scoring.load_scoring_config()


def test_classify_state_thresholds():
    config = _config()
    assert ranker.classify_state(config.strong_candidate_min, config) == RECOMMENDATION_STATE_STRONG
    assert ranker.classify_state(config.possible_candidate_min, config) == RECOMMENDATION_STATE_POSSIBLE
    assert ranker.classify_state(config.weak_candidate_min, config) == RECOMMENDATION_STATE_WEAK
    assert ranker.classify_state(config.weak_candidate_min - 0.01, config) == RECOMMENDATION_STATE_NONE
    assert ranker.classify_state(None, config) == RECOMMENDATION_STATE_NONE


def test_rank_scored_candidates_drops_below_weak_threshold():
    config = _config()
    scored = [
        ScoredCandidate(record=_rec("A"), score=config.weak_candidate_min - 0.05),
        ScoredCandidate(record=_rec("B"), score=config.strong_candidate_min),
    ]
    ranked = ranker.rank_scored_candidates(scored, config)
    assert [c.selector for c in ranked] == ["B"]


def test_rank_scored_candidates_orders_descending_and_assigns_rank():
    config = _config()
    scored = [
        ScoredCandidate(record=_rec("LOW"), score=config.weak_candidate_min + 0.01),
        ScoredCandidate(record=_rec("HIGH"), score=config.strong_candidate_min),
        ScoredCandidate(record=_rec("MID"), score=config.possible_candidate_min),
    ]
    ranked = ranker.rank_scored_candidates(scored, config)
    assert [c.selector for c in ranked] == ["HIGH", "MID", "LOW"]
    assert [c.rank for c in ranked] == [1, 2, 3]


def test_rank_scored_candidates_deterministic_tie_break():
    config = _config()
    scored = [
        ScoredCandidate(record=_rec("Z", category="ZZZ"), score=0.7),
        ScoredCandidate(record=_rec("A", category="AAA"), score=0.7),
    ]
    ranked = ranker.rank_scored_candidates(scored, config)
    assert [c.category for c in ranked] == ["AAA", "ZZZ"]


def test_rank_scored_candidates_respects_max_candidates_returned():
    import dataclasses

    config = dataclasses.replace(_config(), max_candidates_returned=2)
    scored = [ScoredCandidate(record=_rec(str(i)), score=0.9 - i * 0.01) for i in range(5)]
    ranked = ranker.rank_scored_candidates(scored, config)
    assert len(ranked) == 2


def _candidate(category, selector, source, score=0.5):
    return Candidate(category=category, selector=selector, description="d", source_needs_review=False, score=score, rank=0, source=source)


def test_merge_and_rank_verified_always_first_regardless_of_score():
    config = _config()
    verified = [_candidate("RFG", "VER", CANDIDATE_SOURCE_VERIFIED_CATALOG, score=1.0)]
    selector = [_candidate("RFG", "HIGH", CANDIDATE_SOURCE_SELECTOR_CATALOG, score=0.99)]
    merged, state = ranker.merge_and_rank(verified, selector, None, config)
    assert merged[0].source == CANDIDATE_SOURCE_VERIFIED_CATALOG
    assert merged[0].rank == 1
    assert state == RECOMMENDATION_STATE_STRONG


def test_merge_and_rank_placeholder_always_last():
    config = _config()
    selector = [_candidate("RFG", "MID", CANDIDATE_SOURCE_SELECTOR_CATALOG, score=0.6)]
    placeholder = _candidate("RFG", "PLC", CANDIDATE_SOURCE_PLACEHOLDER_MAPPING, score=0.0)
    merged, _state = ranker.merge_and_rank([], selector, placeholder, config)
    assert merged[-1].source == CANDIDATE_SOURCE_PLACEHOLDER_MAPPING


def test_merge_and_rank_dedupes_same_category_selector():
    config = _config()
    verified = [_candidate("RFG", "SAME", CANDIDATE_SOURCE_VERIFIED_CATALOG, score=1.0)]
    selector = [_candidate("RFG", "SAME", CANDIDATE_SOURCE_SELECTOR_CATALOG, score=0.5)]
    merged, _state = ranker.merge_and_rank(verified, selector, None, config)
    assert len(merged) == 1
    assert merged[0].source == CANDIDATE_SOURCE_VERIFIED_CATALOG


def test_merge_and_rank_no_candidate_state_when_nothing_found():
    config = _config()
    merged, state = ranker.merge_and_rank([], [], None, config)
    assert merged == []
    assert state == RECOMMENDATION_STATE_NONE


def test_merge_and_rank_placeholder_only_yields_weak_state():
    config = _config()
    placeholder = _candidate("RFG", "PLC", CANDIDATE_SOURCE_PLACEHOLDER_MAPPING, score=0.0)
    merged, state = ranker.merge_and_rank([], [], placeholder, config)
    assert state == RECOMMENDATION_STATE_WEAK
    assert len(merged) == 1
