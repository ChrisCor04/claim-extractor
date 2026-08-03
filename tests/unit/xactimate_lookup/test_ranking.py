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


def test_ambiguous_candidates_without_margin_require_review(phrase_rules, ranking_config):
    a = _dropdown("Drip edge", sel="DRIP", pos=0)
    b = _dropdown("Drip edge - copper", sel="DRIPC", pos=1)
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
