"""Mapping-level validation: turns the per-item review reasons already
produced by normalizer.py/matcher.py, plus catalog-wide checks and
upstream-extraction awareness, into a flat, typed issue list for
mapping_report.json.
"""

from __future__ import annotations

from itertools import count

from estimate_extractor.mapping.catalog import CatalogEntry
from estimate_extractor.mapping.models import (
    MappedLineItem,
    MappingIssue,
    MappingSeverity,
    MappingStatus,
    NormalizedLineItem,
)

# review_reason (as set by normalizer.py / matcher.py) -> (issue code, severity)
_REASON_TO_ISSUE: dict[str, tuple[str, MappingSeverity]] = {
    "action_not_recognized": ("UNSUPPORTED_ACTION", MappingSeverity.WARNING),
    "action_incompatible": ("UNSUPPORTED_ACTION", MappingSeverity.WARNING),
    "trade_not_recognized": ("TRADE_MISMATCH", MappingSeverity.WARNING),
    "trade_mismatch": ("TRADE_MISMATCH", MappingSeverity.WARNING),
    "component_not_recognized": ("UNKNOWN_COMPONENT", MappingSeverity.WARNING),
    "component_unknown": ("UNKNOWN_COMPONENT", MappingSeverity.WARNING),
    "component_mismatch": ("COMPONENT_MISMATCH", MappingSeverity.INFO),
    "unit_incompatible": ("INCOMPATIBLE_QUANTITY_UNIT", MappingSeverity.WARNING),
    "attribute_conflict": ("ATTRIBUTE_CONFLICT", MappingSeverity.WARNING),
    "tied_candidates": ("MULTIPLE_CLOSE_CANDIDATES", MappingSeverity.WARNING),
    "missing_selector": ("MISSING_SELECTOR", MappingSeverity.WARNING),
    "catalog_entry_requires_review": ("CATALOG_ENTRY_REQUIRES_REVIEW", MappingSeverity.INFO),
}

_SUGGESTED_ACTIONS: dict[str, str] = {
    "MISSING_SELECTOR": "Select and verify the Xactimate selector manually.",
    "MISSING_CATEGORY": "Determine and verify the Xactimate category manually.",
    "UNSUPPORTED_ACTION": "Confirm the detected action against the catalog entry's allowed actions.",
    "TRADE_MISMATCH": "Confirm the item's trade; the best-scoring catalog entry is for a different trade.",
    "UNKNOWN_COMPONENT": "The normalizer could not identify a component; add or extend a normalization_rules.yaml rule.",
    "MULTIPLE_CLOSE_CANDIDATES": "Two or more catalog entries scored nearly identically; pick manually.",
    "INCOMPATIBLE_QUANTITY_UNIT": "The extracted unit does not match any allowed unit for the top candidate.",
    "NO_CATALOG_MATCH": "No catalog entry scored above the review threshold; consider adding one.",
    "LOW_CONFIDENCE": "Overall mapping confidence is low; manual review recommended.",
    "UNRESOLVED_COVERAGE": "Coverage could not be attributed during extraction; mapping proceeded without it.",
    "POSSIBLE_UPSTREAM_EXTRACTION_ISSUE": "The extraction stage flagged this line item for review before mapping began.",
    "AMBIGUOUS_NORMALIZATION": "The normalizer flagged this description as ambiguous.",
}


def _issue(
    issue_ids,
    line_item_id: str | None,
    code: str,
    severity: MappingSeverity,
    message: str,
) -> MappingIssue:
    return MappingIssue(
        issue_id=next(issue_ids),
        line_item_id=line_item_id,
        severity=severity,
        code=code,
        message=message,
        suggested_action=_SUGGESTED_ACTIONS.get(code),
    )


def validate_catalog(catalog: list[CatalogEntry]) -> list[MappingIssue]:
    """Catalog-wide checks, independent of any specific document."""
    issue_ids = (f"mapping_issue_catalog_{i:03d}" for i in count(1))
    issues: list[MappingIssue] = []

    seen_ids: dict[str, int] = {}
    for entry in catalog:
        seen_ids[entry.mapping_id] = seen_ids.get(entry.mapping_id, 0) + 1
    for mapping_id, n in seen_ids.items():
        if n > 1:
            issues.append(
                _issue(issue_ids, None, "DUPLICATE_MAPPING_RULE", MappingSeverity.ERROR, f"mapping_id '{mapping_id}' appears {n} times in the catalog.")
            )

    # Same (trade, component, allowed_actions) covered by more than one
    # mapping_id is a softer ambiguity signal -- flag, don't error.
    seen_shapes: dict[tuple, list[str]] = {}
    for entry in catalog:
        shape = (entry.trade, entry.component, entry.allowed_actions)
        seen_shapes.setdefault(shape, []).append(entry.mapping_id)
    for shape, ids in seen_shapes.items():
        if len(ids) > 1:
            issues.append(
                _issue(
                    issue_ids,
                    None,
                    "DUPLICATE_MAPPING_RULE",
                    MappingSeverity.INFO,
                    f"Catalog entries {ids} cover the same trade/component/allowed_actions combination "
                    f"({shape[0]}/{shape[1]}); matching between them will be decided by score alone.",
                )
            )
    return issues


def validate_line_item(
    issue_ids,
    normalized: NormalizedLineItem,
    mapped: MappedLineItem,
) -> list[MappingIssue]:
    issues: list[MappingIssue] = []
    lid = normalized.line_item_id
    outcome = mapped.mapping

    for reason in outcome.review_reasons:
        if reason in _REASON_TO_ISSUE:
            code, severity = _REASON_TO_ISSUE[reason]
            issues.append(_issue(issue_ids, lid, code, severity, f"{code.replace('_', ' ').title()} for {lid}."))
        elif reason.startswith("no_catalog_match"):
            issues.append(_issue(issue_ids, lid, "NO_CATALOG_MATCH", MappingSeverity.WARNING, reason))

    if outcome.best_match is not None:
        if outcome.best_match.category is None:
            issues.append(_issue(issue_ids, lid, "MISSING_CATEGORY", MappingSeverity.WARNING, f"No verified Xactimate category for {lid}."))
        if outcome.best_match.confidence < 0.6:
            issues.append(
                _issue(issue_ids, lid, "LOW_CONFIDENCE", MappingSeverity.WARNING, f"Mapping confidence {outcome.best_match.confidence:.2f} for {lid} is low.")
            )

    for reason in normalized.review_reasons:
        if reason not in _REASON_TO_ISSUE:
            issues.append(_issue(issue_ids, lid, "AMBIGUOUS_NORMALIZATION", MappingSeverity.INFO, f"{reason} for {lid}."))

    if normalized.original.coverage_id is None:
        issues.append(
            _issue(issue_ids, lid, "UNRESOLVED_COVERAGE", MappingSeverity.INFO, f"coverage_id is null for {lid} (preserved as-is; see extraction output).")
        )

    if normalized.original.extraction_needs_review:
        issues.append(
            _issue(
                issue_ids,
                lid,
                "POSSIBLE_UPSTREAM_EXTRACTION_ISSUE",
                MappingSeverity.INFO,
                f"{lid} was flagged needs_review during extraction: {normalized.original.extraction_warnings}",
            )
        )

    return issues


def validate_mapping(
    catalog: list[CatalogEntry],
    normalized_items: list[NormalizedLineItem],
    mapped_items: list[MappedLineItem],
) -> list[MappingIssue]:
    issues = list(validate_catalog(catalog))
    issue_ids = (f"mapping_issue_{i:03d}" for i in count(1))
    mapped_by_id = {m.line_item_id: m for m in mapped_items}
    for normalized in normalized_items:
        mapped = mapped_by_id.get(normalized.line_item_id)
        if mapped is None:
            continue
        issues.extend(validate_line_item(issue_ids, normalized, mapped))
    return issues
