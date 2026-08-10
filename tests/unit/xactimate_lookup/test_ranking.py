from __future__ import annotations

from estimate_extractor.xactimate_lookup import ranking
from estimate_extractor.xactimate_lookup.models import (
    DECISION_AUTO_SELECT,
    DECISION_NO_MATCH,
    DECISION_REVIEW_REQUIRED,
    DropdownResult,
    RankedCandidate,
)


def _dropdown(text, cat="RFG", sel="X", desc=None, pos=0, conf=0.95):
    return DropdownResult(raw_text=text, row_position=pos, category=cat, selector=sel, description=desc or text, extraction_confidence=conf)


def _ranked(score, cat="RFG", sel="X", desc="d", pos=0, conflict_reasons=None):
    return RankedCandidate(dropdown=_dropdown(desc, cat=cat, sel=sel, pos=pos), score=score, conflict_reasons=conflict_reasons or [])


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


# ---------------------------------------------------------------------
# Phase 5.12 (live-caught): a two-dimension size ("16' x 7'") only ever
# contributed its FIRST number to size_key (e.g. "16"), so a candidate
# stating a DIFFERENT second dimension ("16' x 8'") still counted as
# size_state=MATCH -- live-reproduced as "Overhead door & hardware -
# 16' x 7'" scoring a perfect 1.0 with the wrong 8'-tall variant right
# behind it at 0.9662, too thin a margin to clear AUTO_SELECT despite
# the correct candidate being an unambiguous exact match.
# ---------------------------------------------------------------------


def test_two_dimension_size_conflict_is_caught_even_though_first_number_matches(phrase_rules, ranking_config):
    source = "R&R Overhead door & hardware - 16' x 7'"
    wrong_height = _dropdown("Overhead door & hardware - 16' x 8'", sel="OH16>")
    candidates = ranking.rank_dropdown_results(
        original_description=source, trade="doors", component="garage_door", material=None,
        action="remove_and_replace", unit="EA", size_key="16", grade_key=None,
        dropdowns=[wrong_height], rules=phrase_rules, config=ranking_config,
    )
    assert candidates[0].has_hard_conflict
    assert any("wrong_size" in r for r in candidates[0].conflict_reasons)


def test_two_dimension_size_match_is_not_penalized(phrase_rules, ranking_config):
    source = "R&R Overhead door & hardware - 16' x 7'"
    correct = _dropdown("Overhead door & hardware - 16' x 7'", sel="OH16")
    candidates = ranking.rank_dropdown_results(
        original_description=source, trade="doors", component="garage_door", material=None,
        action="remove_and_replace", unit="EA", size_key="16", grade_key=None,
        dropdowns=[correct], rules=phrase_rules, config=ranking_config,
    )
    assert not candidates[0].has_hard_conflict


def test_two_dimension_conflict_gives_the_correct_candidate_a_real_margin(phrase_rules, ranking_config):
    """The actual live bug: BOTH candidates used to score close enough
    (1.0 vs 0.9662) that classify_decision() stayed REVIEW_REQUIRED even
    though only one of them is the real match. This is the end-to-end
    proof the fix restores a decisive margin."""
    source = "R&R Overhead door & hardware - 16' x 7'"
    correct = _dropdown("Overhead door & hardware - 16' x 7'", sel="OH16", pos=0)
    wrong_height = _dropdown("Overhead door & hardware - 16' x 8'", sel="OH16>", pos=1)
    candidates = ranking.rank_dropdown_results(
        original_description=source, trade="doors", component="garage_door", material=None,
        action="remove_and_replace", unit="EA", size_key="16", grade_key=None,
        dropdowns=[correct, wrong_height], rules=phrase_rules, config=ranking_config,
    )
    assert candidates[0].dropdown.selector == "OH16"
    assert (candidates[0].score - candidates[1].score) >= ranking_config.auto_select_margin
    assert ranking.classify_decision(candidates, ranking_config) == DECISION_AUTO_SELECT


def test_single_dimension_source_does_not_trigger_two_dimension_check(phrase_rules, ranking_config):
    """No two-dimension spec in the source at all -- the new check must
    never fire (source_dimension is None), leaving ordinary single-
    number size comparison completely unaffected."""
    d = _dropdown("Gutter - aluminum - up to 6\"", sel="GUTA6")
    candidates = ranking.rank_dropdown_results(
        original_description="R&R Gutter - aluminum - up to 5\"", trade="gutters", component="gutter",
        material="aluminum", action="remove_and_replace", unit="LF", size_key="up to 5\"", grade_key=None,
        dropdowns=[d], rules=phrase_rules, config=ranking_config,
    )
    # unchanged from the pre-existing single-number-size CONFLICT path --
    # still caught, but via the original mechanism, not the new one.
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


# ---------------------------------------------------------------------
# Phase 5.15 Pass 2 (ground-truth-guided against the completed Aranda
# reference estimate): classify_decision()'s semantic-dominance
# override. score_dropdown_candidate()'s weighted-AVERAGE formula (see
# its own comment) makes exactly 1.0 reachable ONLY when the
# description is a literal/near-exact text match AND every other
# applicable dimension (component/material/size/grade/action)
# independently scored a full MATCH -- there is no partial-credit path
# to exactly 1.0. Live-reproduced on 10 real Aranda rows: the top
# candidate scored this maximum reachable 1.0 while a real, textually-
# close runner-up (that differs from the source in some dimension the
# ranker already checks -- grade upgrade, coat count, single/double,
# activity, different real component) could not also reach it.
# ---------------------------------------------------------------------


def test_exact_top_beats_close_non_exact_runner_up_despite_thin_margin(ranking_config):
    """The core recovery case: top scores the maximum reachable 1.0,
    runner-up is close (0.9868, margin 0.0132 << auto_select_margin
    0.08) but NOT exact -- e.g. '1 coat' vs '2 coats'. Must AUTO_SELECT."""
    top = _ranked(1.0, sel="OH>1")
    runner_up = _ranked(0.9868, sel="OH>")
    assert ranking.classify_decision([top, runner_up], ranking_config) == DECISION_AUTO_SELECT


def test_exact_tie_between_two_perfect_scores_stays_review_required(ranking_config):
    """Counterpart -- the negative-control guard: when the runner-up
    ALSO reaches exactly 1.0 (a genuine tie, e.g. the real 3-way
    'Lighting Installer - Electrician - per hour' tie against LIT/LAB,
    ELE/LAB, LAB/ELE), the override must never fire. This is precisely
    what stops the rule from being a disguised 'margin = 0'."""
    top = _ranked(1.0, sel="LIT")
    tied = _ranked(1.0, sel="ELE")
    assert ranking.classify_decision([top, tied], ranking_config) == DECISION_REVIEW_REQUIRED


def test_near_exact_top_below_threshold_does_not_trigger_override(ranking_config):
    """Top is very strong (0.9167) but not at the exact-match bar --
    the override must not apply merely because a score is 'high', only
    when it hits the maximum reachable value. Ordinary margin logic
    still governs."""
    top = _ranked(0.9167, sel="STEEP")
    runner_up = _ranked(0.8409, sel="STEEP>>")
    assert ranking.classify_decision([top, runner_up], ranking_config) == DECISION_REVIEW_REQUIRED


def test_exact_top_with_hard_conflict_is_not_overridden(ranking_config):
    """The override sits AFTER the existing has_hard_conflict gate in
    classify_decision() -- a conflicting top candidate must still be
    refused even if its raw score is 1.0."""
    top = _ranked(1.0, sel="X", conflict_reasons=["wrong_material: candidate states a different material"])
    runner_up = _ranked(0.5, sel="Y")
    assert ranking.classify_decision([top, runner_up], ranking_config) == DECISION_REVIEW_REQUIRED


def test_exact_top_alone_still_auto_selects(ranking_config):
    """No runner-up at all -- the override's guard clauses must not
    accidentally require a second candidate to exist."""
    top = _ranked(1.0, sel="X")
    assert ranking.classify_decision([top], ranking_config) == DECISION_AUTO_SELECT


def test_exact_top_already_clearing_ordinary_margin_is_unaffected(ranking_config):
    """When the ordinary margin check already passes on its own (top
    exact, runner-up far below), the override branch is never even
    reached -- this just confirms no regression in the common case."""
    top = _ranked(1.0, sel="X")
    runner_up = _ranked(0.5, sel="Y")
    assert ranking.classify_decision([top, runner_up], ranking_config) == DECISION_AUTO_SELECT


# ---------------------------------------------------------------------
# Phase 5.16 (live-caught): Xactimate's steep-roof surcharge family
# distinguishes items by an explicit slope RANGE ("7/12 to 9/12 slope")
# vs a THRESHOLD phrasing ("greater than 12/12 slope" / "over 12/12
# slope") that extract_size_term() doesn't recognize at all -- both
# fell through to size_state=UNSPECIFIED (neutral) instead of CONFLICT,
# live-reproduced as a 0.0758 margin (just under auto_select_margin)
# between the correct RFG/STEEP and the wrong RFG/STEEP>>.
# ---------------------------------------------------------------------


def test_slope_threshold_phrasing_is_a_real_size_conflict(phrase_rules, ranking_config):
    source = "Additional charge for steep roof - 7/12 to 9/12 slope"
    correct = _dropdown("Additional charge for steep roof - 7/12 to 9/12 slope", sel="STEEP")
    wrong_threshold = _dropdown("Additional charge for steep roof greater than 12/12 slope", sel="STEEP>>")
    candidates = ranking.rank_dropdown_results(
        original_description=source, trade="roofing", component="roof", material=None,
        action="install", unit="SQ", size_key="7/12-9/12", grade_key=None,
        dropdowns=[correct, wrong_threshold], rules=phrase_rules, config=ranking_config,
    )
    top, runner_up = candidates[0], candidates[1]
    assert top.dropdown.selector == "STEEP"
    assert runner_up.has_hard_conflict
    assert any("wrong_size" in r for r in runner_up.conflict_reasons)
    assert top.score - runner_up.score >= ranking_config.auto_select_margin
    assert ranking.classify_decision(candidates, ranking_config) == DECISION_AUTO_SELECT


def test_slope_range_match_is_not_penalized_as_a_conflict(phrase_rules, ranking_config):
    """A sibling item stating the SAME slope range (just a different
    component, e.g. framing sheathing) must not be caught by the new
    threshold-conflict check -- it never fires when the candidate's
    slope wording actually matches the source's range."""
    source = "Additional charge for steep roof - 7/12 to 9/12 slope"
    same_range_other_component = _dropdown("Add charge for sheathing steep roof - 7/12 - 9/12 slope", sel="SHSTP")
    candidates = ranking.rank_dropdown_results(
        original_description=source, trade="roofing", component="roof", material=None,
        action="install", unit="SQ", size_key="7/12-9/12", grade_key=None,
        dropdowns=[same_range_other_component], rules=phrase_rules, config=ranking_config,
    )
    assert not any("wrong_size" in r for r in candidates[0].conflict_reasons)
