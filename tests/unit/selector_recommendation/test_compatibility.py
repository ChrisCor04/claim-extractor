from __future__ import annotations

from estimate_extractor.selector_catalog.models import SelectorRecord, normalize_description
from estimate_extractor.selector_recommendation import compatibility
from estimate_extractor.selector_recommendation.models import RecommendationInput


def _rec(description, category="RFG", selector="X"):
    return SelectorRecord(
        category=category,
        selector=selector,
        description_original=description,
        description_normalized=normalize_description(description),
    )


def test_category_hinted_true_when_no_hints_given(rules):
    item = RecommendationInput(line_item_id="x")
    record = _rec("Tear off composition shingles")
    result = compatibility.compute_compatibility(item, record, [], rules)
    assert result.category_hinted is True


def test_category_hinted_false_when_category_outside_hints(rules):
    item = RecommendationInput(line_item_id="x")
    record = _rec("Overhead door - steel", category="DOR", selector="OHDOOR")
    result = compatibility.compute_compatibility(item, record, ["RFG"], rules)
    assert result.category_hinted is False


def test_action_match_when_item_action_phrase_present_in_description(rules):
    item = RecommendationInput(line_item_id="x", action="remove")
    record = _rec("Tear off composition shingles")
    result = compatibility.compute_compatibility(item, record, [], rules)
    assert result.action_signal == "match"
    assert result.action_match is True
    assert result.action_conflict is False


def test_action_conflict_when_description_implies_a_different_action(rules):
    item = RecommendationInput(line_item_id="x", action="install")
    record = _rec("Tear off composition shingles")  # implies "remove"
    result = compatibility.compute_compatibility(item, record, [], rules)
    assert result.action_signal == "conflict"
    assert result.action_conflict is True
    assert result.action_match is False


def test_action_unlabeled_when_item_has_no_action(rules):
    item = RecommendationInput(line_item_id="x", action=None)
    record = _rec("Tear off composition shingles")
    result = compatibility.compute_compatibility(item, record, [], rules)
    assert result.action_signal == "unlabeled"
    assert result.action_conflict is False


def test_action_none_when_description_carries_no_action_wording(rules):
    item = RecommendationInput(line_item_id="x", action="remove")
    record = _rec("Roofing felt - 30 lb.")
    result = compatibility.compute_compatibility(item, record, [], rules)
    assert result.action_signal == "none"
    assert result.action_conflict is False


def test_grade_conflict_when_item_and_candidate_disagree(rules):
    item = RecommendationInput(line_item_id="x", material="carpet - standard grade")
    record = _rec("Carpet - premium grade")
    result = compatibility.compute_compatibility(item, record, [], rules)
    assert result.grade_conflict is True


def test_grade_no_conflict_when_only_one_side_mentions_grade(rules):
    item = RecommendationInput(line_item_id="x", material="carpet")
    record = _rec("Carpet - premium grade")
    result = compatibility.compute_compatibility(item, record, [], rules)
    assert result.grade_conflict is False


def test_distinction_conflict_remove_vs_replace(rules):
    item = RecommendationInput(line_item_id="x", action="remove", original_description="Remove old fence")
    record = _rec("Replace wood fence panel")
    result = compatibility.compute_compatibility(item, record, [], rules)
    assert result.distinction_conflicts


def test_distinction_no_conflict_when_material_types_agree(rules):
    item = RecommendationInput(line_item_id="x", original_description="Remove metal fence panel")
    record = _rec("Remove metal fence panel - standard")
    result = compatibility.compute_compatibility(item, record, [], rules)
    assert result.distinction_conflicts == ()


def test_unit_signal_none_when_no_source_unit(rules):
    item = RecommendationInput(line_item_id="x", source_unit=None)
    record = _rec("Roofing felt - 30 lb. per SF")
    result = compatibility.compute_compatibility(item, record, [], rules)
    assert result.unit_compatible is None
