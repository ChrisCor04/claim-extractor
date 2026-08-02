from estimate_extractor.normalization.text import (
    detect_verb_flags,
    normalize_whitespace,
    parse_item_number_line,
)


def test_normalize_whitespace_collapses_and_strips():
    assert normalize_whitespace("  a   b\tc\n") == "a b c"


def test_parse_item_number_line_plain():
    result = parse_item_number_line("3.  Tear off, haul and dispose of comp. shingles - Laminated")
    assert result == (False, 3, "Tear off, haul and dispose of comp. shingles - Laminated")


def test_parse_item_number_line_with_allowance_marker():
    result = parse_item_number_line('* 1.  R&R Gutter - aluminum - up to 5"')
    assert result == (True, 1, 'R&R Gutter - aluminum - up to 5"')


def test_parse_item_number_line_rejects_non_item_lines():
    assert parse_item_number_line("QUANTITY") is None
    assert parse_item_number_line("2026.  This is an average retail price") is None  # 4-digit year, not an item


def test_detect_verb_flags_remove_and_replace():
    flags = detect_verb_flags('R&R Gutter - aluminum - up to 5"')
    assert flags["is_remove_and_replace"] is True
    assert flags["is_remove_only"] is False


def test_detect_verb_flags_remove_only_tear_off():
    flags = detect_verb_flags("Tear off, haul and dispose of comp. shingles - Laminated")
    assert flags["is_remove_only"] is True


def test_detect_verb_flags_detach_and_reset():
    flags = detect_verb_flags("Detach & Reset Exhaust cap - through roof - up to 4\"")
    assert flags["is_detach_and_reset"] is True


def test_detect_verb_flags_labor_minimum():
    flags = detect_verb_flags("Drywall labor minimum")
    assert flags["is_labor_minimum"] is True


def test_detect_verb_flags_plain_description_sets_no_flags():
    flags = detect_verb_flags("Roofing felt - 15 lb.")
    assert not any(flags.values())
