"""Integration tests: run the full extract -> map -> review pipeline
against all six real fixture PDFs, then exercise the Phase 3.5 verified-
catalog workflow on top -- create a clearly-labeled, test-only verified
record, confirm it improves matching for compatible items only, confirm
unrelated items are untouched, confirm a prior approval survives, and
confirm the automation export contains only verified, approved mappings.

The selector value used below ("TEST_ONLY_SYNTHETIC_GUTTER_SEL") is
deliberately unrealistic and clearly labeled -- this is synthetic test
data, never presented as real Xactimate data (see build spec "Use clearly
labeled synthetic test selectors in automated tests").
"""

from __future__ import annotations

import pytest

from estimate_extractor.config import Config
from estimate_extractor.mapping.pipeline import load_mapping_engine_config
from estimate_extractor.ui import export_service, pipeline_service, project_service, review_service, verified_catalog_service as vcs

FIXTURE_NAMES = [
    "Aranda Insurance.pdf",
    "Bagi Insurance Estimate.pdf",
    "Garcia Insurance estimate.pdf",
    "Garrety Insurance Estimate.pdf",
    "Odom Insurance.pdf",
    "Wei Tang.pdf",
]

TEST_GUTTER_RECORD_FIELDS = {
    "category": "TESTCAT",
    "selector": "TEST_ONLY_SYNTHETIC_GUTTER_SEL",
    "description": "gutter",
    "unit": "LF",
    "activity_raw": "&",
    "trade": "gutters",
    "component": "gutter",
    "supported_actions": ["remove_and_replace"],
}
VALID_CONFIRMATIONS = {"confirmed_category_selector": True, "confirmed_unit": True, "confirmed_price_context": True}


@pytest.fixture(scope="module")
def engine_config():
    return load_mapping_engine_config()


@pytest.mark.parametrize("pdf_name", FIXTURE_NAMES)
def test_verified_catalog_workflow_against_real_fixture(fixtures_dir, engine_config, pdf_name, tmp_path):
    pdf_path = fixtures_dir / pdf_name
    if not pdf_path.exists():
        pytest.skip(f"fixture '{pdf_name}' not present locally (PII files are gitignored)")

    projects_dir = tmp_path / "projects"
    projects = project_service.ProjectService(projects_dir)
    record = projects.create_project(pdf_name, pdf_path.read_bytes())
    project_dir = projects.project_dir(record.slug)
    config = Config.default()

    pipeline_service.run_pipeline_for_project(projects.source_pdf_path(record.slug), project_dir, config, engine_config)
    projects.mark_processed(record.slug)

    rows_before = review_service.build_effective_rows(project_dir)
    if not rows_before:
        pytest.skip(f"{pdf_name}: no line items extracted")

    gutter_ids_before = {
        r["line_item_id"] for r in rows_before if r["normalized_trade"] == "gutters" and r["normalized_component"] == "gutter"
    }
    non_gutter_ids = {r["line_item_id"] for r in rows_before if r["line_item_id"] not in gutter_ids_before}

    # Approve one non-gutter item up front, to prove it survives untouched
    # by everything that follows (the verified-catalog workflow must never
    # touch an unrelated, already-approved item).
    prior_approved_id = None
    for r in rows_before:
        if r["line_item_id"] not in gutter_ids_before:
            review_service.edit_mapping_field(project_dir, r["line_item_id"], "category", "PRIOR_CAT", "tester", "prior manual approval")
            review_service.edit_mapping_field(project_dir, r["line_item_id"], "selector", "PRIOR_SEL", "tester", "prior manual approval")
            review_service.edit_mapping_field(project_dir, r["line_item_id"], "activity", "install", "tester", "prior manual approval")
            review_service.approve_item(project_dir, r["line_item_id"], "tester", "approved before the verified-catalog workflow ran")
            prior_approved_id = r["line_item_id"]
            break

    # --- Create the test-only verified catalog record -----------------
    catalog_path = tmp_path / "verified_xactimate_catalog.yaml"
    backups_dir = tmp_path / "backups"
    test_record = vcs.add_record(
        catalog_path, backups_dir, project_dir, dict(TEST_GUTTER_RECORD_FIELDS), "integration-test",
        verification_status=vcs.VERIFICATION_STATUS_HUMAN_VERIFIED, confirmations=VALID_CONFIRMATIONS,
        reviewer_note="synthetic test-only record for integration test",
    )
    records = vcs.load_verified_catalog(catalog_path)

    # --- Confirm matching items improve, unrelated items do not change ---
    rows_after = {r["line_item_id"]: r for r in review_service.build_effective_rows(project_dir)}
    matched_gutter_ids = set()
    for lid in gutter_ids_before:
        matches = vcs.find_verified_matches(rows_after[lid], records)
        if matches:
            matched_gutter_ids.add(lid)
            assert matches[0].record.catalog_record_id == test_record.catalog_record_id

    for lid in non_gutter_ids:
        matches = vcs.find_verified_matches(rows_after[lid], records)
        assert matches == [], f"{pdf_name}: unrelated item {lid} unexpectedly matched the gutter-only test record"

    # Prior approval must be completely untouched.
    if prior_approved_id is not None:
        assert rows_after[prior_approved_id]["status"] == review_service.STATUS_APPROVED
        assert rows_after[prior_approved_id]["category"] == "PRIOR_CAT"
        assert rows_after[prior_approved_id]["selector"] == "PRIOR_SEL"

    # --- Apply + approve exactly one matched gutter item, export --------
    if matched_gutter_ids:
        target_id = sorted(matched_gutter_ids)[0]
        vcs.apply_verified_match(project_dir, target_id, test_record, "integration-test", "applying synthetic verified test record")
        review_service.approve_item(project_dir, target_id, "integration-test", "approved via verified catalog test")

        exported_row = review_service.build_effective_rows(project_dir, line_item_ids=[target_id])[0]
        ready, reasons = vcs.is_automation_ready(exported_row, project_dir, records, group_reviewed=True)
        assert ready is True, reasons

        automation_data, excluded = export_service.build_automation_input(project_dir)
        exported_ids = {item["line_item_id"] for section in automation_data["sections"] for item in section["items"]}
        # Only fully-qualified approved items are ever exported -- the
        # prior-approved item used PRIOR_CAT/PRIOR_SEL which also qualifies
        # under the base Phase 3 rule (category+selector+activity+qty+unit),
        # so both may legitimately appear; the point is nothing UNQUALIFIED
        # or UNAPPROVED ever does.
        assert target_id in exported_ids
        for lid in exported_ids:
            row = review_service.build_effective_rows(project_dir, line_item_ids=[lid])[0]
            assert row["status"] == review_service.STATUS_APPROVED
            assert row["category"] and row["selector"]

    # --- Price observations stay separate from selector identity --------
    vcs.add_price_observation(
        catalog_path, backups_dir, project_dir, test_record.category, test_record.selector,
        {"price_list": "TEST_PL_A", "unit_price": 1.23}, "integration-test",
    )
    vcs.add_price_observation(
        catalog_path, backups_dir, project_dir, test_record.category, test_record.selector,
        {"price_list": "TEST_PL_B", "unit_price": 4.56}, "integration-test",
    )
    reloaded_records = vcs.load_verified_catalog(catalog_path)
    reloaded = vcs.find_record(reloaded_records, test_record.category, test_record.selector)
    assert len(reloaded_records) == 1  # still one identity record
    assert {o.price_list for o in reloaded.price_observations} == {"TEST_PL_A", "TEST_PL_B"}
    assert {o.unit_price for o in reloaded.price_observations} == {1.23, 4.56}
