from __future__ import annotations

import pytest

from estimate_extractor.selector_catalog.models import TableRegion
from estimate_extractor.selector_catalog.ocr_engine import OCRWord
from estimate_extractor.selector_catalog.row_parser import parse_rows


def _word(text, left, top, width=20, height=10, line=1, conf=0.9):
    return OCRWord(text=text, left=left, top=top, width=width, height=height, confidence=conf, block_num=1, par_num=1, line_num=line, word_num=0)


def _region(**overrides):
    defaults = dict(
        header_top=70,
        header_bottom=95,
        selector_col_left=137,
        selector_col_right=204,
        description_col_left=204,
        description_col_right=533,
        title_bar_category="RFG",
        title_bar_raw_text="Selectors for RFG (COFC8X_JUL26)",
    )
    defaults.update(overrides)
    return TableRegion(**defaults)


def test_selector_punctuation_preserved_exactly():
    region = _region()
    cases = ["ST", "ST-", "ST+", "ST++", "SG2", "SG2+", "SH5/8", "IPO1>", "ISO1<"]
    words = []
    for i, sel in enumerate(cases):
        y = 100 + i * 20
        words.append(_word(sel, 157, y, width=30, line=i + 10))
        words.append(_word("Description text", 237, y, width=100, line=i + 10))
    rows = parse_rows(words, region)
    selectors = [r.selector_raw for r in rows]
    assert selectors == cases


def test_multiline_description_joined_into_one_line():
    """Words on the same tesseract line (a real table row, even if the
    rendered text wrapped visually) are joined with single spaces, not
    concatenated or newline-separated."""
    region = _region()
    words = [
        _word("FLPB6", 157, 100, width=30, line=5),
        _word("Flash", 237, 100, width=40, line=5),
        _word("parapet", 267, 100, width=50, line=5),
        _word("wall", 310, 100, width=30, line=5),
        _word("only", 340, 100, width=30, line=5),
    ]
    rows = parse_rows(words, region)
    assert len(rows) == 1
    assert rows[0].description_raw == "Flash parapet wall only"


def test_truncated_description_detected():
    region = _region()
    words = [
        _word("FCWGC", 157, 100, width=30, line=5),
        _word("Add", 237, 100, width=30, line=5),
        _word("for", 270, 100, width=25, line=5),
        _word("tear...", 300, 100, width=40, line=5),
    ]
    rows = parse_rows(words, region)
    assert rows[0].truncated is True


def test_non_truncated_description_not_flagged():
    region = _region()
    words = [_word("FLPIPE", 157, 100, width=30, line=5), _word("Flashing", 237, 100, width=40, line=5)]
    rows = parse_rows(words, region)
    assert rows[0].truncated is False


def test_stray_punctuation_never_contaminates_selector_join():
    """A stray '|' (e.g. background breadcrumb bleed-through) landing in
    the selector column's x-range must not corrupt the selector text."""
    region = _region()
    words = [
        _word("|", 150, 100, width=2, line=5),
        _word("FLRIG", 157, 100, width=28, line=5),
        _word("Hip", 237, 100, width=20, line=5),
        _word("Ridge", 265, 100, width=25, line=5),
    ]
    rows = parse_rows(words, region)
    assert rows[0].selector_raw == "FLRIG"


def test_row_with_no_words_in_either_column_is_dropped():
    region = _region()
    # A word entirely outside both column ranges (e.g. in the Unit/Price/
    # Green columns) shouldn't produce a phantom row.
    words = [_word("$32.89", 607, 100, width=40, line=5)]
    rows = parse_rows(words, region)
    assert rows == []


def test_implausible_selector_with_no_description_dropped_entirely():
    region = _region()
    words = [_word("###", 157, 100, width=20, line=5)]
    rows = parse_rows(words, region)
    assert rows == []


def test_implausible_selector_with_description_kept_but_blanked():
    """OCR noise in the selector column shouldn't cause the whole row
    (including a real description) to be lost -- the selector is blanked,
    never guessed, and the row is kept for human review."""
    region = _region()
    words = [
        _word("#@!", 157, 100, width=20, line=5),
        _word("Some", 237, 100, width=30, line=5),
        _word("real", 270, 100, width=25, line=5),
        _word("description", 300, 100, width=60, line=5),
    ]
    rows = parse_rows(words, region)
    assert len(rows) == 1
    assert rows[0].selector_raw == ""
    assert "real description" in rows[0].description_raw


def test_multiple_rows_grouped_independently():
    region = _region()
    words = [
        _word("AAA", 157, 100, width=20, line=5),
        _word("First", 237, 100, width=30, line=5),
        _word("BBB", 157, 120, width=20, line=6),
        _word("Second", 237, 120, width=40, line=6),
    ]
    rows = parse_rows(words, region)
    assert [r.selector_raw for r in rows] == ["AAA", "BBB"]
    assert [r.description_raw for r in rows] == ["First", "Second"]


def test_row_confidence_is_mean_of_row_words():
    region = _region()
    words = [
        _word("AAA", 157, 100, width=20, line=5, conf=0.8),
        _word("Desc", 237, 100, width=30, line=5, conf=0.6),
    ]
    rows = parse_rows(words, region)
    assert rows[0].row_confidence == pytest.approx(0.7)


# --- Unit-column contamination QA fix: center-based column assignment -------
#
# Regression coverage for the real ACC/ANCR case found during Phase 3.6 QA:
# Tesseract produced a low-confidence duplicate/"ghost" bounding box for a
# Unit-column "EA" token whose LEFT edge landed a few pixels inside the
# description column's boundary (region.description_col_right=533 in these
# fixtures), even though the word visually belongs to Unit. Assigning by
# word CENTER instead of left edge excludes it correctly.


def test_unit_column_ghost_word_excluded_from_description():
    # Coordinates mirror the real ACC/ANCR screenshot's detected region
    # (selector 10-62, description 62-392) that produced this exact bug.
    region = _region(selector_col_left=10, selector_col_right=62, description_col_left=62, description_col_right=392)
    words = [
        _word("ANCR", 10, 114, width=38, line=3),
        _word("Anchor", 95, 114, width=38, line=3),
        _word("twist", 141, 114, width=26, line=3),
        _word("ground", 183, 114, width=38, line=3),
        _word("type", 224, 114, width=23, line=3),
        # The real "EA" Unit-column value.
        _word("EA", 394, 114, width=14, line=3),
        # A low-confidence duplicate/"ghost" detection of the same "EA",
        # whose LEFT edge (388) is inside the old boundary (392) but whose
        # CENTER (388 + 30/2 = 403) is not -- exactly as observed for real.
        _word("EA", 388, 114, width=30, height=18, line=3, conf=0.44),
    ]
    rows = parse_rows(words, region)
    assert len(rows) == 1
    assert rows[0].selector_raw == "ANCR"
    assert rows[0].description_raw == "Anchor twist ground type"
    assert "EA" not in rows[0].description_raw.split()


def test_legitimate_description_ending_in_unit_like_text_not_stripped():
    """A word whose CENTER is comfortably inside the description column
    must be kept even if it happens to read as a unit abbreviation --
    only coordinate position decides column membership, never the word's
    text content. Real example: 'Sunroom / Garden Room kit - over 180 SF'."""
    region = _region(description_col_right=560)
    words = [
        _word("SUN", 157, 100, width=25, line=5),
        _word("Sunroom", 237, 100, width=55, line=5),
        _word("/", 296, 100, width=6, line=5),
        _word("Garden", 306, 100, width=45, line=5),
        _word("Room", 355, 100, width=38, line=5),
        _word("kit", 397, 100, width=22, line=5),
        _word("-", 423, 100, width=6, line=5),
        _word("over", 433, 100, width=30, line=5),
        _word("180", 467, 100, width=25, line=5),
        # Genuinely part of the description column (well inside the 560
        # right boundary), not a Unit-column artifact.
        _word("SF", 496, 100, width=16, line=5),
    ]
    rows = parse_rows(words, region)
    assert len(rows) == 1
    assert rows[0].description_raw == "Sunroom / Garden Room kit - over 180 SF"


def test_column_boundary_uses_word_center_not_left_edge():
    """Direct test of the coordinate rule itself: a word whose left edge
    is inside a column but whose center is past the boundary must NOT be
    assigned to that column."""
    region = _region(selector_col_left=100, selector_col_right=200, description_col_left=200, description_col_right=400)
    # left=195 (inside [100,200)) but center=195+30/2=210 (outside) --
    # should land in the description column instead, not the selector one.
    straddling_word = _word("XX", 195, 100, width=30, line=5)
    real_selector = _word("SEL1", 105, 100, width=20, line=5)
    words = [real_selector, straddling_word]
    rows = parse_rows(words, region)
    assert len(rows) == 1
    assert rows[0].selector_raw == "SEL1"
    assert "XX" in rows[0].description_raw
