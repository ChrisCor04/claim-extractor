"""Integration tests: run the full extract -> map -> review pipeline
against all six real fixture PDFs through the Phase 3 UI service layer
(never through Streamlit itself -- see docs/local-review-ui.md "Tests").

For each fixture: create a project, run the existing pipeline into it,
approve a small set of qualifying items (after a manual correction, since
the catalog currently has no populated selectors -- see
docs/mapping-engine.md), reject one item, generate both export files, and
confirm unapproved/unqualified items never appear in the automation
export.
"""

from __future__ import annotations

import pytest

from estimate_extractor.config import Config
from estimate_extractor.mapping.pipeline import load_mapping_engine_config
from estimate_extractor.ui import export_service, pipeline_service, project_service, review_service

FIXTURE_NAMES = [
    "Aranda Insurance.pdf",
    "Bagi Insurance Estimate.pdf",
    "Garcia Insurance estimate.pdf",
    "Garrety Insurance Estimate.pdf",
    "Odom Insurance.pdf",
    "Wei Tang.pdf",
]


@pytest.fixture(scope="module")
def engine_config():
    return load_mapping_engine_config()


@pytest.mark.parametrize("pdf_name", FIXTURE_NAMES)
def test_ui_pipeline_against_real_fixture(fixtures_dir, engine_config, pdf_name, tmp_path):
    pdf_path = fixtures_dir / pdf_name
    if not pdf_path.exists():
        pytest.skip(f"fixture '{pdf_name}' not present locally (PII files are gitignored)")

    projects_dir = tmp_path / "projects"
    projects = project_service.ProjectService(projects_dir)

    record = projects.create_project(pdf_name, pdf_path.read_bytes())
    project_dir = projects.project_dir(record.slug)
    config = Config.default()

    stages_seen: list[str] = []
    result = pipeline_service.run_pipeline_for_project(
        projects.source_pdf_path(record.slug),
        project_dir,
        config,
        engine_config,
        progress_callback=lambda stage, detail=None: stages_seen.append(stage),
    )
    projects.mark_processed(record.slug)

    assert set(pipeline_service.STAGES).issubset(set(stages_seen))

    rows = review_service.build_effective_rows(project_dir)
    assert len(rows) == len(result.extraction.canonical.line_items)
    assert len(rows) == len(result.mapping.mapped_items)

    if not rows:
        pytest.skip(f"{pdf_name}: no line items extracted, nothing to review")

    # Approve a small set of items: since the shipped catalog has no
    # populated selectors (see docs/mapping-engine.md "Xactimate data
    # integrity"), no item is machine-qualified for approval yet. Simulate
    # a human verifying one item's selector against a licensed price list
    # -- an explicit, audited override, exactly the workflow this UI exists
    # to support.
    approve_target = rows[0]
    review_service.edit_mapping_field(
        project_dir, approve_target["line_item_id"], "category", "TEST_CAT", "integration-test", "simulated human verification"
    )
    review_service.edit_mapping_field(
        project_dir, approve_target["line_item_id"], "selector", "TEST_SEL", "integration-test", "simulated human verification"
    )
    review_service.edit_mapping_field(
        project_dir, approve_target["line_item_id"], "activity", "install", "integration-test", "simulated human verification"
    )
    approve_event = review_service.approve_item(project_dir, approve_target["line_item_id"], "integration-test", "approved for automation export test")
    assert approve_event["action"] == "approve_mapping"

    reject_target = rows[1] if len(rows) > 1 else rows[0]
    if reject_target["line_item_id"] != approve_target["line_item_id"]:
        review_service.reject_item(project_dir, reject_target["line_item_id"], "integration-test", "out of scope for this test")

    # A reviewer correction on an item that stays unreviewed (never
    # approved) -- must not leak into the automation export.
    correction_target_id = None
    for row in rows[2:]:
        correction_target_id = row["line_item_id"]
        review_service.edit_mapping_field(
            project_dir, correction_target_id, "material", "corrected material text", "integration-test", "reviewer correction, not yet approved"
        )
        break

    approved_estimate_path = export_service.write_approved_estimate(project_dir)
    assert approved_estimate_path.exists()
    assert approved_estimate_path.stat().st_size > 0

    automation_result = export_service.write_automation_input(project_dir)
    assert automation_result.automation_input_path.exists()
    assert automation_result.approved_csv_path.exists()
    assert automation_result.exported_count == 1  # exactly the one fully-qualified approved item

    data, excluded = export_service.build_automation_input(project_dir)
    exported_ids = {item["line_item_id"] for section in data["sections"] for item in section["items"]}
    assert exported_ids == {approve_target["line_item_id"]}
    assert reject_target["line_item_id"] not in exported_ids
    if correction_target_id is not None:
        assert correction_target_id not in exported_ids  # corrected but never approved

    # Reopen the project from a fresh ProjectService (simulates an app
    # restart) and confirm every review decision survived.
    reopened_projects = project_service.ProjectService(projects_dir)
    reopened_record = reopened_projects.load_project(record.slug)
    reopened_rows = review_service.build_effective_rows(reopened_projects.project_dir(reopened_record.slug))
    reopened_by_id = {r["line_item_id"]: r for r in reopened_rows}
    assert reopened_by_id[approve_target["line_item_id"]]["status"] == review_service.STATUS_APPROVED
    assert reopened_by_id[reject_target["line_item_id"]]["status"] == review_service.STATUS_REJECTED

    history = review_service.get_review_history(project_dir)
    assert len(history) >= 4  # 3 field edits + 1 approve (+ 1 reject if distinct target)
