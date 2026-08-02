"""Regression tests for parsing/coverage_attribution.py.

Verifies the conservative-by-design contract: coverage_id is assigned only
when a UNIQUE exact partition of sections into the reported coverage totals
exists within tolerance; otherwise every coverage_id stays null and an
explanatory note is returned.
"""

from __future__ import annotations

from decimal import Decimal

from estimate_extractor.models.canonical import (
    Area,
    Coverage,
    CoverageSummaryFinancials,
    LineItem,
    LineItemConfidence,
    LineItemSource,
    Section,
)
from estimate_extractor.parsing.coverage_attribution import attribute_coverages

TOLERANCE = Decimal("0.05")


def _coverage(coverage_id: str, name: str, rcv: float) -> Coverage:
    return Coverage(
        coverage_id=coverage_id,
        name=name,
        summary=CoverageSummaryFinancials(replacement_cost_value=rcv),
        confidence=0.9,
    )


def _section(section_id: str, name: str, area_id: str | None = None) -> Section:
    return Section(section_id=section_id, name=name, area_id=area_id, confidence=0.9)


def _line_item(line_item_id: str, section_id: str, rcv: float) -> LineItem:
    return LineItem(
        line_item_id=line_item_id,
        section_id=section_id,
        description="Test item",
        description_normalized_whitespace="Test item",
        replacement_cost_value=rcv,
        source=LineItemSource(page_start=1, page_end=1, raw_text="raw"),
        confidence=LineItemConfidence(
            overall=0.9,
            description=0.9,
            quantity=0.9,
            unit_of_measure=0.9,
            unit_price=0.9,
            tax=0.9,
            replacement_cost_value=0.9,
            depreciation=0.9,
            actual_cash_value=0.9,
        ),
    )


def test_single_coverage_is_a_noop():
    coverages = [_coverage("coverage_001", "Dwelling", 1000.0)]
    sections = [_section("section_001", "Roof")]
    line_items = [_line_item("line_0001", "section_001", 1000.0)]
    result = attribute_coverages(coverages, [], sections, line_items, [], TOLERANCE)
    assert result.assigned_section_ids == frozenset()
    assert sections[0].coverage_id is None
    assert line_items[0].coverage_id is None


def test_clean_two_way_partition_is_assigned_and_cascades_to_line_items():
    coverages = [
        _coverage("coverage_001", "Dwelling", 300.0),
        _coverage("coverage_002", "Other Structures", 150.0),
    ]
    sections = [
        _section("section_001", "Roof"),
        _section("section_002", "Siding"),
        _section("section_003", "Fence"),
    ]
    line_items = [
        _line_item("line_0001", "section_001", 200.0),
        _line_item("line_0002", "section_002", 100.0),
        _line_item("line_0003", "section_003", 150.0),
    ]
    result = attribute_coverages(coverages, [], sections, line_items, [], TOLERANCE)

    assert result.assigned_section_ids == frozenset({"section_001", "section_002", "section_003"})
    assert sections[0].coverage_id == "coverage_001"  # Roof: 200 -> part of 300
    assert sections[1].coverage_id == "coverage_001"  # Siding: 100 -> part of 300
    assert sections[2].coverage_id == "coverage_002"  # Fence: 150 -> matches 150 exactly
    assert line_items[0].coverage_id == "coverage_001"
    assert line_items[2].coverage_id == "coverage_002"
    assert result.notes == []


def test_area_gets_coverage_id_only_when_all_its_sections_agree():
    coverages = [
        _coverage("coverage_001", "Dwelling", 100.0),
        _coverage("coverage_002", "Other Structures", 50.0),
    ]
    area = Area(area_id="area_001", name="Exterior", confidence=0.9)
    sections = [
        _section("section_001", "Roof", area_id="area_001"),
        _section("section_002", "Fence", area_id="area_001"),
    ]
    line_items = [
        _line_item("line_0001", "section_001", 100.0),
        _line_item("line_0002", "section_002", 50.0),
    ]
    attribute_coverages(coverages, [area], sections, line_items, [], TOLERANCE)
    # The two sections under this area land in DIFFERENT coverages, so the
    # area itself must stay null (never guess a single coverage for a mixed
    # area).
    assert area.coverage_id is None


def test_ambiguous_partition_leaves_everything_null_with_explanation():
    # Two sections of $100 each and two coverage targets of $100 each: two
    # equally valid assignments exist, so nothing should be assigned.
    coverages = [
        _coverage("coverage_001", "Dwelling", 100.0),
        _coverage("coverage_002", "Other Structures", 100.0),
    ]
    sections = [_section("section_001", "A"), _section("section_002", "B")]
    line_items = [_line_item("line_0001", "section_001", 100.0), _line_item("line_0002", "section_002", 100.0)]
    result = attribute_coverages(coverages, [], sections, line_items, [], TOLERANCE)
    assert result.assigned_section_ids == frozenset()
    assert sections[0].coverage_id is None
    assert sections[1].coverage_id is None
    assert len(result.notes) == 1
    assert "ambiguous" in result.notes[0].lower()


def test_duplicate_coverage_totals_are_skipped_as_unusable():
    # Two coverages report the exact same total -- matching a section's sum
    # to "one of the two identical values" is inherently ambiguous.
    coverages = [
        _coverage("coverage_001", "Dwelling", 100.0),
        _coverage("coverage_002", "Dwelling (Paid When Incurred)", 100.0),
    ]
    sections = [_section("section_001", "Roof")]
    line_items = [_line_item("line_0001", "section_001", 100.0)]
    result = attribute_coverages(coverages, [], sections, line_items, [], TOLERANCE)
    assert result.assigned_section_ids == frozenset()
    assert any("same total" in note for note in result.notes)


def test_no_partition_matches_leaves_everything_null():
    coverages = [
        _coverage("coverage_001", "Dwelling", 999.0),
        _coverage("coverage_002", "Other Structures", 888.0),
    ]
    sections = [_section("section_001", "Roof")]
    line_items = [_line_item("line_0001", "section_001", 123.45)]
    result = attribute_coverages(coverages, [], sections, line_items, [], TOLERANCE)
    assert result.assigned_section_ids == frozenset()
    assert sections[0].coverage_id is None
    assert len(result.notes) == 1
