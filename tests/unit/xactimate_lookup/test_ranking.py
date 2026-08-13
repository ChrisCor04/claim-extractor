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


def _ranked(score, cat="RFG", sel="X", desc="d", pos=0, conflict_reasons=None, conf=0.95):
    return RankedCandidate(dropdown=_dropdown(desc, cat=cat, sel=sel, pos=pos, conf=conf), score=score, conflict_reasons=conflict_reasons or [])


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


def test_no_margin_based_first_result_preference_when_second_is_close_but_not_tied(phrase_rules, ranking_config):
    """Regression guard for "Do not automatically choose the first
    dropdown result": a NEAR-miss margin case (not a bitwise score tie)
    must never be resolved by candidate order alone."""
    # Neither candidate is a plain exact match to the source (both add a
    # different unmentioned qualifier), so the Phase 5.15 exact-top
    # override doesn't apply either -- isolates a genuine near-miss,
    # non-tied margin case from both existing AUTO_SELECT mechanisms.
    a = _dropdown("Drip edge - copper", sel="FIRST", pos=0)
    b = _dropdown("Drip edge - PVC/TPO clad metal", sel="SECOND", pos=1)
    candidates = ranking.rank_dropdown_results(
        original_description="Drip edge", trade=None, component=None, material=None, action=None, unit=None,
        size_key=None, grade_key=None, dropdowns=[a, b], rules=phrase_rules, config=ranking_config,
    )
    assert candidates[0].score != candidates[1].score
    assert ranking.classify_decision(candidates, ranking_config) == DECISION_REVIEW_REQUIRED


def test_genuine_exact_tie_now_uses_the_first_candidate_fallback(phrase_rules, ranking_config):
    """Phase 5.22: the complement of the guard above -- when both
    candidates are a genuine BITWISE score tie (not merely close), this
    is exactly the shape the explicit, narrow first-candidate fallback
    targets. Order is honestly reflected: FIRST wins because it's in
    position 0, not because of any semantic preference."""
    a = _dropdown("Drip edge", sel="FIRST", pos=0)
    b = _dropdown("Drip edge", sel="SECOND", pos=1)
    candidates = ranking.rank_dropdown_results(
        original_description="Drip edge", trade=None, component=None, material=None, action=None, unit=None,
        size_key=None, grade_key=None, dropdowns=[a, b], rules=phrase_rules, config=ranking_config,
    )
    assert candidates[0].score == candidates[1].score
    diag = ranking.classify_decision_with_diagnostics(candidates, ranking_config)
    assert diag.decision == DECISION_AUTO_SELECT
    assert diag.gate == "exact_tie_resolved_by_first_candidate_fallback"
    assert diag.top_selector == "FIRST"


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


def test_exact_tie_between_two_perfect_scores_does_not_use_the_margin_override(ranking_config):
    """Counterpart -- the negative-control guard: when the runner-up
    ALSO reaches exactly 1.0 (a genuine tie, e.g. the real 3-way
    'Lighting Installer - Electrician - per hour' tie against LIT/LAB,
    ELE/LAB, LAB/ELE), the Phase 5.15 'insufficient_margin_exact_top_
    override' must never fire -- that is precisely what stops that rule
    from being a disguised 'margin = 0'. Phase 5.22's separate, later
    first-candidate fallback (see below) now catches this same-category,
    no-preferred-categories shape and AUTO_SELECTs anyway -- confirmed
    here by gate, so this test still proves the ORIGINAL override
    specifically declines, without being confused by the newer one."""
    top = _ranked(1.0, sel="LIT")
    tied = _ranked(1.0, sel="ELE")
    diag = ranking.classify_decision_with_diagnostics([top, tied], ranking_config)
    assert diag.decision == DECISION_AUTO_SELECT
    assert diag.gate == "exact_tie_resolved_by_first_candidate_fallback"
    assert diag.selected_candidate is top


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


# ---------------------------------------------------------------------
# Phase 5.17 (live-caught): a candidate that's a substring of the
# source used to always score a flat 1.0, even when the OMITTED prefix
# is a real, catalog-family-distinguishing action phrase (this catalog
# keeps a SEPARATE "Detach & reset" entry apart from a same-component
# default-action one) -- live-reproduced, "RFG/PAVCRS" ("...- Detach &
# reset") lost to "HVC/PAVC-" ("...- plastic", a generic default-action
# variant) purely because the latter's text happened to be a substring
# of the source.
# ---------------------------------------------------------------------


def test_omitted_action_phrase_denies_the_free_substring_score(phrase_rules, ranking_config):
    source = "Detach & Reset Power attic vent cover only - plastic"
    generic_default_action = _dropdown("Power attic vent cover only - plastic", cat="HVC", sel="PAVC-")
    action_specific = _dropdown("Power attic vent cover only - Detach & reset", cat="RFG", sel="PAVCRS")
    candidates = ranking.rank_dropdown_results(
        original_description=source, trade="roofing", component="unknown", material=None,
        action="detach_and_reset", unit="EA", size_key=None, grade_key=None,
        dropdowns=[generic_default_action, action_specific], rules=phrase_rules, config=ranking_config,
    )
    top = candidates[0]
    assert top.dropdown.selector == "PAVCRS"
    assert not top.has_hard_conflict
    runner_up = next(c for c in candidates if c.dropdown.selector == "PAVC-")
    assert runner_up.has_hard_conflict
    assert any("wrong_action" in r for r in runner_up.conflict_reasons)


def test_omitted_generic_action_prefix_still_gets_the_free_substring_score(phrase_rules, ranking_config):
    """The normal, intended case the substring-bonus branch exists for
    must be completely unaffected -- an omitted GENERIC action prefix
    (e.g. "R&R", which has no action_search_terms/ranking-only phrase
    value at all) is still treated as harmless filler, not evidence of
    a different item."""
    source = "R&R Gutter - aluminum - up to 5\""
    d = _dropdown("Gutter - aluminum - up to 5\"", sel="GUTA")
    candidates = ranking.rank_dropdown_results(
        original_description=source, trade="gutters", component="gutter", material="aluminum",
        action="remove_and_replace", unit="LF", size_key="up to 5\"", grade_key=None,
        dropdowns=[d], rules=phrase_rules, config=ranking_config,
    )
    assert candidates[0].score == 1.0


# ---------------------------------------------------------------------
# Phase 5.17 (live-caught): the component word-overlap check treats
# ANY shared word as a full pass -- "Additional charge for steep roof"
# (component "roof_surcharge") scored component_ok=True against "Add
# charge for sheathing steep roof", a genuinely different FRAMING item,
# purely because both mention "roof".
# ---------------------------------------------------------------------


def test_unrequested_sheathing_marker_is_a_component_conflict(phrase_rules, ranking_config):
    source = "Additional charge for steep roof - 7/12 to 9/12 slope"
    wrong_component = _dropdown("Add charge for sheathing steep roof - 7/12 - 9/12 slope", cat="FRM", sel="SHSTP")
    candidates = ranking.rank_dropdown_results(
        original_description=source, trade="roofing", component="roof_surcharge", material=None,
        action="install", unit="SQ", size_key="7/12-9/12", grade_key=None,
        dropdowns=[wrong_component], rules=phrase_rules, config=ranking_config,
    )
    assert candidates[0].has_hard_conflict
    assert any("wrong_component" in r for r in candidates[0].conflict_reasons)


def test_sheathing_marker_does_not_fire_when_source_itself_mentions_it(phrase_rules, ranking_config):
    """The marker only ever fires when the SOURCE never mentions the
    word at all -- if the source itself is genuinely about sheathing,
    this must not manufacture a false conflict."""
    source = "Add charge for sheathing steep roof - 7/12 to 9/12 slope"
    d = _dropdown("Add charge for sheathing steep roof - 7/12 - 9/12 slope", cat="FRM", sel="SHSTP")
    candidates = ranking.rank_dropdown_results(
        original_description=source, trade="framing", component="sheathing", material=None,
        action="install", unit="SQ", size_key="7/12-9/12", grade_key=None,
        dropdowns=[d], rules=phrase_rules, config=ranking_config,
    )
    assert not any("wrong_component" in r for r in candidates[0].conflict_reasons)


# ---------------------------------------------------------------------
# Phase 5.17 (live-caught): Xactimate's generic cleaning-template family
# ("Clean {V}") has a "- Light" (reduced-intensity) sibling variant --
# "Clean Fence" scored the base "Clean {V}" only ~0.057 above the
# "- Light" sibling, below auto_select_margin, since neither candidate's
# unrequested qualifier was previously recognized as real evidence.
# ---------------------------------------------------------------------


def test_unrequested_light_cleaning_qualifier_is_a_grade_conflict(phrase_rules, ranking_config):
    source = "Clean Fence"
    base = _dropdown("Clean {V}", cat="CLN", sel="AV")
    light = _dropdown("Clean {V} - Light", cat="CLN", sel="AV-")
    candidates = ranking.rank_dropdown_results(
        original_description=source, trade=None, component="fence", material=None,
        action="clean", unit="SF", size_key=None, grade_key=None,
        dropdowns=[base, light], rules=phrase_rules, config=ranking_config,
    )
    top = candidates[0]
    assert top.dropdown.selector == "AV"
    assert not top.has_hard_conflict
    runner_up = next(c for c in candidates if c.dropdown.selector == "AV-")
    assert runner_up.has_hard_conflict
    assert top.score - runner_up.score >= ranking_config.auto_select_margin


def test_light_qualifier_marker_does_not_false_positive_on_unrelated_light_candidates(phrase_rules, ranking_config):
    """The marker is matched as the FULL "{v} - light" template phrase,
    not a bare "light" substring -- an unrelated candidate that merely
    mentions "light" for a completely different reason (e.g. a light
    fixture item) must never be caught by this check."""
    source = "Clean Fence"
    unrelated = _dropdown("Light Fixtures (Bid Item)", cat="LIT", sel="BIDITM")
    candidates = ranking.rank_dropdown_results(
        original_description=source, trade=None, component="fence", material=None,
        action="clean", unit="SF", size_key=None, grade_key=None,
        dropdowns=[unrelated], rules=phrase_rules, config=ranking_config,
    )
    assert not any("grade" in r or "style" in r for r in candidates[0].conflict_reasons)


# ---------------------------------------------------------------------
# Phase 5.18: _below_floor_semantic_dominance() -- 0016 (Power Attic Vent
# Cover) and 0039 (Clean Fence) both scored below auto_select_min
# (0.88) despite being the correct, unambiguous top candidate, because
# an absolute score floor alone can't tell a genuinely uncertain match
# apart from one that's just missing a component/material/size/grade
# signal that was never present in the source text to begin with. The
# override is deliberately narrow -- score >= 0.70, no hard conflict,
# reliable extraction, a genuine positive action-match signal, a strict
# 0.20 margin (2.5x the ordinary 0.08), and a runner-up ceiling of 0.60
# -- and every condition below is tested in isolation so a future edit
# can't loosen one without a targeted test catching it. These are the
# Stage 3 negative controls: real near-miss shapes the override must
# NOT fire on, built BEFORE (well, alongside -- see phase log)
# broadening this override any further.
# ---------------------------------------------------------------------


def _below_floor_ranked(score, match_reasons=None, conflict_reasons=None, conf=0.95, cat="RFG", sel="X"):
    return RankedCandidate(
        dropdown=_dropdown("d", cat=cat, sel=sel, conf=conf), score=score,
        match_reasons=match_reasons or [], conflict_reasons=conflict_reasons or [],
    )


def test_below_floor_dominance_fires_when_every_condition_is_met(ranking_config):
    top = _below_floor_ranked(0.80, match_reasons=["action 'install' matches"])
    runner_up = _below_floor_ranked(0.55, sel="Y")
    assert ranking.classify_decision([top, runner_up], ranking_config) == DECISION_AUTO_SELECT


def test_below_floor_dominance_blocked_by_hard_conflict():
    """Negative control: 'different component/material' shape -- a
    conflict caps the practical score anyway, but even if a conflicted
    candidate somehow still posted a high score, has_hard_conflict must
    veto the override on its own."""
    config = ranking.load_ranking_config()
    top = _below_floor_ranked(0.80, match_reasons=["action 'install' matches"], conflict_reasons=["wrong_material: 'copper' not found"])
    runner_up = _below_floor_ranked(0.55, sel="Y")
    assert ranking.classify_decision([top, runner_up], config) != DECISION_AUTO_SELECT


def test_below_floor_dominance_blocked_by_wrong_size_conflict():
    """Negative control: 'wrong dimensions' shape."""
    config = ranking.load_ranking_config()
    top = _below_floor_ranked(0.80, match_reasons=["action 'install' matches"], conflict_reasons=["wrong_size: '12' not found"])
    runner_up = _below_floor_ranked(0.55, sel="Y")
    assert ranking.classify_decision([top, runner_up], config) != DECISION_AUTO_SELECT


def test_below_floor_dominance_blocked_by_wrong_component_conflict():
    """Negative control: 'different component' shape."""
    config = ranking.load_ranking_config()
    top = _below_floor_ranked(0.80, match_reasons=["action 'install' matches"], conflict_reasons=["wrong_component: candidate is 'gutter', source is 'fence'"])
    runner_up = _below_floor_ranked(0.55, sel="Y")
    assert ranking.classify_decision([top, runner_up], config) != DECISION_AUTO_SELECT


def test_below_floor_dominance_blocked_by_thin_margin():
    """Negative control: 'multiple generic items close together' shape
    -- score and no-conflict alone are not enough if the runner-up is
    close (0.15 margin, under the strict 0.20 requirement), even though
    that margin would clear the ordinary 0.08 auto_select_margin."""
    config = ranking.load_ranking_config()
    top = _below_floor_ranked(0.80, match_reasons=["action 'install' matches"])
    runner_up = _below_floor_ranked(0.65, sel="Y")
    assert ranking.classify_decision([top, runner_up], config) != DECISION_AUTO_SELECT


def test_below_floor_dominance_blocked_by_runner_up_ceiling_even_with_wide_margin():
    """Negative control: 'genuine close low-score alternative' shape --
    live-caught (0016 vs the real RFG/PAVRS competitor): a runner-up
    scoring >= 0.60 represents a real, defensible competing
    interpretation and must veto the override even when the raw margin
    arithmetic alone would clear 0.20 (0.85 - 0.60 == 0.25)."""
    config = ranking.load_ranking_config()
    top = _below_floor_ranked(0.85, match_reasons=["action 'install' matches"])
    runner_up = _below_floor_ranked(0.60, sel="Y")
    assert ranking.classify_decision([top, runner_up], config) != DECISION_AUTO_SELECT


def test_below_floor_dominance_blocked_without_a_positive_action_match_signal():
    """Negative control: score/margin/no-conflict alone must not be
    read as 'action intent confirmed' -- e.g. a source with no action
    verb at all (action_term is None) trivially never conflicts, but
    that is not the same as positive evidence the action is right."""
    config = ranking.load_ranking_config()
    top = _below_floor_ranked(0.80)  # no "action ... matches" entry
    runner_up = _below_floor_ranked(0.55, sel="Y")
    assert ranking.classify_decision([top, runner_up], config) != DECISION_AUTO_SELECT


def test_below_floor_dominance_blocked_below_the_absolute_minimum_score():
    """Negative control: even a clean, wide-margin, action-confirmed
    top candidate must not be promoted if its own absolute score is
    still genuinely weak (< 0.70) -- the override raises confidence in
    an uncertain-but-plausible match, it does not manufacture certainty
    out of a low score."""
    config = ranking.load_ranking_config()
    top = _below_floor_ranked(0.65, match_reasons=["action 'install' matches"])
    runner_up = _below_floor_ranked(0.30, sel="Y")
    assert ranking.classify_decision([top, runner_up], config) != DECISION_AUTO_SELECT


def test_below_floor_dominance_blocked_by_low_extraction_confidence():
    config = ranking.load_ranking_config()
    top = _below_floor_ranked(0.80, match_reasons=["action 'install' matches"], conf=0.5)
    runner_up = _below_floor_ranked(0.55, sel="Y")
    assert ranking.classify_decision([top, runner_up], config) != DECISION_AUTO_SELECT


def test_below_floor_dominance_untouched_when_top_already_clears_the_ordinary_floor():
    """The new branch lives strictly inside `top.score < auto_select_min`
    in classify_decision() -- confirms it cannot suppress an ordinary
    AUTO_SELECT that never needed it."""
    config = ranking.load_ranking_config()
    top = _below_floor_ranked(0.95)  # no action-match reason, no override needed
    runner_up = _below_floor_ranked(0.30, sel="Y")
    assert ranking.classify_decision([top, runner_up], config) == DECISION_AUTO_SELECT


# ---------------------------------------------------------------------
# Observability: classify_decision_with_diagnostics() persists the exact
# values/gate classify_decision() itself consulted, without duplicating
# any branch logic -- classify_decision() is now a pure `.decision`
# delegate over this function (see ranking.py). Each test below proves
# a distinct gate produces the right decision AND the right explanatory
# fields, so a persisted search attempt can explain after the fact why
# AUTO_SELECT did or did not happen.
# ---------------------------------------------------------------------


def test_diagnostics_score_below_auto_select_threshold(ranking_config):
    top = _ranked(0.70, cat="RFG", sel="TOP")
    second = _ranked(0.60, cat="RFG", sel="SECOND")
    diag = ranking.classify_decision_with_diagnostics([top, second], ranking_config)
    assert diag.decision == DECISION_REVIEW_REQUIRED
    assert diag.gate == "below_auto_select_min"
    assert diag.top_category == "RFG"
    assert diag.top_selector == "TOP"
    assert diag.top_score == 0.70
    assert diag.second_category == "RFG"
    assert diag.second_selector == "SECOND"
    assert diag.second_score == 0.60
    assert diag.margin == 0.10
    assert diag.auto_select_min == ranking_config.auto_select_min
    assert diag.auto_select_margin == ranking_config.auto_select_margin
    assert diag.min_extraction_confidence == ranking_config.min_extraction_confidence
    assert diag.top_has_hard_conflict is False
    assert diag.top_conflict_reasons == ()
    # classify_decision() must agree exactly -- it's a pure delegate.
    assert ranking.classify_decision([top, second], ranking_config) == diag.decision


def test_diagnostics_insufficient_margin(ranking_config):
    top = _ranked(0.95, sel="TOP")
    second = _ranked(0.90, sel="SECOND")
    assert (top.score - second.score) < ranking_config.auto_select_margin
    diag = ranking.classify_decision_with_diagnostics([top, second], ranking_config)
    assert diag.decision == DECISION_REVIEW_REQUIRED
    assert diag.gate == "insufficient_margin"
    assert diag.top_score == 0.95
    assert diag.second_score == 0.90
    assert diag.margin == 0.05
    assert ranking.classify_decision([top, second], ranking_config) == diag.decision


def test_diagnostics_extraction_confidence_below_minimum(ranking_config):
    """The `low_extraction_confidence` gate sits AFTER the hard-conflict
    gate in classify_decision_with_diagnostics() and is only reachable
    when extraction_confidence is below the threshold WITHOUT already
    being captured as a conflict_reason -- the real scorer (score_
    dropdown_candidate) always converts a confidence deficit into a
    hard conflict itself (see test_diagnostics_hard_conflict below), so
    this exercises the gate directly via a synthetic candidate,
    matching this file's existing convention for branches the full
    scorer pipeline can't cleanly isolate (see the exact-match-override
    and below-floor-dominance tests' own use of _ranked())."""
    top = _ranked(0.95, sel="TOP", conf=0.5)
    second = _ranked(0.5, sel="SECOND")
    diag = ranking.classify_decision_with_diagnostics([top, second], ranking_config)
    assert diag.decision == DECISION_REVIEW_REQUIRED
    assert diag.gate == "low_extraction_confidence"
    assert diag.top_extraction_confidence == 0.5
    assert diag.min_extraction_confidence == ranking_config.min_extraction_confidence
    assert ranking.classify_decision([top, second], ranking_config) == diag.decision


def test_diagnostics_hard_conflict(ranking_config):
    """Every real conflict_caps type (wrong_component/material/size/
    action/grade_or_style) caps the score below auto_select_min, so a
    real-scored hard conflict is always caught by the
    below_review_required_min/below_auto_select_min gates first -- the
    `hard_conflict` gate itself only fires for a top candidate whose
    score already cleared auto_select_min (see
    test_exact_top_with_hard_conflict_is_not_overridden above for the
    same real, live-caught shape: a conflicting candidate can still
    score 1.0)."""
    top = _ranked(1.0, sel="TOP", conflict_reasons=["wrong_material: candidate states a different material"])
    second = _ranked(0.5, sel="SECOND")
    diag = ranking.classify_decision_with_diagnostics([top, second], ranking_config)
    assert diag.decision == DECISION_REVIEW_REQUIRED
    assert diag.gate == "hard_conflict"
    assert diag.top_has_hard_conflict is True
    assert any("wrong_material" in r for r in diag.top_conflict_reasons)
    assert ranking.classify_decision([top, second], ranking_config) == diag.decision


def test_diagnostics_genuine_auto_select(phrase_rules, ranking_config):
    top = _dropdown("Stain - wood fence/gate", cat="PNT", sel="FENST", pos=0)
    other = _dropdown("Paint - wood deck", cat="PNT", sel="OTHER", pos=1)
    candidates = ranking.rank_dropdown_results(
        original_description="Stain - wood fence/gate", trade="painting", component="fence", material="wood",
        action="stain", unit="SF", size_key=None, grade_key=None, dropdowns=[top, other], rules=phrase_rules, config=ranking_config,
    )
    diag = ranking.classify_decision_with_diagnostics(candidates, ranking_config)
    assert diag.decision == DECISION_AUTO_SELECT
    assert diag.gate == "clear_margin"
    assert diag.top_category == "PNT"
    assert diag.top_selector == "FENST"
    assert diag.top_has_hard_conflict is False
    assert diag.margin is not None and diag.margin >= ranking_config.auto_select_margin
    assert ranking.classify_decision(candidates, ranking_config) == diag.decision


def test_diagnostics_no_match_below_review_threshold(phrase_rules, ranking_config):
    d = _dropdown("Completely unrelated content about swimming pools", sel="POOL")
    candidates = ranking.rank_dropdown_results(
        original_description="Tear off composition shingles", trade="roofing", component="composition_shingles",
        material=None, action="remove", unit="SQ", size_key=None, grade_key=None, dropdowns=[d], rules=phrase_rules, config=ranking_config,
    )
    diag = ranking.classify_decision_with_diagnostics(candidates, ranking_config)
    assert diag.decision == DECISION_NO_MATCH
    assert diag.gate == "below_review_required_min"
    assert diag.top_score is not None and diag.top_score < ranking_config.review_required_min
    assert ranking.classify_decision(candidates, ranking_config) == diag.decision


def test_diagnostics_no_candidates_at_all(ranking_config):
    diag = ranking.classify_decision_with_diagnostics([], ranking_config)
    assert diag.decision == DECISION_NO_MATCH
    assert diag.gate == "no_candidates"
    assert diag.top_category is None
    assert diag.top_score is None
    assert diag.second_score is None
    assert diag.margin is None


def test_diagnostics_to_dict_serializes_all_fields(ranking_config):
    top = _ranked(0.95, cat="RFG", sel="TOP")
    second = _ranked(0.5, cat="RFG", sel="SECOND")
    diag = ranking.classify_decision_with_diagnostics([top, second], ranking_config)
    d = diag.to_dict()
    assert d["decision"] == DECISION_AUTO_SELECT
    assert d["gate"] == "clear_margin"
    assert d["top_category"] == "RFG"
    assert d["top_selector"] == "TOP"
    assert d["top_score"] == 0.95
    assert d["second_category"] == "RFG"
    assert d["second_selector"] == "SECOND"
    assert d["second_score"] == 0.5
    assert isinstance(d["top_conflict_reasons"], list)
    assert d["auto_select_min"] == ranking_config.auto_select_min
    assert d["auto_select_margin"] == ranking_config.auto_select_margin
    assert d["min_extraction_confidence"] == ranking_config.min_extraction_confidence
    assert d["tie_resolution"] is None


# ---------------------------------------------------------------------
# Phase 5.19 (live-caught, odom-insurance-v2 Rows 7/11/18): a cross-
# category EXACT tie between otherwise textually-identical candidates
# (e.g. "Digital satellite system - Detach & reset" scoring identically
# in both ELS and RFG) can never be resolved by text/margin alone --
# _resolve_cross_category_tie() only ever breaks it using the caller's
# own PRE-EXISTING positive category evidence (`preferred_categories`,
# in production sourced from selector_recommendation's own data-
# calibrated trade/component category hints -- see orchestrator.py).
# Every test below drives classify_decision_with_diagnostics() directly
# with a synthetic preferred_categories tuple, matching this file's own
# established convention (_ranked()) for isolating one branch.
# ---------------------------------------------------------------------


_SATELLITE_DESC = "Digital satellite system - Detach & reset"
_STEP_DESC = "Step flashing"
_WRAP_DESC = "House wrap (air/moisture barrier)"


def test_exact_tie_resolved_by_strong_context_row7_shape(ranking_config):
    """Row 7: trade='electrical' hints ('ELE', 'ELS') -- ELS is the tied
    candidate that evidence supports; RFG is not. Must AUTO_SELECT ELS,
    not merely stay REVIEW_REQUIRED, and must not distort either
    candidate's own text/ranking score."""
    els = _ranked(1.0, cat="ELS", sel="DISHRS", desc=_SATELLITE_DESC, pos=0)
    rfg = _ranked(1.0, cat="RFG", sel="DISHRS", desc=_SATELLITE_DESC, pos=1)
    diag = ranking.classify_decision_with_diagnostics([els, rfg], ranking_config, preferred_categories=("ELE", "ELS"))
    assert diag.decision == DECISION_AUTO_SELECT
    assert diag.gate == "exact_tie_resolved_by_context"
    assert diag.top_category == "ELS"
    assert diag.top_selector == "DISHRS"
    assert diag.top_score == 1.0  # score itself is untouched by resolution
    assert diag.selected_candidate is els
    assert diag.second_category == "RFG"  # the losing candidate, recorded for audit
    tr = diag.tie_resolution
    assert tr is not None
    assert tr.tie_detected is True
    assert tr.resolved is True
    assert tr.resolved_category == "ELS"
    assert tr.resolved_selector == "DISHRS"
    assert tr.reason == "context_matched_one_candidate"
    assert tr.preferred_categories == ("ELE", "ELS")
    # No preferred_categories -> context can't resolve it -> Phase 5.22's
    # first-candidate fallback takes over instead of leaving this
    # REVIEW_REQUIRED forever; see test_exact_tie_first_candidate_
    # fallback_row11_shape below for the dedicated coverage.
    no_context_diag = ranking.classify_decision_with_diagnostics([els, rfg], ranking_config)
    assert no_context_diag.decision == DECISION_AUTO_SELECT
    assert no_context_diag.gate == "exact_tie_resolved_by_first_candidate_fallback"


def test_exact_tie_resolved_by_strong_context_row18_shape(ranking_config):
    """Row 18: trade='siding' hints ('SDG',) -- SDG is preferred over INS."""
    ins = _ranked(1.0, cat="INS", sel="HWRAP", desc=_WRAP_DESC, pos=0)
    sdg = _ranked(1.0, cat="SDG", sel="HWRAP", desc=_WRAP_DESC, pos=1)
    diag = ranking.classify_decision_with_diagnostics([ins, sdg], ranking_config, preferred_categories=("SDG",))
    assert diag.decision == DECISION_AUTO_SELECT
    assert diag.gate == "exact_tie_resolved_by_context"
    assert diag.top_category == "SDG"
    assert diag.selected_candidate is sdg


def test_exact_tie_context_declines_without_category_evidence_row11_shape(ranking_config):
    """Row 11: trade='unknown' yields no hint at all -- there is no
    defensible signal for the CONTEXTUAL resolver to prefer RFG or SDG,
    so `tie_resolution.resolved` stays False, exactly as before Phase
    5.22 existed. The OVERALL decision, however, now AUTO_SELECTs via
    the separate first-candidate fallback (see
    test_exact_tie_first_candidate_fallback_row11_shape below) rather
    than staying REVIEW_REQUIRED forever -- this test isolates and
    proves only the context resolver's own half of that story."""
    rfg = _ranked(1.0, cat="RFG", sel="STEP", desc=_STEP_DESC, pos=0)
    sdg = _ranked(1.0, cat="SDG", sel="STEP", desc=_STEP_DESC, pos=1)
    diag = ranking.classify_decision_with_diagnostics([rfg, sdg], ranking_config, preferred_categories=())
    assert diag.decision == DECISION_AUTO_SELECT
    assert diag.gate == "exact_tie_resolved_by_first_candidate_fallback"
    tr = diag.tie_resolution
    assert tr is not None
    assert tr.tie_detected is True
    assert tr.resolved is False
    assert tr.resolved_category is None
    assert tr.reason == "no_category_hint_available"


def test_exact_tie_context_declines_with_conflicting_category_evidence(ranking_config):
    """Both tied categories appear in the hint list -- the CONTEXTUAL
    resolver does not discriminate between them (conflicting evidence
    is treated the same as no evidence, never a guess); the first-
    candidate fallback still applies on top of that, same as any other
    unresolved exact tie."""
    rfg = _ranked(1.0, cat="RFG", sel="STEP", desc=_STEP_DESC, pos=0)
    sdg = _ranked(1.0, cat="SDG", sel="STEP", desc=_STEP_DESC, pos=1)
    diag = ranking.classify_decision_with_diagnostics([rfg, sdg], ranking_config, preferred_categories=("RFG", "SDG"))
    assert diag.decision == DECISION_AUTO_SELECT
    assert diag.gate == "exact_tie_resolved_by_first_candidate_fallback"
    tr = diag.tie_resolution
    assert tr.resolved is False
    assert tr.reason == "both_candidates_match_hint"


def test_exact_tie_context_declines_when_evidence_matches_neither_candidate(ranking_config):
    """The hint list is non-empty but names a category that is neither
    of the two tied candidates -- also not defensible evidence for
    either one specifically, so the contextual resolver still declines;
    the first-candidate fallback applies on top of that."""
    rfg = _ranked(1.0, cat="RFG", sel="STEP", desc=_STEP_DESC, pos=0)
    sdg = _ranked(1.0, cat="SDG", sel="STEP", desc=_STEP_DESC, pos=1)
    diag = ranking.classify_decision_with_diagnostics([rfg, sdg], ranking_config, preferred_categories=("ELS",))
    assert diag.decision == DECISION_AUTO_SELECT
    assert diag.gate == "exact_tie_resolved_by_first_candidate_fallback"
    tr = diag.tie_resolution
    assert tr.resolved is False
    assert tr.reason == "neither_candidate_matches_hint"


def test_exact_tie_resolution_unaffected_by_candidate_order(ranking_config):
    """Candidate order reversal must not change which candidate wins --
    the winner is chosen by which category the context supports, never
    by list position."""
    ins = _ranked(1.0, cat="INS", sel="HWRAP", desc=_WRAP_DESC, pos=0)
    sdg = _ranked(1.0, cat="SDG", sel="HWRAP", desc=_WRAP_DESC, pos=1)
    forward = ranking.classify_decision_with_diagnostics([ins, sdg], ranking_config, preferred_categories=("SDG",))
    reversed_ = ranking.classify_decision_with_diagnostics([sdg, ins], ranking_config, preferred_categories=("SDG",))
    assert forward.decision == reversed_.decision == DECISION_AUTO_SELECT
    assert forward.top_category == reversed_.top_category == "SDG"
    assert forward.top_selector == reversed_.top_selector == "HWRAP"
    assert forward.selected_candidate is sdg
    assert reversed_.selected_candidate is sdg


def test_ordinary_clear_margin_case_unaffected_by_preferred_categories(ranking_config):
    """A normal, non-tied AUTO_SELECT must behave identically whether or
    not preferred_categories is supplied -- tie resolution never even
    attempts to run outside the exact-tie branch."""
    top = _ranked(1.0, cat="RFG", sel="X")
    second = _ranked(0.5, cat="SDG", sel="Y")
    diag = ranking.classify_decision_with_diagnostics([top, second], ranking_config, preferred_categories=("SDG",))
    assert diag.decision == DECISION_AUTO_SELECT
    assert diag.gate == "clear_margin"
    assert diag.tie_resolution is None
    assert diag.selected_candidate is top


def test_near_tie_row27_shape_is_not_treated_as_an_exact_tie(ranking_config):
    """Row 27 shape: top 0.9006 vs second 0.8485 -- a real margin gap,
    not a bitwise score tie. Tie resolution must never fire here, even
    when preferred_categories would otherwise discriminate."""
    top = _ranked(0.9006, cat="FCC", sel="PAD-")
    second = _ranked(0.8485, cat="FCT", sel="B-")
    diag = ranking.classify_decision_with_diagnostics([top, second], ranking_config, preferred_categories=("FCC", "FCT"))
    assert diag.decision == DECISION_REVIEW_REQUIRED
    assert diag.gate == "insufficient_margin"
    assert diag.tie_resolution is None


def test_near_tie_row35_shape_is_not_treated_as_an_exact_tie(ranking_config):
    """Row 35 shape: top 0.9379 vs second 0.9306 -- thinnest near-miss
    margin in the benchmark, still not an exact tie."""
    top = _ranked(0.9379, cat="WDW", sel="MN")
    second = _ranked(0.9306, cat="WDV", sel="MN")
    diag = ranking.classify_decision_with_diagnostics([top, second], ranking_config, preferred_categories=("WDW", "WDV"))
    assert diag.decision == DECISION_REVIEW_REQUIRED
    assert diag.gate == "insufficient_margin"
    assert diag.tie_resolution is None


def test_score_limited_row6_shape_never_reaches_tie_resolution(ranking_config):
    """Row 6 shape: top score (0.6996) itself is below auto_select_min
    -- the margin/tie branch is never even reached, regardless of any
    preferred_categories passed in."""
    top = _ranked(0.6996, cat="RFG", sel="VENTRS")
    second = _ranked(0.6658, cat="RFG", sel="RIDGTRS")
    diag = ranking.classify_decision_with_diagnostics([top, second], ranking_config, preferred_categories=("RFG",))
    assert diag.decision == DECISION_REVIEW_REQUIRED
    assert diag.gate == "below_auto_select_min"
    assert diag.tie_resolution is None


def test_score_limited_row28_shape_never_reaches_tie_resolution(ranking_config):
    """Row 28/31 shape: top score (0.45) is below review_required_min
    entirely -- NO_MATCH via the very first gate, tie resolution never
    attempted."""
    top = _ranked(0.45, cat="SDG", sel="SHTR", conflict_reasons=["wrong_component: ['window'] not found in candidate description"])
    second = _ranked(0.45, cat="SDG", sel="SHTR<")
    diag = ranking.classify_decision_with_diagnostics([top, second], ranking_config, preferred_categories=("SDG",))
    assert diag.decision == DECISION_NO_MATCH
    assert diag.gate == "below_review_required_min"
    assert diag.tie_resolution is None


def test_same_category_tie_context_does_not_resolve_it():
    """Two candidates tied in the SAME category (e.g. duplicate catalog
    rows) is a different problem than cross-category ambiguity -- a
    trade/component hint could never discriminate between them anyway,
    and the CONTEXTUAL resolver must not try. The first-candidate
    fallback still applies on top, same as any other unresolved tie."""
    config = ranking.load_ranking_config()
    a = _ranked(1.0, cat="RFG", sel="A", desc="Drip edge", pos=0)
    b = _ranked(1.0, cat="RFG", sel="B", desc="Drip edge", pos=1)
    diag = ranking.classify_decision_with_diagnostics([a, b], config, preferred_categories=("RFG",))
    assert diag.decision == DECISION_AUTO_SELECT
    assert diag.gate == "exact_tie_resolved_by_first_candidate_fallback"
    tr = diag.tie_resolution
    assert tr.resolved is False
    assert tr.reason == "same_category_tie"


def test_score_tie_without_textual_equivalence_context_does_not_resolve_it():
    """A numeric score tie alone is not sufficient for the CONTEXTUAL
    resolver -- it's scoped to genuinely textually-equivalent candidates
    (the live-caught shape), not any two candidates that happen to sum
    to the same weighted score for unrelated reasons. The first-
    candidate fallback still applies on top, same as any other
    unresolved exact tie."""
    config = ranking.load_ranking_config()
    a = _ranked(1.0, cat="ELS", sel="A", desc="Digital satellite system", pos=0)
    b = _ranked(1.0, cat="RFG", sel="B", desc="Something completely different", pos=1)
    diag = ranking.classify_decision_with_diagnostics([a, b], config, preferred_categories=("ELS",))
    assert diag.decision == DECISION_AUTO_SELECT
    assert diag.gate == "exact_tie_resolved_by_first_candidate_fallback"
    tr = diag.tie_resolution
    assert tr.resolved is False
    assert tr.reason == "not_textually_equivalent"


def test_three_way_tie_context_does_not_resolve_it_but_fallback_still_picks_first():
    """A third candidate tied at the exact same score means this isn't a
    clean pairwise ambiguity for the CONTEXTUAL resolver -- no
    defensible single winner, even when the top two would otherwise
    resolve cleanly. The first-candidate fallback is deliberately
    broader than the contextual resolver (see its own docstring) and
    still applies here, picking whichever candidate is in position 0 --
    the least-defensible sub-case of an explicitly accepted, temporary
    mechanism, not a claim that position 0 is semantically correct."""
    config = ranking.load_ranking_config()
    a = _ranked(1.0, cat="ELS", sel="A", desc=_SATELLITE_DESC, pos=0)
    b = _ranked(1.0, cat="RFG", sel="B", desc=_SATELLITE_DESC, pos=1)
    c = _ranked(1.0, cat="HVC", sel="C", desc=_SATELLITE_DESC, pos=2)
    diag = ranking.classify_decision_with_diagnostics([a, b, c], config, preferred_categories=("ELS",))
    assert diag.decision == DECISION_AUTO_SELECT
    assert diag.gate == "exact_tie_resolved_by_first_candidate_fallback"
    assert diag.selected_candidate is a
    tr = diag.tie_resolution
    assert tr.resolved is False
    assert tr.reason == "three_or_more_way_tie"


def test_classify_decision_plain_wrapper_reflects_tie_resolution_when_passed_through():
    """classify_decision() itself never received preferred_categories in
    this codebase's real call sites (only classify_decision_with_
    diagnostics() does, from orchestrator.py) -- confirms it still
    delegates correctly if a caller does pass one."""
    config = ranking.load_ranking_config()
    els = _ranked(1.0, cat="ELS", sel="DISHRS", desc=_SATELLITE_DESC, pos=0)
    rfg = _ranked(1.0, cat="RFG", sel="DISHRS", desc=_SATELLITE_DESC, pos=1)
    # No preferred_categories -> context can't resolve it -> the
    # first-candidate fallback AUTO_SELECTs position 0 (ELS) instead.
    assert ranking.classify_decision([els, rfg], config) == DECISION_AUTO_SELECT
    diag = ranking.classify_decision_with_diagnostics([els, rfg], config, preferred_categories=("ELS",))
    assert diag.decision == DECISION_AUTO_SELECT


def test_selected_candidate_is_none_for_review_required_and_no_match(ranking_config):
    d1 = ranking.classify_decision_with_diagnostics([], ranking_config)
    assert d1.selected_candidate is None
    top = _ranked(0.6)
    second = _ranked(0.5)
    d2 = ranking.classify_decision_with_diagnostics([top, second], ranking_config)
    assert d2.decision == DECISION_REVIEW_REQUIRED
    assert d2.selected_candidate is None


def test_tie_resolution_to_dict_serializes_all_fields(ranking_config):
    els = _ranked(1.0, cat="ELS", sel="DISHRS", desc=_SATELLITE_DESC, pos=0)
    rfg = _ranked(1.0, cat="RFG", sel="DISHRS", desc=_SATELLITE_DESC, pos=1)
    diag = ranking.classify_decision_with_diagnostics([els, rfg], ranking_config, preferred_categories=("ELS",))
    d = diag.to_dict()["tie_resolution"]
    assert d["tie_detected"] is True
    assert d["resolved"] is True
    assert d["category_a"] == "ELS"
    assert d["category_b"] == "RFG"
    assert d["preferred_categories"] == ["ELS"]
    assert d["resolved_category"] == "ELS"
    assert d["resolved_selector"] == "DISHRS"
    assert d["reason"] == "context_matched_one_candidate"


def test_below_floor_dominance_real_0016_shape_correctly_refuses_to_fire():
    """Live-caught regression lock (Phase 5.18): 0016's real full
    candidate list (RFG/PAVCRS top, RFG/PAVRS runner-up) reduces to a
    true margin of ~0.0555 once the genuine competing candidate is
    included -- well under the strict 0.20 requirement. An earlier,
    simplified 2-candidate offline reconstruction of this row (omitting
    PAVRS) showed a misleadingly wide 0.31 margin and would have wrongly
    cleared the override; this test locks in the real, full-candidate
    shape so that mistake can't silently recur."""
    config = ranking.load_ranking_config()
    top = _below_floor_ranked(0.7958, match_reasons=["action 'install' matches"], cat="RFG", sel="PAVCRS")
    runner_up = _below_floor_ranked(0.7403, cat="RFG", sel="PAVRS")
    assert ranking.classify_decision([top, runner_up], config) == DECISION_REVIEW_REQUIRED


# ---------------------------------------------------------------------
# Phase 5.22: narrow, explicit fallback for a genuine exact tie that the
# contextual resolver (Phase 5.19) was given a real chance to break and
# could not. Deliberately order-DEPENDENT (picks candidates[0]) -- an
# accepted, temporary mechanism, not a claim of semantic preference.
# Each test below maps directly to one required scenario.
# ---------------------------------------------------------------------


def test_fallback_1_unresolved_exact_tie_selects_first_candidate(ranking_config):
    top = _ranked(1.0, cat="RFG", sel="A", desc="Step flashing", pos=0)
    second = _ranked(1.0, cat="SDG", sel="B", desc="Step flashing", pos=1)
    diag = ranking.classify_decision_with_diagnostics([top, second], ranking_config)
    assert diag.decision == DECISION_AUTO_SELECT
    assert diag.gate == "exact_tie_resolved_by_first_candidate_fallback"
    assert diag.selected_candidate is top
    assert diag.top_selector == "A"


def test_fallback_2_reversing_order_reverses_selected_candidate(ranking_config):
    a = _ranked(1.0, cat="RFG", sel="A", desc="Step flashing")
    b = _ranked(1.0, cat="SDG", sel="B", desc="Step flashing")
    forward = ranking.classify_decision_with_diagnostics([a, b], ranking_config)
    reversed_ = ranking.classify_decision_with_diagnostics([b, a], ranking_config)
    assert forward.gate == reversed_.gate == "exact_tie_resolved_by_first_candidate_fallback"
    assert forward.top_selector == "A"
    assert reversed_.top_selector == "B"
    assert forward.selected_candidate is a
    assert reversed_.selected_candidate is b


def test_fallback_3_contextual_tie_resolution_still_takes_precedence(ranking_config):
    """When the contextual resolver CAN establish a preferred candidate,
    it wins -- the fallback never overrides a real signal, and the gate
    proves which mechanism actually decided."""
    els = _ranked(1.0, cat="ELS", sel="DISHRS", desc=_SATELLITE_DESC, pos=0)
    rfg = _ranked(1.0, cat="RFG", sel="DISHRS", desc=_SATELLITE_DESC, pos=1)
    diag = ranking.classify_decision_with_diagnostics([els, rfg], ranking_config, preferred_categories=("ELE", "ELS"))
    assert diag.decision == DECISION_AUTO_SELECT
    assert diag.gate == "exact_tie_resolved_by_context"
    assert diag.gate != "exact_tie_resolved_by_first_candidate_fallback"
    assert diag.selected_candidate is els


def test_fallback_4_near_tie_is_unaffected(ranking_config):
    """Not a bitwise score tie -- the fallback must never fire here,
    only the ordinary insufficient_margin gate."""
    top = _ranked(0.9006, cat="FCC", sel="PAD-")
    second = _ranked(0.8485, cat="FCT", sel="B-")
    diag = ranking.classify_decision_with_diagnostics([top, second], ranking_config)
    assert diag.decision == DECISION_REVIEW_REQUIRED
    assert diag.gate == "insufficient_margin"


def test_fallback_5_below_threshold_exact_tie_is_unaffected(ranking_config):
    """Both candidates tie exactly, but below auto_select_min -- the
    fallback lives strictly inside the post-threshold margin branch and
    must never fire here; below_auto_select_min still governs."""
    top = _ranked(0.60, cat="RFG", sel="A")
    second = _ranked(0.60, cat="SDG", sel="B")
    diag = ranking.classify_decision_with_diagnostics([top, second], ranking_config)
    assert diag.decision == DECISION_REVIEW_REQUIRED
    assert diag.gate == "below_auto_select_min"
    assert diag.tie_resolution is None


def test_fallback_6_hard_conflict_exact_tie_is_unaffected(ranking_config):
    """Top candidate ties the second candidate's score exactly, but has
    a hard conflict -- the hard_conflict gate fires first and the
    fallback must never override it."""
    top = _ranked(1.0, cat="RFG", sel="A", conflict_reasons=["wrong_material: candidate states a different material"])
    second = _ranked(1.0, cat="SDG", sel="B")
    diag = ranking.classify_decision_with_diagnostics([top, second], ranking_config)
    assert diag.decision == DECISION_REVIEW_REQUIRED
    assert diag.gate == "hard_conflict"
    assert diag.tie_resolution is None


def test_fallback_7_low_extraction_confidence_exact_tie_is_unaffected(ranking_config):
    """Top candidate ties the second candidate's score exactly, but its
    own extraction confidence is below the minimum -- that gate fires
    first and the fallback must never override it."""
    top = _ranked(1.0, cat="RFG", sel="A", conf=0.5)
    second = _ranked(1.0, cat="SDG", sel="B")
    diag = ranking.classify_decision_with_diagnostics([top, second], ranking_config)
    assert diag.decision == DECISION_REVIEW_REQUIRED
    assert diag.gate == "low_extraction_confidence"
    assert diag.tie_resolution is None


def test_fallback_8_ordinary_clear_margin_behavior_is_unaffected(ranking_config):
    """A normal, decisively-won AUTO_SELECT (no tie at all) must behave
    identically to before this fallback existed."""
    top = _ranked(1.0, cat="RFG", sel="A")
    second = _ranked(0.5, cat="SDG", sel="B")
    diag = ranking.classify_decision_with_diagnostics([top, second], ranking_config)
    assert diag.decision == DECISION_AUTO_SELECT
    assert diag.gate == "clear_margin"
    assert diag.tie_resolution is None
