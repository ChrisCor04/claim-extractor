from __future__ import annotations

import pytest

from estimate_extractor.selector_catalog.ocr_engine import OCRWord
from estimate_extractor.selector_catalog.table_region import (
    TableRegionNotFoundError,
    detect_table_region,
    parse_title_bar_category,
)


def _word(text, left, top, width=20, height=10, block=1, par=1, line=1, conf=0.9):
    return OCRWord(text=text, left=left, top=top, width=width, height=height, confidence=conf, block_num=block, par_num=par, line_num=line, word_num=0)


def _header_and_row_words(cat="RFG"):
    return [
        _word("Selectors", 173, 15, line=1),
        _word("for", 223, 15, line=1),
        _word(cat, 242, 15, line=1),
        _word("(COFC8X_JUL26)", 267, 15, line=1),
        _word("Sel", 157, 78, width=14, line=3),
        _word("Description", 237, 77, width=58, line=3),
        _word("Unit", 591, 78, width=21, line=3),
        _word("Act", 566, 78, width=16, line=3),
        _word("Green", 651, 78, width=30, line=3),
        _word("FLPIPE", 157, 121, width=31, line=5),
        _word("Flashing", 237, 120, width=43, line=5),
        _word("-", 283, 125, width=4, line=5),
        _word("pipe", 291, 123, width=21, line=5),
        _word("jack", 316, 112, width=20, line=5),
    ]


# --- title-bar parsing (pure regex, no OCR/image I/O) ----------------------


def test_parse_title_bar_category_matches_standard_form():
    cat, raw = parse_title_bar_category("X) Selectors for RFG (COFC8X_JUL26) x")
    assert cat == "RFG"
    assert raw is not None


def test_parse_title_bar_category_case_insensitive():
    cat, _ = parse_title_bar_category("selectors FOR fnc (cofc8x_jul26)")
    assert cat == "FNC"


def test_parse_title_bar_category_no_match_returns_none():
    cat, raw = parse_title_bar_category("some unrelated text")
    assert cat is None
    assert raw is None


def test_parse_title_bar_category_empty_string():
    assert parse_title_bar_category("") == (None, None)


# --- header/column detection ------------------------------------------------


def test_detect_table_region_finds_header_boundaries():
    words = _header_and_row_words()
    region = detect_table_region(words)
    assert region.selector_col_left < region.selector_col_right
    assert region.selector_col_right == region.description_col_left
    assert region.description_col_right > region.description_col_left
    # Header row's own bounds are captured for row-start detection.
    assert region.header_top < region.header_bottom


def test_detect_table_region_raises_when_no_header_present():
    words = [_word("Random", 10, 10), _word("Text", 50, 10)]
    with pytest.raises(TableRegionNotFoundError):
        detect_table_region(words)


def test_detect_table_region_column_split_separates_selector_and_description_data():
    words = _header_and_row_words()
    region = detect_table_region(words)
    selector_word = next(w for w in words if w.text == "FLPIPE")
    description_word = next(w for w in words if w.text == "Flashing")
    assert region.selector_col_left <= selector_word.left < region.selector_col_right
    assert region.description_col_left <= description_word.left < region.description_col_right
