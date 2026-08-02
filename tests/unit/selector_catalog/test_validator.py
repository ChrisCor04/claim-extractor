from __future__ import annotations

from estimate_extractor.selector_catalog.models import ScreenshotManifestEntry, SelectorRecord, SourceReference
from estimate_extractor.selector_catalog.validator import validate_catalog, validate_manifest_coverage, validate_records


def _valid_record(category="RFG", selector="FLPIPE", description="Flashing - pipe jack"):
    return SelectorRecord(
        category=category,
        selector=selector,
        description_original=description,
        description_normalized=description.lower(),
        source_images=[SourceReference(source_image="a.png", source_folder=category, source_sequence=1, ocr_confidence=0.9, row_index=0)],
    )


def test_valid_records_produce_no_errors():
    issues = validate_records([_valid_record()])
    assert issues == []


def test_empty_category_rejected():
    record = _valid_record(category="")
    issues = validate_records([record])
    assert any(i.code == "EMPTY_CATEGORY" for i in issues)


def test_empty_selector_rejected():
    record = _valid_record(selector="")
    issues = validate_records([record])
    assert any(i.code == "EMPTY_SELECTOR" for i in issues)


def test_empty_description_rejected():
    record = _valid_record(description="")
    record.description_normalized = ""
    issues = validate_records([record])
    assert any(i.code == "EMPTY_DESCRIPTION" for i in issues)


def test_missing_provenance_rejected():
    record = _valid_record()
    record.source_images = []
    issues = validate_records([record])
    assert any(i.code == "MISSING_PROVENANCE" for i in issues)


def test_duplicate_key_rejected():
    records = [_valid_record(), _valid_record()]
    issues = validate_records(records)
    assert any(i.code == "DUPLICATE_KEY" for i in issues)


def test_manifest_coverage_flags_missing_screenshot():
    entries = [ScreenshotManifestEntry(relative_path="a.png", folder_category="RFG", sequence=1, status="processed")]
    all_paths = {"a.png", "b.png"}
    issues = validate_manifest_coverage(entries, all_paths)
    assert any(i.code == "SCREENSHOT_NOT_IN_MANIFEST" and "b.png" in i.message for i in issues)


def test_manifest_coverage_passes_when_complete():
    entries = [ScreenshotManifestEntry(relative_path="a.png", folder_category="RFG", sequence=1, status="processed")]
    issues = validate_manifest_coverage(entries, {"a.png"})
    assert issues == []


def test_failed_screenshot_without_error_message_warns():
    entries = [ScreenshotManifestEntry(relative_path="a.png", folder_category="RFG", sequence=1, status="failed", error=None)]
    issues = validate_manifest_coverage(entries, {"a.png"})
    assert any(i.code == "FAILED_WITHOUT_ERROR" for i in issues)


def test_validate_catalog_passes_for_a_clean_catalog():
    report = validate_catalog(
        [_valid_record()],
        [ScreenshotManifestEntry(relative_path="a.png", folder_category="RFG", sequence=1, status="processed")],
        {"a.png"},
    )
    assert report.passed is True
    assert report.errors == []


def test_validate_catalog_fails_when_records_invalid():
    bad_record = _valid_record(selector="")
    report = validate_catalog([bad_record], [], set())
    assert report.passed is False
    assert any(i.code == "EMPTY_SELECTOR" for i in report.errors)
