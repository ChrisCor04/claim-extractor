"""Cross-page continuation-state-machine tests (parsing/state_machine.py)."""

from pathlib import Path

from estimate_extractor.classification.pages import classify_document
from estimate_extractor.models.page import ParsedDocument, ParsedPage
from estimate_extractor.parsing.line_items import ColumnSchema
from estimate_extractor.parsing.state_machine import IdFactory, walk_estimate_body

SCHEMA = ColumnSchema(core_fields=("unit_price", "tax", "replacement_cost_value"))

PAGE_1_TEXT = """State Farm Claims
ARANDA, GENARO
Dwelling
Exterior
Dwelling Roof
3,366.04 Surface Area
QUANTITY
UNIT PRICE
TAX
RCV
AGE/LIFE
DEPREC.
ACV
CONDITION
DEP %
1.  Tear off, haul and dispose of comp. shingles - Laminated
33.66 SQ
68.75
0.00
2,314.13
2,314.13
2.  Laminated - comp. shingle rfg. - w/out felt
35.33 SQ
277.38
371.51
10,171.35
2/30 yrs
(678.09)
9,493.26
Avg.
6.67%
"""

PAGE_2_TEXT = """State Farm Claims
ARANDA, GENARO
CONTINUED - Dwelling Roof
QUANTITY
UNIT PRICE
RCV
ACV
TAX
AGE/LIFE
DEPREC.
CONDITION
DEP %
3.  Hip / Ridge cap - Standard profile - composition shingles
143.00 LF
6.74
36.45
1,000.27
2/30 yrs
(66.68)
933.59
Avg.
6.67%
Totals:  Dwelling Roof
407.96
13,485.75
744.77
12,740.98
"""


def _document() -> ParsedDocument:
    pages = [
        ParsedPage(page_number=1, width=612, height=792, raw_text=PAGE_1_TEXT),
        ParsedPage(page_number=2, width=612, height=792, raw_text=PAGE_2_TEXT),
    ]
    return ParsedDocument(source_path=Path("test.pdf"), sha256="deadbeef", page_count=2, pages=pages)


def test_line_items_on_continuation_page_attach_to_same_section():
    document = _document()
    page_records = classify_document(document.pages)
    id_factory = IdFactory()
    boilerplate = {"state farm claims", "aranda, genaro"}

    body = walk_estimate_body(document, page_records, [], id_factory, SCHEMA, boilerplate)

    assert len(body.sections) == 1
    section = body.sections[0]
    assert section.name == "Dwelling Roof"
    assert section.source_pages == [1, 2]
    assert section.continued_from_page == 1
    assert section.measurements.surface_area_sf == 3366.04

    assert len(body.line_items) == 3
    assert all(li.section_id == section.section_id for li in body.line_items)
    assert [li.source_line_number for li in body.line_items] == [1, 2, 3]

    # The section-closing total on the continuation page is correctly
    # attributed back to the one section spanning both pages.
    section_totals = [st for st in body.summary_totals if st.section_id == section.section_id]
    assert len(section_totals) == 1
    assert section_totals[0].replacement_cost_value == 13485.75
