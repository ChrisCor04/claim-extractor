"""Deterministic weighted scoring config + material-similarity helper.

No LLM anywhere in this stage. Fuzzy text similarity is optional (RapidFuzz
if installed, else the stdlib difflib.SequenceMatcher as a zero-dependency
fallback) and is only ONE of six weighted components -- a high text
similarity alone can never dominate a component/trade/action/unit conflict,
per the build spec's "Matching logic" requirement.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True, slots=True)
class ScoringConfig:
    weights: dict[str, float]
    mapped_min: float
    partially_mapped_min: float
    needs_review_min: float
    missing_selector_forces_review: bool
    tie_margin: float
    fuzzy_enabled: bool
    fuzzy_min_score: float


def load_scoring_config(path: Path) -> ScoringConfig:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    weights = raw.get("weights", {})
    thresholds = raw.get("thresholds", {})
    fuzzy = raw.get("fuzzy_matching", {})
    return ScoringConfig(
        weights=weights,
        mapped_min=float(thresholds.get("mapped_min", 0.92)),
        partially_mapped_min=float(thresholds.get("partially_mapped_min", 0.80)),
        needs_review_min=float(thresholds.get("needs_review_min", 0.60)),
        missing_selector_forces_review=bool(raw.get("missing_selector_forces_review", True)),
        tie_margin=float(raw.get("tie_margin", 0.03)),
        fuzzy_enabled=bool(fuzzy.get("enabled", True)),
        fuzzy_min_score=float(fuzzy.get("min_score", 0.55)),
    )


def _fuzzy_ratio(a: str, b: str) -> float:
    try:
        from rapidfuzz import fuzz  # type: ignore[import-not-found]

        return fuzz.ratio(a, b) / 100.0
    except ImportError:
        return difflib.SequenceMatcher(None, a, b).ratio()


def material_similarity(material: str | None, canonical_terms: tuple[str, ...], config: ScoringConfig) -> float:
    if not material or not canonical_terms:
        return 0.3
    material_lower = material.lower()
    for term in canonical_terms:
        if term in material_lower or material_lower in term:
            return 1.0
    if not config.fuzzy_enabled:
        return 0.0
    best = max((_fuzzy_ratio(material_lower, term) for term in canonical_terms), default=0.0)
    return best if best >= config.fuzzy_min_score else 0.0
