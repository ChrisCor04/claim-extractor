from estimate_extractor.mapping.normalizer import clean_description, extract_attributes, normalize_line_item
from estimate_extractor.mapping.pipeline import DEFAULT_CONFIG_DIR
from estimate_extractor.mapping.rules_config import load_normalization_rules


def _rules():
    return load_normalization_rules(DEFAULT_CONFIG_DIR / "normalization_rules.yaml")


def test_clean_description_lowercases_and_collapses_whitespace():
    assert clean_description("  R&R   Gutter  -  aluminum ") == "r&r gutter - aluminum"


def test_clean_description_preserves_dimensions_and_quotes():
    text = 'R&R Overhead door & hardware - 16\' x 7\''
    cleaned = clean_description(text)
    assert "16' x 7'" in cleaned


def test_never_alters_quantity_or_unit():
    rules = _rules()
    normalized, _, _, _ = normalize_line_item("R&R Gutter", 200.0, "LF", rules)
    assert normalized.quantity == 200.0
    assert normalized.unit_of_measure == "LF"


def test_felt_excluded_attribute_extracted():
    rules = _rules()
    normalized, _, _, _ = normalize_line_item("Laminated - comp. shingle rfg. - w/out felt", 35.33, "SQ", rules)
    assert normalized.attributes.get("felt_included") is False


def test_felt_included_attribute_extracted():
    rules = _rules()
    normalized, _, _, _ = normalize_line_item("3 tab 25 yr comp shng. w/ felt", 1.0, "SQ", rules)
    assert normalized.attributes.get("felt_included") is True


def test_tab_count_attribute_extracted():
    attrs = extract_attributes("3 tab 25 yr comp shng. w/out felt- per ind material source", _rules())
    assert attrs.get("tab_count") == 3


def test_weight_lb_attribute_extracted():
    attrs = extract_attributes("roofing felt - 15 lb.", _rules())
    assert attrs.get("weight_lb") == 15.0


def test_slope_range_attribute_extracted():
    attrs = extract_attributes("additional charge for steep roof - 7/12 to 9/12 slope", _rules())
    assert attrs.get("slope_range") == "7/12 to 9/12"


def test_dimension_text_preserved_in_attributes():
    attrs = extract_attributes("r&r overhead door & hardware - 16' x 7'", _rules())
    assert "dimensions" in attrs
    assert "16'" in attrs["dimensions"] and "7'" in attrs["dimensions"]


def test_unknown_description_never_invents_a_concept():
    rules = _rules()
    normalized, confidence, needs_review, reasons = normalize_line_item(
        "Completely unrecognizable gibberish xyz123", 1.0, "EA", rules
    )
    assert normalized.action.value == "unknown"
    assert normalized.trade.value == "unknown"
    assert normalized.component == "unknown"
    assert needs_review is True
    assert reasons  # non-empty: something explains why


def test_full_normalization_of_real_fixture_item():
    rules = _rules()
    normalized, confidence, needs_review, reasons = normalize_line_item(
        'R&R Gutter - aluminum - up to 5"', 200.0, "LF", rules
    )
    assert normalized.action.value == "remove_and_replace"
    assert normalized.trade.value == "gutters"
    assert normalized.component == "gutter"
    assert confidence.overall > 0.5
