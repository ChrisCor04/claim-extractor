from decimal import Decimal

from estimate_extractor.config import ValidationConfig
from estimate_extractor.validation.arithmetic import within_tolerance


def test_within_absolute_tolerance():
    config = ValidationConfig(money_absolute_tolerance=Decimal("0.05"), percentage_tolerance=Decimal("0.001"))
    result = within_tolerance(Decimal("100.00"), Decimal("100.04"), config)
    assert result.within_tolerance is True


def test_outside_absolute_but_within_percentage_tolerance():
    config = ValidationConfig(money_absolute_tolerance=Decimal("0.05"), percentage_tolerance=Decimal("0.01"))
    # 1000.00 vs 1005.00 is a 0.5% difference, within a 1% tolerance.
    result = within_tolerance(Decimal("1000.00"), Decimal("1005.00"), config)
    assert result.within_tolerance is True


def test_outside_both_tolerances():
    config = ValidationConfig(money_absolute_tolerance=Decimal("0.05"), percentage_tolerance=Decimal("0.001"))
    result = within_tolerance(Decimal("100.00"), Decimal("150.00"), config)
    assert result.within_tolerance is False
    assert result.difference == Decimal("50.00")
