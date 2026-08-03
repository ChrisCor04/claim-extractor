from __future__ import annotations

import pytest

from estimate_extractor.xactimate_lookup.adapter import AdapterError, FakeXactimateAdapter, UnexpectedDialogError
from estimate_extractor.xactimate_lookup.models import DropdownResult


def test_fake_adapter_returns_scripted_results_for_matching_query():
    d = DropdownResult(raw_text="RFG ARMVN Tear off shingles", row_position=0, category="RFG", selector="ARMVN")
    adapter = FakeXactimateAdapter(dropdown_script={"shingles": [d]})
    adapter.focus_search()
    adapter.clear_search()
    adapter.search_by_description("shingles")
    raw = adapter.capture_dropdown()
    results = adapter.parse_dropdown(raw)
    assert results == [d]


def test_fake_adapter_search_by_category_selector_builds_expected_query():
    d = DropdownResult(raw_text="RFG ARMVN Tear off shingles", row_position=0, category="RFG", selector="ARMVN")
    adapter = FakeXactimateAdapter(dropdown_script={"RFG ARMVN": [d]})
    adapter.search_by_category_selector("RFG", "ARMVN")
    results = adapter.parse_dropdown(adapter.capture_dropdown())
    assert results == [d]


def test_fake_adapter_returns_empty_for_unscripted_query():
    adapter = FakeXactimateAdapter(dropdown_script={})
    adapter.search_by_description("nothing configured")
    assert adapter.parse_dropdown(adapter.capture_dropdown()) == []


def test_fake_adapter_context_verification_configurable():
    adapter = FakeXactimateAdapter(application_verified=False, project_verified=False)
    assert adapter.verify_application() is False
    assert adapter.verify_project() is False


def test_fake_adapter_context_verification_defaults_true():
    adapter = FakeXactimateAdapter()
    assert adapter.verify_application() is True
    assert adapter.verify_project() is True


def test_fake_adapter_simulates_extraction_failure():
    adapter = FakeXactimateAdapter(dropdown_script={"bad query": []}, fail_on_capture_for={"bad query"})
    adapter.search_by_description("bad query")
    with pytest.raises(AdapterError):
        adapter.capture_dropdown()


def test_fake_adapter_simulates_unexpected_dialog():
    adapter = FakeXactimateAdapter(dropdown_script={"q": []}, raise_unexpected_dialog_for={"q"})
    adapter.search_by_description("q")
    with pytest.raises(UnexpectedDialogError):
        adapter.capture_dropdown()


def test_fake_adapter_read_populated_fields_matches_selection():
    d = DropdownResult(raw_text="RFG ARMVN Tear off shingles", row_position=0, category="RFG", selector="ARMVN", description="Tear off shingles", item_number="42")
    adapter = FakeXactimateAdapter()
    adapter.select_candidate(d)
    populated = adapter.read_populated_fields()
    assert populated.category == "RFG"
    assert populated.selector == "ARMVN"
    assert populated.item_number == "42"


def test_fake_adapter_populated_unit_override():
    d = DropdownResult(raw_text="RFG ARMVN Tear off shingles", row_position=0, category="RFG", selector="ARMVN")
    adapter = FakeXactimateAdapter(populated_unit="LF")
    adapter.select_candidate(d)
    assert adapter.read_populated_fields().unit == "LF"


def test_fake_adapter_logs_every_call():
    adapter = FakeXactimateAdapter()
    adapter.verify_application()
    adapter.verify_project()
    adapter.focus_search()
    adapter.clear_search()
    names = [c[0] for c in adapter.log.calls]
    assert names == ["verify_application", "verify_project", "focus_search", "clear_search"]


def test_fake_adapter_evidence_reference_is_a_string_not_content():
    adapter = FakeXactimateAdapter()
    adapter.search_by_description("q")
    evidence = adapter.capture_evidence()
    assert isinstance(evidence, str)


def test_fake_adapter_supports_live_execution_defaults_false():
    assert FakeXactimateAdapter().supports_live_execution is False


def test_fake_adapter_recover_resets_state_and_logs():
    d = DropdownResult(raw_text="x", row_position=0, category="RFG", selector="ARMVN")
    adapter = FakeXactimateAdapter()
    adapter.search_by_description("q")
    adapter.select_candidate(d)
    adapter.recover()
    assert adapter.log.calls[-1][0] == "recover"
    populated = adapter.read_populated_fields()
    assert populated.category is None  # selection cleared by recover()
