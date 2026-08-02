"""Area/section header detection and measurement-block parsing.

Observed layout (consistent across all five carriers in the fixture set):
a short standalone label line (e.g. "Dwelling Roof", "Front Elevation",
"ROOF1"), optionally followed by a run of "<number> <measurement phrase>"
lines (e.g. "3,366.04 Surface Area"), followed by the QUANTITY column
header and then numbered line items.

A label line with no measurement/item content of its own, immediately
followed by another label line that DOES have content, acts as an AREA
grouping the section(s) that follow (e.g. Aranda: "Dwelling" is the area,
"Exterior" is the first section in it). A label line that is immediately
followed by content is a SECTION in its own right, with no known area
(area_id stays null rather than being guessed -- see docs/canonical-schema.md
"Known limitations").
"""

from __future__ import annotations

import re
from decimal import Decimal

from estimate_extractor.models.canonical import Area, Section, SectionMeasurements
from estimate_extractor.normalization.money import parse_decimal
from estimate_extractor.normalization.units import KNOWN_UNITS

LABEL_LINE_RE = re.compile(r"^[A-Z0-9][A-Za-z0-9/&,'._\- ]{1,49}$")

# Roof-diagram noise that otherwise matches LABEL_LINE_RE's generic shape:
# fascia/facet labels (F1, F12), bare parenthesized diagram markers
# ((A), (1)), and pure feet/inches dimension tokens (42', 32' 6", 10").
_FASCIA_LABEL_RE = re.compile(r"^F\d{1,2}$", re.IGNORECASE)
_SHORT_DIAGRAM_TOKEN_RE = re.compile(r"^\(?[A-Za-z0-9]{1,2}\)?$")
_FEET_INCHES_ONLY_RE = re.compile(r"""^\d+'(\s*\d+"?)?$|^\d+"$""")
_REAL_WORD_RE = re.compile(r"[A-Za-z]{3,}")

# Fixed, standard Xactimate sketch-annotation phrases: every wall
# opening (window/door/missing-wall) in a room/roof sketch is annotated
# with one of these, immediately followed by a "<dim> X <dim>" opening
# size and then this phrase. They are real English phrases (so
# _REAL_WORD_RE alone doesn't exclude them) but are universal Xactimate
# terminology, not section names -- confirmed against the Garrety fixture,
# where "Opens into Exterior" (repeated ~40 times as sketch annotations for
# window/wall openings) was otherwise hijacking section detection and
# stealing line items that belonged to the real "Ext_Surfaces" section.
_SKETCH_ANNOTATION_RE = re.compile(r"^(opens?\s+into\b|missing\s+wall\b)", re.IGNORECASE)

CONTINUED_RE = re.compile(r"^continued\s*-\s*(.+)$", re.IGNORECASE)

CATEGORY_HEADING_RE = re.compile(r"^\*{2,3}(.+?)\*{2,3}$")

# Multi-word measurement phrases -> canonical SectionMeasurements field.
# Matched by substring against the lowercased phrase following the number,
# longest/most-specific phrases first.
MEASUREMENT_LABEL_MAP: list[tuple[str, str]] = [
    ("walls & ceiling", "walls_and_ceiling_sf"),
    ("walls and ceiling", "walls_and_ceiling_sf"),
    ("surface area", "surface_area_sf"),
    ("total perimeter length", "perimeter_lf"),
    ("exterior perimeter of walls", "perimeter_lf"),
    ("total ridge length", "ridge_lf"),
    ("total hip length", "hip_lf"),
    ("number of squares", "number_of_squares"),
    ("floor perimeter", "floor_perimeter_lf"),
    ("ceil. perimeter", "ceiling_perimeter_lf"),
    ("ceiling perimeter", "ceiling_perimeter_lf"),
    ("sf walls", "wall_area_sf"),
    ("exterior wall area", "wall_area_sf"),
    ("sf ceiling", "ceiling_area_sf"),
    ("sf floor", "floor_area_sf"),
]


def is_label_line(line: str) -> bool:
    s = line.strip()
    if not s or len(s) > 50:
        return False
    if not LABEL_LINE_RE.match(s):
        return False
    # Reject anything that is itself a recognizable structured token.
    if s.lower() in {"quantity", "unit", "unit price", "tax", "o&p", "rcv", "acv", "description"}:
        return False
    # Reject roof-diagram noise (fascia labels, bare dimension tokens).
    if _FASCIA_LABEL_RE.match(s) or _SHORT_DIAGRAM_TOKEN_RE.match(s) or _FEET_INCHES_ONLY_RE.match(s):
        return False
    # Reject standard Xactimate sketch-opening annotations.
    if _SKETCH_ANNOTATION_RE.match(s):
        return False
    # A genuine area/section/category label always contains at least one
    # real word (>=3 letters) or is a known short alphanumeric code like
    # "ROOF1"/"HVAC" -- this excludes bare numbers, dates, and diagram
    # tokens that happen to fit the generic character-class shape above.
    if _REAL_WORD_RE.search(s):
        return True
    return False


def match_continued(line: str) -> str | None:
    m = CONTINUED_RE.match(line.strip())
    return m.group(1).strip() if m else None


def match_category_heading(line: str) -> str | None:
    m = CATEGORY_HEADING_RE.match(line.strip())
    return m.group(1).strip() if m else None


def is_real_quantity_unit(unit_raw: str) -> bool:
    """Distinguish a genuine line-item quantity unit ('SQ', 'LF', 'EA', a
    short unfamiliar code) from a multi-word measurement-block label
    ('Surface Area', 'Total Ridge Length') that happens to match the same
    '<number> <words>' shape."""
    u = unit_raw.strip()
    if not u:
        return False
    if u.upper() in KNOWN_UNITS:
        return True
    # Unfamiliar but short, single-token unit (carrier-specific code).
    return " " not in u and len(u) <= 4


def parse_measurement_field(quantity_raw: str, unit_raw: str) -> tuple[str, Decimal] | None:
    label = unit_raw.strip().lower()
    value = parse_decimal(quantity_raw)
    if value is None:
        return None
    for phrase, field_name in MEASUREMENT_LABEL_MAP:
        if phrase in label:
            return field_name, value
    return None


def new_section(
    *,
    section_id: str,
    name: str,
    coverage_id: str | None,
    area_id: str | None,
    page: int,
    confidence: float = 0.85,
) -> Section:
    return Section(
        section_id=section_id,
        coverage_id=coverage_id,
        area_id=area_id,
        name=name,
        section_type=None,
        parent_section_id=None,
        continued_from_page=None,
        source_pages=[page],
        measurements=SectionMeasurements(),
        confidence=confidence,
    )


def new_area(
    *,
    area_id: str,
    name: str,
    coverage_id: str | None,
    page: int,
    confidence: float = 0.8,
) -> Area:
    return Area(
        area_id=area_id,
        coverage_id=coverage_id,
        name=name,
        area_type=None,
        parent_area_id=None,
        source_pages=[page],
        confidence=confidence,
    )


def apply_measurements(section: Section, measurements: dict[str, Decimal]) -> None:
    if not measurements:
        return
    current = section.measurements.model_dump()
    for field_name, value in measurements.items():
        current[field_name] = float(value)
    section.measurements = SectionMeasurements(**current)
