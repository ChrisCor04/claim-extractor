"""Word/line reconstruction with coordinates from a PyMuPDF page.

PyMuPDF's ``page.get_text("words")`` returns one tuple per word:
``(x0, y0, x1, y1, text, block_no, line_no, word_no)`` in extraction order.
We group consecutive words sharing the same ``(block_no, line_no)`` into a
:class:`~estimate_extractor.models.page.Line`, which gives us per-line
bounding boxes and word-level boxes for debug output and note attachment,
without pulling in a second PDF library.
"""

from __future__ import annotations

import fitz  # PyMuPDF

from estimate_extractor.models.page import Line, Word


def extract_words(page: "fitz.Page") -> list[tuple]:
    return page.get_text("words")


def find_grid_annotation_texts(page: "fitz.Page", min_repeats: int = 3, x_bucket: float = 3.0) -> frozenset[str]:
    """Identify text that belongs to a repeating multi-column grid (e.g. a
    roof/room sketch's per-opening annotation table: "Window" / dimension /
    "Opens into Exterior", repeated once per window/door/wall opening) using
    only position -- no literal text blocklist.

    The signal: real section/area/category headers always stand alone on
    their row (exactly one text span at that y-coordinate). Sketch
    annotations always share their row with 1-2 other spans at fixed,
    repeating x-positions (one column per field: shape type, dimensions,
    "opens into X"). A row shape (its tuple of rounded x0 positions) that
    recurs ``min_repeats`` or more times on a page is therefore a grid, not
    prose, regardless of what the grid's cells say -- so this generalizes to
    an unseen carrier's own annotation vocabulary without a text blocklist.

    Real line-item data rows (qty/price/tax/RCV/...) also have multiple
    spans per row and can also recur with a shared shape; that's harmless
    here since their cell text (bare numbers) was never going to pass
    ``is_label_line`` in the first place -- this function only needs to be
    safe for the *header/label* candidates it's consulted for, not exhaustive
    about which rows are numeric vs. prose.
    """
    try:
        page_dict = page.get_text("dict")
    except Exception:
        return frozenset()

    rows: dict[float, list[tuple[float, str]]] = {}
    for block in page_dict.get("blocks", []):
        for line in block.get("lines", []):
            text = "".join(span.get("text", "") for span in line.get("spans", [])).strip()
            if not text:
                continue
            bbox = line.get("bbox")
            if not bbox:
                continue
            y_key = round(bbox[1])
            rows.setdefault(y_key, []).append((bbox[0], text))

    shape_groups: dict[tuple[int, ...], list[list[tuple[float, str]]]] = {}
    for spans in rows.values():
        if len(spans) < 2:
            continue  # solo-span rows are exactly what real headers look like
        spans_sorted = sorted(spans)
        shape = tuple(round(x0 / x_bucket) for x0, _text in spans_sorted)
        shape_groups.setdefault(shape, []).append(spans_sorted)

    excluded: set[str] = set()
    for group in shape_groups.values():
        if len(group) < min_repeats:
            continue
        for spans_sorted in group:
            for _x0, text in spans_sorted:
                excluded.add(text)
    return frozenset(excluded)


def build_lines(page: "fitz.Page") -> list[Line]:
    raw_words = extract_words(page)
    lines: list[Line] = []
    current_key: tuple[int, int] | None = None
    current_words: list[Word] = []

    def flush() -> None:
        if not current_words:
            return
        text = " ".join(w.text for w in current_words)
        x0 = min(w.x0 for w in current_words)
        y0 = min(w.y0 for w in current_words)
        x1 = max(w.x1 for w in current_words)
        y1 = max(w.y1 for w in current_words)
        lines.append(Line(text=text, y0=y0, y1=y1, x0=x0, x1=x1, words=tuple(current_words)))

    for x0, y0, x1, y1, text, block_no, line_no, _word_no in raw_words:
        key = (block_no, line_no)
        if key != current_key:
            flush()
            current_words = []
            current_key = key
        current_words.append(Word(text=text, x0=x0, y0=y0, x1=x1, y1=y1))
    flush()
    return lines
