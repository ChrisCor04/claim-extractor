"""Regression tests for validation/rules.py severity decisions."""

from __future__ import annotations

from itertools import count

from estimate_extractor.models.canonical import (
    CanonicalEstimate,
    ClaimData,
    DocumentMeta,
    ExtractionStatus,
    FieldValue,
    ValidationState,
)
from estimate_extractor.models.validation import IssueCode, Severity
from estimate_extractor.validation.rules import check_claim


def _canonical(carrier_detected: str, *, with_claim_number: bool) -> CanonicalEstimate:
    claim_kwargs = {}
    if with_claim_number:
        claim_kwargs["claim_number"] = FieldValue[str](
            value="ABC123", confidence=0.99, source_pages=[1], raw_values=["ABC123"]
        )
    document = DocumentMeta(
        source_filename="test.pdf",
        source_sha256="deadbeef",
        carrier_detected=carrier_detected,
        carrier_confidence=0.99,
        page_count=1,
        extraction_status=ExtractionStatus.SUCCESS,
        extractor_version="0.1.0",
    )
    return CanonicalEstimate(
        document=document,
        claim=ClaimData(**claim_kwargs),
        validation_state=ValidationState(status=ExtractionStatus.SUCCESS),
    )


def _missing_claim_number_issue(canonical):
    issue_ids = (f"issue_{i:03d}" for i in count(1))
    issues = check_claim(canonical, issue_ids)
    matches = [i for i in issues if i.code == IssueCode.MISSING_CLAIM_NUMBER]
    assert len(matches) == 1, "expected exactly one MISSING_CLAIM_NUMBER issue"
    return matches[0]


def test_usaa_missing_claim_number_is_info_not_warning():
    canonical = _canonical("USAA", with_claim_number=False)
    issue = _missing_claim_number_issue(canonical)
    assert issue.severity == Severity.INFO


def test_usaa_carrier_name_matching_is_case_insensitive():
    canonical = _canonical("usaa", with_claim_number=False)
    issue = _missing_claim_number_issue(canonical)
    assert issue.severity == Severity.INFO


def test_other_carrier_missing_claim_number_stays_warning():
    canonical = _canonical("State Farm", with_claim_number=False)
    issue = _missing_claim_number_issue(canonical)
    assert issue.severity == Severity.WARNING


def test_present_claim_number_raises_no_missing_issue():
    canonical = _canonical("USAA", with_claim_number=True)
    issue_ids = (f"issue_{i:03d}" for i in count(1))
    issues = check_claim(canonical, issue_ids)
    assert not [i for i in issues if i.code == IssueCode.MISSING_CLAIM_NUMBER]
