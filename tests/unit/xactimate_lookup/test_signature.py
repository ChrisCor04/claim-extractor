from __future__ import annotations

from estimate_extractor.xactimate_lookup import signature as sig


def test_signature_stable_across_wording_variance(phrase_rules):
    s1 = sig.compute_item_signature(
        "roofing", "composition_shingles", "3-tab composition shingles", "remove", "SQ",
        "Tear off composition shingles - 3 tab (no haul off)", phrase_rules,
    )
    s2 = sig.compute_item_signature(
        "roofing", "composition_shingles", "3-tab composition shingles", "remove", "SQ",
        "Tear off comp shingles 3-tab", phrase_rules,
    )
    assert s1 == s2


def test_signature_differs_on_material(phrase_rules):
    s1 = sig.compute_item_signature("gutters", "gutter", "aluminum", "remove_and_replace", "LF", "R&R aluminum gutter", phrase_rules)
    s2 = sig.compute_item_signature("gutters", "gutter", "vinyl", "remove_and_replace", "LF", "R&R vinyl gutter", phrase_rules)
    assert s1 != s2


def test_signature_differs_on_size(phrase_rules):
    s1 = sig.compute_item_signature("gutters", "gutter", "aluminum", "remove_and_replace", "LF", "R&R aluminum gutter up to 5 inches", phrase_rules)
    s2 = sig.compute_item_signature("gutters", "gutter", "aluminum", "remove_and_replace", "LF", "R&R aluminum gutter up to 6 inches", phrase_rules)
    assert s1 != s2


def test_signature_differs_on_action(phrase_rules):
    s1 = sig.compute_item_signature("roofing", "composition_shingles", "laminated composition shingles", "remove", "SQ", "Tear off laminated shingles", phrase_rules)
    s2 = sig.compute_item_signature("roofing", "composition_shingles", "laminated composition shingles", "install", "SQ", "Install laminated shingles", phrase_rules)
    assert s1 != s2


def test_signature_differs_on_unit(phrase_rules):
    s1 = sig.compute_item_signature("roofing", "composition_shingles", None, "remove", "SQ", "Tear off shingles", phrase_rules)
    s2 = sig.compute_item_signature("roofing", "composition_shingles", None, "remove", "LF", "Tear off shingles", phrase_rules)
    assert s1 != s2


def test_signature_never_includes_price_like_tokens(phrase_rules):
    s = sig.compute_item_signature(
        "roofing", "composition_shingles", "laminated composition shingles", "remove", "SQ",
        "Tear off laminated shingles - $123.45 per SQ", phrase_rules,
    )
    assert "$" not in s
    assert "123.45" not in s


def test_signature_handles_missing_fields_without_crashing(phrase_rules):
    s = sig.compute_item_signature(None, None, None, None, None, "", phrase_rules)
    assert isinstance(s, str)
    assert "unknown" in s


def test_compute_normalized_description_prefers_component_material_action():
    from estimate_extractor.xactimate_lookup import signature as sig

    result = sig.compute_normalized_description("roofing", "composition_shingles", "3-tab composition shingles", "remove")
    assert result == "composition shingles 3-tab composition shingles remove"


def test_compute_normalized_description_falls_back_to_trade_when_nothing_else_present():
    from estimate_extractor.xactimate_lookup import signature as sig

    result = sig.compute_normalized_description("roofing", "unknown", None, "unknown")
    assert result == "roofing"


def test_compute_normalized_description_handles_all_missing():
    from estimate_extractor.xactimate_lookup import signature as sig

    assert sig.compute_normalized_description(None, None, None, None) == ""


def test_signature_distinguishes_leading_style_words(phrase_rules):
    """Regression guard: leading_style_keywords (3-tab, laminated) must
    still feed grade_key for signature purposes even though they're
    ordered before component in the search phrase -- otherwise two
    genuinely different shingle types would collapse onto one signature."""
    s1 = sig.compute_item_signature(
        "roofing", "composition_shingles", "3-tab composition shingles", "remove", "SQ",
        "Tear off composition shingles - 3 tab (no haul off)", phrase_rules,
    )
    s2 = sig.compute_item_signature(
        "roofing", "composition_shingles", "laminated composition shingles", "remove", "SQ",
        "Replace laminated composition shingles", phrase_rules,
    )
    assert s1 != s2
