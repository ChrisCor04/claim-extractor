"""Deterministic, explainable scoring for one (item, candidate) pair.

Weighted components first (description/category/component/material/
action/attribute/verified-evidence/unit), then hard caps applied when a
real compatibility conflict is present -- same shape as
mapping/scorer.py's conflict-override rules: a high text-similarity score
can never mask a category/action/unit/distinction conflict. No LLM or
external call anywhere in this module.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from estimate_extractor.selector_catalog.models import SelectorRecord, normalize_description
from estimate_extractor.selector_recommendation import explanations
from estimate_extractor.selector_recommendation.candidate_generation import RecommendationRules, tokenize
from estimate_extractor.selector_recommendation.compatibility import CompatibilityResult, compute_compatibility
from estimate_extractor.selector_recommendation.models import RecommendationInput

DEFAULT_SCORING_PATH = Path(__file__).resolve().parents[3] / "config" / "selector_recommendation_scoring.yaml"


@dataclass(frozen=True, slots=True)
class ScoringConfig:
    weights: dict[str, float]
    strong_candidate_min: float
    possible_candidate_min: float
    weak_candidate_min: float
    conflict_caps: dict[str, float]
    uncertain_source_penalty: float
    ocr_confidence_threshold: float
    ocr_confidence_max_penalty: float
    fuzzy_enabled: bool
    fuzzy_min_score: float
    max_category_pool: int
    max_candidates_returned: int
    max_categories_searched_when_no_hint: int
    tie_margin: float


def load_scoring_config(path: Path = DEFAULT_SCORING_PATH) -> ScoringConfig:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    thresholds = raw.get("thresholds", {})
    ocr_penalty = raw.get("ocr_confidence_penalty", {})
    fuzzy = raw.get("fuzzy_matching", {})
    limits = raw.get("candidate_limits", {})
    return ScoringConfig(
        weights=raw.get("weights", {}),
        strong_candidate_min=float(thresholds.get("strong_candidate_min", 0.80)),
        possible_candidate_min=float(thresholds.get("possible_candidate_min", 0.55)),
        weak_candidate_min=float(thresholds.get("weak_candidate_min", 0.35)),
        conflict_caps=raw.get("conflict_caps", {}),
        uncertain_source_penalty=float(raw.get("uncertain_source_penalty", 0.15)),
        ocr_confidence_threshold=float(ocr_penalty.get("threshold", 0.85)),
        ocr_confidence_max_penalty=float(ocr_penalty.get("max_penalty", 0.10)),
        fuzzy_enabled=bool(fuzzy.get("enabled", True)),
        fuzzy_min_score=float(fuzzy.get("min_score", 0.35)),
        max_category_pool=int(limits.get("max_category_pool", 400)),
        max_candidates_returned=int(limits.get("max_candidates_returned", 8)),
        max_categories_searched_when_no_hint=int(limits.get("max_categories_searched_when_no_hint", 12)),
        tie_margin=float(raw.get("tie_margin", 0.03)),
    )


def _fuzzy_ratio(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    try:
        from rapidfuzz import fuzz  # type: ignore[import-not-found]

        return fuzz.ratio(a, b) / 100.0
    except ImportError:
        return difflib.SequenceMatcher(None, a, b).ratio()


def _description_similarity(item: RecommendationInput, record: SelectorRecord) -> float:
    item_text = normalize_description(item.normalized_description or item.original_description or "")
    if not item_text or not record.description_normalized:
        return 0.0
    if item_text in record.description_normalized or record.description_normalized in item_text:
        return 1.0
    return _fuzzy_ratio(item_text, record.description_normalized)


def _component_similarity(item: RecommendationInput, record: SelectorRecord) -> float:
    component_tokens = tokenize((item.component or "").replace("_", " "))
    if not component_tokens:
        return 0.3  # neutral: no component signal available, per "missing context reduces confidence"
    record_tokens = tokenize(record.description_normalized)
    overlap = len(component_tokens & record_tokens)
    return min(1.0, overlap / len(component_tokens))


def _material_similarity(item: RecommendationInput, record: SelectorRecord, config: ScoringConfig) -> float:
    if not item.material:
        return 0.3
    material_lower = item.material.lower()
    if material_lower in record.description_normalized or record.description_normalized in material_lower:
        return 1.0
    if not config.fuzzy_enabled:
        return 0.0
    score = _fuzzy_ratio(material_lower, record.description_normalized)
    return score if score >= config.fuzzy_min_score else 0.0


def _attribute_match(item: RecommendationInput, record: SelectorRecord) -> float:
    if not item.attributes:
        return 0.3
    for value in item.attributes.values():
        if value is None:
            continue
        text = str(value).lower()
        if text and text in record.description_normalized:
            return 1.0
    return 0.4  # attributes present but none textually confirmed -- weak neutral, not a conflict


@dataclass(slots=True)
class ScoredCandidate:
    record: SelectorRecord
    score: float
    match_reasons: list[str] = field(default_factory=list)
    penalties: list[str] = field(default_factory=list)
    verified_evidence: bool = False


def score_candidate(
    item: RecommendationInput,
    record: SelectorRecord,
    hinted_categories: list[str],
    rules: RecommendationRules,
    config: ScoringConfig,
    *,
    verified_evidence: bool = False,
    include_uncertain: bool = False,
) -> ScoredCandidate:
    compat = compute_compatibility(item, record, hinted_categories, rules)
    description_score = _description_similarity(item, record)
    component_score = _component_similarity(item, record)
    material_score = _material_similarity(item, record, config)
    attribute_score = _attribute_match(item, record)
    # "none"/"unlabeled" are both an absence of comparable evidence (no
    # action wording in the candidate, or no action recorded on the item)
    # -- neutral, not a conflict. Only an actual detected disagreement
    # ("conflict") earns 0.0; see compatibility.py's action_signal states.
    action_score = 1.0 if compat.action_match else (0.0 if compat.action_signal == "conflict" else 0.5)
    unit_score = 1.0 if compat.unit_compatible is True else (0.5 if compat.unit_compatible is None else 0.0)
    category_score = 1.0 if compat.category_hinted else 0.0
    verified_score = 1.0 if verified_evidence else 0.0

    weights = config.weights
    weighted = (
        weights.get("description_similarity", 0.30) * description_score
        + weights.get("category_compatibility", 0.20) * category_score
        + weights.get("component_similarity", 0.15) * component_score
        + weights.get("material_match", 0.10) * material_score
        + weights.get("action_match", 0.10) * action_score
        + weights.get("attribute_match", 0.05) * attribute_score
        + weights.get("verified_rule_evidence", 0.05) * verified_score
        + weights.get("unit_compatibility", 0.05) * unit_score
    )

    match_reasons: list[str] = []
    penalties: list[str] = []

    for reason in (
        explanations.describe_description_similarity(description_score),
        explanations.describe_category(compat.category_hinted, hinted_categories, record.category),
        explanations.describe_component(component_score),
        explanations.describe_material(material_score),
        explanations.describe_action(compat),
        explanations.describe_attribute(attribute_score),
        explanations.describe_unit(compat),
        explanations.verified_evidence_reason(verified_evidence),
    ):
        if reason:
            match_reasons.append(reason)

    caps = config.conflict_caps
    if hinted_categories and not compat.category_hinted:
        weighted = min(weighted, caps.get("category_incompatible", 0.50))
        penalty = explanations.category_penalty(compat.category_hinted, hinted_categories, record.category)
        if penalty:
            penalties.append(penalty)
    if compat.action_conflict:
        weighted = min(weighted, caps.get("action_conflict", 0.45))
        penalty = explanations.action_penalty(compat)
        if penalty:
            penalties.append(penalty)
    if compat.unit_compatible is False:
        weighted = min(weighted, caps.get("unit_incompatible", 0.55))
        penalty = explanations.unit_penalty(compat)
        if penalty:
            penalties.append(penalty)
    if compat.grade_conflict:
        weighted = min(weighted, caps.get("distinction_conflict", 0.45))
        penalty = explanations.grade_penalty(compat)
        if penalty:
            penalties.append(penalty)
    if compat.distinction_conflicts:
        weighted = min(weighted, caps.get("distinction_conflict", 0.45))
        penalties.extend(explanations.distinction_penalties(compat))

    if record.ocr_confidence is not None and record.ocr_confidence < config.ocr_confidence_threshold:
        deficit = config.ocr_confidence_threshold - record.ocr_confidence
        penalty_amount = min(config.ocr_confidence_max_penalty, deficit * config.ocr_confidence_max_penalty)
        weighted = max(0.0, weighted - penalty_amount)
        penalties.append(explanations.ocr_confidence_penalty_reason(record.ocr_confidence, config.ocr_confidence_threshold))

    if include_uncertain and record.needs_review:
        weighted = max(0.0, weighted - config.uncertain_source_penalty)
        penalties.append(explanations.uncertain_source_penalty_reason())

    return ScoredCandidate(
        record=record,
        score=round(min(1.0, max(0.0, weighted)), 4),
        match_reasons=match_reasons,
        penalties=penalties,
        verified_evidence=verified_evidence,
    )
