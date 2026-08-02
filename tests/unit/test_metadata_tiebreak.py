"""Regression test: when two metadata values tie on frequency, the value
found on a more authoritative page (estimate-detail/claim-metadata) must
win over one found on a narrative carrier-letter page."""

from __future__ import annotations

from pathlib import Path

from estimate_extractor.classification.pages import classify_page
from estimate_extractor.models.page import ParsedDocument, ParsedPage
from estimate_extractor.parsing.metadata import extract_claim_metadata


def _page(number: int, text: str) -> ParsedPage:
    return ParsedPage(page_number=number, width=612, height=792, raw_text=text)


def test_tied_claim_number_prefers_claim_metadata_page_over_carrier_letter():
    # Page 1 reads like a narrative cover letter and states one claim number.
    page1 = _page(
        1,
        "Dear JOHN DOE:\nWe have prepared this estimate regarding your loss.\n"
        "Claim Number: 1234567-1-1\n"
        "Thank you for choosing us for your insurance needs.\n",
    )
    # Page 2 is a structured claim header with the same label, different value.
    page2 = _page(
        2,
        "Insured:\nJOHN DOE\nClaim Number:\n1234567-1\nPolicy Number:\nP-998877\n"
        "Type of Loss:\nHail\nPrice List:\nTXDF1_JAN26\n",
    )
    document = ParsedDocument(source_path=Path("test.pdf"), sha256="deadbeef", page_count=2, pages=[page1, page2])

    record1 = classify_page(page1)
    record2 = classify_page(page2)
    assert record1.classification.value != "estimate_detail"
    assert record2.classification.value == "claim_metadata"

    claim = extract_claim_metadata(document, [record1, record2])

    assert claim.claim_number is not None
    assert claim.claim_number.value == "1234567-1"
    assert set(claim.claim_number.raw_values) == {"1234567-1-1", "1234567-1"}
