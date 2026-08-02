from __future__ import annotations

from estimate_extractor.selector_recommendation import explanations
from estimate_extractor.selector_recommendation.compatibility import CompatibilityResult


def test_describe_description_similarity_thresholds():
    assert explanations.describe_description_similarity(0.9) is not None
    assert explanations.describe_description_similarity(0.6) is not None
    assert explanations.describe_description_similarity(0.1) is None


def test_category_penalty_only_when_hinted_and_incompatible():
    assert explanations.category_penalty(False, ["RFG"], "DOR") is not None
    assert explanations.category_penalty(True, ["RFG"], "RFG") is None
    assert explanations.category_penalty(False, [], "DOR") is None  # no hints -> nothing to violate


def test_action_penalty_only_on_conflict():
    conflict = CompatibilityResult(category_hinted=True, action_signal="conflict", action_match=False, action_conflict=True, unit_compatible=None, grade_conflict=False)
    match = CompatibilityResult(category_hinted=True, action_signal="match", action_match=True, action_conflict=False, unit_compatible=None, grade_conflict=False)
    assert explanations.action_penalty(conflict) is not None
    assert explanations.action_penalty(match) is None


def test_unit_penalty_and_reason_only_on_explicit_signal():
    incompatible = CompatibilityResult(category_hinted=True, action_signal="none", action_match=False, action_conflict=False, unit_compatible=False, grade_conflict=False)
    unknown = CompatibilityResult(category_hinted=True, action_signal="none", action_match=False, action_conflict=False, unit_compatible=None, grade_conflict=False)
    assert explanations.unit_penalty(incompatible) is not None
    assert explanations.unit_penalty(unknown) is None


def test_verified_evidence_reason_only_when_verified():
    assert explanations.verified_evidence_reason(True) is not None
    assert explanations.verified_evidence_reason(False) is None
