from __future__ import annotations

from estimate_extractor.xactimate_lookup import phrase_generator as pg


def test_spec_worked_example(phrase_rules):
    r = pg.generate_search_phrase(
        "Remove and replace aluminum gutter/downspout up to 5 inches",
        normalized_component="unknown", normalized_material="aluminum", normalized_action="remove_and_replace",
        rules=phrase_rules,
    )
    assert r.phrase == "gutter aluminum up to 5"


def test_phrase_is_deterministic(phrase_rules):
    args = ("R&R Gutter - aluminum - up to 5\"", "gutter", "aluminum", "remove_and_replace")
    r1 = pg.generate_search_phrase(*args, rules=phrase_rules)
    r2 = pg.generate_search_phrase(*args, rules=phrase_rules)
    assert r1.phrase == r2.phrase


def test_compound_actions_are_dropped_by_default(phrase_rules):
    r = pg.generate_search_phrase("Detach & Reset vinyl window", "window", "vinyl", "detach_and_reset", rules=phrase_rules)
    assert "detach" not in r.phrase
    assert "reset" not in r.phrase


def test_meaningful_action_is_kept(phrase_rules):
    r = pg.generate_search_phrase("Clean with pressure/chemical spray - siding", "siding", None, "pressure_wash", rules=phrase_rules)
    assert "pressure wash" in r.phrase


def test_duplicate_words_across_buckets_are_removed(phrase_rules):
    r = pg.generate_search_phrase(
        "Tear off composition shingles - 3 tab (no haul off)",
        "composition_shingles", "3-tab composition shingles", "remove",
        rules=phrase_rules,
    )
    words = r.phrase.split()
    assert len(words) == len(set(words)), f"duplicate words in phrase: {r.phrase!r}"


def test_hyphenated_style_and_spaced_style_canonicalize_together(phrase_rules):
    r1 = pg.generate_search_phrase("3-tab shingle roof", "composition_shingles", None, None, rules=phrase_rules)
    r2 = pg.generate_search_phrase("3 tab shingle roof", "composition_shingles", None, None, rules=phrase_rules)
    assert r1.phrase == r2.phrase


def test_numeric_range_is_preserved_not_split(phrase_rules):
    r = pg.generate_search_phrase("R&R Window screen, 1 - 9 SF", "window", None, "remove_and_replace", rules=phrase_rules)
    assert "1-9" in r.phrase.split()


def test_up_to_pattern_keeps_qualifier(phrase_rules):
    r = pg.generate_search_phrase("Gutter aluminum up to 5 inches", "gutter", "aluminum", None, rules=phrase_rules)
    assert "up to 5" in r.phrase


def test_filler_words_removed(phrase_rules):
    r = pg.generate_search_phrase("Existing damaged aluminum gutter", "gutter", "aluminum", None, rules=phrase_rules)
    assert "existing" not in r.phrase
    assert "damaged" not in r.phrase


def test_empty_description_returns_empty_result(phrase_rules):
    r = pg.generate_search_phrase("", None, None, None, rules=phrase_rules)
    assert r.phrase == ""
    assert r.terms == []


def test_fallback_used_when_no_structured_terms_found(phrase_rules):
    r = pg.generate_search_phrase("Ice & water barrier", "unknown", None, None, rules=phrase_rules)
    assert r.phrase != ""
    assert any("fallback" in reason for reason in r.reasons)


def test_never_produces_empty_phrase_for_nonempty_description(phrase_rules):
    r = pg.generate_search_phrase("String Light", "unknown", None, None, rules=phrase_rules)
    assert r.phrase != ""


def test_material_keyword_does_not_falsely_match_inside_longer_word(phrase_rules):
    # "laminate" (material) must not fire inside "laminated" (a distinct,
    # far more common roofing style word) -- word-boundary regression test.
    r = pg.generate_search_phrase("R&R Laminated shingles", "composition_shingles", "laminated composition shingles", "remove_and_replace", rules=phrase_rules)
    assert r.phrase.split().count("laminated") <= 1


def test_reasons_and_dropped_explain_every_bucket(phrase_rules):
    r = pg.generate_search_phrase("Gutter aluminum up to 5 inches", "gutter", "aluminum", None, rules=phrase_rules)
    assert any("component" in reason for reason in r.reasons)
    assert any("material" in reason for reason in r.reasons)
    assert "grade" in r.dropped
    assert "action" in r.dropped


def test_max_phrase_terms_is_respected(phrase_rules):
    import dataclasses

    short_rules = dataclasses.replace(phrase_rules, max_phrase_terms=2)
    r = pg.generate_search_phrase("Gutter aluminum up to 5 inches - high grade", "gutter", "aluminum", None, rules=short_rules)
    assert len(r.terms) <= 2


def test_leading_style_word_precedes_component(phrase_rules):
    r = pg.generate_search_phrase("Replace laminated composition shingles", "composition_shingles", "laminated composition shingles", "replace", rules=phrase_rules)
    assert r.phrase == "laminated composition shingles"


def test_grade_word_still_trails(phrase_rules):
    r = pg.generate_search_phrase("Carpet - High grade", "carpet", None, None, rules=phrase_rules)
    assert r.phrase == "carpet high grade"


def test_modifier_wet_is_preserved_and_leads(phrase_rules):
    r = pg.generate_search_phrase('Remove wet drywall 1/2"', "drywall", None, "remove", rules=phrase_rules)
    assert r.phrase == "wet drywall 1/2"


def test_fraction_size_is_captured(phrase_rules):
    r = pg.generate_search_phrase('Remove wet drywall 1/2"', "drywall", None, "remove", rules=phrase_rules)
    assert "1/2" in r.phrase.split()


def test_fraction_size_with_word_unit(phrase_rules):
    r = pg.generate_search_phrase('Pipe insulation 1/2 inch', "pipe", None, None, rules=phrase_rules)
    assert "1/2" in r.phrase.split()
