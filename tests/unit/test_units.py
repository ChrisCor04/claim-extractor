from estimate_extractor.normalization.units import normalize_unit


def test_known_unit_uppercased():
    assert normalize_unit("sq") == ("SQ", True)
    assert normalize_unit("LF") == ("LF", True)
    assert normalize_unit("ea") == ("EA", True)


def test_unfamiliar_unit_preserved_uppercase_but_flagged_unknown():
    unit, known = normalize_unit("widgets")
    assert unit == "WIDGETS"
    assert known is False


def test_none_and_blank():
    assert normalize_unit(None) == (None, False)
    assert normalize_unit("") == (None, False)
