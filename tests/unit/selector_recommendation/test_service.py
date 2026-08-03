from __future__ import annotations

import json

import pytest

from estimate_extractor.selector_recommendation import service
from estimate_extractor.selector_recommendation.models import CANDIDATE_SOURCE_VERIFIED_CATALOG, Candidate
from estimate_extractor.ui import review_service
from estimate_extractor.ui import verified_catalog_service as vcs


def _normalized_item(line_item_id, description, quantity=10.0, unit="SQ", trade="roofing", component="composition_shingles", material="laminated composition shingles", action="remove_and_replace", attributes=None):
    return {
        "line_item_id": line_item_id,
        "original": {
            "description": description,
            "quantity": quantity,
            "unit_of_measure": unit,
            "coverage_id": "coverage_001",
            "section_name": "Dwelling Roof",
            "area_name": "Dwelling",
            "source_pages": [7],
            "notes": [],
            "extraction_confidence": 0.95,
            "extraction_needs_review": False,
            "extraction_warnings": [],
        },
        "normalized": {
            "action": action,
            "trade": trade,
            "component": component,
            "material": material,
            "attributes": attributes or {},
            "quantity": quantity,
            "unit_of_measure": unit,
        },
        "confidence": {"overall": 0.9, "action": 0.9, "trade": 0.9, "component": 0.9, "material": 0.9},
        "needs_review": False,
        "review_reasons": [],
    }


def _mapped_item(line_item_id, status="mapped", best_match=None, review_reasons=None, action="remove_and_replace", trade="roofing", component="composition_shingles", material="laminated composition shingles", quantity=10.0, unit="SQ"):
    return {
        "line_item_id": line_item_id,
        "coverage_id": "coverage_001",
        "normalization": {
            "action": action,
            "trade": trade,
            "component": component,
            "material": material,
            "attributes": {},
            "quantity": quantity,
            "unit_of_measure": unit,
        },
        "mapping": {
            "status": status,
            "best_match": best_match,
            "alternatives": [],
            "needs_review": status != "mapped",
            "review_reasons": review_reasons or [],
        },
    }


def _write_project(tmp_path, normalized, mapped):
    project_dir = tmp_path / "test-project"
    (project_dir / "mapping").mkdir(parents=True)
    (project_dir / "review").mkdir(parents=True)
    (project_dir / "mapping" / "normalized_estimate.json").write_text(json.dumps(normalized), encoding="utf-8")
    (project_dir / "mapping" / "mapped_estimate.json").write_text(json.dumps(mapped), encoding="utf-8")
    return project_dir


def _candidate(category="RFG", selector="ARMVN", score=0.9, needs_review=False, source="selector_catalog"):
    return Candidate(
        category=category, selector=selector, description="Tear off composition shingles - 3 tab",
        source_needs_review=needs_review, score=score, rank=1, source=source,
    )


# ---------------------------------------------------------------------------
# build_recommendation_input
# ---------------------------------------------------------------------------


def test_build_recommendation_input_maps_effective_row_fields():
    row = {
        "line_item_id": "line_0001",
        "original_description": "Tear off composition shingles",
        "normalized_action": "remove",
        "normalized_trade": "roofing",
        "normalized_component": "composition_shingles",
        "normalized_material": "3-tab composition shingles",
        "original_unit": "SQ",
        "unit": "SQ",
        "quantity": 24.99,
        "section_name": "ROOF1",
        "area_name": None,
        "coverage_id": "coverage_001",
        "mapping_status": "partially_mapped",
        "machine_category": "RFG",
        "machine_selector": None,
        "category": "RFG",
        "selector": None,
        "status": "unreviewed",
    }
    item = service.build_recommendation_input(row, {"normalized": {"attributes": {"tab_count": 3.0}}})
    assert item.line_item_id == "line_0001"
    assert item.trade == "roofing"
    assert item.component == "composition_shingles"
    assert item.attributes == {"tab_count": 3.0}
    assert item.existing_category == "RFG"
    assert item.existing_selector is None


def test_build_recommendation_input_tolerates_missing_normalized_item():
    row = {"line_item_id": "line_0001", "original_description": "x", "normalized_action": None, "normalized_trade": None, "normalized_component": None, "normalized_material": None, "original_unit": None, "unit": None, "quantity": None, "section_name": None, "area_name": None, "coverage_id": None, "mapping_status": None, "machine_category": None, "machine_selector": None, "category": None, "selector": None, "status": "unreviewed"}
    item = service.build_recommendation_input(row, None)
    assert item.attributes == {}


# ---------------------------------------------------------------------------
# recommend_for_item / recommend_for_project (uses db_conn/rules fixtures)
# ---------------------------------------------------------------------------


def test_recommend_for_project_end_to_end(tmp_path, db_conn):
    import shutil
    from estimate_extractor.selector_catalog import database

    db_path = tmp_path / "master_selectors.db"
    database.replace_all_records(database.create_database(db_path), database.load_all_records(db_conn))

    normalized = [_normalized_item("line_0001", "Tear off composition shingles - 3 tab")]
    mapped = [_mapped_item("line_0001")]
    project_dir = _write_project(tmp_path, normalized, mapped)

    results = service.recommend_for_project(project_dir, db_path)
    assert len(results) == 1
    assert results[0].line_item_id == "line_0001"
    assert results[0].candidates
    assert results[0].candidates[0].selector == "ARMVN"


def test_recommend_for_project_raises_when_db_missing(tmp_path):
    normalized = [_normalized_item("line_0001", "x")]
    mapped = [_mapped_item("line_0001")]
    project_dir = _write_project(tmp_path, normalized, mapped)
    with pytest.raises(service.RecommendationServiceError):
        service.recommend_for_project(project_dir, tmp_path / "does_not_exist.db")


# ---------------------------------------------------------------------------
# apply_candidate
# ---------------------------------------------------------------------------


def test_apply_candidate_writes_overrides_and_records_event(tmp_path):
    normalized = [_normalized_item("line_0001", "Tear off composition shingles")]
    mapped = [_mapped_item("line_0001", best_match={"mapping_id": "m1", "category": "RFG", "selector": None, "activity": "remove", "description": "placeholder", "confidence": 0.5})]
    project_dir = _write_project(tmp_path, normalized, mapped)

    candidate = _candidate()
    service.apply_candidate(project_dir, "line_0001", candidate, "tester", "matches source description")

    rows = review_service.build_effective_rows(project_dir)
    row = rows[0]
    assert row["category"] == "RFG"
    assert row["selector"] == "ARMVN"

    events = service.get_recommendation_events(project_dir)
    assert len(events) == 1
    assert events[0]["action"] == "accepted"
    assert events[0]["candidate_selector"] == "ARMVN"


def test_apply_candidate_requires_a_reason(tmp_path):
    normalized = [_normalized_item("line_0001", "x")]
    mapped = [_mapped_item("line_0001")]
    project_dir = _write_project(tmp_path, normalized, mapped)
    with pytest.raises(service.RecommendationServiceError):
        service.apply_candidate(project_dir, "line_0001", _candidate(), "tester", "")


def test_apply_candidate_blocks_silent_overwrite_of_approved_item(tmp_path):
    normalized = [_normalized_item("line_0001", "Tear off composition shingles")]
    mapped = [
        _mapped_item(
            "line_0001",
            best_match={"mapping_id": "m1", "category": "RFG", "selector": "ORIGINAL", "activity": "remove", "description": "d", "confidence": 0.9},
        )
    ]
    project_dir = _write_project(tmp_path, normalized, mapped)
    review_service.approve_item(project_dir, "line_0001", "tester")

    different_candidate = _candidate(selector="DIFFERENT")
    with pytest.raises(service.RecommendationApplyBlockedError):
        service.apply_candidate(project_dir, "line_0001", different_candidate, "tester", "trying to override")

    # the approved mapping must be untouched
    row = review_service.build_effective_rows(project_dir)[0]
    assert row["selector"] == "ORIGINAL"
    assert row["status"] == review_service.STATUS_APPROVED


def test_apply_candidate_allows_override_when_explicitly_authorized(tmp_path):
    normalized = [_normalized_item("line_0001", "Tear off composition shingles")]
    mapped = [_mapped_item("line_0001", best_match={"mapping_id": "m1", "category": "RFG", "selector": "ORIGINAL", "activity": "remove", "description": "d", "confidence": 0.9})]
    project_dir = _write_project(tmp_path, normalized, mapped)
    review_service.approve_item(project_dir, "line_0001", "tester")

    different_candidate = _candidate(selector="DIFFERENT")
    service.apply_candidate(project_dir, "line_0001", different_candidate, "tester", "corrected after review", allow_override_approved=True)
    row = review_service.build_effective_rows(project_dir)[0]
    assert row["selector"] == "DIFFERENT"


def test_apply_candidate_with_approve_requires_can_approve_gate(tmp_path):
    normalized = [_normalized_item("line_0001", "Tear off composition shingles")]
    mapped = [_mapped_item("line_0001", best_match={"mapping_id": "m1", "category": "RFG", "selector": None, "activity": "remove", "description": "d", "confidence": 0.5})]
    project_dir = _write_project(tmp_path, normalized, mapped)

    candidate = _candidate()
    events = service.apply_candidate(project_dir, "line_0001", candidate, "tester", "confirmed", approve=True)
    row = review_service.build_effective_rows(project_dir)[0]
    assert row["status"] == review_service.STATUS_APPROVED
    events_recorded = service.get_recommendation_events(project_dir)
    assert events_recorded[-1]["action"] == "accepted_and_approved"


def test_reject_candidate_records_event_without_touching_mapping(tmp_path):
    normalized = [_normalized_item("line_0001", "Tear off composition shingles")]
    mapped = [_mapped_item("line_0001", best_match={"mapping_id": "m1", "category": "RFG", "selector": "ORIGINAL", "activity": "remove", "description": "d", "confidence": 0.9})]
    project_dir = _write_project(tmp_path, normalized, mapped)

    service.reject_candidate(project_dir, "line_0001", _candidate(selector="NOT_THIS"), "tester", reason="wrong component")
    row = review_service.build_effective_rows(project_dir)[0]
    assert row["selector"] == "ORIGINAL"  # untouched

    events = service.get_recommendation_events(project_dir)
    assert events[0]["action"] == "rejected"


# ---------------------------------------------------------------------------
# save_recommendation_as_verified_rule (delegates to Phase 3.5 workflow)
# ---------------------------------------------------------------------------


def test_save_recommendation_as_verified_rule_requires_confirmations(tmp_path):
    normalized = [_normalized_item("line_0001", "Tear off composition shingles")]
    mapped = [_mapped_item("line_0001")]
    project_dir = _write_project(tmp_path, normalized, mapped)
    catalog_path = tmp_path / "verified_catalog.yaml"
    backups_dir = tmp_path / "backups"

    item = service.build_recommendation_input(review_service.build_effective_rows(project_dir)[0], normalized[0])
    candidate = _candidate()

    with pytest.raises(vcs.VerificationConfirmationError):
        service.save_recommendation_as_verified_rule(
            catalog_path, backups_dir, project_dir, item, candidate, "tester", confirmations={"confirmed_category_selector": True},
        )


def test_save_recommendation_as_verified_rule_creates_record_and_applies_it(tmp_path):
    normalized = [_normalized_item("line_0001", "Tear off composition shingles")]
    mapped = [_mapped_item("line_0001")]
    project_dir = _write_project(tmp_path, normalized, mapped)
    catalog_path = tmp_path / "verified_catalog.yaml"
    backups_dir = tmp_path / "backups"

    item = service.build_recommendation_input(review_service.build_effective_rows(project_dir)[0], normalized[0])
    candidate = _candidate()
    confirmations = {"confirmed_category_selector": True, "confirmed_unit": True, "confirmed_price_context": True}

    record = service.save_recommendation_as_verified_rule(
        catalog_path, backups_dir, project_dir, item, candidate, "tester", confirmations=confirmations, reviewer_note="looks correct",
    )
    assert record.verification_status == vcs.VERIFICATION_STATUS_HUMAN_VERIFIED
    row = review_service.build_effective_rows(project_dir)[0]
    assert row["category"] == "RFG"
    assert row["selector"] == "ARMVN"

    events = service.get_recommendation_events(project_dir)
    assert events[-1]["action"] == "saved_reusable_rule"


# ---------------------------------------------------------------------------
# evaluate_project: synthetic-approval exclusion from ground truth
# ---------------------------------------------------------------------------


def test_evaluate_project_treats_benchmark_run_approvals_as_not_ground_truth(tmp_path):
    normalized = [_normalized_item("line_0001", "Tear off composition shingles")]
    mapped = [_mapped_item("line_0001", best_match={"mapping_id": "m1", "category": "TEST_CAT", "selector": "TEST_SEL", "activity": "install", "description": "d", "confidence": 0.9})]
    project_dir = _write_project(tmp_path, normalized, mapped)

    state = {
        "line_0001": {
            "status": "approved",
            "overrides": {},
            "reviewer_note": "benchmark approval",
            "reviewer": "benchmark-run",
            "updated_at": "2026-01-01T00:00:00+00:00",
        }
    }
    (project_dir / "review" / "review_state.json").write_text(json.dumps(state), encoding="utf-8")

    ground_truth = service.real_ground_truth(project_dir)
    assert ground_truth == {}


def test_evaluate_project_uses_real_non_synthetic_approval_as_ground_truth(tmp_path):
    normalized = [_normalized_item("line_0001", "Tear off composition shingles")]
    mapped = [_mapped_item("line_0001", best_match={"mapping_id": "m1", "category": "RFG", "selector": "ARMVN", "activity": "remove", "description": "d", "confidence": 0.95})]
    project_dir = _write_project(tmp_path, normalized, mapped)

    state = {
        "line_0001": {
            "status": "approved",
            "overrides": {},
            "reviewer_note": "confirmed against my licensed Xactimate install",
            "reviewer": "jane.reviewer",
            "updated_at": "2026-01-01T00:00:00+00:00",
        }
    }
    (project_dir / "review" / "review_state.json").write_text(json.dumps(state), encoding="utf-8")

    ground_truth = service.real_ground_truth(project_dir)
    assert ground_truth == {"line_0001": ("RFG", "ARMVN")}


# ---------------------------------------------------------------------------
# resolve_candidate_screenshot
# ---------------------------------------------------------------------------


def test_resolve_candidate_screenshot_missing_does_not_crash(tmp_path):
    candidate = _candidate()
    candidate.source_image = "Screenshots_By_CAT/RFG/does_not_exist.png"
    (tmp_path / "SomeLibrary" / "Screenshots_By_CAT" / "RFG").mkdir(parents=True)
    result = service.resolve_candidate_screenshot(candidate, tmp_path)
    assert result is None
    assert candidate.screenshot_available is False


def test_resolve_candidate_screenshot_none_source_image():
    candidate = _candidate()
    candidate.source_image = None
    result = service.resolve_candidate_screenshot(candidate, None)
    assert result is None
    assert candidate.screenshot_available is False
