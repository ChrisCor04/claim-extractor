"""Integration tests: the Phase 3.8 description-first lookup workflow
against real local project fixtures under projects/ (gitignored,
proprietary-adjacent local artifacts -- these tests skip gracefully, not
fail, when no processed projects are present locally, matching this
repo's existing convention). Every test that mutates project state
operates on a disposable tmp_path copy, never a real project directory
directly -- see tests/integration/test_selector_recommendation.py for
the same pattern.
"""

from __future__ import annotations

import shutil

import pytest

from estimate_extractor.ui import review_service
from estimate_extractor.ui import verified_catalog_service as vcs
from estimate_extractor.xactimate_lookup import service
from estimate_extractor.xactimate_lookup.adapter import FakeXactimateAdapter
from estimate_extractor.xactimate_lookup.models import (
    LOOKUP_PATH_DESCRIPTION_SEARCH,
    LOOKUP_PATH_TRUSTED,
    DropdownResult,
)


@pytest.fixture(scope="module")
def real_projects_dir(fixtures_dir):
    root = fixtures_dir.parents[1] / "projects"
    if not root.exists() or not any(
        (p / "mapping" / "mapped_estimate.json").exists() for p in root.iterdir() if p.is_dir()
    ):
        pytest.skip("no local processed projects found under projects/ (real, gitignored claim data)")
    return root


@pytest.fixture
def real_project_dirs(real_projects_dir):
    return sorted(p for p in real_projects_dir.iterdir() if p.is_dir() and (p / "mapping" / "mapped_estimate.json").exists())


def test_plan_for_every_real_project_fixture(real_project_dirs, tmp_path):
    assert real_project_dirs
    registry_db = tmp_path / "reg.db"
    for project_dir in real_project_dirs:
        plans = service.plan_for_project(project_dir, registry_db, verified_catalog_path=None)
        assert plans, f"{project_dir.name}: expected at least one lookup plan"
        for p in plans:
            assert p.path in (LOOKUP_PATH_TRUSTED, LOOKUP_PATH_DESCRIPTION_SEARCH)
            assert p.search_input  # never empty -- see phrase_generator's fallback


def test_phrase_generation_never_empty_across_all_real_line_items(real_project_dirs, tmp_path):
    registry_db = tmp_path / "reg.db"
    empty = 0
    total = 0
    for project_dir in real_project_dirs:
        plans = service.plan_for_project(project_dir, registry_db, verified_catalog_path=None)
        for p in plans:
            total += 1
            if p.path == LOOKUP_PATH_DESCRIPTION_SEARCH and not p.search_input.strip():
                empty += 1
    assert total > 0
    assert empty == 0


def test_learn_once_reuse_later_on_a_real_fixture(real_project_dirs, tmp_path):
    """The core Phase 3.8 promise: an item resolved via description
    search once becomes a trusted CAT/SEL lookup for the next run with
    the same signature."""
    project_dir = real_project_dirs[0]
    proj_copy = tmp_path / "proj_copy"
    shutil.copytree(project_dir, proj_copy)
    registry_db = tmp_path / "reg.db"

    plans = service.plan_for_project(proj_copy, registry_db, verified_catalog_path=None, line_item_ids=None)
    rows_by_id = {r["line_item_id"]: r for r in review_service.build_effective_rows(proj_copy)}
    target_plan = next(
        p for p in plans
        if p.path == LOOKUP_PATH_DESCRIPTION_SEARCH and rows_by_id[p.line_item_id]["status"] != review_service.STATUS_APPROVED
    )

    row = next(r for r in review_service.build_effective_rows(proj_copy) if r["line_item_id"] == target_plan.line_item_id)
    normalized_by_id = service.recommendation_service.load_normalized_items(proj_copy)
    item = service.build_lookup_input(row, normalized_by_id.get(target_plan.line_item_id))

    service.record_resolution(
        proj_copy, registry_db, tmp_path / "backups", item, target_plan.item_signature, target_plan.search_input,
        category="RFG", selector="TESTSEL", xactimate_description="Test resolved description", unit="SQ",
        action="remove", xactimate_item_number="9999", reviewer="integration-test",
        approval_reason="Confirmed exact match in Xactimate dropdown.",
    )

    replans = service.plan_for_project(proj_copy, registry_db, verified_catalog_path=None, line_item_ids=[target_plan.line_item_id])
    assert replans[0].path == LOOKUP_PATH_TRUSTED
    assert replans[0].trusted_mapping.category == "RFG"
    assert replans[0].trusted_mapping.selector == "TESTSEL"

    updated_row = next(r for r in review_service.build_effective_rows(proj_copy) if r["line_item_id"] == target_plan.line_item_id)
    assert updated_row["category"] == "RFG"
    assert updated_row["selector"] == "TESTSEL"


def test_verified_catalog_counts_as_trusted_mapping_on_real_fixture(real_project_dirs, tmp_path):
    project_dir = real_project_dirs[0]
    proj_copy = tmp_path / "proj_copy"
    shutil.copytree(project_dir, proj_copy)
    registry_db = tmp_path / "reg.db"
    catalog_path = tmp_path / "verified_catalog.yaml"

    rows = review_service.build_effective_rows(proj_copy)
    row = next(r for r in rows if r["normalized_trade"] == "roofing")

    vcs.add_record(
        catalog_path, tmp_path / "backups", proj_copy,
        {
            "category": "ZZZ_VERIFIED", "selector": "ZZZ_SEL", "description": row["original_description"],
            "unit": row["unit"] or "EA", "trade": row["normalized_trade"], "component": row["normalized_component"],
            "aliases": [row["original_description"]],
        },
        "integration-test", verification_status=vcs.VERIFICATION_STATUS_HUMAN_VERIFIED,
        confirmations={"confirmed_category_selector": True, "confirmed_unit": True, "confirmed_price_context": True},
    )

    plans = service.plan_for_project(proj_copy, registry_db, verified_catalog_path=catalog_path, line_item_ids=[row["line_item_id"]])
    assert plans[0].path == LOOKUP_PATH_TRUSTED
    assert plans[0].trusted_mapping.category == "ZZZ_VERIFIED"


def test_dry_run_orchestration_on_real_fixture_never_commits(real_project_dirs, tmp_path):
    project_dir = real_project_dirs[0]
    registry_db = tmp_path / "reg.db"

    plans = service.plan_for_project(project_dir, registry_db, verified_catalog_path=None)
    description_plans = [p for p in plans if p.path == LOOKUP_PATH_DESCRIPTION_SEARCH][:5]
    assert description_plans

    script = {
        p.search_input: [
            DropdownResult(
                raw_text=f"RFG SEL{i} {p.search_input}", row_position=0, category="RFG", selector=f"SEL{i}",
                description=p.search_input, extraction_confidence=0.9,
            )
        ]
        for i, p in enumerate(description_plans)
    }
    adapter = FakeXactimateAdapter(dropdown_script=script)
    outcomes = service.dry_run_for_project(
        project_dir, adapter, registry_db, verified_catalog_path=None,
        line_item_ids=[p.line_item_id for p in description_plans],
    )
    assert len(outcomes) == len(description_plans)
    assert not any(name == "commit_item" for name, _a, _k in adapter.log.calls)
    assert not any(o.committed for o in outcomes)


def test_lookup_stats_reports_na_ground_truth_for_synthetic_approvals(real_project_dirs, tmp_path):
    """Every real local project's existing approvals are synthetic
    benchmark-run data (see selector_recommendation docs) -- lookup stats
    must not fabricate agreement numbers from them."""
    registry_db = tmp_path / "reg.db"
    stats = service.compute_lookup_stats(real_project_dirs, registry_db, verified_catalog_path=None)
    assert stats.items_evaluated > 0
    assert stats.ground_truth_items == 0
    assert stats.top1_agreement is None
    assert stats.top3_agreement is None


def test_disabled_mapping_is_never_reused_on_real_fixture(real_project_dirs, tmp_path):
    project_dir = real_project_dirs[0]
    proj_copy = tmp_path / "proj_copy"
    shutil.copytree(project_dir, proj_copy)
    registry_db = tmp_path / "reg.db"

    plans = service.plan_for_project(proj_copy, registry_db, verified_catalog_path=None)
    rows_by_id = {r["line_item_id"]: r for r in review_service.build_effective_rows(proj_copy)}
    target_plan = next(
        p for p in plans
        if p.path == LOOKUP_PATH_DESCRIPTION_SEARCH and rows_by_id[p.line_item_id]["status"] != review_service.STATUS_APPROVED
    )
    row = rows_by_id[target_plan.line_item_id]
    normalized_by_id = service.recommendation_service.load_normalized_items(proj_copy)
    item = service.build_lookup_input(row, normalized_by_id.get(target_plan.line_item_id))

    record = service.record_resolution(
        proj_copy, registry_db, tmp_path / "backups", item, target_plan.item_signature, target_plan.search_input,
        category="RFG", selector="TESTSEL", xactimate_description="x", unit="SQ", action="remove",
        xactimate_item_number=None, reviewer="integration-test", approval_reason="first approval",
    )
    service.disable_mapping(registry_db, tmp_path / "backups", record.mapping_id, "integration-test", "superseded")

    replans = service.plan_for_project(proj_copy, registry_db, verified_catalog_path=None, line_item_ids=[target_plan.line_item_id])
    assert replans[0].path == LOOKUP_PATH_DESCRIPTION_SEARCH
