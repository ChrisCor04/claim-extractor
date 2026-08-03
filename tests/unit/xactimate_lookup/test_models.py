from __future__ import annotations

from estimate_extractor.xactimate_lookup.models import InternalMappingRecord, MAPPING_STATUS_DISABLED


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
