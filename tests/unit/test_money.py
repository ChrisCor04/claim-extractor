from decimal import Decimal

from estimate_extractor.models.canonical import DepreciationType
from estimate_extractor.normalization.money import (
    bracket_style,
    classify_depreciation,
    is_money_token,
    parse_decimal,
    parse_percent,
)


def test_parse_plain_currency():
    assert parse_decimal("$14,582.40") == Decimal("14582.40")


def test_parse_parenthesized_currency_returns_magnitude():
    assert parse_decimal("(9,981.19)") == Decimal("9981.19")


def test_parse_angle_bracket_currency_returns_magnitude():
    assert parse_decimal("<409.84>") == Decimal("409.84")


def test_parse_na_returns_none():
    assert parse_decimal("NA") is None
    assert parse_decimal("N/A") is None
    assert parse_decimal("") is None
    assert parse_decimal(None) is None


def test_parse_percent():
    assert parse_percent("23.33%") == Decimal("23.33")
    assert parse_percent("10.0%") == Decimal("10.0")
    assert parse_percent("NA") is None


def test_parse_percent_strips_depreciation_method_tag():
    assert parse_percent("33.3% [%]") == Decimal("33.3")


def test_bracket_style():
    assert bracket_style("(1.62)") == "paren"
    assert bracket_style("<258.27>") == "angle"
    assert bracket_style("92.99") == "plain"


def test_classify_depreciation_recoverable():
    value, dtype = classify_depreciation("(162.15)")
    assert value == Decimal("162.15")
    assert dtype == DepreciationType.RECOVERABLE


def test_classify_depreciation_nonrecoverable():
    value, dtype = classify_depreciation("<258.27>")
    assert value == Decimal("258.27")
    assert dtype == DepreciationType.NONRECOVERABLE


def test_classify_depreciation_zero_is_none_type_regardless_of_bracket():
    value, dtype = classify_depreciation("(0.00)")
    assert value == Decimal("0.00")
    assert dtype == DepreciationType.NONE

    value, dtype = classify_depreciation("0.00")
    assert value == Decimal("0.00")
    assert dtype == DepreciationType.NONE


def test_classify_depreciation_plain_positive_is_ambiguous():
    value, dtype = classify_depreciation("162.15")
    assert value == Decimal("162.15")
    assert dtype == DepreciationType.UNKNOWN


def test_classify_depreciation_absent():
    value, dtype = classify_depreciation(None)
    assert value is None
    assert dtype == DepreciationType.UNKNOWN


def test_is_money_token():
    assert is_money_token("68.75")
    assert is_money_token("(1.62)")
    assert is_money_token("<258.27>")
    assert is_money_token("9.98 *")
    assert not is_money_token("SQ")
    assert not is_money_token("2/25 yrs")
