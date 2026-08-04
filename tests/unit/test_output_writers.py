"""Regression tests for estimate_extractor.output (Phase 5.0 Priority 1).

cli.py and ui/pipeline_service.py have imported from
estimate_extractor.output.{csv_writer,json_writer,report_writer} since
before this repository's earliest commit, but the package itself was never
created -- both entry points were unable to import at all. These tests
pin down both the import fix and each writer's actual behavior so this
cannot silently regress again.
"""

from __future__ import annotations

from pathlib import Path

from estimate_extractor.models.canonical import (
    Area,
    CanonicalEstimate,
    DocumentMeta,
    ExtractionStatus,
    LineItem,
    LineItemConfidence,
    LineItemSource,
    Section,
    ValidationState,
)
from estimate_extractor.models.page import Line, PageClassification, PageRecord, ParsedDocument, ParsedPage
from estimate_extractor.models.validation import ExtractionReport, Reconciliation, ReportSummary
from estimate_extractor.output.csv_writer import write_line_items_csv
from estimate_extractor.output.json_writer import (
    write_canonical_estimate,
    write_debug_pages,
    write_document_pages,
    write_raw_text_pages,
)
from estimate_extractor.output.report_writer import write_extraction_report


def _confidence() -> LineItemConfidence:
    return LineItemConfidence(
        overall=0.95, description=0.95, quantity=0.95, unit_of_measure=0.95,
        unit_price=0.95, tax=0.95, replacement_cost_value=0.95, depreciation=0.95,
        actual_cash_value=0.95,
    )


def _canonical_with_area_and_section() -> CanonicalEstimate:
    area = Area(area_id="area_001", name="Dwelling", confidence=0.9)
    section = Section(section_id="section_001", area_id="area_001", name="Dwelling Roof", confidence=0.9)
    line_item = LineItem(
        line_item_id="line_0001",
        area_id="area_001",
        section_id="section_001",
        description='Tear off comp shingles',
        description_normalized_whitespace='Tear off comp shingles',
        quantity=20.5,
        unit_of_measure="SQ",
        source=LineItemSource(page_start=2, page_end=2, raw_text="raw text"),
        confidence=_confidence(),
        needs_review=True,
        review_reasons=["low confidence"],
    )
    return CanonicalEstimate(
        document=DocumentMeta(
            source_filename="test.pdf", source_sha256="deadbeef", carrier_detected="State Farm",
            carrier_confidence=0.99, page_count=1, extraction_status=ExtractionStatus.SUCCESS,
            extractor_version="0.1.0",
        ),
        areas=[area],
        sections=[section],
        line_items=[line_item],
        validation_state=ValidationState(status=ExtractionStatus.SUCCESS),
    )


def test_cli_module_imports_without_error():
    import estimate_extractor.cli  # noqa: F401


def test_ui_pipeline_service_module_imports_without_error():
    import estimate_extractor.ui.pipeline_service  # noqa: F401


def test_write_canonical_estimate(tmp_path: Path):
    canonical = _canonical_with_area_and_section()
    out = tmp_path / "canonical_estimate.json"
    write_canonical_estimate(canonical, out, pretty=True)
    assert out.exists()
    assert '"area_id": "area_001"' in out.read_text(encoding="utf-8")


def test_write_line_items_csv_resolves_area_and_section_names(tmp_path: Path):
    canonical = _canonical_with_area_and_section()
    out = tmp_path / "line_items.csv"
    write_line_items_csv(canonical, out)
    text = out.read_text(encoding="utf-8")
    assert "area_name" in text.splitlines()[0]
    assert "Dwelling Roof" in text
    assert "Dwelling" in text
    assert "SQ" in text
    assert "low confidence" in text


def test_write_extraction_report(tmp_path: Path):
    report = ExtractionReport(
        status=ExtractionStatus.SUCCESS,
        summary=ReportSummary(
            pages_total=1, pages_classified=1, pages_excluded=0, line_items_extracted=1,
            high_confidence_items=1, review_items=0, warnings=0, fatal_errors=0,
        ),
        reconciliation=Reconciliation(),
    )
    out = tmp_path / "extraction_report.json"
    write_extraction_report(report, out, pretty=True)
    assert out.exists()
    assert '"status": "success"' in out.read_text(encoding="utf-8")


def test_write_document_pages(tmp_path: Path):
    records = [PageRecord(page=1, classification=PageClassification.ESTIMATE_DETAIL, include_in_estimate=True, confidence=0.9)]
    out = tmp_path / "document_pages.json"
    write_document_pages(records, out, pretty=True)
    assert out.exists()
    assert '"estimate_detail"' in out.read_text(encoding="utf-8")


def _parsed_document() -> ParsedDocument:
    page = ParsedPage(
        page_number=1, width=612.0, height=792.0, raw_text="Contact: test@example.com 555-123-4567",
        lines=[Line(text="Contact: test@example.com 555-123-4567", y0=0, y1=10, x0=0, x1=100)],
    )
    return ParsedDocument(source_path=Path("test.pdf"), sha256="deadbeef", page_count=1, pages=[page])


def test_write_raw_text_pages_one_file_per_page(tmp_path: Path):
    write_raw_text_pages(_parsed_document(), tmp_path / "raw_text")
    out_file = tmp_path / "raw_text" / "page_0001.txt"
    assert out_file.exists()
    assert "test@example.com" in out_file.read_text(encoding="utf-8")


def test_write_debug_pages_redacts_when_requested(tmp_path: Path):
    document = _parsed_document()
    records = [PageRecord(page=1, classification=PageClassification.ESTIMATE_DETAIL, include_in_estimate=True, confidence=0.9)]

    unredacted_dir = tmp_path / "debug_plain"
    write_debug_pages(document, records, unredacted_dir, pretty=True, redact=False)
    assert "test@example.com" in (unredacted_dir / "page_0001.json").read_text(encoding="utf-8")

    redacted_dir = tmp_path / "debug_redacted"
    write_debug_pages(document, records, redacted_dir, pretty=True, redact=True)
    redacted_text = (redacted_dir / "page_0001.json").read_text(encoding="utf-8")
    assert "test@example.com" not in redacted_text
    assert "REDACTED_EMAIL" in redacted_text
