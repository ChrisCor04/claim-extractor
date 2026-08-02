"""Tests for the Phase 3.6 QA cleanup pass (cleanup.py) using a fake
WordBoxOCREngine -- no real Tesseract dependency, fully deterministic.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from estimate_extractor.selector_catalog import database
from estimate_extractor.selector_catalog.cleanup import (
    CleanupReport,
    backup_database,
    run_full_cleanup,
    run_malformed_selector_cleanup,
    run_unit_contamination_cleanup,
)
from estimate_extractor.selector_catalog.models import SelectorRecord, SourceReference, normalize_description
from estimate_extractor.selector_catalog.ocr_engine import OCRWord


def _word(text, left, top, width=20, height=10, line=1, conf=0.9):
    return OCRWord(text=text, left=left, top=top, width=width, height=height, confidence=conf, block_num=1, par_num=1, line_num=line, word_num=0)


def _header_words():
    return [
        _word("Sel", 157, 78, width=14, line=3),
        _word("Description", 237, 77, width=58, line=3),
        _word("Unit", 591, 78, width=21, line=3),
    ]


class FakeEngine:
    def __init__(self, words_by_filename: dict):
        self.words_by_filename = words_by_filename
        self.call_count = 0

    def extract_words(self, image_path: Path) -> list[OCRWord]:
        self.call_count += 1
        return self.words_by_filename.get(image_path.name, [])

    def extract_title_bar_text(self, image_path: Path) -> str:
        return ""


def _record(category, selector, description, source_image="a.png", needs_review=False, sequence=1):
    return SelectorRecord(
        category=category,
        selector=selector,
        description_original=description,
        description_normalized=normalize_description(description),
        needs_review=needs_review,
        source_images=[SourceReference(source_image=source_image, source_folder=category, source_sequence=sequence, ocr_confidence=0.9, row_index=0)],
    )


def _make_extracted_root(tmp_path, filenames):
    root = tmp_path / "Lib" / "Screenshots_By_CAT" / "ACC"
    root.mkdir(parents=True)
    for name in filenames:
        (root / name).write_bytes(b"fake")
    return tmp_path


# --- backup_database ---------------------------------------------------


def test_backup_database_creates_timestamped_copy(tmp_path):
    db_path = tmp_path / "master_selectors.db"
    conn = database.create_database(db_path)
    database.replace_all_records(conn, [_record("RFG", "A", "x")])
    conn.close()

    backups_dir = tmp_path / "backups"
    backup_path = backup_database(db_path, backups_dir)
    assert backup_path.exists()
    assert backup_path.parent == backups_dir
    assert backup_path.read_bytes() == db_path.read_bytes()


def test_backup_database_handles_missing_source_gracefully(tmp_path):
    backup_path = backup_database(tmp_path / "does_not_exist.db", tmp_path / "backups")
    assert not backup_path.exists()  # nothing to copy, but must not raise


# --- run_unit_contamination_cleanup -------------------------------------


def test_unit_contamination_corrected_via_coordinate_reparse(tmp_path):
    extracted_root = _make_extracted_root(tmp_path, ["a.png"])
    # Selector column ~[152,204), description column ~[204,587) given the
    # header words below (Sel@157, Description@237, Unit@591) -- "EA" at
    # left=600 is clearly beyond the description column's right edge.
    words = _header_words() + [
        _word("ANCR", 157, 114, width=38, line=5),
        _word("Anchor", 237, 114, width=38, line=5),
        _word("type", 300, 114, width=23, line=5),
        _word("EA", 600, 114, width=14, line=5),
    ]
    # Fake the region detection indirectly via a real header row + real
    # row_parser/table_region -- so the fake engine only needs to supply words.
    engine = FakeEngine(words_by_filename={"a.png": words})
    record = _record("ACC", "ANCR", "Anchor type EA", source_image="Screenshots_By_CAT/ACC/a.png")
    report = CleanupReport()

    run_unit_contamination_cleanup([record], extracted_root, engine, report)

    assert report.unit_contamination_candidates == 1
    assert report.descriptions_corrected == 1
    assert record.description_original == "Anchor type"
    assert record.needs_review is False


def test_unit_contamination_confirmed_legitimate_not_stripped(tmp_path):
    extracted_root = _make_extracted_root(tmp_path, ["a.png"])
    words = _header_words() + [
        _word("SUN", 157, 114, width=25, line=5),
        _word("Sunroom", 237, 114, width=55, line=5),
        _word("over", 310, 114, width=30, line=5),
        _word("180", 350, 114, width=25, line=5),
        _word("SF", 390, 114, width=16, line=5),  # genuinely within description column
    ]
    engine = FakeEngine(words_by_filename={"a.png": words})
    record = _record("ACC", "SUN", "Sunroom over 180 SF", source_image="Screenshots_By_CAT/ACC/a.png")
    report = CleanupReport()

    run_unit_contamination_cleanup([record], extracted_root, engine, report)

    assert report.unit_contamination_candidates == 1
    assert report.descriptions_corrected == 0
    assert report.unit_contamination_confirmed_legitimate == 1
    assert record.description_original == "Sunroom over 180 SF"  # untouched
    assert record.needs_review is False
    assert "possible_unit_column_contamination" not in record.review_reasons


def test_unit_contamination_unresolved_when_reparse_diverges_unrelatedly(tmp_path):
    """If a fresh coordinate re-parse of the source screenshot produces a
    description that differs from the stored one in some way OTHER than
    a clean trailing-unit-token removal, the record must be left
    untouched and flagged for review -- never silently overwritten with
    unrelated re-derived text (see build spec 'do not silently correct')."""
    extracted_root = _make_extracted_root(tmp_path, ["a.png"])
    words = _header_words() + [
        _word("SWR", 157, 114, width=25, line=5),
        _word("Tile", 237, 114, width=25, line=5),
        _word("shower", 280, 114, width=40, line=5),
        _word("999", 340, 114, width=25, line=5),
        _word("SF", 380, 114, width=16, line=5),
    ]
    engine = FakeEngine(words_by_filename={"a.png": words})
    # Stored description has a DIFFERENT number than what re-parsing will
    # produce -- simulating the real TIL/SWR> cross-screenshot-variance case.
    record = _record("ACC", "SWR", "Tile shower 111 SF", source_image="Screenshots_By_CAT/ACC/a.png")
    report = CleanupReport()

    run_unit_contamination_cleanup([record], extracted_root, engine, report)

    assert report.descriptions_corrected == 0
    assert report.unit_contamination_unresolved == 1
    assert record.description_original == "Tile shower 111 SF"  # untouched
    assert "possible_unit_column_contamination" in record.review_reasons
    assert record.needs_review is True


def test_unit_contamination_unresolved_when_source_missing(tmp_path):
    extracted_root = _make_extracted_root(tmp_path, ["a.png"])
    engine = FakeEngine(words_by_filename={})
    record = _record("ACC", "ANCR", "Anchor type EA", source_image="Screenshots_By_CAT/ACC/does_not_exist.png")
    report = CleanupReport()

    run_unit_contamination_cleanup([record], extracted_root, engine, report)

    assert report.unit_contamination_unresolved == 1
    assert record.description_original == "Anchor type EA"
    assert "possible_unit_column_contamination" in record.review_reasons


# --- run_malformed_selector_cleanup --------------------------------------


def test_malformed_selector_resolved_from_overlapping_screenshot():
    malformed = _record("RFG", "amrv", "Ridge vent - floating ventilator", source_image="a.png")
    well_formed = _record("RFG", "MTLRV", "Ridge vent - floating ventilator", source_image="b.png")
    report = CleanupReport()

    result = run_malformed_selector_cleanup([malformed, well_formed], report)

    assert report.malformed_selectors_total == 1
    assert report.malformed_resolved_from_overlap == 1
    assert report.malformed_still_review == 0
    assert len(result) == 1  # malformed record absorbed, no longer a separate record
    assert result[0].selector == "MTLRV"
    sources = {s.source_image for s in result[0].source_images}
    assert sources == {"a.png", "b.png"}  # provenance preserved, never discarded


def test_malformed_selector_not_resolved_without_independent_evidence():
    """No matching well-formed record anywhere -- must stay exactly as
    OCR'd, flagged for review, never guessed."""
    malformed = _record("RFG", "oRiIocm", "Hip / Ridge cap - metal roofing", source_image="a.png")
    report = CleanupReport()

    result = run_malformed_selector_cleanup([malformed], report)

    assert report.malformed_resolved_from_overlap == 0
    assert report.malformed_still_review == 1
    assert len(result) == 1
    assert result[0].selector == "oRiIocm"  # preserved exactly, never corrected
    assert "malformed_selector_candidate" in result[0].review_reasons
    assert result[0].needs_review is True


def test_malformed_selector_not_resolved_by_different_description():
    """An exact description match is required -- a merely similar
    description is not 'identical evidence'."""
    malformed = _record("RFG", "amrv", "Ridge vent - floating ventilator", source_image="a.png")
    similar_but_different = _record("RFG", "MTLRV", "Ridge vent - Metal roofing - floating ventilator", source_image="b.png")
    report = CleanupReport()

    result = run_malformed_selector_cleanup([malformed, similar_but_different], report)

    assert report.malformed_resolved_from_overlap == 0
    assert any(r.selector == "amrv" and r.needs_review for r in result)


def test_malformed_selector_not_resolved_by_candidate_from_same_screenshot():
    """Corroboration must come from an INDEPENDENT source screenshot --
    two records sharing the exact same single source don't prove anything."""
    malformed = _record("RFG", "amrv", "Ridge vent - floating ventilator", source_image="a.png")
    same_source = _record("RFG", "MTLRV", "Ridge vent - floating ventilator", source_image="a.png")
    report = CleanupReport()

    result = run_malformed_selector_cleanup([malformed, same_source], report)
    assert report.malformed_resolved_from_overlap == 0


def test_malformed_selector_candidate_itself_malformed_not_used_as_evidence():
    """A second malformed-shaped record must never be used as
    'well-formed' corroborating evidence for another."""
    malformed_a = _record("RFG", "amrv", "Ridge vent - floating ventilator", source_image="a.png")
    malformed_b = _record("RFG", "amrb", "Ridge vent - floating ventilator", source_image="b.png")
    report = CleanupReport()

    result = run_malformed_selector_cleanup([malformed_a, malformed_b], report)
    assert report.malformed_resolved_from_overlap == 0
    assert report.malformed_still_review == 2
    assert len(result) == 2


# --- run_full_cleanup (end-to-end, real SQLite + fake engine) -----------


def test_run_full_cleanup_end_to_end(tmp_path):
    extracted_root = _make_extracted_root(tmp_path, ["a.png"])
    words = _header_words() + [
        _word("ANCR", 157, 114, width=38, line=5),
        _word("Anchor", 237, 114, width=38, line=5),
        _word("type", 300, 114, width=23, line=5),
        _word("EA", 600, 114, width=14, line=5),
    ]
    engine = FakeEngine(words_by_filename={"a.png": words})

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    conn = database.create_database(data_dir / "master_selectors.db")
    database.replace_all_records(
        conn,
        [
            _record("ACC", "ANCR", "Anchor type EA", source_image="Screenshots_By_CAT/ACC/a.png"),
            _record("RFG", "stiR+", "Steel tile ridge", source_image="Screenshots_By_CAT/RFG/x.png"),
        ],
    )
    conn.close()

    backups_dir = tmp_path / "backups"
    report = run_full_cleanup(data_dir, extracted_root, backups_dir, engine=engine)

    assert report.backup_path is not None and report.backup_path.exists()
    assert report.records_inspected == 2
    assert report.descriptions_corrected == 1

    # Reload from disk to confirm persistence, not just in-memory state.
    conn = sqlite3.connect(str(data_dir / "master_selectors.db"))
    conn.row_factory = sqlite3.Row
    rows = {row["selector"]: row for row in conn.execute("SELECT * FROM selectors")}
    conn.close()
    assert rows["ANCR"]["description_original"] == "Anchor type"
    assert rows["stiR+"]["needs_review"] == 1  # still malformed, no matching evidence, stayed in review queue

    assert (data_dir / "master_selectors.csv").exists()
    assert (data_dir / "master_selectors.json").exists()


def test_run_full_cleanup_requires_no_candidates_dir_to_still_work(tmp_path):
    """If .candidates/ is empty/missing (e.g. a DB imported some other
    way), the corroboration step should just find nothing to corroborate,
    not crash."""
    extracted_root = _make_extracted_root(tmp_path, ["a.png"])
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    conn = database.create_database(data_dir / "master_selectors.db")
    database.replace_all_records(conn, [_record("RFG", "FLPIPE", "Flashing - pipe jack")])
    conn.close()

    engine = FakeEngine(words_by_filename={})
    report = run_full_cleanup(data_dir, extracted_root, tmp_path / "backups", engine=engine)
    assert report.records_inspected == 1
    assert report.category_mismatches_before == 0
    assert report.category_mismatches_after == 0
