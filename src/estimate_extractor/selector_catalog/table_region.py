"""Locates the selector table's header row and column boundaries within a
screenshot's OCR'd words, and extracts the title-bar category ("Selectors
for <CAT> (...)") for cross-checking against the folder-derived category.

Screenshot window size and position vary (confirmed across the real
reference library: 832x1300 to 1043x1387), so column boundaries are
detected per-image from the header row itself -- never hardcoded pixel
offsets.
"""

from __future__ import annotations

import re
from pathlib import Path

from estimate_extractor.selector_catalog.models import TableRegion
from estimate_extractor.selector_catalog.ocr_engine import OCRWord, WordBoxOCREngine

_TITLE_RE = re.compile(r"\bselectors\s+for\s+([A-Za-z0-9_]+)\b", re.IGNORECASE)


class TableRegionNotFoundError(Exception):
    """Raised when the header row (Sel/Description columns) can't be
    located in a screenshot -- the caller records this as a failed
    screenshot, never silently skips it."""


def _group_lines(words: list[OCRWord]) -> dict[tuple[int, int, int], list[OCRWord]]:
    lines: dict[tuple[int, int, int], list[OCRWord]] = {}
    for w in words:
        lines.setdefault(w.line_key, []).append(w)
    for line_words in lines.values():
        line_words.sort(key=lambda w: w.left)
    return lines


def _line_text(line_words: list[OCRWord]) -> str:
    return " ".join(w.text for w in line_words)


def parse_title_bar_category(raw_text: str) -> tuple[str | None, str | None]:
    """Pure regex parse of the title-bar OCR string -- no image I/O, fully
    unit-testable. Returns (category, raw_text_if_matched)."""
    if not raw_text:
        return None, None
    match = _TITLE_RE.search(raw_text)
    if not match:
        return None, None
    return match.group(1).upper(), raw_text


def detect_title_bar_category(image_path: Path, engine: WordBoxOCREngine) -> tuple[str | None, str | None]:
    raw_text = engine.extract_title_bar_text(image_path)
    return parse_title_bar_category(raw_text)


def _find_header_line(lines: dict[tuple[int, int, int], list[OCRWord]]) -> list[OCRWord] | None:
    for line_words in lines.values():
        lowered = [w.text.strip(".:|").lower() for w in line_words]
        if "sel" in lowered and any(t.startswith("desc") for t in lowered):
            return sorted(line_words, key=lambda w: w.left)
    return None


def detect_table_region(words: list[OCRWord]) -> TableRegion:
    """Locates the header row and its Sel/Description column boundaries.
    Does NOT populate title_bar_category/title_bar_raw_text -- those come
    from the dedicated detect_title_bar_category() pass, which the caller
    (pipeline.py) merges in, since it needs a separate, targeted OCR call
    (see ocr_engine.extract_title_bar_text)."""
    lines = _group_lines(words)
    header_line = _find_header_line(lines)
    if header_line is None:
        raise TableRegionNotFoundError("Could not locate a 'Sel | Description' header row in this screenshot.")

    sel_word = next(w for w in header_line if w.text.strip(".:|").lower() == "sel")
    desc_word = next(w for w in header_line if w.text.strip(".:|").lower().startswith("desc"))
    unit_word = next((w for w in header_line if w.text.strip(".:|)(").lower().startswith("unit")), None)

    split_x = round((sel_word.right + desc_word.left) / 2)
    description_right = (unit_word.left - 4) if unit_word is not None else None
    if description_right is None or description_right <= desc_word.left:
        # Fall back to a generous fixed margin if the Unit header wasn't
        # detected (rare OCR miss) -- never silently produce an empty range.
        description_right = desc_word.left + 600

    header_top = min(w.top for w in header_line)
    header_bottom = max(w.bottom for w in header_line)

    return TableRegion(
        header_top=header_top,
        header_bottom=header_bottom,
        # A tight margin, not a generous one: real selector text starts at
        # essentially the same x as the "Sel" header (verified against real
        # screenshots). A wide margin here lets stray characters from a
        # background sidebar panel (e.g. a breadcrumb "|" separator) bleed
        # into the selector column and corrupt the join.
        selector_col_left=max(0, sel_word.left - 5),
        selector_col_right=split_x,
        description_col_left=split_x,
        description_col_right=description_right,
        title_bar_category=None,
        title_bar_raw_text=None,
    )
