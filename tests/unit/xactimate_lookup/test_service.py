from __future__ import annotations

import json

import pytest

from estimate_extractor.xactimate_lookup import service
from estimate_extractor.xactimate_lookup.adapter import FakeXactimateAdapter
from estimate_extractor.xactimate_lookup.models import DropdownResult, LOOKUP_PATH_DESCRIPTION_SEARCH, LOOKUP_PATH_TRUSTED


def _normalized_item(line_item_id, description, quantity=10.0, unit="SQ", trade="roofing", component="composition_shingles", material="3-tab composition shingles", action="remove"):
    return {
        "line_item_id": line_item_id,
        "original": {
            "description": description, "quantity": quantity, "unit_of_measure": unit, "coverage_id": "coverage_001",
            "section_name": "Dwelling Roof", "area_name": "Dwelling", "source_pages": [7], "notes": [],
            "extraction_confidence": 0.95, "extraction_needs_review": False, "extraction_warnings": [],
        },
        "normalized": {
            "action": action, "trade": trade, "component": component, "material": material,
            "attributes": {}, "quantity": quantity, "unit_of_measure": unit,
        },
        "confidence": {"overall": 0.9, "action": 0.9, "trade": 0.9, "component": 0.9, "material": 0.9},
        "needs_review": False, "review_reasons": [],
    }


def _mapped_item(line_item_id, status="mapped", best_match=None, trade="roofing", component="composition_shingles", material="3-tab composition shingles", action="remove", quantity=10.0, unit="SQ"):
    return {
        "line_item_id": line_item_id, "coverage_id": "coverage_001",
        "normalization": {"action": action, "trade": trade, "component": component, "material": material, "attributes": {}, "quantity": quantity, "unit_of_measure": unit},
        "mapping": {"status": status, "best_match": best_match, "alternatives": [], "needs_review": status != "mapped", "review_reasons": []},
    }


def _write_project(tmp_path, normalized, mapped):
    project_dir = tmp_path / "test-project"
    (project_dir / "mapping").mkdir(parents=True)
    (project_dir / "review").mkdir(parents=True)
    (project_dir / "mapping" / "normalized_estimate.json").write_text(json.dumps(normalized), encoding="utf-8")
    (project_dir / "mapping" / "mapped_estimate.json").write_text(json.dumps(mapped), encoding="utf-8")
    return project_dir


DESC = "Tear off composition shingles - 3 tab (no haul off)"


def test_plan_for_project_defaults_to_description_search(tmp_path):
    normalized = [_normalized_item("line_0001", DESC)]
    mapped = [_mapped_item("line_0001")]
    project_dir = _write_project(tmp_path, normalized, mapped)

    plans = service.plan_for_project(project_dir, tmp_path / "reg.db", verified_catalog_path=None)
    assert len(plans) == 1
    assert plans[0].path == LOOKUP_PATH_DESCRIPTION_SEARCH
    assert plans[0].search_input


def test_record_resolution_then_replan_uses_trusted_path(tmp_path):
    normalized = [_normalized_item("line_0001", DESC)]
    mapped = [_mapped_item("line_0001")]
    project_dir = _write_project(tmp_path, normalized, mapped)
    registry_db = tmp_path / "reg.db"
    backups_dir = tmp_path / "backups"

    plans = service.plan_for_project(project_dir, registry_db, verified_catalog_path=None)
    plan = plans[0]

    from estimate_extractor.ui import review_service
    row = review_service.build_effective_rows(project_dir)[0]
    item = service.build_lookup_input(row, normalized[0])

    record = service.record_resolution(
        project_dir, registry_db, backups_dir, item, plan.item_signature, plan.search_input,
        category="RFG", selector="ARMVN", xactimate_description=DESC, unit="SQ", action="remove",
        xactimate_item_number="1234", reviewer="tester", approval_reason="Confirmed exact match in dropdown.",
    )
    assert record.status == "approved"

    replans = service.plan_for_project(project_dir, registry_db, verified_catalog_path=None)
    assert replans[0].path == LOOKUP_PATH_TRUSTED
    assert replans[0].trusted_mapping.category == "RFG"
    assert replans[0].trusted_mapping.selector == "ARMVN"


def test_record_resolution_requires_reviewer_and_reason(tmp_path):
    normalized = [_normalized_item("line_0001", DESC)]
    mapped = [_mapped_item("line_0001")]
    project_dir = _write_project(tmp_path, normalized, mapped)
    from estimate_extractor.ui import review_service

    row = review_service.build_effective_rows(project_dir)[0]
    item = service.build_lookup_input(row, normalized[0])

    with pytest.raises(service.LookupServiceError):
        service.record_resolution(
            project_dir, tmp_path / "reg.db", tmp_path / "backups", item, "sig", "phrase",
            category="RFG", selector="ARMVN", xactimate_description=DESC, unit="SQ", action="remove",
            xactimate_item_number=None, reviewer="", approval_reason="reason",
        )
    with pytest.raises(service.LookupServiceError):
        service.record_resolution(
            project_dir, tmp_path / "reg.db", tmp_path / "backups", item, "sig", "phrase",
            category="RFG", selector="ARMVN", xactimate_description=DESC, unit="SQ", action="remove",
            xactimate_item_number=None, reviewer="tester", approval_reason="",
        )


def test_record_resolution_blocks_silent_conflict(tmp_path):
    normalized = [_normalized_item("line_0001", DESC)]
    mapped = [_mapped_item("line_0001")]
    project_dir = _write_project(tmp_path, normalized, mapped)
    registry_db = tmp_path / "reg.db"
    backups_dir = tmp_path / "backups"
    from estimate_extractor.ui import review_service

    row = review_service.build_effective_rows(project_dir)[0]
    item = service.build_lookup_input(row, normalized[0])

    service.record_resolution(
        project_dir, registry_db, backups_dir, item, "sig-1", "phrase",
        category="RFG", selector="ARMVN", xactimate_description=DESC, unit="SQ", action="remove",
        xactimate_item_number=None, reviewer="tester", approval_reason="first approval",
    )
    with pytest.raises(service.MappingConflictError):
        service.record_resolution(
            project_dir, registry_db, backups_dir, item, "sig-1", "phrase",
            category="RFG", selector="DIFFERENT", xactimate_description=DESC, unit="SQ", action="remove",
            xactimate_item_number=None, reviewer="tester2", approval_reason="trying to change it",
        )


def test_record_resolution_allow_override_replaces_mapping(tmp_path):
    normalized = [_normalized_item("line_0001", DESC)]
    mapped = [_mapped_item("line_0001")]
    project_dir = _write_project(tmp_path, normalized, mapped)
    registry_db = tmp_path / "reg.db"
    backups_dir = tmp_path / "backups"
    from estimate_extractor.ui import review_service

    row = review_service.build_effective_rows(project_dir)[0]
    item = service.build_lookup_input(row, normalized[0])

    first = service.record_resolution(
        project_dir, registry_db, backups_dir, item, "sig-1", "phrase",
        category="RFG", selector="ARMVN", xactimate_description=DESC, unit="SQ", action="remove",
        xactimate_item_number=None, reviewer="tester", approval_reason="first approval",
    )
    updated = service.record_resolution(
        project_dir, registry_db, backups_dir, item, "sig-1", "phrase",
        category="RFG", selector="CORRECTED", xactimate_description=DESC, unit="SQ", action="remove",
        xactimate_item_number=None, reviewer="tester2", approval_reason="correcting a mistake", allow_override=True,
    )
    assert updated.mapping_id == first.mapping_id
    assert updated.selector == "CORRECTED"


def test_disable_mapping_makes_it_unreusable(tmp_path):
    normalized = [_normalized_item("line_0001", DESC)]
    mapped = [_mapped_item("line_0001")]
    project_dir = _write_project(tmp_path, normalized, mapped)
    registry_db = tmp_path / "reg.db"
    backups_dir = tmp_path / "backups"
    from estimate_extractor.ui import review_service

    row = review_service.build_effective_rows(project_dir)[0]
    item = service.build_lookup_input(row, normalized[0])
    record = service.record_resolution(
        project_dir, registry_db, backups_dir, item, "sig-1", "phrase",
        category="RFG", selector="ARMVN", xactimate_description=DESC, unit="SQ", action="remove",
        xactimate_item_number=None, reviewer="tester", approval_reason="first approval",
    )
    service.disable_mapping(registry_db, backups_dir, record.mapping_id, "tester", "no longer valid")

    active = service.list_mappings(registry_db, active_only=True)
    all_records = service.list_mappings(registry_db, active_only=False)
    assert active == []
    assert len(all_records) == 1
    assert all_records[0].status == "disabled"

    replans = service.plan_for_project(project_dir, registry_db, verified_catalog_path=None)
    assert replans[0].path == LOOKUP_PATH_DESCRIPTION_SEARCH  # disabled mapping is never reused


def test_disable_mapping_requires_reason(tmp_path):
    with pytest.raises(service.LookupServiceError):
        service.disable_mapping(tmp_path / "reg.db", tmp_path / "backups", "m1", "tester", "")


def test_dry_run_for_project_never_commits(tmp_path):
    normalized = [_normalized_item("line_0001", DESC)]
    mapped = [_mapped_item("line_0001")]
    project_dir = _write_project(tmp_path, normalized, mapped)
    d = DropdownResult(raw_text=DESC, row_position=0, category="RFG", selector="ARMVN", description=DESC, extraction_confidence=0.97)
    adapter = FakeXactimateAdapter(dropdown_script={"composition shingles 3 tab": [d]})
    outcomes = service.dry_run_for_project(project_dir, adapter, tmp_path / "reg.db", verified_catalog_path=None)
    assert len(outcomes) == 1
    assert not any(name == "commit_item" for name, _a, _k in adapter.log.calls)


def test_compute_lookup_stats_counts_trusted_and_description_search(tmp_path):
    normalized = [_normalized_item("line_0001", DESC), _normalized_item("line_0002", "Roofing felt - 15 lb.", component="unknown", material=None)]
    mapped = [_mapped_item("line_0001"), _mapped_item("line_0002", component="unknown", material=None)]
    project_dir = _write_project(tmp_path, normalized, mapped)
    stats = service.compute_lookup_stats([project_dir], tmp_path / "reg.db", verified_catalog_path=None)
    assert stats.items_evaluated == 2
    assert stats.items_requiring_description_search == 2
    assert stats.items_resolved_by_existing_mapping == 0
    assert stats.learned_mapping_reuse_rate == 0.0


def test_compute_lookup_stats_excludes_synthetic_ground_truth(tmp_path):
    normalized = [_normalized_item("line_0001", DESC)]
    mapped = [_mapped_item("line_0001", best_match={"mapping_id": "m1", "category": "TEST_CAT", "selector": "TEST_SEL", "activity": "remove", "description": "d", "confidence": 0.9})]
    project_dir = _write_project(tmp_path, normalized, mapped)
    state = {
        "line_0001": {"status": "approved", "overrides": {}, "reviewer": "benchmark-run", "reviewer_note": "simulated verification", "updated_at": "2026-01-01T00:00:00+00:00"}
    }
    (project_dir / "review" / "review_state.json").write_text(json.dumps(state), encoding="utf-8")

    stats = service.compute_lookup_stats([project_dir], tmp_path / "reg.db", verified_catalog_path=None)
    assert stats.ground_truth_items == 0
    assert stats.top1_agreement is None


# ---------------------------------------------------------------------------
# record_resolution: applies to the item itself, not just the registry
# ---------------------------------------------------------------------------


def test_record_resolution_applies_category_selector_to_the_item(tmp_path):
    from estimate_extractor.ui import review_service

    normalized = [_normalized_item("line_0001", DESC)]
    mapped = [_mapped_item("line_0001")]
    project_dir = _write_project(tmp_path, normalized, mapped)

    row = review_service.build_effective_rows(project_dir)[0]
    item = service.build_lookup_input(row, normalized[0])
    service.record_resolution(
        project_dir, tmp_path / "reg.db", tmp_path / "backups", item, "sig-1", "phrase",
        category="RFG", selector="ARMVN", xactimate_description=DESC, unit="SQ", action="remove",
        xactimate_item_number="1234", reviewer="tester", approval_reason="matches dropdown row 1",
    )
    updated_row = review_service.build_effective_rows(project_dir)[0]
    assert updated_row["category"] == "RFG"
    assert updated_row["selector"] == "ARMVN"
    assert updated_row["status"] == review_service.STATUS_UNREVIEWED  # approve=False by default


def test_record_resolution_with_approve_flag_approves_the_item(tmp_path):
    from estimate_extractor.ui import review_service

    normalized = [_normalized_item("line_0001", DESC)]
    mapped = [_mapped_item("line_0001", best_match={"mapping_id": "m1", "category": "RFG", "selector": None, "activity": "remove", "description": "d", "confidence": 0.5})]
    project_dir = _write_project(tmp_path, normalized, mapped)

    row = review_service.build_effective_rows(project_dir)[0]
    item = service.build_lookup_input(row, normalized[0])
    service.record_resolution(
        project_dir, tmp_path / "reg.db", tmp_path / "backups", item, "sig-1", "phrase",
        category="RFG", selector="ARMVN", xactimate_description=DESC, unit="SQ", action="remove",
        xactimate_item_number=None, reviewer="tester", approval_reason="matches dropdown row 1", approve=True,
    )
    updated_row = review_service.build_effective_rows(project_dir)[0]
    assert updated_row["status"] == review_service.STATUS_APPROVED


def test_record_resolution_blocks_silent_overwrite_of_approved_item(tmp_path):
    from estimate_extractor.ui import review_service

    normalized = [_normalized_item("line_0001", DESC)]
    mapped = [_mapped_item("line_0001", best_match={"mapping_id": "m1", "category": "RFG", "selector": "ORIGINAL", "activity": "remove", "description": "d", "confidence": 0.9})]
    project_dir = _write_project(tmp_path, normalized, mapped)
    review_service.approve_item(project_dir, "line_0001", "tester")

    row = review_service.build_effective_rows(project_dir)[0]
    item = service.build_lookup_input(row, normalized[0])

    with pytest.raises(service.LookupApplyBlockedError):
        service.record_resolution(
            project_dir, tmp_path / "reg.db", tmp_path / "backups", item, "sig-1", "phrase",
            category="RFG", selector="DIFFERENT", xactimate_description=DESC, unit="SQ", action="remove",
            xactimate_item_number=None, reviewer="tester2", approval_reason="trying to override",
        )
    unchanged = review_service.build_effective_rows(project_dir)[0]
    assert unchanged["selector"] == "ORIGINAL"


def test_record_resolution_item_only_skips_registry(tmp_path):
    from estimate_extractor.ui import review_service

    normalized = [_normalized_item("line_0001", DESC)]
    mapped = [_mapped_item("line_0001")]
    project_dir = _write_project(tmp_path, normalized, mapped)
    registry_db = tmp_path / "reg.db"

    row = review_service.build_effective_rows(project_dir)[0]
    item = service.build_lookup_input(row, normalized[0])
    result = service.record_resolution(
        project_dir, registry_db, tmp_path / "backups", item, "sig-1", "phrase",
        category="RFG", selector="ARMVN", xactimate_description=DESC, unit="SQ", action="remove",
        xactimate_item_number=None, reviewer="tester", approval_reason="item only", save_as_reusable_mapping=False,
    )
    assert result is None
    assert service.list_mappings(registry_db, active_only=False) == []
    updated_row = review_service.build_effective_rows(project_dir)[0]
    assert updated_row["selector"] == "ARMVN"  # still applied to the item


# ---------------------------------------------------------------------------
# Automation plan / diagnostics (Phase 4.0)
# ---------------------------------------------------------------------------


def test_build_automation_plan_describes_description_search_item(tmp_path):
    normalized = [_normalized_item("line_0001", DESC)]
    mapped = [_mapped_item("line_0001")]
    project_dir = _write_project(tmp_path, normalized, mapped)

    entries = service.build_automation_plan(project_dir, tmp_path / "reg.db", verified_catalog_path=None)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["lookup_method"] == "description_search"
    assert entry["search_phrase"]
    assert entry["expected_dropdown_terms"]
    assert entry["quantity"] == 10.0
    assert "search_by_description" in entry["planned_adapter_actions"]
    assert "unsupported_adapter" in entry["stop_conditions"]
    assert "unit_mismatch" in entry["stop_conditions"]


def test_build_automation_plan_describes_trusted_item(tmp_path):
    from estimate_extractor.ui import review_service

    normalized = [_normalized_item("line_0001", DESC)]
    mapped = [_mapped_item("line_0001")]
    project_dir = _write_project(tmp_path, normalized, mapped)
    registry_db = tmp_path / "reg.db"

    row = review_service.build_effective_rows(project_dir)[0]
    item = service.build_lookup_input(row, normalized[0])
    plans = service.plan_for_project(project_dir, registry_db, verified_catalog_path=None)
    service.record_resolution(
        project_dir, registry_db, tmp_path / "backups", item, plans[0].item_signature, plans[0].search_input,
        category="RFG", selector="ARMVN", xactimate_description=DESC, unit="SQ", action="remove",
        xactimate_item_number=None, reviewer="tester", approval_reason="first approval",
    )

    entries = service.build_automation_plan(project_dir, registry_db, verified_catalog_path=None)
    entry = entries[0]
    assert entry["lookup_method"] == "trusted_cat_sel"
    assert entry["search_input"] == "RFG ARMVN"
    assert entry["expected_dropdown_terms"] == ["RFG", "ARMVN"]
    assert "search_by_category_selector" in entry["planned_adapter_actions"]


def test_run_diagnostics_against_default_fake_adapter_warns_no_real_adapter(tmp_path):
    report = service.run_diagnostics(registry_db_path=tmp_path / "reg.db")
    assert report.adapter_class == "FakeXactimateAdapter"
    assert report.supports_live_execution is False
    assert any("No real XactimateAdapter" in w for w in report.warnings)
    assert report.phrase_rules_loaded is True
    assert report.ranking_config_loaded is True


def test_run_diagnostics_with_explicit_adapter(tmp_path):
    from estimate_extractor.xactimate_lookup.adapter import FakeXactimateAdapter

    adapter = FakeXactimateAdapter(application_verified=False)
    report = service.run_diagnostics(adapter=adapter, registry_db_path=tmp_path / "reg.db")
    assert report.application_verified is False
    assert not any("No real XactimateAdapter" in w for w in report.warnings)


def test_events_are_colocated_with_the_registry_used_not_a_hardcoded_path(tmp_path):
    """Regression guard: the audit-event log must live beside whichever
    registry_db_path was actually used, never a hardcoded global path --
    otherwise every test/tmp-registry run would silently pollute the real
    repository's config/internal_lookup_events.json."""
    from estimate_extractor.ui import review_service

    normalized = [_normalized_item("line_0001", DESC)]
    mapped = [_mapped_item("line_0001")]
    project_dir = _write_project(tmp_path, normalized, mapped)
    registry_db = tmp_path / "some_subdir" / "reg.db"

    row = review_service.build_effective_rows(project_dir)[0]
    item = service.build_lookup_input(row, normalized[0])
    service.record_resolution(
        project_dir, registry_db, tmp_path / "backups", item, "sig-1", "phrase",
        category="RFG", selector="ARMVN", xactimate_description=DESC, unit="SQ", action="remove",
        xactimate_item_number=None, reviewer="tester", approval_reason="first approval",
    )

    events_path = registry_db.parent / "internal_lookup_events.json"
    assert events_path.exists()
    assert not service.DEFAULT_EVENTS_PATH.exists()
    events = service.get_events(registry_db)
    assert len(events) == 1


class _RealLikeAdapter(FakeXactimateAdapter):
    """A stand-in with a non-Fake class name and group-control methods,
    so compute_capability_flags() can be exercised against something
    that looks like a real, pilot-validated adapter without needing
    Windows/live Xactimate in a unit test."""

    supports_live_execution = True

    def ensure_group(self, group_name: str) -> None: ...

    def select_group(self, group_name: str) -> None: ...

    def verify_group(self, group_name: str) -> bool:
        return True


def test_capability_flags_with_no_adapter_are_conservative(tmp_path):
    flags = service.compute_capability_flags()
    assert flags.planning_available is True
    assert flags.resume_available is True
    assert flags.live_adapter_available is False
    assert flags.group_control_available is False
    assert flags.safe_autofill_available is False
    assert flags.production_project_allowed is False
    assert flags.unattended_mode_allowed is False
    assert any("No real XactimateAdapter" in n for n in flags.notes)


def test_capability_flags_fake_adapter_never_counts_as_real_even_if_flagged_live(tmp_path):
    """Flipping supports_live_execution on a FakeXactimateAdapter must
    never make it look like a validated live adapter -- the real/Fake
    distinction is load-bearing for safe_autofill_available."""
    adapter = FakeXactimateAdapter()
    adapter.supports_live_execution = True
    flags = service.compute_capability_flags(adapter)
    assert flags.live_adapter_available is False
    assert flags.safe_autofill_available is False


def test_capability_flags_real_adapter_with_group_control_enables_safe_autofill(tmp_path):
    adapter = _RealLikeAdapter()
    flags = service.compute_capability_flags(adapter)
    assert flags.live_adapter_available is True
    assert flags.group_control_available is True
    assert flags.safe_autofill_available is True
    # Never automatically enabled by a successful pilot -- always False.
    assert flags.production_project_allowed is False
    assert flags.unattended_mode_allowed is False


def test_capability_flags_real_adapter_without_group_control_blocks_safe_autofill():
    class _NoGroupControlAdapter(FakeXactimateAdapter):
        supports_live_execution = True

    flags = service.compute_capability_flags(_NoGroupControlAdapter())
    assert flags.live_adapter_available is True
    assert flags.group_control_available is False
    assert flags.safe_autofill_available is False


def test_capability_flags_unverified_project_blocks_everything_live():
    adapter = _RealLikeAdapter(project_verified=False)
    flags = service.compute_capability_flags(adapter)
    assert flags.live_adapter_available is False
    assert flags.group_control_available is False
    assert flags.safe_autofill_available is False
