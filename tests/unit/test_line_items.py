from decimal import Decimal

from estimate_extractor.parsing.line_items import ColumnSchema, assemble_line_item

STATE_FARM_SCHEMA = ColumnSchema(core_fields=("unit_price", "tax", "replacement_cost_value"))
USAA_SCHEMA = ColumnSchema(core_fields=("unit_price", "tax", "overhead_and_profit", "replacement_cost_value"))
ALLSTATE_SCHEMA = ColumnSchema(core_fields=("unit_price", "replacement_cost_value"))


def test_state_farm_style_row_with_depreciation():
    # Real row from the Aranda fixture (State Farm).
    lines = [
        '* 1.  R&R Gutter - aluminum - up to 5"',
        "200.00 LF",
        "9.80",
        "66.83",
        "2,026.83",
        "2/25 yrs",
        "(162.15)",
        "1,864.68",
        "Avg.",
        "8.00%",
        "Accounted for gutters present on eaves and cornice returns",
    ]
    draft, end_idx = assemble_line_item(lines, 0, STATE_FARM_SCHEMA)
    assert draft.item_number == 1
    assert draft.has_allowance_marker is True
    assert draft.quantity == Decimal("200.00")
    assert draft.unit_of_measure == "LF"
    assert draft.unit_price == Decimal("9.80")
    assert draft.tax == Decimal("66.83")
    assert draft.replacement_cost_value == Decimal("2026.83")
    assert draft.age == "2"
    assert draft.life_expectancy == "25"
    assert draft.depreciation_amount == Decimal("162.15")
    assert draft.depreciation_type == "recoverable"
    assert draft.actual_cash_value == Decimal("1864.68")
    assert draft.condition == "Avg."
    assert draft.depreciation_percent == Decimal("8.00")
    assert len(draft.notes) == 1
    assert "Accounted for gutters" in draft.notes[0].text
    assert end_idx == len(lines)


def test_row_with_no_depreciation_defaults_to_none_type():
    # Real row from the Aranda fixture: no age/life/deprec/cond/dep% at all.
    lines = [
        "3.  Tear off, haul and dispose of comp. shingles - Laminated",
        "33.66 SQ",
        "68.75",
        "0.00",
        "2,314.13",
        "2,314.13",
    ]
    draft, end_idx = assemble_line_item(lines, 0, STATE_FARM_SCHEMA)
    assert draft.replacement_cost_value == Decimal("2314.13")
    assert draft.actual_cash_value == Decimal("2314.13")
    assert draft.depreciation_amount == Decimal("0.00")
    assert draft.depreciation_type == "none"
    assert draft.age is None
    assert draft.condition is None
    assert end_idx == len(lines)


def test_usaa_style_row_with_overhead_and_profit_column():
    lines = [
        "1.  Remove 3 tab - 25 yr. - composition shingle roofing - incl. felt",
        "30.19 SQ",
        "68.34",
        "0.00",
        "515.80",
        "2,578.98",
        "0/25 yrs",
        "(0.00)",
        "2,578.98",
    ]
    draft, end_idx = assemble_line_item(lines, 0, USAA_SCHEMA)
    assert draft.unit_price == Decimal("68.34")
    assert draft.tax == Decimal("0.00")
    assert draft.overhead_and_profit == Decimal("515.80")
    assert draft.replacement_cost_value == Decimal("2578.98")
    assert draft.depreciation_type == "none"
    assert draft.actual_cash_value == Decimal("2578.98")
    assert end_idx == len(lines)


def test_allstate_style_row_no_tax_combined_age_condition():
    lines = [
        "1.  Remove Laminated - comp. shingle rfg. - w/",
        "30.69 SQ",
        "69.36",
        "2,128.66",
        "7/30 yrs Avg.",
        "NA",
        "(0.00)",
        "2,128.66",
        "felt",
    ]
    draft, end_idx = assemble_line_item(lines, 0, ALLSTATE_SCHEMA)
    assert draft.unit_price == Decimal("69.36")
    assert draft.tax is None  # Allstate's schema has no tax column at all
    assert draft.replacement_cost_value == Decimal("2128.66")
    assert draft.condition == "Avg."
    assert draft.age == "7"
    assert draft.life_expectancy == "30"
    # The wrapped description tail ("felt") is reconstructed, not dropped
    # or misread as a new item/note.
    assert draft.description == "Remove Laminated - comp. shingle rfg. - w/ felt"
    assert draft.description_reconstructed is True
    assert "possible_description_wrap" in draft.review_reasons
    assert end_idx == len(lines)


def test_category_heading_marker_is_not_absorbed_into_description():
    lines = [
        "6.  Drip edge",
        "247.46 LF",
        "3.18",
        "786.92",
        "7/35 yrs Avg.",
        "20%",
        "(157.38)",
        "629.54",
        "**Roof Components**",
        "7.  R&R Rain cap - 6\"",
    ]
    draft, end_idx = assemble_line_item(lines, 0, ALLSTATE_SCHEMA)
    assert draft.description == "Drip edge"
    assert draft.description_reconstructed is False
    # Stops before the category heading, handing it back to the caller.
    assert lines[end_idx] == "**Roof Components**"


def test_missing_fields_are_flagged_not_fabricated():
    lines = ["5.  Some mystery item with no data at all"]
    draft, end_idx = assemble_line_item(lines, 0, STATE_FARM_SCHEMA)
    assert draft.quantity is None
    assert draft.unit_price is None
    assert "missing_quantity" in draft.review_reasons
    assert "missing_unit_price" in draft.review_reasons
