"""Groups OCR'd words below the header row into table rows, assigns each
word to the Selector or Description column, joins wrapped text, and flags
truncated rows. Never fabricates missing text -- an unreadable selector or
description is left empty and surfaced later as a review-queue row, never
guessed.
"""

from __future__ import annotations

import re

from estimate_extractor.selector_catalog.models import ExtractedRow, is_truncated
from estimate_extractor.selector_catalog.ocr_engine import OCRWord
from estimate_extractor.selector_catalog.table_region import TableRegion

# A plausible selector token: starts with a letter or digit, then allowed
# punctuation. This is a *plausibility* filter to reject OCR noise/background
# bleed-through that happens to land in the selector column's x-range -- it
# is not a semantic validator, and matching tokens are still stored exactly
# as OCR'd (see build spec "preserve selector values exactly").
_SELECTOR_PLAUSIBLE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+\-<>/.]{0,20}$")


def _group_rows(words: list[OCRWord], region: TableRegion) -> list[list[OCRWord]]:
    """Groups words below the header into rows using Tesseract's own line
    grouping (block/par/line_num) -- reliable here because ``--psm 6``
    treats the dialog as one uniform text block and Tesseract's line
    segmentation lines up cleanly with visual table rows (verified against
    real reference screenshots)."""
    candidates = [w for w in words if w.top >= region.header_bottom - 2]
    lines: dict[tuple[int, int, int], list[OCRWord]] = {}
    for w in candidates:
        lines.setdefault(w.line_key, []).append(w)
    ordered = sorted(lines.values(), key=lambda lw: min(w.top for w in lw))
    return [sorted(lw, key=lambda w: w.left) for lw in ordered]


def _assign_columns(row_words: list[OCRWord], region: TableRegion) -> tuple[list[OCRWord], list[OCRWord]]:
    """Assigns each word to the Selector or Description column using its
    horizontal CENTER, not its left edge.

    Root-caused during Phase 3.6 QA: Tesseract occasionally produces a
    low-confidence duplicate/"ghost" bounding box for a Unit-column token
    (e.g. a second "EA" detection whose box is a few pixels wider than the
    real one) whose LEFT edge lands just inside the description column's
    right boundary even though the word visually belongs to Unit. Using
    the word's center is far more robust to this: a word genuinely
    straddling or past the boundary has its center past it too, while a
    real description word's center sits comfortably inside its column.
    Verified against the real ACC/ANCR contamination case (a duplicate
    "EA" box at left=388 width=30 -- inside the old left-edge boundary at
    392 -- has center=403, correctly outside it)."""
    selector_words = [w for w in row_words if region.selector_col_left <= w.center_x < region.selector_col_right]
    # Pure-punctuation tokens (e.g. a stray "|" bleeding through from a
    # background breadcrumb panel) can land inside the selector column's
    # x-range on some screenshots; a real selector code always has at
    # least one alphanumeric character, so this is a safe, general filter
    # independent of exact column-boundary tuning.
    selector_words = [w for w in selector_words if any(c.isalnum() for c in w.text)]
    description_words = [
        w for w in row_words if region.description_col_left <= w.center_x < region.description_col_right
    ]
    return selector_words, description_words


def _mean_confidence(words: list[OCRWord]) -> float | None:
    confs = [w.confidence for w in words if w.confidence is not None]
    return sum(confs) / len(confs) if confs else None


def parse_rows(words: list[OCRWord], region: TableRegion) -> list[ExtractedRow]:
    rows: list[ExtractedRow] = []
    for row_words in _group_rows(words, region):
        selector_words, description_words = _assign_columns(row_words, region)
        selector_text = "".join(w.text for w in selector_words).strip()  # selector codes never contain spaces
        description_text = " ".join(w.text for w in description_words).strip()

        if not selector_text and not description_text:
            continue  # nothing in either column -- not a real row (e.g. a scrollbar sliver)

        if selector_text and not _SELECTOR_PLAUSIBLE_RE.match(selector_text):
            # Doesn't look like a real selector code -- likely OCR noise
            # from background bleed-through. If there's no description
            # either, this isn't a genuine table row; drop it silently.
            # If there IS a description, keep the row (selector blanked
            # out, never guessed) so a human reviews it instead of losing
            # the description entirely.
            if not description_text:
                continue
            selector_text = ""

        all_row_words = selector_words + description_words
        rows.append(
            ExtractedRow(
                selector_raw=selector_text,
                description_raw=description_text,
                row_confidence=_mean_confidence(all_row_words),
                truncated=is_truncated(description_text),
                y_top=min(w.top for w in row_words),
            )
        )
    return rows
