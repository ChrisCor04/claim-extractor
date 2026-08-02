from estimate_extractor.normalization.dates import normalize_date


def test_date_only():
    assert normalize_date("4/27/2026") == "2026-04-27"


def test_datetime_with_am_pm():
    assert normalize_date("6/4/2026 9:57 AM") == "2026-06-04T09:57:00"


def test_datetime_pm():
    assert normalize_date("10/10/2011 3:00 PM") == "2011-10-10T15:00:00"


def test_none_and_blank():
    assert normalize_date(None) is None
    assert normalize_date("") is None
    assert normalize_date("NA") is None


def test_unparseable_returns_none():
    assert normalize_date("not a date") is None
