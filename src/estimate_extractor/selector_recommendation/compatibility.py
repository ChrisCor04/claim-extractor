"""Compatibility signals between a RecommendationInput and one candidate
SelectorRecord: category, action, unit, grade, and the "important
distinctions" the build spec requires never be collapsed (remove vs
replace vs detach-and-reset, labor vs material, minimum-charge vs
ordinary item, grade words, material-type words). Every signal here is a
soft, explainable input to scoring.py -- nothing in this module rejects a
candidate outright; scoring.py's conflict_caps decide how much a conflict
costs.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from estimate_extractor.selector_catalog.models import SelectorRecord
from estimate_extractor.selector_recommendation.candidate_generation import RecommendationRules
from estimate_extractor.selector_recommendation.models import RecommendationInput

# Real Unit-column abbreviations observed in the reference library (see
# selector_catalog.models.KNOWN_UNIT_TOKENS) -- reused here only as a
# SOFT textual signal against candidate descriptions, never to reject a
# candidate for a missing unit (the catalog does not store one; see build
# spec "Unit handling").
_UNIT_TEXT_HINTS = {
    "SF": ("per sf", " sf", " sq ft", "square foot"),
    "SQ": ("per sq", " sq)", " sq "),
    "LF": ("per lf", " lf", "linear foot"),
    "EA": ("per ea", " each"),
    "HR": ("per hour", " hr"),
    "CY": ("per cy", "cubic yard"),
    "SY": ("per sy", "square yard"),
    "GAL": ("per gal", "gallon"),
    "LB": ("per lb", "pound"),
    "DA": ("per day",),
    "MO": ("per month",),
}


@dataclass(frozen=True, slots=True)
class CompatibilityResult:
    category_hinted: bool
    action_signal: str  # "match" | "conflict" | "unlabeled" | "none"
    action_match: bool
    action_conflict: bool
    unit_compatible: bool | None  # None = no textual signal either way
    grade_conflict: bool
    distinction_conflicts: tuple[str, ...] = field(default_factory=tuple)


def _detect_action_hits(text: str, action_vocabulary: dict[str, tuple[str, ...]]) -> set[str]:
    lower = (text or "").lower()
    hits: set[str] = set()
    for action, phrases in action_vocabulary.items():
        for phrase in phrases:
            if phrase and phrase in lower:
                hits.add(action)
                break
    return hits


def _action_signal(item: RecommendationInput, record: SelectorRecord, rules: RecommendationRules) -> tuple[str, bool, bool]:
    hits = _detect_action_hits(record.description_normalized, rules.action_vocabulary)
    if not hits:
        return "none", False, False
    if item.action and item.action in hits:
        return "match", True, False
    if item.action:
        return "conflict", False, True
    return "unlabeled", False, False


def _unit_signal(item: RecommendationInput, record: SelectorRecord) -> bool | None:
    if not item.source_unit:
        return None
    unit = item.source_unit.strip().upper()
    hints = _UNIT_TEXT_HINTS.get(unit)
    if not hints:
        return None
    desc = f" {record.description_normalized} "
    if any(h in desc for h in hints):
        return True
    # A DIFFERENT known unit's text is present instead -- soft conflict.
    for other_unit, other_hints in _UNIT_TEXT_HINTS.items():
        if other_unit == unit:
            continue
        if any(h in desc for h in other_hints):
            return False
    return None


def _grade_conflict(item: RecommendationInput, record: SelectorRecord, rules: RecommendationRules) -> bool:
    item_text = " ".join(filter(None, [item.normalized_description, item.original_description, item.material])).lower()
    item_grades = {g for g in rules.grade_vocabulary if g in item_text}
    record_grades = {g for g in rules.grade_vocabulary if g in record.description_normalized}
    if not item_grades or not record_grades:
        return False
    return item_grades.isdisjoint(record_grades)


def _distinction_conflicts(item: RecommendationInput, record: SelectorRecord, rules: RecommendationRules) -> tuple[str, ...]:
    item_text = " ".join(
        filter(None, [item.normalized_description, item.original_description, item.action, item.material])
    ).lower()
    conflicts: list[str] = []
    for group in rules.distinction_keyword_groups:
        item_hits = {kw for kw in group if kw in item_text}
        record_hits = {kw for kw in group if kw in record.description_normalized}
        if item_hits and record_hits and item_hits.isdisjoint(record_hits):
            conflicts.append(f"distinction_conflict: item indicates {sorted(item_hits)} but candidate indicates {sorted(record_hits)}")
    return tuple(conflicts)


def compute_compatibility(
    item: RecommendationInput,
    record: SelectorRecord,
    hinted_categories: list[str],
    rules: RecommendationRules,
) -> CompatibilityResult:
    category_hinted = (not hinted_categories) or (record.category in hinted_categories)
    action_signal, action_match, action_conflict = _action_signal(item, record, rules)
    return CompatibilityResult(
        category_hinted=category_hinted,
        action_signal=action_signal,
        action_match=action_match,
        action_conflict=action_conflict,
        unit_compatible=_unit_signal(item, record),
        grade_conflict=_grade_conflict(item, record, rules),
        distinction_conflicts=_distinction_conflicts(item, record, rules),
    )
