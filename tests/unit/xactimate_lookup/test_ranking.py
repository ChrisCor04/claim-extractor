from __future__ import annotations

from estimate_extractor.xactimate_lookup import ranking
from estimate_extractor.xactimate_lookup.models import (
    DECISION_AUTO_SELECT,
    DECISION_NO_MATCH,
    DECISION_REVIEW_REQUIRED,
    DropdownResult,
)


def _dropdown(text, cat="RFG", sel="X", desc=None, pos=0, conf=0.95):
    return DropdownResult(raw_text=text, row_position=pos, category=cat, selector=sel, description=desc or text, extraction_confidence=conf)


def test_exact_strong_match_yields_auto_select(phrase_rules, ranking_config):
    top = _dropdown("Stain - wood fence/gate", sel="FENST", pos=0)
    other = _dropdown("Paint - wood deck", sel="OTHER", pos=1)
    candidates = ranking.rank_dropdown_results(
        original_description="Stain - wood fence/gate", trade="painting", component="fence", material="wood",
        action="stain", unit="SF", size_key=None, grade_key=None, dropdowns=[top, other], rules=phrase_rules, config=ranking_config,
    )
    assert ranking.classify_decision(candidates, ranking_config) == DECISION_AUTO_SELECT
    assert candidates[0].dropdown.selector == "FENST"


def test_no_candidates_yields_no_match(ranking_config):
    assert ranking.classify_decision([], ranking_config) == DECISION_NO_MATCH


def test_wrong_component_is_a_hard_conflict(phrase_rules, ranking_config):
    d = _dropdown("Clean floor lamp - crystal", sel="LAMP")
    candidates = ranking.rank_dropdown_results(
        original_description="Tear off composition shingles", trade="roofing", component="composition_shingles",
        material=None, action="remove", unit="SQ", size_key=None, grade_key=None, dropdowns=[d], rules=phrase_rules, config=ranking_config,
    )
    assert candidates[0].has_hard_conflict
    assert any("wrong_component" in r for r in candidates[0].conflict_reasons)


def test_unknown_component_sentinel_is_not_a_conflict(phrase_rules, ranking_config):
    d = _dropdown("Roofing felt - 15 lb.", sel="FELT")
    candidates = ranking.rank_dropdown_results(
        original_description="Roofing felt - 15 lb.", trade="roofing", component="unknown",
        material=None, action="unknown", unit="SQ", size_key=None, grade_key=None, dropdowns=[d], rules=phrase_rules, config=ranking_config,
    )
    assert not candidates[0].has_hard_conflict


def test_exact_match_beats_a_superset_variant_the_source_never_mentioned(phrase_rules, ranking_config):
    """Phase 5.6 (live-caught against real Xactimate dropdown data): an
    exact-text match must score STRICTLY higher than a candidate whose
    description is a superset of the source text (adds a material/size/
    attachment qualifier the source description never mentioned) --
    both used to score an identical flat 1.0 (any substring-containment
    match was treated as "perfect"), which live-reproduced a 0.0 margin
    for "Drip edge" vs "Drip edge - copper"/"- PVC/TPO clad metal"/etc.
    and forced REVIEW_REQUIRED on an item a human would select without
    hesitation. The plain, exact match must now clearly AUTO_SELECT."""
    a = _dropdown("Drip edge", sel="DRIP", pos=0)
    b = _dropdown("Drip edge - copper", sel="DRIPC", pos=1)
    candidates = ranking.rank_dropdown_results(
        original_description="Drip edge", trade="roofing", component=None, material=None,
        action=None, unit="LF", size_key=None, grade_key=None, dropdowns=[a, b], rules=phrase_rules, config=ranking_config,
    )
    assert candidates[0].dropdown.selector == "DRIP"
    assert candidates[0].score == 1.0
    assert candidates[0].score - candidates[1].score >= ranking_config.auto_select_margin
    assert ranking.classify_decision(candidates, ranking_config) == DECISION_AUTO_SELECT


def test_two_different_superset_variants_still_require_review(phrase_rules, ranking_config):
    """The complement of the above: when NEITHER candidate is an exact
    match (both add a different unmentioned qualifier), the exact-match
    fix must not manufacture a false margin between them -- genuinely
    ambiguous cases still require review."""
    a = _dropdown("Drip edge - copper", sel="DRIPC", pos=0)
    b = _dropdown("Drip edge - PVC/TPO clad metal", sel="DRIPP", pos=1)
    candidates = ranking.rank_dropdown_results(
        original_description="Drip edge", trade="roofing", component=None, material=None,
        action=None, unit="LF", size_key=None, grade_key=None, dropdowns=[a, b], rules=phrase_rules, config=ranking_config,
    )
    assert ranking.classify_decision(candidates, ranking_config) == DECISION_REVIEW_REQUIRED


def test_low_extraction_confidence_forces_review(phrase_rules, ranking_config):
    d = _dropdown("Stain - wood fence/gate", sel="FENST", conf=0.3)
    candidates = ranking.rank_dropdown_results(
        original_description="Stain - wood fence/gate", trade="painting", component="fence", material="wood",
        action="stain", unit="SF", size_key=None, grade_key=None, dropdowns=[d], rules=phrase_rules, config=ranking_config,
    )
    assert ranking.classify_decision(candidates, ranking_config) == DECISION_REVIEW_REQUIRED


def test_weak_score_below_review_threshold_yields_no_match(phrase_rules, ranking_config):
    d = _dropdown("Completely unrelated content about swimming pools", sel="POOL")
    candidates = ranking.rank_dropdown_results(
        original_description="Tear off composition shingles", trade="roofing", component="composition_shingles",
        material=None, action="remove", unit="SQ", size_key=None, grade_key=None, dropdowns=[d], rules=phrase_rules, config=ranking_config,
    )
    assert ranking.classify_decision(candidates, ranking_config) == DECISION_NO_MATCH


def test_wrong_size_is_a_hard_conflict(phrase_rules, ranking_config):
    d = _dropdown("Drip edge/gutter apron up to 10", sel="DRIP10")
    candidates = ranking.rank_dropdown_results(
        original_description="Gutter up to 5", trade="gutters", component="gutter", material=None,
        action=None, unit="LF", size_key="up to 5", grade_key=None, dropdowns=[d], rules=phrase_rules, config=ranking_config,
    )
    assert any("wrong_size" in r for r in candidates[0].conflict_reasons)


# ---------------------------------------------------------------------
# Phase 5.10A: absence of a material/size/grade word in the candidate is
# NOT itself conflicting evidence -- only an EXPLICIT, different stated
# value is. The live-caught case: "R&R Gutter splash guard" (source
# material: aluminum) against the objectively correct "SFG/GSG -- Gutter
# splash guard" candidate, which states no material at all.
# ---------------------------------------------------------------------


def test_material_absent_from_candidate_is_unspecified_not_a_conflict(phrase_rules, ranking_config):
    """The exact live-caught defect: a candidate that simply never
    mentions material must not be penalized as if it contradicted the
    source's material."""
    d = _dropdown("Gutter splash guard", cat="SFG", sel="GSG")
    candidates = ranking.rank_dropdown_results(
        original_description="R&R Gutter splash guard", trade="gutters", component="gutter splash guard",
        material="aluminum", action="remove_and_replace", unit="EA", size_key=None, grade_key=None,
        dropdowns=[d], rules=phrase_rules, config=ranking_config,
    )
    assert not any("wrong_material" in r for r in candidates[0].conflict_reasons)
    assert not candidates[0].has_hard_conflict


def test_material_explicit_different_value_is_still_a_hard_conflict(phrase_rules, ranking_config):
    """The counterpart -- a candidate that explicitly states a
    DIFFERENT, recognized material is real, opposing evidence and must
    still cap the score."""
    d = _dropdown("Copper gutter splash guard", cat="SFG", sel="GSGCU")
    candidates = ranking.rank_dropdown_results(
        original_description="R&R Gutter splash guard", trade="gutters", component="gutter splash guard",
        material="aluminum", action="remove_and_replace", unit="EA", size_key=None, grade_key=None,
        dropdowns=[d], rules=phrase_rules, config=ranking_config,
    )
    assert any("wrong_material" in r for r in candidates[0].conflict_reasons)
    assert candidates[0].has_hard_conflict


def test_material_matching_value_is_a_real_match(phrase_rules, ranking_config):
    d = _dropdown("Aluminum gutter splash guard", cat="SFG", sel="GSG")
    candidates = ranking.rank_dropdown_results(
        original_description="R&R Gutter splash guard", trade="gutters", component="gutter splash guard",
        material="aluminum", action="remove_and_replace", unit="EA", size_key=None, grade_key=None,
        dropdowns=[d], rules=phrase_rules, config=ranking_config,
    )
    assert any("material matches" in r for r in candidates[0].match_reasons)
    assert not candidates[0].has_hard_conflict


def test_splash_guard_wins_over_generic_guard_screen_variants(phrase_rules, ranking_config):
    """Stage 2: the exact phrase 'splash guard' must outrank generic
    'guard/screen' variants that merely share the word 'guard' -- phrase
    specificity, not just word overlap."""
    splash = _dropdown("Gutter splash guard", cat="SFG", sel="GSG", pos=0)
    generic = _dropdown("Gutter guard/screen", cat="SFG", sel="GRD", pos=1)
    premium = _dropdown("Gutter guard/screen - Premium grade", cat="SFG", sel="GRD++", pos=2)
    candidates = ranking.rank_dropdown_results(
        original_description="R&R Gutter splash guard", trade="gutters", component="gutter splash guard",
        material="aluminum", action="remove_and_replace", unit="EA", size_key=None, grade_key=None,
        dropdowns=[splash, generic, premium], rules=phrase_rules, config=ranking_config,
    )
    assert candidates[0].dropdown.selector == "GSG"
    assert candidates[0].score > candidates[1].score


def test_size_absent_from_candidate_is_unspecified_not_a_conflict(phrase_rules, ranking_config):
    d = _dropdown("Gutter splash guard", sel="GSG")
    candidates = ranking.rank_dropdown_results(
        original_description="R&R Gutter splash guard - 5\"", trade="gutters", component="gutter",
        material=None, action="remove_and_replace", unit="EA", size_key="5\"", grade_key=None,
        dropdowns=[d], rules=phrase_rules, config=ranking_config,
    )
    assert not any("wrong_size" in r for r in candidates[0].conflict_reasons)


def test_size_explicit_different_value_is_still_a_hard_conflict(phrase_rules, ranking_config):
    d = _dropdown("Gutter - aluminum - up to 6\"", sel="GUTA6")
    candidates = ranking.rank_dropdown_results(
        original_description="R&R Gutter - aluminum - up to 5\"", trade="gutters", component="gutter",
        material="aluminum", action="remove_and_replace", unit="LF", size_key="up to 5\"", grade_key=None,
        dropdowns=[d], rules=phrase_rules, config=ranking_config,
    )
    assert any("wrong_size" in r for r in candidates[0].conflict_reasons)


def test_prior_verified_mapping_boosts_score(phrase_rules, ranking_config):
    d = _dropdown("Roofing felt - 15 lb.", sel="FELT")
    without = ranking.rank_dropdown_results(
        original_description="Roofing felt - 15 lb.", trade="roofing", component="roofing felt", material=None,
        action=None, unit="SQ", size_key=None, grade_key=None, dropdowns=[d], rules=phrase_rules, config=ranking_config,
        prior_verified_mapping=False,
    )
    with_prior = ranking.rank_dropdown_results(
        original_description="Roofing felt - 15 lb.", trade="roofing", component="roofing felt", material=None,
        action=None, unit="SQ", size_key=None, grade_key=None, dropdowns=[d], rules=phrase_rules, config=ranking_config,
        prior_verified_mapping=True,
    )
    assert with_prior[0].score >= without[0].score


def test_ranking_bounded_by_max_dropdown_candidates_considered(phrase_rules, ranking_config):
    import dataclasses

    bounded_config = dataclasses.replace(ranking_config, max_dropdown_candidates_considered=2)
    dropdowns = [_dropdown(f"Item {i}", sel=f"S{i}", pos=i) for i in range(10)]
    candidates = ranking.rank_dropdown_results(
        original_description="Item 0", trade=None, component=None, material=None, action=None, unit=None,
        size_key=None, grade_key=None, dropdowns=dropdowns, rules=phrase_rules, config=bounded_config,
    )
    assert len(candidates) == 2


def test_does_not_automatically_choose_first_result_when_second_is_equally_strong(phrase_rules, ranking_config):
    # Regression guard for "Do not automatically choose the first
    # dropdown result": row_position=0 is NOT preferred when tied.
    a = _dropdown("Drip edge", sel="FIRST", pos=0)
    b = _dropdown("Drip edge", sel="SECOND", pos=1)
    candidates = ranking.rank_dropdown_results(
        original_description="Drip edge", trade=None, component=None, material=None, action=None, unit=None,
        size_key=None, grade_key=None, dropdowns=[a, b], rules=phrase_rules, config=ranking_config,
    )
    assert ranking.classify_decision(candidates, ranking_config) == DECISION_REVIEW_REQUIRED
