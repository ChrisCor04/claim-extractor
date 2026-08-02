"""Typed models for the selector-recommendation layer.

Deliberately decoupled from ``estimate_extractor.mapping.models`` beyond
the plain values copied out of a mapped/normalized line item in
``RecommendationInput`` -- this stage reads Phase 2's output files, it
does not import Phase 2's internal models, so the two stages can evolve
independently (same pattern as ``mapping.models.OriginalItem``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

RECOMMENDATION_STATE_STRONG = "strong_candidate"
RECOMMENDATION_STATE_POSSIBLE = "possible_candidate"
RECOMMENDATION_STATE_WEAK = "weak_candidate"
RECOMMENDATION_STATE_NONE = "no_candidate"
VALID_RECOMMENDATION_STATES = frozenset(
    {RECOMMENDATION_STATE_STRONG, RECOMMENDATION_STATE_POSSIBLE, RECOMMENDATION_STATE_WEAK, RECOMMENDATION_STATE_NONE}
)

# Candidate provenance -- distinguishes a Phase 3.5 human-verified rule
# (always ranked first, never scored against the deterministic weights)
# from an ordinary Phase 3.6 selector-catalog suggestion and from a
# Phase 2 placeholder catalog entry (lowest priority; see build spec
# "Verified catalog priority").
CANDIDATE_SOURCE_VERIFIED_CATALOG = "verified_catalog"
CANDIDATE_SOURCE_SELECTOR_CATALOG = "selector_catalog"
CANDIDATE_SOURCE_PLACEHOLDER_MAPPING = "placeholder_mapping"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True, slots=True)
class RecommendationInput:
    """Everything the recommendation service knows about one source line
    item. Every field except ``line_item_id`` is optional -- a missing
    field reduces confidence (fewer positive signals available) rather
    than crashing the recommender, per the build spec "Missing context
    should reduce confidence rather than crash the recommendation
    system."."""

    line_item_id: str
    original_description: str | None = None
    normalized_description: str | None = None
    action: str | None = None
    trade: str | None = None
    component: str | None = None
    material: str | None = None
    attributes: dict = field(default_factory=dict)
    source_unit: str | None = None
    quantity: float | None = None
    section: str | None = None
    area: str | None = None
    coverage: str | None = None
    existing_mapping_status: str | None = None
    existing_category: str | None = None
    existing_selector: str | None = None
    review_status: str | None = None


@dataclass(slots=True)
class Candidate:
    """One recommended CAT/SEL pair. Shape matches the build spec's
    example JSON, plus a ``source`` field distinguishing verified-catalog
    / selector-catalog / placeholder-mapping provenance (see build spec
    "Verified catalog priority")."""

    category: str
    selector: str
    description: str
    source_needs_review: bool
    score: float
    rank: int
    match_reasons: list[str] = field(default_factory=list)
    penalties: list[str] = field(default_factory=list)
    source_image: str | None = None
    ocr_confidence: float | None = None
    source: str = CANDIDATE_SOURCE_SELECTOR_CATALOG
    screenshot_available: bool = True

    def to_dict(self) -> dict:
        return {
            "category": self.category,
            "selector": self.selector,
            "description": self.description,
            "source_needs_review": self.source_needs_review,
            "score": self.score,
            "rank": self.rank,
            "match_reasons": list(self.match_reasons),
            "penalties": list(self.penalties),
            "source_image": self.source_image,
            "ocr_confidence": self.ocr_confidence,
            "source": self.source,
            "screenshot_available": self.screenshot_available,
        }

    @staticmethod
    def from_dict(data: dict) -> Candidate:
        return Candidate(
            category=data["category"],
            selector=data["selector"],
            description=data.get("description", ""),
            source_needs_review=bool(data.get("source_needs_review", False)),
            score=float(data.get("score", 0.0)),
            rank=int(data.get("rank", 0)),
            match_reasons=list(data.get("match_reasons") or []),
            penalties=list(data.get("penalties") or []),
            source_image=data.get("source_image"),
            ocr_confidence=data.get("ocr_confidence"),
            source=data.get("source", CANDIDATE_SOURCE_SELECTOR_CATALOG),
            screenshot_available=bool(data.get("screenshot_available", True)),
        )


@dataclass(slots=True)
class RecommendationResult:
    """The full ranked recommendation for one line item."""

    line_item_id: str
    state: str
    candidates: list[Candidate] = field(default_factory=list)
    locked_by_approval: bool = False
    include_uncertain: bool = False
    generated_at: str = field(default_factory=utc_now_iso)
    latency_ms: float | None = None

    def to_dict(self) -> dict:
        return {
            "line_item_id": self.line_item_id,
            "state": self.state,
            "candidates": [c.to_dict() for c in self.candidates],
            "locked_by_approval": self.locked_by_approval,
            "include_uncertain": self.include_uncertain,
            "generated_at": self.generated_at,
            "latency_ms": self.latency_ms,
        }

    @staticmethod
    def from_dict(data: dict) -> RecommendationResult:
        return RecommendationResult(
            line_item_id=data["line_item_id"],
            state=data["state"],
            candidates=[Candidate.from_dict(c) for c in (data.get("candidates") or [])],
            locked_by_approval=bool(data.get("locked_by_approval", False)),
            include_uncertain=bool(data.get("include_uncertain", False)),
            generated_at=data.get("generated_at", utc_now_iso()),
            latency_ms=data.get("latency_ms"),
        )
