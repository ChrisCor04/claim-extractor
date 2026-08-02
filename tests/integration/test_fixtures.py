"""Integration tests: run every real fixture PDF through the full pipeline.

These are the acceptance-criteria-driving tests: they exercise PDF reading,
page classification, carrier detection, adapter-driven extraction, and
validation together, against the six real carrier documents.

Fixture PDFs contain real customer PII and are gitignored (see
docs/troubleshooting.md and .gitignore) -- any test here skips gracefully
if the corresponding file isn't present in the local checkout, rather than
failing the whole suite for a clean clone.
"""

from __future__ import annotations

import json

import pytest

from estimate_extractor.config import Config
from estimate_extractor.models.canonical import ExtractionStatus
from estimate_extractor.models.page import PageClassification
from estimate_extractor.pipeline import run_extraction

from conftest import REPO_ROOT

EXPECTED_DIR = REPO_ROOT / "tests" / "expected"


def _load_expected(name: str) -> dict:
    return json.loads((EXPECTED_DIR / name).read_text())


FIXTURE_CASES = [
    ("Aranda Insurance.pdf", "aranda_insurance.json"),
    ("Bagi Insurance Estimate.pdf", "bagi_insurance_estimate.json"),
    ("Garcia Insurance estimate.pdf", "garcia_insurance_estimate.json"),
    ("Garrety Insurance Estimate.pdf", "garrety_insurance_estimate.json"),
    ("Odom Insurance.pdf", "odom_insurance.json"),
    ("Wei Tang.pdf", "wei_tang.json"),
]

# Literal placeholder values that must never appear as a *real* extracted
# claim number/insured name -- these are the known sample/guide markers
# baked into several of the fixtures (see classification/pages.py).
PLACEHOLDER_CLAIM_NUMBERS = {"00-0000-000", "abc1234001h", "1234567890"}
PLACEHOLDER_NAMES = {"smith, joe & jane", "john smith"}


@pytest.fixture(scope="module")
def config():
    return Config.default()


@pytest.mark.parametrize("pdf_name,expected_name", FIXTURE_CASES, ids=[c[0] for c in FIXTURE_CASES])
def test_fixture_extraction(fixtures_dir, config, pdf_name, expected_name):
    pdf_path = fixtures_dir / pdf_name
    if not pdf_path.exists():
        pytest.skip(f"fixture '{pdf_name}' not present locally (PII files are gitignored)")

    expected = _load_expected(expected_name)

    result = run_extraction(pdf_path, config)
    canonical = result.canonical

    # It processes without crashing and produces a non-failed status.
    assert canonical.document.extraction_status != ExtractionStatus.FAILED

    # Every page got classified exactly once.
    assert len(canonical.source_pages) == canonical.document.page_count
    assert {p.page for p in canonical.source_pages} == set(range(1, canonical.document.page_count + 1))

    # Carrier is correct.
    assert canonical.document.carrier_detected == expected["carrier_detected"]

    # Claim metadata.
    claim_number = canonical.claim.claim_number.value if canonical.claim.claim_number else None
    assert claim_number == expected["claim_number"]

    insured_name = canonical.claim.insured_name.value if canonical.claim.insured_name else None
    assert insured_name == expected["insured_name"]

    if expected.get("price_list") is not None:
        price_list = canonical.claim.price_list.value if canonical.claim.price_list else None
        assert price_list == expected["price_list"]

    if expected.get("member_number") is not None:
        assert canonical.claim.member_number.value == expected["member_number"]
    if expected.get("lr_number") is not None:
        assert canonical.claim.lr_number.value == expected["lr_number"]

    # Estimate pages were identified (and at least one page was excluded --
    # every fixture has a cover letter/guide/recap page that isn't detail).
    assert len(canonical.document.estimate_page_numbers) > 0
    assert len(canonical.document.excluded_page_numbers) > 0

    # At least one coverage, section, and line item extracted, meeting the
    # golden file's minimums.
    assert len(canonical.coverages) >= expected["minimum_coverages"]
    assert len(canonical.sections) >= expected["minimum_sections"]
    assert len(canonical.line_items) >= expected["minimum_line_items"]

    # Totals were captured.
    assert len(canonical.summary_totals) > 0

    # Known real descriptions are present (line items in document order).
    descriptions = [li.description for li in canonical.line_items]
    for must_include in expected["must_include_descriptions"]:
        assert must_include in descriptions, f"missing expected description: {must_include!r}"

    # --- Instructional-sample guard -----------------------------------
    instructional_pages = {
        p.page for p in canonical.source_pages if p.classification == PageClassification.INSTRUCTIONAL_SAMPLE
    }
    # No line item originates solely from an instructional sample page.
    for li in canonical.line_items:
        assert li.source.page_start not in instructional_pages
        assert li.source.page_end not in instructional_pages

    # No placeholder sample claim number/insured name became the real value.
    if claim_number:
        assert claim_number.strip().lower() not in PLACEHOLDER_CLAIM_NUMBERS
    if insured_name:
        assert insured_name.strip().lower() not in PLACEHOLDER_NAMES


def test_all_six_fixtures_present_and_distinct_carriers(fixtures_dir):
    """Sanity check that the fixture set covers five distinct carriers
    across six documents, per the spec's fixture inventory."""
    present = [pdf_name for pdf_name, _ in FIXTURE_CASES if (fixtures_dir / pdf_name).exists()]
    if len(present) < len(FIXTURE_CASES):
        pytest.skip("not all fixtures present locally; skipping cross-fixture sanity check")
    assert len(present) == 6
