"""Final ranking: converts scored selector-catalog candidates into the
public ``Candidate`` shape, merges in verified-catalog and placeholder-
mapping candidates at their spec-mandated priority, assigns rank/state.

Priority order (build spec "Verified catalog priority"):
  1. existing approved item-level human override -- handled upstream in
     service.py (locks the item, this module still returns candidates for
     visibility but callers must check ``locked_by_approval``)
  2. existing human-verified reusable catalog rule -- always ranked first
     here, never competes on the deterministic score
  3. eligible Phase 3.6 selector-catalog recommendation -- this module's
     scored/ranked output
  4. needs-review Phase 3.6 selector reference -- only present in the
     scored list at all when include_uncertain=True (query.py's job)
  5. placeholder mapping suggestion -- always ranked last
"""

from __future__ import annotations

from estimate_extractor.selector_recommendation.models import (
    RECOMMENDATION_STATE_NONE,
    RECOMMENDATION_STATE_POSSIBLE,
    RECOMMENDATION_STATE_STRONG,
    RECOMMENDATION_STATE_WEAK,
    CANDIDATE_SOURCE_SELECTOR_CATALOG,
    Candidate,
)
from estimate_extractor.selector_recommendation.scoring import ScoredCandidate, ScoringConfig


def classify_state(top_score: float | None, config: ScoringConfig) -> str:
    if top_score is None:
        return RECOMMENDATION_STATE_NONE
    if top_score >= config.strong_candidate_min:
        return RECOMMENDATION_STATE_STRONG
    if top_score >= config.possible_candidate_min:
        return RECOMMENDATION_STATE_POSSIBLE
    if top_score >= config.weak_candidate_min:
        return RECOMMENDATION_STATE_WEAK
    return RECOMMENDATION_STATE_NONE


def rank_scored_candidates(scored: list[ScoredCandidate], config: ScoringConfig) -> list[Candidate]:
    """Drops anything below weak_candidate_min, sorts descending,
    deterministic tie-break on (category, selector) so ranking never
    depends on incidental dict/DB ordering, caps to
    max_candidates_returned, and converts to the public Candidate shape."""
    survivors = [s for s in scored if s.score >= config.weak_candidate_min]
    survivors.sort(key=lambda s: (-s.score, s.record.category, s.record.selector))
    top = survivors[: config.max_candidates_returned]

    if len(top) > 1 and (top[0].score - top[1].score) <= config.tie_margin:
        top[0].match_reasons = list(top[0].match_reasons) + [
            "tied_candidates: top two candidates scored within the configured tie margin"
        ]

    candidates: list[Candidate] = []
    for i, s in enumerate(top, start=1):
        candidates.append(
            Candidate(
                category=s.record.category,
                selector=s.record.selector,
                description=s.record.description_original,
                source_needs_review=s.record.needs_review,
                score=s.score,
                rank=i,
                match_reasons=list(s.match_reasons),
                penalties=list(s.penalties),
                source_image=s.record.primary_source_image,
                ocr_confidence=s.record.ocr_confidence,
                source=CANDIDATE_SOURCE_SELECTOR_CATALOG,
            )
        )
    return candidates


def merge_and_rank(
    verified_candidates: list[Candidate],
    selector_candidates: list[Candidate],
    placeholder_candidate: Candidate | None,
    config: ScoringConfig,
) -> tuple[list[Candidate], str]:
    """Merges the three candidate sources at their mandated priority,
    re-numbers rank across the merged list, and classifies the overall
    recommendation state. A human-verified rule always yields
    strong_candidate regardless of the selector-catalog scores, since it
    is authoritative evidence rather than a heuristic estimate."""
    seen_keys: set[tuple[str, str]] = set()
    merged: list[Candidate] = []

    for c in verified_candidates:
        key = (c.category, c.selector)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        merged.append(c)

    for c in selector_candidates:
        key = (c.category, c.selector)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        merged.append(c)

    if placeholder_candidate is not None:
        key = (placeholder_candidate.category, placeholder_candidate.selector)
        if key not in seen_keys and placeholder_candidate.category and placeholder_candidate.selector:
            merged.append(placeholder_candidate)

    for i, c in enumerate(merged, start=1):
        c.rank = i

    if verified_candidates:
        state = RECOMMENDATION_STATE_STRONG
    elif selector_candidates:
        state = classify_state(selector_candidates[0].score, config)
    elif placeholder_candidate is not None:
        state = RECOMMENDATION_STATE_WEAK
    else:
        state = RECOMMENDATION_STATE_NONE

    return merged, state
