"""Regression tests for section labels that don't share a page with their
own item table:

1. A label + measurement block sits on a page with no QUANTITY table of its
   own, classified roof_diagram/measurement_summary rather than
   estimate_detail (confirmed against the Garrety fixture: "Ext_Surfaces").
2. A bare label is the very last line on a page with nothing after it, and
   the item table begins on the very next page with no repeated label at
   all -- not even a "CONTINUED -" marker (confirmed against the Bagi
   fixture: "Fence").

Both used to silently lose the section name (falling back to an
"Unlabeled (N)" section) before the fixes in parsing/state_machine.py.
"""

from __future__ import annotations

from pathlib import Path

from estimate_extractor.models.page import PageClassification, ParsedDocument, ParsedPage, PageRecord
from estimate_extractor.parsing.line_items import ColumnSchema
from estimate_extractor.parsing.state_machine import IdFactory, walk_estimate_body

SCHEMA = ColumnSchema(core_fields=("unit_price", "tax", "replacement_cost_value"))


def _document(pages_text: list[str]) -> ParsedDocument:
    pages = [
        ParsedPage(page_number=i + 1, width=612, height=792, raw_text=text)
        for i, text in enumerate(pages_text)
    ]
    return ParsedDocument(source_path=Path("test.pdf"), sha256="deadbeef", page_count=len(pages), pages=pages)


def test_label_on_roof_diagram_page_attaches_to_items_on_next_page():
    page1_text = """Ext_Surfaces
2,801.13 SF Walls
475.45 LF Floor Perimeter
"""
    page2_text = """QUANTITY
UNIT PRICE
TAX
RCV
17.  R&R Gutter - aluminum - up to 5"
34.92 LF
9.80
11.67
353.89
Totals:  Ext_Surfaces
11.67
353.89
"""
    document = _document([page1_text, page2_text])
    page_records = [
        PageRecord(page=1, classification=PageClassification.ROOF_DIAGRAM, include_in_estimate=True, confidence=0.9, reasons=[]),
        PageRecord(page=2, classification=PageClassification.ESTIMATE_DETAIL, include_in_estimate=True, confidence=0.9, reasons=[]),
    ]
    body = walk_estimate_body(document, page_records, [], IdFactory(), SCHEMA, boilerplate=set())

    assert len(body.sections) == 1
    section = body.sections[0]
    assert section.name == "Ext_Surfaces"
    assert section.measurements.wall_area_sf == 2801.13
    assert section.measurements.floor_perimeter_lf == 475.45
    assert len(body.line_items) == 1
    assert body.line_items[0].section_id == section.section_id
    assert body.line_items[0].description.startswith("R&R Gutter")


def test_label_with_no_content_on_its_page_attaches_to_items_on_next_page():
    page1_text = """Totals:  Left Elevation
895.48
179.28
716.20
Fence
"""
    page2_text = """DESCRIPTION
QUANTITY
UNIT PRICE
TAX
RCV
22.  Clean with pressure/chemical spray - Light
714.00 SF
0.40
0.00
285.60
Totals:  Fence
0.00
285.60
"""
    document = _document([page1_text, page2_text])
    page_records = [
        PageRecord(page=1, classification=PageClassification.ESTIMATE_DETAIL, include_in_estimate=True, confidence=0.9, reasons=[]),
        PageRecord(page=2, classification=PageClassification.ESTIMATE_DETAIL, include_in_estimate=True, confidence=0.9, reasons=[]),
    ]
    body = walk_estimate_body(document, page_records, [], IdFactory(), SCHEMA, boilerplate=set())

    section_names = [s.name for s in body.sections]
    assert "Fence" in section_names
    assert not any(name.startswith("Unlabeled") for name in section_names)

    fence_section = next(s for s in body.sections if s.name == "Fence")
    assert len(body.line_items) == 1
    assert body.line_items[0].section_id == fence_section.section_id
    assert "Clean with pressure" in body.line_items[0].description


def test_pending_label_discarded_when_next_page_has_no_real_content():
    # If the next page turns out not to have a QUANTITY table or
    # measurement block after all, the deferred label must be discarded
    # cleanly rather than wrongly opening a section with nothing in it.
    page1_text = "Some Trailing Label\n"
    page2_text = "Just narrative text with no table at all.\n"
    document = _document([page1_text, page2_text])
    page_records = [
        PageRecord(page=1, classification=PageClassification.ESTIMATE_DETAIL, include_in_estimate=True, confidence=0.9, reasons=[]),
        PageRecord(page=2, classification=PageClassification.ESTIMATE_DETAIL, include_in_estimate=True, confidence=0.9, reasons=[]),
    ]
    body = walk_estimate_body(document, page_records, [], IdFactory(), SCHEMA, boilerplate=set())
    assert body.sections == []
    assert body.line_items == []
