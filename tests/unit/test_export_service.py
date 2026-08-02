from __future__ import annotations

import csv
import json

from estimate_extractor.ui import export_service, review_service


def _normalized_item(line_item_id, description, coverage_id=None, quantity=10.0, unit="SQ", section_name="Dwelling Roof"):
    return {
        "line_item_id": line_item_id,
        "original": {
            "description": description,
            "quantity": quantity,
            "unit_of_measure": unit,
            "coverage_id": coverage_id,
            "section_name": section_name,
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


def _mapped_item(line_item_id, coverage_id=None, status="mapped", best_match=None):
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
            "review_reasons": [] if best_match and best_match.get("selector") else ["missing_selector"],
        },
    }


def _write_project(tmp_path):
    project_dir = tmp_path / "aranda-insurance"
    (project_dir / "mapping").mkdir(parents=True)
    (project_dir / "review").mkdir(parents=True)
    (project_dir / "extraction").mkdir(parents=True)

    normalized = [
        _normalized_item("line_0001", "R&R Laminated shingles", coverage_id="coverage_001"),
        _normalized_item("line_0002", "R&R Gutter aluminum", coverage_id="coverage_001", unit="LF"),
        _normalized_item("line_0003", "Unresolved item", coverage_id=None),
    ]
    mapped = [
        _mapped_item(
            "line_0001",
            coverage_id="coverage_001",
            status="mapped",
            best_match={"mapping_id": "rfg_test", "category": "RFG", "selector": "SEL1", "activity": "install", "description": "Laminated shingles", "confidence": 0.95},
        ),
        _mapped_item(
            "line_0002",
            coverage_id="coverage_001",
            status="partially_mapped",
            best_match={"mapping_id": "gut_test", "category": "GUT", "selector": None, "activity": "install", "description": "Gutter", "confidence": 0.85},
        ),
        _mapped_item("line_0003", coverage_id=None, status="unmapped", best_match=None),
    ]

    (project_dir / "mapping" / "normalized_estimate.json").write_text(json.dumps(normalized), encoding="utf-8")
    (project_dir / "mapping" / "mapped_estimate.json").write_text(json.dumps(mapped), encoding="utf-8")

    canonical = {
        "document": {"carrier_detected": "State Farm"},
        "claim": {
            "claim_number": {"value": "4399W552P"},
            "insured_name": {"value": "ARANDA, GENARO"},
            "property_address": {"line1": "420 Revival Rd, Royse City, TX"},
        },
        "line_items": [{}, {}, {}],
    }
    (project_dir / "extraction" / "canonical_estimate.json").write_text(json.dumps(canonical), encoding="utf-8")
    return project_dir


def test_automation_export_excludes_unapproved_and_unqualified_items(tmp_path):
    project_dir = _write_project(tmp_path)
    review_service.approve_item(project_dir, "line_0001", "tester")  # fully qualified -- included
    # line_0002 stays unreviewed; line_0003 stays unreviewed too

    data, excluded = export_service.build_automation_input(project_dir)

    all_exported_ids = {item["line_item_id"] for section in data["sections"] for item in section["items"]}
    assert all_exported_ids == {"line_0001"}
    # unreviewed items are not "excluded" (that implies a decision was made) -- see export_service docstring
    excluded_ids = {e["line_item_id"] for e in excluded}
    assert "line_0001" not in excluded_ids


def test_automation_export_excludes_approved_but_unqualified_item(tmp_path):
    project_dir = _write_project(tmp_path)
    review_service.edit_mapping_field(project_dir, "line_0002", "selector", "SEL2", "tester", "verified")
    # still missing nothing else since category/activity already present -- approve
    review_service.approve_item(project_dir, "line_0002", "tester")

    # Now hand-corrupt review state to simulate a stale "approved" status
    # missing its selector again (defense-in-depth check).
    state_path = project_dir / "review" / "review_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    del state["line_0002"]["overrides"]["selector"]
    state_path.write_text(json.dumps(state), encoding="utf-8")

    data, excluded = export_service.build_automation_input(project_dir)
    exported_ids = {item["line_item_id"] for section in data["sections"] for item in section["items"]}
    assert "line_0002" not in exported_ids
    excluded_ids = {e["line_item_id"]: e["reason"] for e in excluded}
    assert "line_0002" in excluded_ids
    assert "selector" in excluded_ids["line_0002"].lower()


def test_automation_export_fails_safely_with_zero_approved_items(tmp_path):
    project_dir = _write_project(tmp_path)
    result = export_service.write_automation_input(project_dir)  # nothing approved yet -- must not raise

    assert result.exported_count == 0
    assert result.automation_input_path.exists()
    data = json.loads(result.automation_input_path.read_text(encoding="utf-8"))
    assert data["sections"] == []


def test_automation_export_rejected_item_is_excluded_with_reason(tmp_path):
    project_dir = _write_project(tmp_path)
    review_service.reject_item(project_dir, "line_0001", "tester", "wrong scope")

    data, excluded = export_service.build_automation_input(project_dir)
    exported_ids = {item["line_item_id"] for section in data["sections"] for item in section["items"]}
    assert "line_0001" not in exported_ids
    excluded_ids = {e["line_item_id"] for e in excluded}
    assert "line_0001" in excluded_ids


def test_approved_line_items_csv_has_expected_columns_and_rows(tmp_path):
    project_dir = _write_project(tmp_path)
    review_service.approve_item(project_dir, "line_0001", "tester")

    result = export_service.write_automation_input(project_dir)
    with result.approved_csv_path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))

    assert rows[0] == export_service.APPROVED_CSV_COLUMNS
    assert len(rows) == 2  # header + one approved, qualified item
    assert rows[1][0] == "line_0001"
    assert rows[1][5] == "RFG"  # category
    assert rows[1][6] == "SEL1"  # selector


def test_build_approved_estimate_preserves_machine_and_reviewed_mapping_separately(tmp_path):
    project_dir = _write_project(tmp_path)
    review_service.edit_mapping_field(project_dir, "line_0002", "selector", "SEL_HUMAN", "tester", "verified against price list")

    data = export_service.build_approved_estimate(project_dir)
    item = next(i for i in data["items"] if i["line_item_id"] == "line_0002")

    assert item["machine_mapping"]["selector"] is None  # what the mapper actually produced
    assert item["reviewed_mapping"]["selector"] == "SEL_HUMAN"  # what the human confirmed
    assert item["original"]["description"] == "R&R Gutter aluminum"
    assert item["original"]["quantity"] == 10.0


def test_write_approved_estimate_writes_file(tmp_path):
    project_dir = _write_project(tmp_path)
    path = export_service.write_approved_estimate(project_dir)
    assert path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["project"]["claim_number"] == "4399W552P"
    assert len(data["items"]) == 3
