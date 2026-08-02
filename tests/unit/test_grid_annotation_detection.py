"""Regression tests for pdf/layout.py:find_grid_annotation_texts -- the
position-based (not text-blocklist-based) detector for repeating sketch
annotation grids, added to fix a class of bug where real English annotation
phrases ("Opens into Exterior", "Missing Wall...", "Door", "Window") were
being mistaken for real section/category headers.

Builds small synthetic PDFs in-memory with PyMuPDF so this doesn't depend on
the (gitignored, PII-containing) real fixture PDFs being present locally.
"""

from __future__ import annotations

import fitz
import pytest

from estimate_extractor.pdf.layout import find_grid_annotation_texts


def _make_page(rows: list[list[tuple[float, str]]]) -> "fitz.Page":
    """rows: list of rows, each a list of (x0, text) placed at a fresh y."""
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    y = 100.0
    for row in rows:
        for x0, text in row:
            page.insert_text((x0, y), text, fontsize=10)
        y += 15.0
    return page


def test_repeating_two_column_grid_is_excluded():
    # Simulates a sketch's per-opening annotation table: "Door"/"Window" in
    # one column, "Opens into Exterior" in another, repeated many times.
    rows = [
        [(36, "Door"), (378, "Opens into Exterior")],
        [(36, "Window"), (378, "Opens into Exterior")],
        [(36, "Window"), (378, "Opens into Exterior")],
        [(36, "Door"), (378, "Opens into Exterior")],
    ]
    page = _make_page(rows)
    excluded = find_grid_annotation_texts(page, min_repeats=3)
    assert "Door" in excluded
    assert "Window" in excluded
    assert "Opens into Exterior" in excluded


def test_solo_header_line_never_excluded():
    # A real section header stands alone on its row -- never part of a
    # multi-span row, so it must never be excluded regardless of how many
    # times the *other* rows on the page repeat.
    rows = [
        [(165, "Ext_Surfaces")],
        [(36, "Door"), (378, "Opens into Exterior")],
        [(36, "Window"), (378, "Opens into Exterior")],
        [(36, "Window"), (378, "Opens into Exterior")],
    ]
    page = _make_page(rows)
    excluded = find_grid_annotation_texts(page, min_repeats=3)
    assert "Ext_Surfaces" not in excluded
    assert "Door" in excluded


def test_shape_recurring_fewer_than_threshold_is_not_excluded():
    # Only 2 repeats of the same row-shape: below the min_repeats=3
    # threshold, so nothing is excluded (conservative -- avoid false
    # positives on a page with just one or two incidental multi-span rows).
    rows = [
        [(36, "Foo"), (378, "Bar")],
        [(36, "Baz"), (378, "Qux")],
    ]
    page = _make_page(rows)
    excluded = find_grid_annotation_texts(page, min_repeats=3)
    assert excluded == frozenset()


def test_no_spans_returns_empty_set():
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    assert find_grid_annotation_texts(page) == frozenset()
