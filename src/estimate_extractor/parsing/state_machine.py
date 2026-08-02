"""Document-order ID assignment and the cross-page estimate-body walk.

This is the state machine referenced throughout the spec: it walks every
ESTIMATE_DETAIL / ESTIMATE_DETAIL_CONTINUATION page in page order, keeping
track of the currently "open" area and section so that line items on a
continuation page (marked "CONTINUED - <section name>", or simply
following on from an unclosed section on the next page) attach to the same
section object rather than starting a new one.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from estimate_extractor.models.canonical import (
    Area,
    Coverage,
    LineItem,
    LineItemConfidence,
    LineItemFlags,
    LineItemSource,
    Note,
    Section,
    SummaryTotal,
    SummaryType,
)
from estimate_extractor.models.page import (
    ESTIMATE_DETAIL_CLASSIFICATIONS,
    PageClassification,
    ParsedDocument,
    PageRecord,
)
from estimate_extractor.parsing.line_items import ColumnSchema, assemble_line_item, compute_confidence, draft_flags
from estimate_extractor.parsing.sections import (
    apply_measurements,
    is_label_line,
    is_real_quantity_unit,
    match_category_heading,
    match_continued,
    new_area,
    new_section,
    parse_measurement_field,
)
from estimate_extractor.parsing.notes import is_note_like_phrase
from estimate_extractor.parsing.totals import assemble_summary_total
from estimate_extractor.pdf.tables import TokenKind, classify_line

# Per-page boilerplate that varies by page (so it never repeats often enough
# to be caught by the frequency-based boilerplate detector in pipeline.py)
# but is structurally always boilerplate: page-number and date footer/header
# lines. Skipped in lookahead alongside the exact-string boilerplate set.
_PAGE_NUMBER_LINE_RE = re.compile(r"^page:?\s*\d+$", re.IGNORECASE)
_DATE_LINE_RE = re.compile(r"^date:\s", re.IGNORECASE)


def _is_skippable_in_lookahead(line: str, boilerplate: set[str]) -> bool:
    s = line.strip()
    if not s:
        return True
    low = s.lower()
    if low in boilerplate:
        return True
    return bool(_PAGE_NUMBER_LINE_RE.match(s) or _DATE_LINE_RE.match(s))


class IdFactory:
    def __init__(self) -> None:
        self._counters: dict[str, int] = defaultdict(int)

    def next(self, prefix: str, width: int = 3) -> str:
        self._counters[prefix] += 1
        return f"{prefix}_{self._counters[prefix]:0{width}d}"

    def next_line_item(self) -> str:
        return self.next("line", width=4)


@dataclass(slots=True)
class EstimateBody:
    areas: list[Area] = field(default_factory=list)
    sections: list[Section] = field(default_factory=list)
    line_items: list[LineItem] = field(default_factory=list)
    summary_totals: list[SummaryTotal] = field(default_factory=list)


def _default_coverage_id(coverages: list[Coverage]) -> str | None:
    if len(coverages) == 1:
        return coverages[0].coverage_id
    return None


def _to_float(v: Decimal | None) -> float | None:
    return None if v is None else float(v)


def walk_estimate_body(
    document: ParsedDocument,
    page_records: list[PageRecord],
    coverages: list[Coverage],
    id_factory: IdFactory,
    schema: ColumnSchema,
    boilerplate: set[str],
    low_confidence_threshold: float = 0.80,
) -> EstimateBody:
    default_coverage_id = _default_coverage_id(coverages)

    areas_list: list[Area] = []
    sections_list: list[Section] = []
    line_items: list[LineItem] = []
    summary_totals: list[SummaryTotal] = []

    areas_by_name: dict[str, Area] = {}
    sections_by_name: dict[str, Section] = {}

    open_area: Area | None = None
    open_section: Section | None = None
    current_category_heading: str | None = None
    # Set once the document-wide "Line Item Totals:" row is seen; the
    # coverage recap table that typically follows it on the same page
    # (bare coverage-name rows with no "Totals:" prefix) is redundant with
    # what coverages.py already extracted from the "Summary for <Coverage>"
    # blocks, so we stop trying to interpret trailing label lines as new
    # sections/areas once we're past it.
    past_estimate_body = False
    # A label can appear as the very last line of a page with nothing after
    # it on that page (lookahead finds nothing), and the next page can begin
    # directly with the QUANTITY table / measurements with no repeated
    # label at all (confirmed against the Bagi fixture: "Fence" is the last
    # line of page 3; page 4 opens straight into "DESCRIPTION"/"QUANTITY").
    # Remember such a label and resolve it against the start of the next
    # page instead of discarding it.
    pending_cross_page_label: str | None = None

    # A section's label + measurement block can legitimately land on a page
    # that has no QUANTITY table of its own and is therefore classified
    # roof_diagram/measurement_summary rather than estimate_detail (its
    # line items start fresh on the next page). Confirmed against the
    # Garrety fixture: "Ext_Surfaces" + its SF Walls/Ceiling/Floor Perimeter
    # measurements sit at the tail of a roof-diagram-dominated page, with
    # the item table beginning on the following page. Walking these pages
    # too (for label/measurement detection only -- they have no numbered
    # items to misinterpret) lets that section register correctly instead
    # of falling back to an "Unlabeled" section on the next page.
    walkable_classifications = ESTIMATE_DETAIL_CLASSIFICATIONS | {
        PageClassification.ROOF_DIAGRAM,
        PageClassification.MEASUREMENT_SUMMARY,
    }
    detail_pages = sorted(
        (pr for pr in page_records if pr.classification in walkable_classifications),
        key=lambda pr: pr.page,
    )

    for page_record in detail_pages:
        page = document.pages[page_record.page - 1]
        lines = page.raw_text.splitlines()
        idx = 0
        n = len(lines)

        if pending_cross_page_label is not None:
            peek_idx = 0
            while peek_idx < n and _is_skippable_in_lookahead(lines[peek_idx], boilerplate):
                peek_idx += 1
            peek_tok = classify_line(lines[peek_idx]) if peek_idx < n else None
            if peek_tok is not None and (
                peek_tok.kind == TokenKind.HEADER_KEYWORD
                or (peek_tok.kind == TokenKind.QTY_UNIT and not is_real_quantity_unit(peek_tok.unit_raw or ""))
            ):
                key = pending_cross_page_label.lower()
                if key in sections_by_name:
                    open_section = sections_by_name[key]
                    if page_record.page not in open_section.source_pages:
                        open_section.source_pages.append(page_record.page)
                else:
                    sec = new_section(
                        section_id=id_factory.next("section"),
                        name=pending_cross_page_label,
                        coverage_id=open_area.coverage_id if open_area else default_coverage_id,
                        area_id=open_area.area_id if open_area else None,
                        page=page_record.page,
                    )
                    sections_by_name[key] = sec
                    sections_list.append(sec)
                    open_section = sec
                current_category_heading = None
            pending_cross_page_label = None

        while idx < n:
            raw = lines[idx]
            s = raw.strip()
            if not s or s.lower() in boilerplate:
                idx += 1
                continue

            tok = classify_line(raw)

            if tok.kind == TokenKind.ITEM_START:
                draft, idx = assemble_line_item(lines, idx, schema)

                if open_section is None:
                    sec_id = id_factory.next("section")
                    open_section = new_section(
                        section_id=sec_id,
                        name=f"Unlabeled ({page_record.page})",
                        coverage_id=open_area.coverage_id if open_area else default_coverage_id,
                        area_id=open_area.area_id if open_area else None,
                        page=page_record.page,
                        confidence=0.4,
                    )
                    sections_list.append(open_section)
                elif page_record.page not in open_section.source_pages:
                    # Section stayed open across a page boundary with no
                    # explicit "CONTINUED -" marker (e.g. its label lives on
                    # a preceding roof-diagram/measurement page and its
                    # items start fresh on the next page) -- still record
                    # every page its content actually appears on.
                    open_section.source_pages.append(page_record.page)

                confidences = compute_confidence(draft, schema)
                review_reasons = list(draft.review_reasons)
                needs_review = confidences["overall"] < low_confidence_threshold or bool(review_reasons)

                flags_dict = draft_flags(draft)
                notes = [
                    Note(note_type=nd.note_type, text=nd.text, source_pages=[page_record.page], confidence=0.85)
                    for nd in draft.notes
                ]

                line_item = LineItem(
                    line_item_id=id_factory.next_line_item(),
                    source_line_number=draft.item_number,
                    coverage_id=open_section.coverage_id,
                    area_id=open_section.area_id,
                    section_id=open_section.section_id,
                    category_heading=current_category_heading,
                    description=draft.description,
                    description_normalized_whitespace=draft.description,
                    quantity=_to_float(draft.quantity),
                    unit_of_measure=draft.unit_of_measure,
                    unit_price=_to_float(draft.unit_price),
                    tax=_to_float(draft.tax),
                    overhead_and_profit=_to_float(draft.overhead_and_profit),
                    replacement_cost_value=_to_float(draft.replacement_cost_value),
                    age=draft.age,
                    life_expectancy=draft.life_expectancy,
                    condition=draft.condition,
                    depreciation_percent=_to_float(draft.depreciation_percent),
                    depreciation_amount=_to_float(draft.depreciation_amount),
                    depreciation_type=draft.depreciation_type,  # type: ignore[arg-type]
                    actual_cash_value=_to_float(draft.actual_cash_value),
                    notes=notes,
                    flags=LineItemFlags(**flags_dict),
                    source=LineItemSource(
                        page_start=page_record.page,
                        page_end=page_record.page,
                        raw_text=draft.raw_text,
                    ),
                    confidence=LineItemConfidence(**confidences),
                    needs_review=needs_review,
                    review_reasons=review_reasons,
                )
                line_items.append(line_item)
                continue

            if tok.kind == TokenKind.TOTALS_LABEL:
                total_draft, idx = assemble_summary_total(lines, idx, schema)
                name = total_draft.name
                label_lower = total_draft.label.lower()

                summary_type = SummaryType.SECTION_TOTAL
                target_section_id = open_section.section_id if open_section else None
                target_area_id = open_area.area_id if open_area else None
                target_coverage_id = (
                    open_section.coverage_id if open_section else (open_area.coverage_id if open_area else default_coverage_id)
                )

                if label_lower.startswith("line item totals"):
                    summary_type = SummaryType.LINE_ITEM_TOTAL
                    target_section_id = None
                    target_area_id = None
                    open_section = None
                    open_area = None
                    past_estimate_body = True
                elif label_lower.startswith("grand total"):
                    summary_type = SummaryType.GRAND_TOTAL
                    target_section_id = None
                    target_area_id = None
                elif name and open_area and name.lower() == open_area.name.lower():
                    summary_type = SummaryType.AREA_TOTAL
                    target_section_id = None
                    open_area = None
                elif name and open_section and name.lower() == open_section.name.lower():
                    summary_type = SummaryType.SECTION_TOTAL
                    open_section = None
                elif name and name.lower() in sections_by_name:
                    summary_type = SummaryType.SECTION_TOTAL
                    target_section_id = sections_by_name[name.lower()].section_id
                elif name and name.lower() in areas_by_name:
                    summary_type = SummaryType.AREA_TOTAL
                    target_area_id = areas_by_name[name.lower()].area_id
                    target_section_id = None

                summary_totals.append(
                    SummaryTotal(
                        summary_id=id_factory.next("summary"),
                        summary_type=summary_type,
                        coverage_id=target_coverage_id,
                        area_id=target_area_id,
                        section_id=target_section_id,
                        label=total_draft.label,
                        tax=_to_float(total_draft.tax),
                        replacement_cost_value=_to_float(total_draft.replacement_cost_value),
                        depreciation=_to_float(total_draft.depreciation),
                        actual_cash_value=_to_float(total_draft.actual_cash_value),
                        source_page=page_record.page,
                    )
                )
                continue

            continued_name = match_continued(s)
            if continued_name:
                match = sections_by_name.get(continued_name.lower())
                if match:
                    open_section = match
                    if page_record.page not in open_section.source_pages:
                        open_section.source_pages.append(page_record.page)
                    if open_section.continued_from_page is None:
                        open_section.continued_from_page = page_record.page - 1
                idx += 1
                continue

            category = match_category_heading(s)
            if category:
                current_category_heading = category
                idx += 1
                continue

            if past_estimate_body:
                idx += 1
                continue

            if (
                tok.kind == TokenKind.TEXT
                and is_label_line(s)
                and not is_note_like_phrase(s)
                and s not in page.grid_annotation_texts
            ):
                # Collapse immediately-repeated label lines (Xactimate
                # sometimes prints a section/area name twice in a row).
                while idx + 1 < n and lines[idx + 1].strip().lower() == s.lower():
                    idx += 1

                next_idx = idx + 1
                while next_idx < n and not lines[next_idx].strip():
                    next_idx += 1
                # Boilerplate (repeated letterhead/claim-header lines) is
                # only skipped when it isn't already a recognized
                # structural token -- a HEADER_KEYWORD/ITEM_START/etc. must
                # never be skipped over, since it's exactly the signal this
                # lookahead is trying to find.
                while (
                    next_idx < n
                    and _is_skippable_in_lookahead(lines[next_idx], boilerplate)
                    and classify_line(lines[next_idx]).kind == TokenKind.TEXT
                ):
                    next_idx += 1
                    while next_idx < n and not lines[next_idx].strip():
                        next_idx += 1
                next_tok = classify_line(lines[next_idx]) if next_idx < n else None
                next_text = lines[next_idx].strip() if next_idx < n else ""

                if next_tok is None:
                    # Nothing else on this page: defer the decision to the
                    # start of the next page instead of discarding it (see
                    # ``pending_cross_page_label`` above).
                    pending_cross_page_label = s
                    idx += 1
                    continue

                if (
                    next_tok.kind == TokenKind.TEXT
                    and is_label_line(next_text)
                    and not is_note_like_phrase(next_text)
                    and next_text not in page.grid_annotation_texts
                ):
                    # Bare wrapper label with no content of its own: an AREA.
                    key = s.lower()
                    if key in areas_by_name:
                        open_area = areas_by_name[key]
                    else:
                        a = new_area(
                            area_id=id_factory.next("area"),
                            name=s,
                            coverage_id=default_coverage_id,
                            page=page_record.page,
                        )
                        areas_by_name[key] = a
                        areas_list.append(a)
                        open_area = a
                    idx += 1
                    continue

                if next_tok.kind == TokenKind.HEADER_KEYWORD or (
                    next_tok.kind == TokenKind.QTY_UNIT and not is_real_quantity_unit(next_tok.unit_raw or "")
                ):
                    # A QUANTITY header or a measurement block follows: this
                    # label is a SECTION with its own content.
                    pass  # handled below
                elif next_tok.kind == TokenKind.ITEM_START:
                    # Items follow directly with no header/measurements: a
                    # sub-category heading within the currently open section.
                    current_category_heading = s
                    idx += 1
                    continue
                else:
                    idx += 1
                    continue

                key = s.lower()
                if key in sections_by_name:
                    open_section = sections_by_name[key]
                    if page_record.page not in open_section.source_pages:
                        open_section.source_pages.append(page_record.page)
                else:
                    sec = new_section(
                        section_id=id_factory.next("section"),
                        name=s,
                        coverage_id=open_area.coverage_id if open_area else default_coverage_id,
                        area_id=open_area.area_id if open_area else None,
                        page=page_record.page,
                    )
                    sections_by_name[key] = sec
                    sections_list.append(sec)
                    open_section = sec
                current_category_heading = None

                m_idx = idx + 1
                measurements: dict[str, Decimal] = {}
                while m_idx < n:
                    mtok = classify_line(lines[m_idx])
                    if mtok.kind == TokenKind.BLANK:
                        m_idx += 1
                        continue
                    if mtok.kind == TokenKind.QTY_UNIT and not is_real_quantity_unit(mtok.unit_raw or ""):
                        parsed = parse_measurement_field(mtok.quantity_raw or "", mtok.unit_raw or "")
                        if parsed:
                            measurements[parsed[0]] = parsed[1]
                        m_idx += 1
                        continue
                    break
                if measurements:
                    apply_measurements(open_section, measurements)
                idx = m_idx
                continue

            idx += 1

    return EstimateBody(
        areas=areas_list, sections=sections_list, line_items=line_items, summary_totals=summary_totals
    )
