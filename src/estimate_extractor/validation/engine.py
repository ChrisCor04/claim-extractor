"""Validation orchestration: runs all rule/arithmetic checks, assembles the
ExtractionReport, determines overall extraction status, and propagates
issues back onto the line items they concern (needs_review/review_reasons)
so a reader of canonical_estimate.json alone still sees the uncertainty.
"""

from __future__ import annotations

from itertools import count

from estimate_extractor.config import ValidationConfig
from estimate_extractor.models.canonical import CanonicalEstimate, ExtractionStatus
from estimate_extractor.models.page import PageRecord
from estimate_extractor.models.validation import (
    ExtractionReport,
    Issue,
    IssueCode,
    Reconciliation,
    ReportSummary,
    Severity,
)
from estimate_extractor.validation.arithmetic import (
    check_line_item_arithmetic,
    check_section_totals,
    compute_reconciliation,
)
from estimate_extractor.validation.rules import check_claim, check_document, check_line_items


def run_validation(
    canonical: CanonicalEstimate,
    page_records: list[PageRecord],
    config: ValidationConfig,
    carrier_threshold: float,
    coverage_attribution_notes: list[str] | None = None,
) -> ExtractionReport:
    issue_ids = (f"issue_{i:03d}" for i in count(1))

    claim_has_any_metadata = bool(
        (canonical.claim.claim_number and canonical.claim.claim_number.value)
        or (canonical.claim.insured_name and canonical.claim.insured_name.value)
        or (canonical.claim.policy_number and canonical.claim.policy_number.value)
    )

    issues: list[Issue] = []
    for note in coverage_attribution_notes or []:
        issues.append(
            Issue(
                issue_id=next(issue_ids),
                severity=Severity.INFO,
                code=IssueCode.UNRESOLVED_COVERAGE_ATTRIBUTION,
                field="coverage_id",
                message=note,
            )
        )
    issues += check_document(
        canonical.document.page_count,
        page_records,
        canonical.document.carrier_confidence,
        carrier_threshold,
        claim_has_any_metadata,
        issue_ids,
    )
    issues += check_claim(canonical, issue_ids)
    issues += check_line_items(canonical, issue_ids)
    issues += check_line_item_arithmetic(canonical.line_items, config, issue_ids)
    issues += check_section_totals(canonical, config, issue_ids)

    # Propagate line-item-scoped issues back onto the line items themselves.
    issues_by_line_item: dict[str, list[Issue]] = {}
    for issue in issues:
        if issue.line_item_id:
            issues_by_line_item.setdefault(issue.line_item_id, []).append(issue)
    for li in canonical.line_items:
        related = issues_by_line_item.get(li.line_item_id, [])
        if not related:
            continue
        li.needs_review = True
        existing = set(li.review_reasons)
        new_reasons = [i.code.value.lower() for i in related if i.code.value.lower() not in existing]
        if new_reasons:
            li.review_reasons = li.review_reasons + new_reasons

    reported, calculated, paid_when_incurred_total, difference, ok, note = compute_reconciliation(canonical, config)
    reconciliation = Reconciliation(
        reported_line_item_total=reported,
        calculated_line_item_total=calculated,
        paid_when_incurred_total=paid_when_incurred_total,
        difference=difference,
        within_tolerance=ok,
        note=note,
    )

    fatal_count = sum(1 for i in issues if i.severity == Severity.FATAL)
    error_count = sum(1 for i in issues if i.severity == Severity.ERROR)
    warning_count = sum(1 for i in issues if i.severity == Severity.WARNING)

    high_confidence_items = sum(
        1 for li in canonical.line_items if li.confidence.overall >= config.low_confidence_threshold
    )
    review_items = sum(1 for li in canonical.line_items if li.needs_review)

    if fatal_count > 0 or not canonical.line_items:
        status = ExtractionStatus.FAILED
    elif error_count > 0 or review_items > 0 or (ok is False) or not claim_has_any_metadata:
        status = ExtractionStatus.NEEDS_REVIEW
    else:
        status = ExtractionStatus.SUCCESS

    summary = ReportSummary(
        pages_total=canonical.document.page_count,
        pages_classified=len(page_records),
        pages_excluded=len(canonical.document.excluded_page_numbers),
        line_items_extracted=len(canonical.line_items),
        high_confidence_items=high_confidence_items,
        review_items=review_items,
        warnings=warning_count,
        fatal_errors=fatal_count,
    )

    return ExtractionReport(status=status, summary=summary, reconciliation=reconciliation, issues=issues)
