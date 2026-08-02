"""Pipeline orchestration tests using a fake WordBoxOCREngine (no real
Tesseract dependency) -- exercises resumability, failure isolation,
category-mismatch flagging, and low-confidence review-queue routing
deterministically. Real-OCR end-to-end coverage lives in
tests/integration/test_selector_catalog_pipeline.py.
"""

from __future__ import annotations

from pathlib import Path

from estimate_extractor.selector_catalog import exporter
from estimate_extractor.selector_catalog.models import SCREENSHOT_STATUS_FAILED, SCREENSHOT_STATUS_PROCESSED
from estimate_extractor.selector_catalog.ocr_engine import OCRWord
from estimate_extractor.selector_catalog.pipeline import run_import


def _word(text, left, top, width=20, height=10, line=1, conf=0.9):
    return OCRWord(text=text, left=left, top=top, width=width, height=height, confidence=conf, block_num=1, par_num=1, line_num=line, word_num=0)


def _header_words(cat="RFG"):
    return [
        _word("Selectors", 173, 15, line=1),
        _word("for", 223, 15, line=1),
        _word(cat, 242, 15, line=1),
        _word("Sel", 157, 78, width=14, line=3),
        _word("Description", 237, 77, width=58, line=3),
        _word("Unit", 591, 78, width=21, line=3),
    ]


class FakeEngine:
    """A WordBoxOCREngine that returns pre-programmed words per image path,
    keyed by filename -- avoids any real Tesseract dependency in unit
    tests while exercising the exact same pipeline code path."""

    def __init__(self, words_by_filename: dict, title_by_filename: dict | None = None, raise_for: set | None = None):
        self.words_by_filename = words_by_filename
        self.title_by_filename = title_by_filename or {}
        self.raise_for = raise_for or set()
        self.call_count = 0

    def extract_words(self, image_path: Path) -> list[OCRWord]:
        self.call_count += 1
        name = image_path.name
        if name in self.raise_for:
            raise RuntimeError("simulated OCR failure")
        return self.words_by_filename.get(name, [])

    def extract_title_bar_text(self, image_path: Path) -> str:
        return self.title_by_filename.get(image_path.name, "")


def _make_library(tmp_path, folder="RFG", filenames=("RFG_001_a.png", "RFG_002_b.png")):
    root = tmp_path / "Lib" / "Screenshots_By_CAT" / folder
    root.mkdir(parents=True)
    for name in filenames:
        (root / name).write_bytes(b"fake-png-bytes")
    return tmp_path


def test_run_import_processes_all_screenshots_and_builds_records(tmp_path):
    lib_root = _make_library(tmp_path)
    words = _header_words() + [_word("FLPIPE", 157, 121, width=31, line=5), _word("Flashing", 237, 120, width=43, line=5)]
    engine = FakeEngine(
        words_by_filename={"RFG_001_a.png": words, "RFG_002_b.png": words},
        title_by_filename={"RFG_001_a.png": "Selectors for RFG (X)", "RFG_002_b.png": "Selectors for RFG (X)"},
    )
    data_dir = tmp_path / "data"
    result = run_import(lib_root, data_dir, engine=engine)

    assert result.processed == 2
    assert result.failed == 0
    assert len(result.records) == 1  # both screenshots show the same FLPIPE row -- merged
    assert result.records[0].source_images and len(result.records[0].source_images) == 2


def test_resumed_import_reuses_cache_without_reinvoking_engine(tmp_path):
    lib_root = _make_library(tmp_path)
    words = _header_words() + [_word("FLPIPE", 157, 121, width=31, line=5), _word("Flashing", 237, 120, width=43, line=5)]
    engine = FakeEngine(words_by_filename={"RFG_001_a.png": words, "RFG_002_b.png": words})
    data_dir = tmp_path / "data"

    first = run_import(lib_root, data_dir, engine=engine)
    assert first.processed == 2
    calls_after_first_run = engine.call_count

    second = run_import(lib_root, data_dir, engine=engine)
    assert second.from_cache == 2
    assert second.processed == 0
    assert engine.call_count == calls_after_first_run  # engine never invoked again


def test_force_reprocesses_even_when_cached(tmp_path):
    lib_root = _make_library(tmp_path)
    words = _header_words() + [_word("FLPIPE", 157, 121, width=31, line=5), _word("Flashing", 237, 120, width=43, line=5)]
    engine = FakeEngine(words_by_filename={"RFG_001_a.png": words, "RFG_002_b.png": words})
    data_dir = tmp_path / "data"

    run_import(lib_root, data_dir, engine=engine)
    result = run_import(lib_root, data_dir, engine=engine, force=True)
    assert result.processed == 2
    assert result.from_cache == 0


def test_one_failed_screenshot_does_not_abort_the_import(tmp_path):
    lib_root = _make_library(tmp_path, filenames=("RFG_001_a.png", "RFG_002_bad.png"))
    words = _header_words() + [_word("FLPIPE", 157, 121, width=31, line=5), _word("Flashing", 237, 120, width=43, line=5)]
    engine = FakeEngine(words_by_filename={"RFG_001_a.png": words}, raise_for={"RFG_002_bad.png"})
    data_dir = tmp_path / "data"

    result = run_import(lib_root, data_dir, engine=engine)
    assert result.processed == 1
    assert result.failed == 1
    failed_entries = [e for e in result.manifest_entries if e.status == SCREENSHOT_STATUS_FAILED]
    assert len(failed_entries) == 1
    assert failed_entries[0].error is not None
    assert "simulated OCR failure" in failed_entries[0].error


def test_missing_table_region_recorded_as_failed_not_crashed(tmp_path):
    lib_root = _make_library(tmp_path, filenames=("RFG_001_a.png",))
    engine = FakeEngine(words_by_filename={"RFG_001_a.png": [_word("nonsense", 10, 10)]})  # no Sel/Description header
    data_dir = tmp_path / "data"

    result = run_import(lib_root, data_dir, engine=engine)
    assert result.failed == 1
    assert result.processed == 0


def test_non_cat_folder_always_skipped(tmp_path):
    lib_root = _make_library(tmp_path, folder="_NON_CAT_PROJECT_UI", filenames=("_NON_CAT_PROJECT_UI_001_x.png",))
    engine = FakeEngine(words_by_filename={})
    data_dir = tmp_path / "data"

    result = run_import(lib_root, data_dir, engine=engine)
    assert result.skipped == 1
    assert result.processed == 0
    assert engine.call_count == 0  # never OCR'd -- skipped outright
    entry = result.manifest_entries[0]
    assert entry.status == SCREENSHOT_STATUS_PROCESSED or entry.skip_reason == "non_cat_project_ui_folder"


def test_category_mismatch_flags_row_for_review_when_corroborated(tmp_path):
    """A folder-wide majority of independent screenshots agreeing on the
    SAME alternate category is trusted as a genuine mismatch -- mirrors
    the real ELE-folder/FNC-title case (6/9 screenshots agreeing)."""
    filenames = ("ELE_001_x.png", "ELE_002_x.png", "ELE_003_x.png")
    lib_root = _make_library(tmp_path, folder="ELE", filenames=filenames)
    words_by_filename = {}
    title_by_filename = {}
    for i, name in enumerate(filenames):
        words_by_filename[name] = _header_words() + [
            _word(f"CRS{i}", 157, 121, width=20, line=5),
            _word("Casing", 237, 120, width=40, line=5),
        ]
        title_by_filename[name] = "Selectors for FNC (X)"
    engine = FakeEngine(words_by_filename=words_by_filename, title_by_filename=title_by_filename)
    data_dir = tmp_path / "data"

    result = run_import(lib_root, data_dir, engine=engine)
    assert len(result.records) == 3
    for record in result.records:
        assert record.category == "ELE"  # folder wins as primary key
        assert record.title_bar_category == "FNC"  # title kept alongside, never silently discarded
        assert record.category_mismatch is True
        assert "category_mismatch" in record.review_reasons
        assert record.needs_review is True


def test_low_confidence_uncorroborated_title_mismatch_not_flagged(tmp_path):
    """A SINGLE screenshot's title-bar disagreement, with no independent
    corroboration from other screenshots in the same folder, must NOT be
    treated as a genuine category mismatch -- this is exactly the false-
    positive pattern found for real in the DOR/DMO folders during Phase
    3.6 QA (isolated/noisy title misreads, not systemic mismatches)."""
    lib_root = _make_library(tmp_path, folder="DOR", filenames=("DOR_001_x.png",))
    words = _header_words() + [_word("CRS", 157, 121, width=20, line=5), _word("Casing", 237, 120, width=40, line=5)]
    engine = FakeEngine(words_by_filename={"DOR_001_x.png": words}, title_by_filename={"DOR_001_x.png": "Selectors for ELE (X)"})
    data_dir = tmp_path / "data"

    result = run_import(lib_root, data_dir, engine=engine)
    assert len(result.records) == 1
    record = result.records[0]
    assert record.category == "DOR"
    assert record.title_bar_category == "ELE"  # still recorded as supporting metadata
    assert record.category_mismatch is False  # but NOT trusted as a genuine mismatch
    assert "category_mismatch" not in record.review_reasons


def test_mixed_folder_readings_with_no_clear_majority_not_flagged(tmp_path):
    """A folder whose title-bar readings split roughly evenly between its
    own name and one alternate (no clear majority) must not flag a
    systemic mismatch -- mirrors the real DMO-folder/DRY-title 50/50 split."""
    filenames = ("DMO_001_x.png", "DMO_002_x.png", "DMO_003_x.png", "DMO_004_x.png")
    lib_root = _make_library(tmp_path, folder="DMO", filenames=filenames)
    words_by_filename = {}
    title_by_filename = {}
    titles = ["DRY", "DMO", "DRY", "DMO"]
    for i, (name, title) in enumerate(zip(filenames, titles)):
        words_by_filename[name] = _header_words() + [
            _word(f"SEL{i}", 157, 121, width=20, line=5),
            _word("Description", 237, 120, width=40, line=5),
        ]
        title_by_filename[name] = f"Selectors for {title} (X)"
    engine = FakeEngine(words_by_filename=words_by_filename, title_by_filename=title_by_filename)
    data_dir = tmp_path / "data"

    result = run_import(lib_root, data_dir, engine=engine)
    assert len(result.records) == 4
    assert all(not r.category_mismatch for r in result.records)


def test_low_confidence_row_routed_to_review_queue(tmp_path):
    lib_root = _make_library(tmp_path, filenames=("RFG_001_a.png",))
    low_conf_words = _header_words() + [
        _word("FLPIPE", 157, 121, width=31, line=5, conf=0.5),
        _word("Flashing", 237, 120, width=43, line=5, conf=0.5),
    ]
    engine = FakeEngine(words_by_filename={"RFG_001_a.png": low_conf_words})
    data_dir = tmp_path / "data"

    result = run_import(lib_root, data_dir, engine=engine)
    assert result.records[0].needs_review is True
    assert "low_ocr_confidence" in result.records[0].review_reasons
    assert any(row.selector == "FLPIPE" and row.in_database for row in result.review_queue)


def test_empty_selector_row_goes_to_review_queue_not_database(tmp_path):
    lib_root = _make_library(tmp_path, filenames=("RFG_001_a.png",))
    # A description with no plausible selector token at all in that column.
    words = _header_words() + [_word("Some orphaned description text", 237, 120, width=140, line=5)]
    engine = FakeEngine(words_by_filename={"RFG_001_a.png": words})
    data_dir = tmp_path / "data"

    result = run_import(lib_root, data_dir, engine=engine)
    assert result.records == []
    assert len(result.review_queue) == 1
    assert result.review_queue[0].reason == "empty_selector"
    assert result.review_queue[0].in_database is False


def test_validation_passes_for_a_complete_clean_import(tmp_path):
    lib_root = _make_library(tmp_path, filenames=("RFG_001_a.png",))
    words = _header_words() + [_word("FLPIPE", 157, 121, width=31, line=5), _word("Flashing", 237, 120, width=43, line=5)]
    engine = FakeEngine(words_by_filename={"RFG_001_a.png": words}, title_by_filename={"RFG_001_a.png": "Selectors for RFG (X)"})
    data_dir = tmp_path / "data"

    result = run_import(lib_root, data_dir, engine=engine)
    assert result.validation.passed is True


def test_manifest_persists_trailing_skipped_screenshots_to_disk(tmp_path):
    """Regression test: '_NON_CAT_PROJECT_UI' sorts *after* every real
    (uppercase) CAT folder name, so in a real run its screenshots are
    always processed last. The non-CAT skip branch (and the from-cache
    reuse branch) both `continue` before the in-loop incremental manifest
    write, so without a final unconditional flush after the loop, trailing
    skipped/cached entries were silently dropped from the on-disk
    manifest even though `result.manifest_entries` (in memory) had them --
    caught by a real full-library import during this phase's build."""
    root = tmp_path / "Lib" / "Screenshots_By_CAT"
    (root / "RFG").mkdir(parents=True)
    (root / "RFG" / "RFG_001_a.png").write_bytes(b"fake")
    (root / "_NON_CAT_PROJECT_UI").mkdir(parents=True)
    (root / "_NON_CAT_PROJECT_UI" / "_NON_CAT_PROJECT_UI_001_z.png").write_bytes(b"fake")

    words = _header_words() + [_word("FLPIPE", 157, 121, width=31, line=5), _word("Flashing", 237, 120, width=43, line=5)]
    engine = FakeEngine(words_by_filename={"RFG_001_a.png": words})
    data_dir = tmp_path / "data"

    result = run_import(tmp_path, data_dir, engine=engine)
    assert result.skipped == 1

    on_disk_entries = exporter.load_manifest(data_dir / "screenshot_processing_manifest.json")
    on_disk_paths = {e.relative_path for e in on_disk_entries}
    assert len(on_disk_entries) == 2
    assert any("_NON_CAT_PROJECT_UI" in p for p in on_disk_paths)
    assert any("RFG_001_a.png" in p for p in on_disk_paths)


def test_malformed_lowercase_selector_flagged_even_at_high_confidence(tmp_path):
    """A selector containing lowercase letters must be flagged for review
    regardless of raw OCR word confidence -- real examples (RFG/stiR+,
    MTL/Bites) had confidence ABOVE the low_ocr_confidence threshold and
    were false negatives before this check existed."""
    lib_root = _make_library(tmp_path, filenames=("RFG_001_a.png",))
    # High confidence (0.95, well above LOW_CONFIDENCE_THRESHOLD) but the
    # selector text itself is malformed-shaped.
    words = _header_words() + [
        _word("stiR+", 157, 121, width=31, line=5, conf=0.95),
        _word("Steel", 237, 120, width=43, line=5, conf=0.95),
    ]
    engine = FakeEngine(words_by_filename={"RFG_001_a.png": words})
    data_dir = tmp_path / "data"

    result = run_import(lib_root, data_dir, engine=engine)
    assert len(result.records) == 1
    record = result.records[0]
    assert record.selector == "stiR+"  # preserved exactly, never guessed/corrected
    assert "malformed_selector_candidate" in record.review_reasons
    assert record.needs_review is True
    assert "low_ocr_confidence" not in record.review_reasons  # confidence alone did not trigger this


def test_unit_contamination_candidate_flagged_by_ongoing_pipeline(tmp_path):
    """The heuristic safety-net check fires on every import (not just the
    one-time cleanup pass) for any description that still ends in a known
    unit token after row parsing -- defense in depth."""
    lib_root = _make_library(tmp_path, filenames=("RFG_001_a.png",))
    words = _header_words() + [
        _word("XYZ", 157, 121, width=25, line=5),
        _word("Something", 237, 120, width=60, line=5),
        _word("EA", 300, 120, width=15, line=5),
    ]
    engine = FakeEngine(words_by_filename={"RFG_001_a.png": words})
    data_dir = tmp_path / "data"

    result = run_import(lib_root, data_dir, engine=engine)
    assert len(result.records) == 1
    record = result.records[0]
    assert "possible_unit_column_contamination" in record.review_reasons
    assert record.needs_review is True
    # The description is NOT modified by this heuristic -- flag only.
    assert record.description_original.endswith("EA")
