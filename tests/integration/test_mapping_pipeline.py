"""Integration tests: run the full extract -> map pipeline against all six
real fixture PDFs and verify the mapping stage's non-negotiable contracts:
no line item disappears, no extracted fact is altered, unresolved items
never crash the pipeline, and every output file is produced.

Skips gracefully (not fail) when the gitignored, PII-containing fixture
PDFs aren't present locally -- same convention as
tests/integration/test_fixtures.py.
"""

from __future__ import annotations

import pytest

from estimate_extractor.config import Config
from estimate_extractor.mapping.outputs import write_all_mapping_outputs
from estimate_extractor.mapping.pipeline import load_mapping_engine_config, run_mapping
from estimate_extractor.pipeline import run_extraction

FIXTURE_NAMES = [
    "Aranda Insurance.pdf",
    "Bagi Insurance Estimate.pdf",
    "Garcia Insurance estimate.pdf",
    "Garrety Insurance Estimate.pdf",
    "Odom Insurance.pdf",
    "Wei Tang.pdf",
]


@pytest.fixture(scope="module")
def engine_config():
    return load_mapping_engine_config()


@pytest.mark.parametrize("pdf_name", FIXTURE_NAMES)
def test_mapping_pipeline_against_real_fixture(fixtures_dir, engine_config, pdf_name, tmp_path):
    pdf_path = fixtures_dir / pdf_name
    if not pdf_path.exists():
        pytest.skip(f"fixture '{pdf_name}' not present locally (PII files are gitignored)")

    config = Config.default()
    extraction_result = run_extraction(pdf_path, config)
    canonical = extraction_result.canonical
    canonical_dict = canonical.model_dump(mode="json")

    mapping_result = run_mapping(canonical_dict, engine_config)

    extracted_by_id = {li.line_item_id: li for li in canonical.line_items}
    mapped_by_id = {m.line_item_id: m for m in mapping_result.mapped_items}
    normalized_by_id = {n.line_item_id: n for n in mapping_result.normalized_items}

    # Every extracted line item receives exactly one mapping result; none
    # disappear.
    assert set(extracted_by_id.keys()) == set(mapped_by_id.keys())
    assert set(extracted_by_id.keys()) == set(normalized_by_id.keys())

    for line_item_id, original_li in extracted_by_id.items():
        normalized = normalized_by_id[line_item_id]
        mapped = mapped_by_id[line_item_id]

        # IDs remain unchanged.
        assert normalized.line_item_id == line_item_id
        assert mapped.line_item_id == line_item_id

        # Descriptions, quantity, and unit are preserved exactly.
        assert normalized.original.description == original_li.description
        assert normalized.original.quantity == original_li.quantity
        assert normalized.original.unit_of_measure == original_li.unit_of_measure

        # coverage_id is preserved exactly, including when null.
        assert normalized.original.coverage_id == original_li.coverage_id
        assert mapped.coverage_id == original_li.coverage_id

        # Every mapped item has exactly one status; never None/missing.
        assert mapped.mapping.status is not None

    # All mapping-status counts equal the total (no item double-counted or
    # dropped from the summary).
    s = mapping_result.report.summary
    assert s.mapped + s.partially_mapped + s.needs_review + s.unmapped == s.total_items
    assert s.total_items == len(canonical.line_items)

    # Unresolved/low-confidence items never crash the pipeline -- reaching
    # this point at all proves it; explicitly assert status is a known one.
    assert mapping_result.report.status in ("mapped", "needs_review")

    # All four output files are created.
    write_all_mapping_outputs(mapping_result, tmp_path)
    for filename in ("normalized_estimate.json", "mapped_estimate.json", "mapping_report.json", "mapping_review.csv"):
        assert (tmp_path / filename).exists()
        assert (tmp_path / filename).stat().st_size > 0
