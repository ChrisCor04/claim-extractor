"""Merges per-screenshot selector-row candidates that share the same
(category, selector) primary key -- screenshots overlap heavily (scroll
position, repeated navigation), so the same selector is commonly OCR'd
more than once. Retains every source image, prefers a complete
description over a UI-truncated one, and never silently discards a
genuine description conflict -- it is flagged for review instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from difflib import SequenceMatcher

from estimate_extractor.selector_catalog.models import SelectorRecord, is_truncated, normalize_description

# Two OCR reads of the same on-screen text are not always byte-identical
# (e.g. a degree mark "°" misread as an apostrophe on one screenshot and
# read correctly on another) -- this is OCR noise, not a genuine content
# conflict. Anything at or above this normalized-text similarity is
# treated as "the same row read twice", not flagged as conflicting.
NEAR_DUPLICATE_SIMILARITY = 0.90


def _strip_ellipsis(text: str) -> str:
    stripped = text.rstrip()
    for suffix in ("...", "..", "…"):
        if stripped.endswith(suffix):
            return stripped[: -len(suffix)].rstrip()
    return stripped


def _is_truncation_of(shorter_candidate: str, longer_candidate: str) -> bool:
    """True if `shorter_candidate` looks like a UI-truncated prefix of
    `longer_candidate` (after stripping a trailing ellipsis) -- i.e. the
    two aren't a genuine conflict, just two views of the same truncation."""
    a, b = _strip_ellipsis(shorter_candidate), _strip_ellipsis(longer_candidate)
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    if not shorter:
        return True
    return longer.startswith(shorter)


def _is_near_duplicate(a: str, b: str) -> bool:
    ratio = SequenceMatcher(None, normalize_description(a), normalize_description(b)).ratio()
    return ratio >= NEAR_DUPLICATE_SIMILARITY


@dataclass(slots=True)
class DeduplicationResult:
    records: list[SelectorRecord]
    # (category, selector, [all raw descriptions observed]) for genuinely
    # conflicting groups -- surfaced in the extraction report, never lost.
    conflicts: list[tuple[str, str, list[str]]] = field(default_factory=list)


def merge_records(candidates: list[SelectorRecord]) -> DeduplicationResult:
    """`candidates` are one-row-per-screenshot SelectorRecords (each with
    exactly one entry in source_images). Records with an empty category or
    selector must be filtered out by the caller before calling this --
    they are never merge keys (see build spec "no empty selectors")."""
    groups: dict[tuple[str, str], list[SelectorRecord]] = {}
    for c in candidates:
        if not c.category or not c.selector:
            continue
        groups.setdefault(c.key, []).append(c)

    merged: list[SelectorRecord] = []
    conflicts: list[tuple[str, str, list[str]]] = []

    for (category, selector), group in groups.items():
        if len(group) == 1:
            merged.append(group[0])
            continue

        # Prefer a non-truncated description; among equals, prefer higher
        # OCR confidence, then the longest text as a final tiebreaker.
        best = max(
            group,
            key=lambda c: (not is_truncated(c.description_original), c.ocr_confidence or 0.0, len(c.description_original)),
        )

        distinct_normalized = {normalize_description(c.description_original) for c in group}
        is_conflict = False
        if len(distinct_normalized) > 1:
            for c in group:
                if c is best or c.description_original == best.description_original:
                    continue
                if _is_truncation_of(c.description_original, best.description_original):
                    continue
                if _is_near_duplicate(c.description_original, best.description_original):
                    continue
                is_conflict = True
                break

        review_reasons = list(best.review_reasons)
        needs_review = best.needs_review
        category_mismatch = any(c.category_mismatch for c in group)
        title_bar_category = next((c.title_bar_category for c in group if c.title_bar_category), None)

        conflicting_descriptions: list[str] = []
        if is_conflict:
            needs_review = True
            if "conflicting_descriptions" not in review_reasons:
                review_reasons.append("conflicting_descriptions")
            conflicting_descriptions = sorted(
                {c.description_original for c in group if c.description_original != best.description_original}
            )
            conflicts.append((category, selector, [c.description_original for c in group]))
        elif is_truncated(best.description_original) and "description_truncated_in_screenshot" not in review_reasons:
            review_reasons.append("description_truncated_in_screenshot")
            needs_review = True

        if category_mismatch and "category_mismatch" not in review_reasons:
            review_reasons.append("category_mismatch")

        all_sources = [ref for c in group for ref in c.source_images]

        merged.append(
            SelectorRecord(
                category=category,
                selector=selector,
                description_original=best.description_original,
                description_normalized=normalize_description(best.description_original),
                needs_review=needs_review,
                review_reasons=review_reasons,
                ocr_confidence=best.ocr_confidence,
                title_bar_category=title_bar_category,
                category_mismatch=category_mismatch,
                conflicting_descriptions=conflicting_descriptions,
                source_images=all_sources,
            )
        )

    merged.sort(key=lambda r: (r.category, r.selector))
    return DeduplicationResult(records=merged, conflicts=conflicts)
