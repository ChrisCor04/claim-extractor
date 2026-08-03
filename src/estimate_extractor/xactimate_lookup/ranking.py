"""Ranks captured Xactimate dropdown results against the source line
item and classifies the outcome as AUTO_SELECT / REVIEW_REQUIRED /
NO_MATCH. Same deterministic weighted-score + hard-conflict-cap shape as
selector_recommendation/scoring.py -- no LLM, nothing opaque. AUTO_SELECT
additionally requires a strong score, no hard conflict, reliable
extraction, AND a clear margin over the runner-up (see build spec
"Do not automatically choose the first dropdown result.").
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from estimate_extractor.selector_catalog.models import normalize_description
from estimate_extractor.xactimate_lookup.models import (
    DECISION_AUTO_SELECT,
    DECISION_NO_MATCH,
    DECISION_REVIEW_REQUIRED,
    DropdownResult,
    RankedCandidate,
)
from estimate_extractor.xactimate_lookup.phrase_generator import PhraseRules

DEFAULT_RANKING_PATH = Path(__file__).resolve().parents[3] / "config" / "xactimate_lookup_ranking.yaml"


@dataclass(frozen=True, slots=True)
class RankingConfig:
    weights: dict[str, float]
    conflict_caps: dict[str, float]
    auto_select_min: float
    review_required_min: float
    auto_select_margin: float
    min_extraction_confidence: float
    fuzzy_enabled: bool
    fuzzy_min_score: float
    max_dropdown_candidates_considered: int


def load_ranking_config(path: Path = DEFAULT_RANKING_PATH) -> RankingConfig:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    thresholds = raw.get("thresholds", {})
    fuzzy = raw.get("fuzzy_matching", {})
    return RankingConfig(
        weights=raw.get("weights", {}),
        conflict_caps=raw.get("conflict_caps", {}),
        auto_select_min=float(thresholds.get("auto_select_min", 0.88)),
        review_required_min=float(thresholds.get("review_required_min", 0.55)),
        auto_select_margin=float(raw.get("auto_select_margin", 0.08)),
        min_extraction_confidence=float(raw.get("min_extraction_confidence", 0.80)),
        fuzzy_enabled=bool(fuzzy.get("enabled", True)),
        fuzzy_min_score=float(fuzzy.get("min_score", 0.35)),
        max_dropdown_candidates_considered=int(raw.get("max_dropdown_candidates_considered", 15)),
    )


def _fuzzy_ratio(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    try:
        from rapidfuzz import fuzz  # type: ignore[import-not-found]

        return fuzz.ratio(a, b) / 100.0
    except ImportError:
        return difflib.SequenceMatcher(None, a, b).ratio()


def score_dropdown_candidate(
    *,
    original_description: str,
    trade: str | None,
    component: str | None,
    material: str | None,
    action: str | None,
    unit: str | None,
    size_key: str | None,
    grade_key: str | None,
    dropdown: DropdownResult,
    rules: PhraseRules,
    config: RankingConfig,
    prior_verified_mapping: bool = False,
) -> RankedCandidate:
    item_text = normalize_description(original_description or "")
    candidate_text = normalize_description(dropdown.description or dropdown.raw_text or "")

    description_score = 0.0
    if item_text and candidate_text:
        description_score = 1.0 if (item_text in candidate_text or candidate_text in item_text) else _fuzzy_ratio(item_text, candidate_text)

    # "unknown" is Phase 2's sentinel for "no component was extracted" --
    # it must never be treated as a literal component to search for
    # (that would manufacture a false wrong_component conflict on every
    # item with unrecognized component, exactly the kind of "missing
    # context" case that should reduce confidence, not fabricate one).
    component_clean = component if component and component != "unknown" else ""
    component_words = {w for w in re.findall(r"[a-z0-9]+", component_clean.replace("_", " "))}
    candidate_words = set(re.findall(r"[a-z0-9]+", candidate_text))
    component_ok = not component_words or bool(component_words & candidate_words)
    component_score = 1.0 if component_ok else 0.0

    material_ok = True
    material_score = 0.0
    material_applicable = bool(material)
    if material:
        material_lower = material.lower()
        material_words = set(re.findall(r"[a-z0-9]+", material_lower))
        if material_lower in candidate_text or candidate_text in material_lower:
            material_score = 1.0
        elif material_words & candidate_words:
            # At least one material word (e.g. "aluminum") appears
            # literally in the candidate -- treat as a match even if the
            # full material phrase doesn't line up word-for-word.
            material_score = 1.0
        else:
            fuzzy = _fuzzy_ratio(material_lower, candidate_text) if config.fuzzy_enabled else 0.0
            material_score = fuzzy
            # A real conflict requires BOTH low fuzzy similarity AND zero
            # literal word overlap -- otherwise this is just a case where
            # the candidate description doesn't mention material at all,
            # which is neutral, not a conflict.
            material_ok = fuzzy >= config.fuzzy_min_score

    size_ok = True
    size_score = 0.0
    size_applicable = bool(size_key)
    if size_key:
        size_ok = size_key.lower() in candidate_text
        size_score = 1.0 if size_ok else 0.0

    grade_ok = True
    grade_score = 0.0
    grade_applicable = bool(grade_key)
    if grade_key:
        grade_ok = grade_key.lower() in candidate_text
        grade_score = 1.0 if grade_ok else 0.0

    action_term = rules.action_search_terms.get(action or "") if action else None
    action_ok = True
    action_score = 0.0
    action_applicable = bool(action_term)
    if action_term:
        action_ok = action_term.lower() in candidate_text
        action_score = 1.0 if action_ok else 0.5  # absence is neutral -- most descriptions omit action entirely

    verified_score = 1.0 if prior_verified_mapping else 0.0

    # Weighted AVERAGE OVER APPLICABLE DIMENSIONS ONLY -- a dimension
    # with no real signal to check (no size/grade/action wording, no
    # prior verified mapping) is excluded from both the numerator and
    # denominator rather than diluted in at a flat neutral score. This
    # matters concretely: without it, a description-search item with no
    # size/grade info and no prior mapping can never structurally exceed
    # ~0.85 even with a perfect description/component/material/action
    # match, making auto_select_min effectively unreachable (found via
    # real-data testing against config/xactimate_lookup_ranking.yaml's
    # thresholds -- see docs/xactimate-lookup.md "Ranking calibration").
    # unit_compatibility is always excluded: DropdownResult currently
    # carries no structured unit to actually check.
    weights = config.weights
    weighted_sum = weights.get("description_similarity", 0.25) * description_score
    weighted_sum += weights.get("component_match", 0.25) * component_score
    applicable_weight = weights.get("description_similarity", 0.25) + weights.get("component_match", 0.25)

    for applicable, weight_key, score in (
        (material_applicable, "material_match", material_score),
        (size_applicable, "size_match", size_score),
        (action_applicable, "action_compatibility", action_score),
        (grade_applicable, "grade_style_match", grade_score),
        (prior_verified_mapping, "prior_verified_mapping", verified_score),
    ):
        if not applicable:
            continue
        w = weights.get(weight_key, 0.0)
        weighted_sum += w * score
        applicable_weight += w

    weighted = weighted_sum / applicable_weight if applicable_weight else 0.0

    match_reasons: list[str] = []
    conflict_reasons: list[str] = []
    caps = config.conflict_caps

    if description_score >= 0.85:
        match_reasons.append("description closely matches the dropdown result")
    if not component_ok:
        weighted = min(weighted, caps.get("wrong_component", 0.45))
        conflict_reasons.append(f"wrong_component: {sorted(component_words)} not found in candidate description")
    elif component_words:
        match_reasons.append("component matches")
    if material and not material_ok:
        weighted = min(weighted, caps.get("wrong_material", 0.55))
        conflict_reasons.append(f"wrong_material: {material!r} not found in candidate description")
    elif material and material_score >= 0.99:
        match_reasons.append("material matches")
    if size_key and not size_ok:
        weighted = min(weighted, caps.get("wrong_size", 0.60))
        conflict_reasons.append(f"wrong_size: {size_key!r} not found in candidate description")
    elif size_key and size_ok:
        match_reasons.append("size matches")
    if grade_key and not grade_ok:
        weighted = min(weighted, caps.get("incompatible_grade_or_style", 0.55))
        conflict_reasons.append(f"incompatible grade/style: {grade_key!r} not found in candidate description")
    elif grade_key and grade_ok:
        match_reasons.append("grade/style matches")
    if action_term and not action_ok:
        weighted = min(weighted, caps.get("wrong_action", 0.50))
        conflict_reasons.append(f"wrong_action: {action_term!r} not found in candidate description")
    if prior_verified_mapping:
        match_reasons.append("backed by a prior verified internal mapping")

    if dropdown.extraction_confidence < config.min_extraction_confidence:
        conflict_reasons.append(f"low dropdown extraction confidence ({dropdown.extraction_confidence:.2f})")

    return RankedCandidate(
        dropdown=dropdown,
        score=round(min(1.0, max(0.0, weighted)), 4),
        match_reasons=match_reasons,
        conflict_reasons=conflict_reasons,
    )


def rank_dropdown_results(
    *,
    original_description: str,
    trade: str | None,
    component: str | None,
    material: str | None,
    action: str | None,
    unit: str | None,
    size_key: str | None,
    grade_key: str | None,
    dropdowns: list[DropdownResult],
    rules: PhraseRules,
    config: RankingConfig,
    prior_verified_mapping: bool = False,
) -> list[RankedCandidate]:
    bounded = dropdowns[: config.max_dropdown_candidates_considered]
    scored = [
        score_dropdown_candidate(
            original_description=original_description, trade=trade, component=component, material=material,
            action=action, unit=unit, size_key=size_key, grade_key=grade_key, dropdown=d, rules=rules,
            config=config, prior_verified_mapping=prior_verified_mapping,
        )
        for d in bounded
    ]
    scored.sort(key=lambda c: (-c.score, c.dropdown.row_position))
    return scored


def classify_decision(candidates: list[RankedCandidate], config: RankingConfig) -> str:
    """Never automatically returns AUTO_SELECT for an empty or single
    weak result -- see build spec 'Do not automatically choose the first
    dropdown result.'."""
    if not candidates:
        return DECISION_NO_MATCH

    top = candidates[0]
    if top.score < config.review_required_min:
        return DECISION_NO_MATCH

    if top.score < config.auto_select_min:
        return DECISION_REVIEW_REQUIRED
    if top.has_hard_conflict:
        return DECISION_REVIEW_REQUIRED
    if top.dropdown.extraction_confidence < config.min_extraction_confidence:
        return DECISION_REVIEW_REQUIRED

    second_score = candidates[1].score if len(candidates) > 1 else 0.0
    if (top.score - second_score) < config.auto_select_margin:
        return DECISION_REVIEW_REQUIRED

    return DECISION_AUTO_SELECT
