from __future__ import annotations

from estimate_extractor.xactimate_lookup.models import (
    DECISION_AUTO_SELECT,
    InternalMappingRecord,
    LookupOutcome,
    MAPPING_STATUS_DISABLED,
)


def _record(**overrides):
    defaults = dict(
        mapping_id="m1", item_signature="sig", source_description="desc", search_phrase="phrase",
        category="RFG", selector="ARMVN", xactimate_description="Tear off shingles", unit="SQ",
        action="remove", reviewer="tester", approval_reason="matches",
    )
    defaults.update(overrides)
    return InternalMappingRecord(**defaults)


def test_round_trips_through_dict():
    record = _record()
    restored = InternalMappingRecord.from_dict(record.to_dict())
    assert restored == record


def test_is_reusable_true_for_approved():
    assert _record().is_reusable is True


def test_is_reusable_false_for_disabled():
    assert _record(status=MAPPING_STATUS_DISABLED).is_reusable is False


def test_preserves_selector_punctuation_exactly():
    record = _record(selector="ARMVN>>")
    restored = InternalMappingRecord.from_dict(record.to_dict())
    assert restored.selector == "ARMVN>>"


def _plan():
    from estimate_extractor.xactimate_lookup.models import LookupPlan, LOOKUP_PATH_DESCRIPTION_SEARCH

    return LookupPlan(
        line_item_id="line_0001", path=LOOKUP_PATH_DESCRIPTION_SEARCH, item_signature="sig", search_input="Drip edge",
    )


def test_lookup_outcome_to_dict_decision_diagnostics_is_none_when_absent():
    """Backward compatibility: an outcome built without ever reaching
    ranking (decision_diagnostics still at its default None) must
    serialize the new key as None rather than raise -- existing readers
    of the other to_dict() keys are completely unaffected."""
    outcome = LookupOutcome(line_item_id="line_0001", decision=DECISION_AUTO_SELECT, plan=_plan())
    assert outcome.to_dict()["decision_diagnostics"] is None


def test_lookup_outcome_to_dict_serializes_decision_diagnostics_when_present():
    class _FakeDiagnostics:
        def to_dict(self):
            return {"decision": DECISION_AUTO_SELECT, "gate": "clear_margin"}

    outcome = LookupOutcome(
        line_item_id="line_0001", decision=DECISION_AUTO_SELECT, plan=_plan(), decision_diagnostics=_FakeDiagnostics(),
    )
    assert outcome.to_dict()["decision_diagnostics"] == {"decision": DECISION_AUTO_SELECT, "gate": "clear_margin"}
