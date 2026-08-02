"""Pure string-building helpers that turn a scored candidate's raw
signals into the human-readable ``match_reasons`` / ``penalties`` lists
the build spec requires ("Every score must be explainable through:
match_reasons, penalties"). No scoring logic lives here -- scoring.py
decides what happened; this module only phrases it.
"""

from __future__ import annotations

from estimate_extractor.selector_recommendation.compatibility import CompatibilityResult


def describe_description_similarity(score: float) -> str | None:
    if score >= 0.85:
        return "description closely matches the candidate's catalog description"
    if score >= 0.55:
        return "description shares substantial wording with the candidate"
    return None


def describe_category(hinted: bool, categories: list[str], category: str) -> str | None:
    if not categories:
        return None
    if hinted:
        return f"category {category!r} is compatible with the item's trade/component hints"
    return None


def category_penalty(hinted: bool, categories: list[str], category: str) -> str | None:
    if categories and not hinted:
        return f"category {category!r} is outside the hinted categories {categories} for this trade/component"
    return None


def describe_component(score: float) -> str | None:
    if score >= 0.5:
        return "component wording overlaps with the candidate description"
    return None


def describe_material(score: float) -> str | None:
    if score >= 0.99:
        return "material text matches the candidate description"
    if score >= 0.55:
        return "material text is similar to the candidate description"
    return None


def describe_action(compat: CompatibilityResult) -> str | None:
    if compat.action_match:
        return "action wording in the candidate description matches the item's action"
    return None


def action_penalty(compat: CompatibilityResult) -> str | None:
    if compat.action_conflict:
        return "candidate description implies a different action than the item's normalized action"
    return None


def describe_attribute(score: float) -> str | None:
    if score >= 0.99:
        return "an extracted attribute value appears directly in the candidate description"
    return None


def describe_unit(compat: CompatibilityResult) -> str | None:
    if compat.unit_compatible is True:
        return "candidate description text is consistent with the item's source unit"
    return None


def unit_penalty(compat: CompatibilityResult) -> str | None:
    if compat.unit_compatible is False:
        return "candidate description text suggests a different unit than the item's source unit"
    return None


def grade_penalty(compat: CompatibilityResult) -> str | None:
    if compat.grade_conflict:
        return "candidate grade/quality wording conflicts with the item's grade wording"
    return None


def distinction_penalties(compat: CompatibilityResult) -> list[str]:
    return list(compat.distinction_conflicts)


def verified_evidence_reason(verified: bool) -> str | None:
    if verified:
        return "backed by a Phase 3.5 human-verified catalog rule for this category/selector"
    return None


def uncertain_source_penalty_reason() -> str:
    return "source selector-catalog record is flagged needs_review (shown only because uncertain references were explicitly included)"


def ocr_confidence_penalty_reason(ocr_confidence: float, threshold: float) -> str:
    return f"selector-catalog OCR confidence ({ocr_confidence:.2f}) is below the quality threshold ({threshold:.2f})"
