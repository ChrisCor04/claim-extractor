"""Non-arithmetic structural validation rules: document, claim, and
line-item level checks."""

from __future__ import annotations

from collections import Counter

from estimate_extractor.classification.pages import INSTRUCTIONAL_PLACEHOLDER_MARKERS
from estimate_extractor.models.canonical import CanonicalEstimate
from estimate_extractor.models.page import PageClassification, PageRecord
from estimate_extractor.models.validation import Issue, IssueCode, Severity

_REVIEW_REASON_TO_ISSUE: dict[str, tuple[IssueCode, Severity]] = {
    "missing_quantity": (IssueCode.MISSING_QUANTITY, Severity.WARNING),
    "missing_unit_price": (IssueCode.MALFORMED_MONEY_FIELD, Severity.WARNING),
    "missing_tax": (IssueCode.MALFORMED_MONEY_FIELD, Severity.INFO),
    "missing_overhead_and_profit": (IssueCode.MALFORMED_MONEY_FIELD, Severity.INFO),
    "missing_replacement_cost_value": (IssueCode.MALFORMED_MONEY_FIELD, Severity.WARNING),
    "missing_actual_cash_value": (IssueCode.MALFORMED_MONEY_FIELD, Severity.WARNING),
    "possible_description_wrap": (IssueCode.POSSIBLE_DESCRIPTION_WRAP, Severity.INFO),
    "ambiguous_depreciation_notation": (IssueCode.AMBIGUOUS_LINE_CONTINUATION, Severity.INFO),
    "unrecognized_unit": (IssueCode.UNRECOGNIZED_UNIT, Severity.INFO),
}


def check_document(
    document_meta_page_count: int,
    page_records: list[PageRecord],
    carrier_confidence: float,
    carrier_threshold: float,
    claim_has_any_metadata: bool,
    issue_ids,
) -> list[Issue]:
    issues: list[Issue] = []

    covered_pages = {pr.page for pr in page_records}
    expected_pages = set(range(1, document_meta_page_count + 1))
    if covered_pages != expected_pages:
        missing = sorted(expected_pages - covered_pages)
        issues.append(
            Issue(
                issue_id=next(issue_ids),
                severity=Severity.ERROR,
                code=IssueCode.PAGE_NOT_CLASSIFIED,
                message=f"Pages not classified: {missing}",
            )
        )

    page_numbers = [pr.page for pr in page_records]
    dupes = [p for p, count in Counter(page_numbers).items() if count > 1]
    if dupes:
        issues.append(
            Issue(
                issue_id=next(issue_ids),
                severity=Severity.ERROR,
                code=IssueCode.DUPLICATE_PAGE_NUMBER,
                message=f"Duplicate page numbers in classification: {sorted(dupes)}",
            )
        )

    for pr in page_records:
        if pr.classification == PageClassification.UNKNOWN:
            issues.append(
                Issue(
                    issue_id=next(issue_ids),
                    severity=Severity.WARNING,
                    code=IssueCode.PAGE_UNKNOWN_CLASSIFICATION,
                    page=pr.page,
                    message=f"Page {pr.page} could not be classified with confidence.",
                    suggested_action=f"Review source page {pr.page}.",
                )
            )

    if not claim_has_any_metadata:
        issues.append(
            Issue(
                issue_id=next(issue_ids),
                severity=Severity.ERROR,
                code=IssueCode.NO_METADATA_SOURCE,
                message="No claim number, insured name, or policy number could be extracted from any page.",
            )
        )

    if carrier_confidence < carrier_threshold:
        issues.append(
            Issue(
                issue_id=next(issue_ids),
                severity=Severity.INFO,
                code=IssueCode.LOW_CARRIER_CONFIDENCE,
                message=(
                    f"Carrier detection confidence ({carrier_confidence:.2f}) is below the configured "
                    f"threshold ({carrier_threshold:.2f}); the generic adapter was used."
                ),
            )
        )

    return issues


def _distinct_normalized(raw_values: list[str]) -> list[str]:
    return sorted({v.strip().lower() for v in raw_values if v and v.strip()})


# Carriers whose own template has no "Claim Number" label at all (USAA
# identifies claims via Member Number + L/R Number instead -- see
# tests/expected/odom_insurance.json). For these, a missing claim number is
# the expected, normal state, not an anomaly, so it is reported at info
# severity rather than warning -- otherwise it fires on every single
# document from that carrier and trains reviewers to ignore it.
CARRIERS_WITHOUT_CLAIM_NUMBER = frozenset({"usaa"})


def check_claim(canonical: CanonicalEstimate, issue_ids) -> list[Issue]:
    issues: list[Issue] = []
    claim = canonical.claim
    carrier_key = (canonical.document.carrier_detected or "").strip().lower()

    if claim.claim_number and len(_distinct_normalized(claim.claim_number.raw_values)) > 1:
        issues.append(
            Issue(
                issue_id=next(issue_ids),
                severity=Severity.WARNING,
                code=IssueCode.CONFLICTING_CLAIM_NUMBER,
                field="claim.claim_number",
                message=f"Conflicting claim number values found: {claim.claim_number.raw_values}",
            )
        )
    if claim.insured_name and len(_distinct_normalized(claim.insured_name.raw_values)) > 1:
        issues.append(
            Issue(
                issue_id=next(issue_ids),
                severity=Severity.WARNING,
                code=IssueCode.CONFLICTING_INSURED_NAME,
                field="claim.insured_name",
                message=f"Conflicting insured name values found: {claim.insured_name.raw_values}",
            )
        )
    if claim.property_address and len(set(claim.property_address.raw_values)) > 1:
        issues.append(
            Issue(
                issue_id=next(issue_ids),
                severity=Severity.WARNING,
                code=IssueCode.CONFLICTING_PROPERTY_ADDRESS,
                field="claim.property_address",
                message=f"Conflicting property address values found: {claim.property_address.raw_values}",
            )
        )

    suspect_values = []
    if claim.claim_number and claim.claim_number.value:
        suspect_values.append(claim.claim_number.value.lower())
    if claim.insured_name and claim.insured_name.value:
        suspect_values.append(claim.insured_name.value.lower())
    for v in suspect_values:
        if v in INSTRUCTIONAL_PLACEHOLDER_MARKERS:
            issues.append(
                Issue(
                    issue_id=next(issue_ids),
                    severity=Severity.FATAL,
                    code=IssueCode.INSTRUCTIONAL_SAMPLE_VALUE_SUSPECTED,
                    field="claim",
                    message=f"Selected claim value '{v}' matches a known instructional-sample placeholder.",
                    suggested_action="This must not be used as real claim data; investigate page classification.",
                )
            )

    if not claim.claim_number or not claim.claim_number.value:
        expected_absent = carrier_key in CARRIERS_WITHOUT_CLAIM_NUMBER
        issues.append(
            Issue(
                issue_id=next(issue_ids),
                severity=Severity.INFO if expected_absent else Severity.WARNING,
                code=IssueCode.MISSING_CLAIM_NUMBER,
                field="claim.claim_number",
                message=(
                    "No claim number could be extracted (expected for this carrier -- "
                    "see claim.member_number / claim.lr_number instead)."
                    if expected_absent
                    else "No claim number could be extracted."
                ),
            )
        )
    if not claim.price_list or not claim.price_list.value:
        issues.append(
            Issue(
                issue_id=next(issue_ids),
                severity=Severity.INFO,
                code=IssueCode.MISSING_PRICE_LIST,
                field="claim.price_list",
                message="No price list could be extracted.",
            )
        )

    return issues


def check_line_items(canonical: CanonicalEstimate, issue_ids) -> list[Issue]:
    issues: list[Issue] = []

    ids_seen = Counter(li.line_item_id for li in canonical.line_items)
    for lid, count in ids_seen.items():
        if count > 1:
            issues.append(
                Issue(
                    issue_id=next(issue_ids),
                    severity=Severity.FATAL,
                    code=IssueCode.DUPLICATE_LINE_ITEM_ID,
                    line_item_id=lid,
                    message=f"Line item id '{lid}' was assigned more than once.",
                )
            )

    by_section: dict[str, list[int]] = {}
    for li in canonical.line_items:
        if li.section_id and li.source_line_number is not None:
            by_section.setdefault(li.section_id, []).append(li.source_line_number)
    for section_id, numbers in by_section.items():
        dupes = [n for n, c in Counter(numbers).items() if c > 1]
        if dupes:
            issues.append(
                Issue(
                    issue_id=next(issue_ids),
                    severity=Severity.WARNING,
                    code=IssueCode.DUPLICATE_SOURCE_LINE_NUMBER,
                    message=f"Section '{section_id}' has duplicate source line numbers: {sorted(dupes)}",
                )
            )

    all_numbers = sorted(
        {li.source_line_number for li in canonical.line_items if li.source_line_number is not None}
    )
    gaps = []
    for a, b in zip(all_numbers, all_numbers[1:]):
        if b - a > 1:
            gaps.append((a, b))
    if gaps:
        issues.append(
            Issue(
                issue_id=next(issue_ids),
                severity=Severity.INFO,
                code=IssueCode.LINE_NUMBER_SEQUENCE_GAP,
                message=f"Line item number sequence has {len(gaps)} gap(s), e.g. {gaps[:5]}.",
            )
        )

    for li in canonical.line_items:
        if not li.description or not li.description.strip():
            issues.append(
                Issue(
                    issue_id=next(issue_ids),
                    severity=Severity.ERROR,
                    code=IssueCode.MISSING_DESCRIPTION,
                    line_item_id=li.line_item_id,
                    page=li.source.page_start,
                    message="Line item has no description.",
                )
            )
        if li.quantity is None:
            issues.append(
                Issue(
                    issue_id=next(issue_ids),
                    severity=Severity.WARNING,
                    code=IssueCode.MISSING_QUANTITY,
                    line_item_id=li.line_item_id,
                    page=li.source.page_start,
                    message="Line item has no quantity.",
                )
            )
        if li.unit_of_measure is None:
            issues.append(
                Issue(
                    issue_id=next(issue_ids),
                    severity=Severity.WARNING,
                    code=IssueCode.MISSING_UNIT,
                    line_item_id=li.line_item_id,
                    page=li.source.page_start,
                    message="Line item has no unit of measure.",
                )
            )
        for reason in li.review_reasons:
            mapped = _REVIEW_REASON_TO_ISSUE.get(reason)
            if mapped:
                code, severity = mapped
                issues.append(
                    Issue(
                        issue_id=next(issue_ids),
                        severity=severity,
                        code=code,
                        line_item_id=li.line_item_id,
                        page=li.source.page_start,
                        field=None,
                        message=f"{reason.replace('_', ' ').capitalize()} for line item {li.line_item_id}.",
                        suggested_action=f"Review source page {li.source.page_start}.",
                    )
                )

    for section in canonical.sections:
        if section.name.startswith("Unlabeled ("):
            orphan_items = [li for li in canonical.line_items if li.section_id == section.section_id]
            if orphan_items:
                issues.append(
                    Issue(
                        issue_id=next(issue_ids),
                        severity=Severity.WARNING,
                        code=IssueCode.ORPHAN_CONTINUATION_ROW,
                        message=(
                            f"{len(orphan_items)} line item(s) in section '{section.section_id}' had no "
                            "recognizable section header on their page."
                        ),
                        suggested_action=f"Review source page(s) {section.source_pages}.",
                    )
                )

    return issues
