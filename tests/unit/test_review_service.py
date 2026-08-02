from __future__ import annotations

import json

import pytest

from estimate_extractor.ui import review_service
from estimate_extractor.ui.review_service import (
    ApprovalBlockedError,
    ReviewServiceError,
    STATUS_APPROVED,
    STATUS_NEEDS_MORE_INFO,
    STATUS_REJECTED,
    STATUS_UNREVIEWED,
)


def _normalized_item(line_item_id, description, coverage_id=None, quantity=10.0, unit="SQ", **overrides):
    item = {
        "line_item_id": line_item_id,
        "original": {
            "description": description,
            "quantity": quantity,
            "unit_of_measure": unit,
            "coverage_id": coverage_id,
            "section_name": "Dwelling Roof",
            "area_name": "Dwelling",
            "source_pages": [7],
            "notes": [],
            "extraction_confidence": 0.95,
            "extraction_needs_review": False,
            "extraction_warnings": [],
        },
        "normalized": {
            "action": "remove_and_replace",
            "trade": "roofing",
            "component": "composition_shingles",
            "material": "laminated composition shingles",
            "attributes": {},
            "quantity": quantity,
            "unit_of_measure": unit,
        },
        "confidence": {"overall": 0.9, "action": 0.9, "trade": 0.9, "component": 0.9, "material": 0.9},
        "needs_review": False,
        "review_reasons": [],
    }
    item.update(overrides)
    return item


def _mapped_item(line_item_id, coverage_id=None, status="mapped", best_match=None, review_reasons=None):
    return {
        "line_item_id": line_item_id,
        "coverage_id": coverage_id,
        "normalization": {
            "action": "remove_and_replace",
            "trade": "roofing",
            "component": "composition_shingles",
            "material": "laminated composition shingles",
            "attributes": {},
            "quantity": 10.0,
            "unit_of_measure": "SQ",
        },
        "mapping": {
            "status": status,
            "best_match": best_match,
            "alternatives": [],
            "needs_review": status != "mapped",
            "review_reasons": review_reasons or [],
        },
    }


def _write_project(tmp_path):
    project_dir = tmp_path / "aranda-insurance"
    (project_dir / "mapping").mkdir(parents=True)
    (project_dir / "review").mkdir(parents=True)

    normalized = [
        _normalized_item("line_0001", "R&R Laminated shingles", coverage_id="coverage_001"),
        _normalized_item("line_0002", "R&R Gutter aluminum", coverage_id="coverage_001", unit="LF"),
        _normalized_item("line_0003", "Some unrecognized line", coverage_id=None),
    ]
    mapped = [
        _mapped_item(
            "line_0001",
            coverage_id="coverage_001",
            status="mapped",
            best_match={
                "mapping_id": "rfg_test",
                "category": "RFG",
                "selector": "SEL1",
                "activity": "install",
                "description": "Laminated shingles",
                "confidence": 0.95,
            },
        ),
        _mapped_item(
            "line_0002",
            coverage_id="coverage_001",
            status="partially_mapped",
            best_match={
                "mapping_id": "gut_test",
                "category": "GUT",
                "selector": None,
                "activity": "install",
                "description": "Gutter",
                "confidence": 0.85,
            },
            review_reasons=["missing_selector"],
        ),
        _mapped_item("line_0003", coverage_id=None, status="unmapped", best_match=None, review_reasons=["no_catalog_match"]),
    ]

    (project_dir / "mapping" / "normalized_estimate.json").write_text(json.dumps(normalized), encoding="utf-8")
    (project_dir / "mapping" / "mapped_estimate.json").write_text(json.dumps(mapped), encoding="utf-8")
    return project_dir


def test_build_effective_rows_reflects_machine_values_with_no_overrides(tmp_path):
    project_dir = _write_project(tmp_path)
    rows = review_service.build_effective_rows(project_dir)
    assert len(rows) == 3

    row1 = next(r for r in rows if r["line_item_id"] == "line_0001")
    assert row1["category"] == "RFG"
    assert row1["selector"] == "SEL1"
    assert row1["status"] == STATUS_UNREVIEWED
    assert row1["coverage_id"] == "coverage_001"


def test_can_approve_rules(tmp_path):
    project_dir = _write_project(tmp_path)
    rows = {r["line_item_id"]: r for r in review_service.build_effective_rows(project_dir)}

    ok, reasons = review_service.can_approve(rows["line_0001"])
    assert ok is True
    assert reasons == []

    ok, reasons = review_service.can_approve(rows["line_0002"])
    assert ok is False
    assert "Missing selector." in reasons

    ok, reasons = review_service.can_approve(rows["line_0003"])
    assert ok is False
    assert "Missing category." in reasons
    assert "Missing selector." in reasons


def test_unresolved_coverage_does_not_block_approval(tmp_path):
    project_dir = _write_project(tmp_path)
    # line_0003 has coverage_id None *and* missing category/selector -- fix
    # the mapping-completeness blockers and confirm coverage_id being null
    # alone never appears as a rejection reason.
    review_service.edit_mapping_field(project_dir, "line_0003", "category", "GEN", "tester", "test fix")
    review_service.edit_mapping_field(project_dir, "line_0003", "selector", "GEN999", "tester", "test fix")
    review_service.edit_mapping_field(project_dir, "line_0003", "activity", "install", "tester", "test fix")

    rows = {r["line_item_id"]: r for r in review_service.build_effective_rows(project_dir)}
    ok, reasons = review_service.can_approve(rows["line_0003"])
    assert ok is True
    assert rows["line_0003"]["coverage_id"] is None
    assert not any("coverage" in r.lower() for r in reasons)


def test_edit_mapping_field_preserves_original_machine_value_and_writes_history(tmp_path):
    project_dir = _write_project(tmp_path)
    event = review_service.edit_mapping_field(project_dir, "line_0002", "selector", "SEL_NEW", "tester", "verified in price list")

    rows = review_service.build_effective_rows(project_dir, line_item_ids=["line_0002"])
    assert rows[0]["selector"] == "SEL_NEW"
    assert rows[0]["machine_selector"] is None  # untouched machine value

    state = json.loads((project_dir / "review" / "review_state.json").read_text(encoding="utf-8"))
    override = state["line_0002"]["overrides"]["selector"]
    assert override["original_machine_value"] is None
    assert override["reviewed_value"] == "SEL_NEW"
    assert override["review_reason"] == "verified in price list"

    assert event["action"] == "edit_field"
    assert event["field_changes"]["selector"]["after"] == "SEL_NEW"

    history = review_service.get_review_history(project_dir)
    assert len(history) == 1
    assert history[0]["event_id"] == "review_event_001"


def test_edit_mapping_field_requires_a_reason(tmp_path):
    project_dir = _write_project(tmp_path)
    with pytest.raises(ReviewServiceError):
        review_service.edit_mapping_field(project_dir, "line_0002", "selector", "SEL_NEW", "tester", "")


def test_edit_mapping_field_rejects_immutable_fields(tmp_path):
    project_dir = _write_project(tmp_path)
    for field in ("line_item_id", "original_description", "quantity", "unit", "source_page"):
        with pytest.raises(ReviewServiceError):
            review_service.edit_mapping_field(project_dir, "line_0002", field, "x", "tester", "reason")


def test_edit_mapping_field_never_touches_machine_output_files(tmp_path):
    project_dir = _write_project(tmp_path)
    before = (project_dir / "mapping" / "mapped_estimate.json").read_text(encoding="utf-8")
    review_service.edit_mapping_field(project_dir, "line_0002", "selector", "SEL_NEW", "tester", "reason")
    after = (project_dir / "mapping" / "mapped_estimate.json").read_text(encoding="utf-8")
    assert before == after  # machine output is byte-for-byte unchanged


def test_approve_item_succeeds_when_qualified(tmp_path):
    project_dir = _write_project(tmp_path)
    event = review_service.approve_item(project_dir, "line_0001", "tester", "looks correct")
    assert event["action"] == "approve_mapping"

    rows = review_service.build_effective_rows(project_dir, line_item_ids=["line_0001"])
    assert rows[0]["status"] == STATUS_APPROVED
    assert rows[0]["approved"] is True


def test_approve_item_blocked_when_unqualified(tmp_path):
    project_dir = _write_project(tmp_path)
    with pytest.raises(ApprovalBlockedError) as excinfo:
        review_service.approve_item(project_dir, "line_0002", "tester")
    assert "line_0002" in str(excinfo.value)

    rows = review_service.build_effective_rows(project_dir, line_item_ids=["line_0002"])
    assert rows[0]["status"] == STATUS_UNREVIEWED  # unchanged -- blocked approvals never partially apply


def test_reject_item(tmp_path):
    project_dir = _write_project(tmp_path)
    review_service.reject_item(project_dir, "line_0002", "tester", "not usable")
    rows = review_service.build_effective_rows(project_dir, line_item_ids=["line_0002"])
    assert rows[0]["status"] == STATUS_REJECTED
    assert rows[0]["rejected"] is True


def test_mark_needs_more_info(tmp_path):
    project_dir = _write_project(tmp_path)
    review_service.mark_needs_more_info(project_dir, "line_0002", "tester", "waiting on adjuster")
    rows = review_service.build_effective_rows(project_dir, line_item_ids=["line_0002"])
    assert rows[0]["status"] == STATUS_NEEDS_MORE_INFO


def test_waive_activity_requirement_unblocks_approval(tmp_path):
    project_dir = _write_project(tmp_path)
    review_service.edit_mapping_field(project_dir, "line_0002", "selector", "SEL_NEW", "tester", "verified")
    review_service.edit_mapping_field(project_dir, "line_0002", "category", "GUT", "tester", "already correct")
    review_service.edit_mapping_field(project_dir, "line_0002", "activity", None, "tester", "no activity needed")

    rows = review_service.build_effective_rows(project_dir, line_item_ids=["line_0002"])
    ok, reasons = review_service.can_approve(rows[0])
    assert ok is False
    assert any("activity" in r.lower() for r in reasons)

    review_service.waive_activity_requirement(project_dir, "line_0002", "tester", "activity genuinely not applicable")
    rows = review_service.build_effective_rows(project_dir, line_item_ids=["line_0002"])
    ok, reasons = review_service.can_approve(rows[0])
    assert ok is True


def test_bulk_set_status_reports_applied_and_blocked(tmp_path):
    project_dir = _write_project(tmp_path)
    result = review_service.bulk_set_status(project_dir, ["line_0001", "line_0002"], STATUS_APPROVED, "tester")
    assert result.applied == ["line_0001"]
    assert "line_0002" in result.blocked
    assert result.blocked["line_0002"]  # has reasons


def test_bulk_assign_field_updates_all_selected(tmp_path):
    project_dir = _write_project(tmp_path)
    result = review_service.bulk_assign_field(project_dir, ["line_0001", "line_0002"], "trade", "gutters", "tester", "batch correction")
    assert set(result.applied) == {"line_0001", "line_0002"}

    rows = {r["line_item_id"]: r for r in review_service.build_effective_rows(project_dir)}
    assert rows["line_0001"]["normalized_trade"] == "gutters"
    assert rows["line_0002"]["normalized_trade"] == "gutters"


def test_override_extraction_field_only_allows_attribution_fields(tmp_path):
    project_dir = _write_project(tmp_path)
    with pytest.raises(ReviewServiceError):
        review_service.override_extraction_field(project_dir, "line_0001", "description", "changed", "tester", "reason")


def test_override_extraction_field_updates_effective_row_not_source_file(tmp_path):
    project_dir = _write_project(tmp_path)
    normalized_before = (project_dir / "mapping" / "normalized_estimate.json").read_text(encoding="utf-8")

    review_service.override_extraction_field(project_dir, "line_0003", "coverage_id", "coverage_002", "tester", "identified via recap table")

    normalized_after = (project_dir / "mapping" / "normalized_estimate.json").read_text(encoding="utf-8")
    assert normalized_before == normalized_after  # extractor output untouched

    rows = review_service.build_effective_rows(project_dir, line_item_ids=["line_0003"])
    assert rows[0]["coverage_id"] == "coverage_002"


def test_get_project_summary_counts_approved_items(tmp_path):
    from estimate_extractor.ui.project_service import ProjectRecord

    project_dir = _write_project(tmp_path)
    (project_dir / "extraction").mkdir(parents=True, exist_ok=True)
    canonical = {
        "document": {"carrier_detected": "State Farm", "extraction_status": "success"},
        "claim": {"claim_number": {"value": "CLM123"}, "insured_name": {"value": "Jane Doe"}},
        "line_items": [{}, {}, {}],
    }
    (project_dir / "extraction" / "canonical_estimate.json").write_text(json.dumps(canonical), encoding="utf-8")
    mapping_report = {"status": "needs_review", "summary": {"needs_review": 1, "unmapped": 1}}
    (project_dir / "mapping" / "mapping_report.json").write_text(json.dumps(mapping_report), encoding="utf-8")

    review_service.approve_item(project_dir, "line_0001", "tester")

    record = ProjectRecord(slug="aranda-insurance", source_filename="Aranda Insurance.pdf", source_sha256="abc", created_at="2026-01-01T00:00:00")
    summary = review_service.get_project_summary(project_dir, record)

    assert summary.carrier == "State Farm"
    assert summary.claim_number == "CLM123"
    assert summary.total_line_items == 3
    assert summary.approved_count == 1
    assert summary.unresolved_count == 2
