"""Scores every catalog entry against a normalized line item and decides a
conservative mapping status.

Deterministic rules first (exact component/trade/action/unit match), fuzzy
material-text similarity only as one of six weighted components (see
scorer.py). A high text-similarity score alone can never produce
`mapped`/`partially_mapped` when the unit is incompatible, the action
conflicts, the trade conflicts, the component is unknown, or the top two
candidates are nearly tied -- each of those caps the score and/or forces
`needs_review` regardless of the raw weighted total.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from estimate_extractor.mapping.catalog import CatalogEntry
from estimate_extractor.mapping.models import (
    MappingCandidate,
    MappingOutcome,
    MappingStatus,
    Normalized,
)
from estimate_extractor.mapping.scorer import ScoringConfig, material_similarity


@dataclass(frozen=True, slots=True)
class ScoredCandidate:
    entry: CatalogEntry
    score: float
    conflict_reasons: tuple[str, ...]


def score_candidate(normalized: Normalized, entry: CatalogEntry, config: ScoringConfig) -> ScoredCandidate:
    reasons: list[str] = []

    component_ok = normalized.component != "unknown" and normalized.component == entry.component
    component_score = 1.0 if component_ok else 0.0
    if not component_ok:
        reasons.append("component_mismatch" if normalized.component != "unknown" else "component_unknown")

    trade_ok = normalized.trade.value != "unknown" and normalized.trade.value == entry.trade
    trade_score = 1.0 if trade_ok else 0.0
    if not trade_ok:
        reasons.append("trade_mismatch")

    action_ok = normalized.action.value in entry.allowed_actions
    action_score = 1.0 if action_ok else 0.0
    if not action_ok:
        reasons.append("action_incompatible")

    unit_ok = (
        not entry.allowed_units
        or normalized.unit_of_measure is None
        or normalized.unit_of_measure.upper() in entry.allowed_units
    )
    unit_score = 1.0 if unit_ok else 0.0
    if not unit_ok:
        reasons.append("unit_incompatible")

    material_score = material_similarity(normalized.material, entry.canonical_terms, config)
    context_score = 0.5  # neutral: section/area context compatibility is not modeled in this MVP

    attribute_conflict = False
    for key, required_value in entry.required_attributes.items():
        if key in normalized.attributes and normalized.attributes[key] != required_value:
            attribute_conflict = True
            reasons.append("attribute_conflict")
            break

    weights = config.weights
    weighted = (
        weights.get("component_match", 0.35) * component_score
        + weights.get("trade_match", 0.20) * trade_score
        + weights.get("action_compatibility", 0.15) * action_score
        + weights.get("unit_compatibility", 0.15) * unit_score
        + weights.get("material_similarity", 0.10) * material_score
        + weights.get("context_compatibility", 0.05) * context_score
    )

    # Hard caps: a conflict on any of these must not be masked by a high
    # material-text similarity score.
    if not component_ok:
        weighted = min(weighted, 0.5)
    if not trade_ok:
        weighted = min(weighted, 0.4)
    if not action_ok:
        weighted = min(weighted, 0.5)
    if not unit_ok:
        weighted = min(weighted, 0.5)
    if attribute_conflict:
        weighted = min(weighted, 0.5)

    return ScoredCandidate(entry=entry, score=round(weighted, 4), conflict_reasons=tuple(reasons))


def _to_candidate(scored: ScoredCandidate) -> MappingCandidate:
    x = scored.entry.xactimate
    return MappingCandidate(
        mapping_id=scored.entry.mapping_id,
        category=x.category,
        selector=x.selector,
        activity=x.activity,
        description=x.description,
        confidence=scored.score,
    )


@dataclass(slots=True)
class MatchOutcome:
    mapping: MappingOutcome
    all_scored: list[ScoredCandidate] = field(default_factory=list)


def match_line_item(
    normalized: Normalized, catalog: list[CatalogEntry], config: ScoringConfig
) -> MatchOutcome:
    if normalized.component == "unknown" or normalized.trade.value == "unknown":
        return MatchOutcome(
            mapping=MappingOutcome(
                status=MappingStatus.UNMAPPED,
                best_match=None,
                alternatives=[],
                needs_review=True,
                review_reasons=["no_catalog_match: trade/component not recognized during normalization"],
            ),
            all_scored=[],
        )

    scored = sorted(
        (score_candidate(normalized, entry, config) for entry in catalog),
        key=lambda c: c.score,
        reverse=True,
    )

    if not scored or scored[0].score < config.needs_review_min:
        return MatchOutcome(
            mapping=MappingOutcome(
                status=MappingStatus.UNMAPPED,
                best_match=None,
                alternatives=[_to_candidate(c) for c in scored[:3] if c.score > 0],
                needs_review=True,
                review_reasons=["no_catalog_match: no candidate scored above the review threshold"],
            ),
            all_scored=scored,
        )

    top = scored[0]
    review_reasons: list[str] = list(top.conflict_reasons)
    tied = len(scored) > 1 and (top.score - scored[1].score) <= config.tie_margin
    if tied:
        review_reasons.append("tied_candidates")

    missing_selector = top.entry.xactimate.selector is None
    if missing_selector and config.missing_selector_forces_review:
        review_reasons.append("missing_selector")
    if top.entry.requires_review:
        review_reasons.append("catalog_entry_requires_review")

    if tied:
        status = MappingStatus.NEEDS_REVIEW
    elif top.score >= config.mapped_min and not missing_selector:
        status = MappingStatus.MAPPED
    elif top.score >= config.partially_mapped_min:
        status = MappingStatus.PARTIALLY_MAPPED
    else:
        status = MappingStatus.NEEDS_REVIEW

    needs_review = status != MappingStatus.MAPPED or bool(review_reasons)

    alternatives = [_to_candidate(c) for c in scored[1:4] if c.score >= config.needs_review_min]

    return MatchOutcome(
        mapping=MappingOutcome(
            status=status,
            best_match=_to_candidate(top),
            alternatives=alternatives,
            needs_review=needs_review,
            review_reasons=review_reasons,
        ),
        all_scored=scored,
    )
