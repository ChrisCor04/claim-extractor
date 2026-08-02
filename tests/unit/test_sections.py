from decimal import Decimal

from estimate_extractor.parsing.sections import (
    is_label_line,
    is_real_quantity_unit,
    match_category_heading,
    match_continued,
    parse_measurement_field,
)


def test_is_label_line_accepts_real_section_names():
    assert is_label_line("Dwelling Roof")
    assert is_label_line("Front Elevation")
    assert is_label_line("HVAC")
    assert is_label_line("ROOF1")
    assert is_label_line("Fence")


def test_is_label_line_rejects_roof_diagram_noise():
    assert not is_label_line("F1")
    assert not is_label_line("F12")
    assert not is_label_line("(A)")
    assert not is_label_line("42'")
    assert not is_label_line('32\' 6"')
    assert not is_label_line('10"')
    assert not is_label_line("6/4/26")  # date fragment, no real words


def test_is_label_line_rejects_column_headers():
    assert not is_label_line("QUANTITY")
    assert not is_label_line("UNIT PRICE")


def test_is_real_quantity_unit():
    assert is_real_quantity_unit("SQ")
    assert is_real_quantity_unit("LF")
    assert not is_real_quantity_unit("Surface Area")
    assert not is_real_quantity_unit("Total Perimeter Length")


def test_parse_measurement_field():
    field, value = parse_measurement_field("3,366.04", "Surface Area")
    assert field == "surface_area_sf"
    assert value == Decimal("3366.04")

    field, value = parse_measurement_field("51.57", "Total Ridge Length")
    assert field == "ridge_lf"
    assert value == Decimal("51.57")


def test_parse_measurement_field_unrecognized_label_returns_none():
    assert parse_measurement_field("28.00", "SY Flooring") is None


def test_match_continued():
    assert match_continued("CONTINUED - Dwelling Roof") == "Dwelling Roof"
    assert match_continued("continued - roof1") == "roof1"
    assert match_continued("Dwelling Roof") is None


def test_match_category_heading():
    assert match_category_heading("***Roof Surface***") == "Roof Surface"
    assert match_category_heading("**Additional Charges**") == "Additional Charges"
    assert match_category_heading("Roof Surface") is None
