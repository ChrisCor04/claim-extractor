from __future__ import annotations

import csv
import json

from estimate_extractor.selector_catalog import exporter
from estimate_extractor.selector_catalog.models import ScreenshotManifestEntry, SelectorRecord, SourceReference


def _record():
    return SelectorRecord(
        category="RFG",
        selector="FLPIPE",
        description_original="Flashing - pipe jack",
        description_normalized="flashing - pipe jack",
        needs_review=True,
        review_reasons=["low_ocr_confidence"],
        ocr_confidence=0.82,
        source_images=[SourceReference(source_image="a.png", source_folder="RFG", source_sequence=1, ocr_confidence=0.82, row_index=0)],
    )


def test_write_master_selectors_csv(tmp_path):
    path = tmp_path / "master_selectors.csv"
    exporter.write_master_selectors_csv([_record()], path)
    with path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1
    assert rows[0]["category"] == "RFG"
    assert rows[0]["selector"] == "FLPIPE"
    assert rows[0]["needs_review"] == "True"
    assert rows[0]["source_image_count"] == "1"


def test_write_master_selectors_json_round_trips(tmp_path):
    path = tmp_path / "master_selectors.json"
    exporter.write_master_selectors_json([_record()], path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert len(data) == 1
    assert data[0]["category"] == "RFG"
    assert data[0]["source_images"][0]["source_image"] == "a.png"


def test_write_and_load_manifest_round_trips(tmp_path):
    path = tmp_path / "manifest.json"
    entries = [
        ScreenshotManifestEntry(relative_path="a.png", folder_category="RFG", sequence=1, status="processed", rows_extracted=10),
        ScreenshotManifestEntry(relative_path="b.png", folder_category="RFG", sequence=2, status="failed", error="boom"),
    ]
    exporter.write_manifest(entries, path)
    loaded = exporter.load_manifest(path)
    assert len(loaded) == 2
    by_path = {e.relative_path: e for e in loaded}
    assert by_path["a.png"].rows_extracted == 10
    assert by_path["b.png"].error == "boom"


def test_load_manifest_missing_file_returns_empty_list(tmp_path):
    assert exporter.load_manifest(tmp_path / "does_not_exist.json") == []


def test_write_review_queue_csv(tmp_path):
    path = tmp_path / "review_queue.csv"
    rows = [
        exporter.ReviewQueueRow(category="RFG", selector="", description_original="Some text", reason="empty_selector", in_database=False, source_image="a.png", ocr_confidence=0.5),
        exporter.ReviewQueueRow(category="RFG", selector="FLPIPE", description_original="Flashing", reason="low_ocr_confidence", in_database=True, source_image="b.png", ocr_confidence=0.8),
    ]
    exporter.write_review_queue_csv(rows, path)
    with path.open(newline="", encoding="utf-8") as f:
        read_rows = list(csv.DictReader(f))
    assert len(read_rows) == 2
    assert read_rows[0]["reason"] == "empty_selector"
    assert read_rows[1]["in_database"] == "True"


def test_write_extraction_report_writes_markdown(tmp_path):
    path = tmp_path / "report.md"
    exporter.write_extraction_report("# Report\n\nHello", path)
    assert path.read_text(encoding="utf-8") == "# Report\n\nHello"
