"""Integration tests: the Phase 3.7 selector-recommendation layer against
the real, local Phase 3.6 selector catalog (master_selectors.db) and real
local project fixtures under projects/ (both are large/proprietary-
adjacent local artifacts, gitignored -- these tests skip gracefully, not
fail, when either isn't present locally, matching this repo's existing
convention -- see tests/integration/test_selector_catalog_pipeline.py).
"""

from __future__ import annotations

import json
import shutil

import pytest

from estimate_extractor.selector_recommendation import service
from estimate_extractor.selector_recommendation.models import (
    CANDIDATE_SOURCE_VERIFIED_CATALOG,
)
from estimate_extractor.ui import review_service
from estimate_extractor.ui import verified_catalog_service as vcs

REPO_ROOT_RELATIVE_DB = "fixtures/reference/data/master_selectors.db"


@pytest.fixture(scope="module")
def real_db_path(fixtures_dir):
    path = fixtures_dir.parent / "reference" / "data" / "master_selectors.db"
    if not path.exists():
        pytest.skip("real master_selectors.db not present locally (gitignored, built via `selectors import`)")
    return path


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


def test_recommend_for_every_real_project_fixture(real_project_dirs, real_db_path):
    assert real_project_dirs, "expected at least one real processed project"
    for project_dir in real_project_dirs:
        results = service.recommend_for_project(project_dir, real_db_path)
        assert results, f"{project_dir.name}: expected at least one recommendation result"
        for r in results:
            ranks = [c.rank for c in r.candidates]
            assert ranks == sorted(ranks)
            assert ranks == list(range(1, len(ranks) + 1))
            for c in r.candidates:
                assert 0.0 <= c.score <= 1.0
                assert c.category and c.selector


def test_default_excludes_needs_review_selector_catalog_records_across_real_data(real_project_dirs, real_db_path):
    project_dir = real_project_dirs[0]
    results = service.recommend_for_project(project_dir, real_db_path, include_uncertain=False)
    for r in results:
        assert not any(c.source_needs_review for c in r.candidates)


def test_candidates_reference_real_selector_catalog_records(real_project_dirs, real_db_path):
    from estimate_extractor.selector_catalog import database

    project_dir = real_project_dirs[0]
    results = service.recommend_for_project(project_dir, real_db_path, top=3)

    conn = database.create_database(real_db_path)
    try:
        real_keys = {(r.category, r.selector) for r in database.load_all_records(conn)}
    finally:
        conn.close()

    checked = 0
    for r in results:
        for c in r.candidates:
            if c.source == "placeholder_mapping":
                continue
            assert (c.category, c.selector) in real_keys
            checked += 1
    assert checked > 0


def test_verified_catalog_priority_over_selector_catalog_scores(tmp_path, real_project_dirs, real_db_path):
    project_dir = real_project_dirs[0]
    rows = review_service.build_effective_rows(project_dir)
    row = next(r for r in rows if r["normalized_trade"] == "roofing")

    catalog_path = tmp_path / "verified_catalog.yaml"
    vcs.add_record(
        catalog_path,
        tmp_path / "backups",
        project_dir,
        {
            "category": "ZZZ_VERIFIED",
            "selector": "ZZZ_VERIFIED_SEL",
            "description": row["original_description"],
            "unit": row["unit"] or "EA",
            "trade": row["normalized_trade"],
            "component": row["normalized_component"],
            "aliases": [row["original_description"]],
        },
        "integration-test",
        verification_status=vcs.VERIFICATION_STATUS_HUMAN_VERIFIED,
        confirmations={"confirmed_category_selector": True, "confirmed_unit": True, "confirmed_price_context": True},
    )

    results = service.recommend_for_project(
        project_dir, real_db_path, verified_catalog_path=catalog_path, line_item_ids=[row["line_item_id"]]
    )
    result = results[0]
    assert result.candidates, "expected at least the injected verified candidate"
    assert result.candidates[0].source == CANDIDATE_SOURCE_VERIFIED_CATALOG
    assert result.candidates[0].category == "ZZZ_VERIFIED"
    assert result.state == "strong_candidate"


def test_apply_candidate_writes_through_review_service_and_preserves_history(tmp_path, real_project_dirs, real_db_path):
    project_dir = real_project_dirs[0]
    proj_copy = tmp_path / "proj_copy"
    shutil.copytree(project_dir, proj_copy)

    results = service.recommend_for_project(proj_copy, real_db_path, top=1)
    target = next((r for r in results if r.candidates and not r.locked_by_approval), None)
    if target is None:
        pytest.skip("no unlocked line item with a candidate in this fixture")

    history_before = len(review_service.get_review_history(proj_copy))
    candidate = target.candidates[0]
    service.apply_candidate(proj_copy, target.line_item_id, candidate, "integration-test", "matches source description")

    history_after = review_service.get_review_history(proj_copy)
    assert len(history_after) > history_before
    row = next(r for r in review_service.build_effective_rows(proj_copy) if r["line_item_id"] == target.line_item_id)
    assert row["category"] == candidate.category
    assert row["selector"] == candidate.selector

    events = service.get_recommendation_events(proj_copy)
    assert events[-1]["action"] == "accepted"


def test_approved_mapping_protected_from_silent_overwrite_real_fixture(tmp_path, real_project_dirs, real_db_path):
    project_dir = real_project_dirs[0]
    proj_copy = tmp_path / "proj_copy"
    shutil.copytree(project_dir, proj_copy)

    results = service.recommend_for_project(proj_copy, real_db_path, top=2)
    target = next((r for r in results if len(r.candidates) >= 2 and not r.locked_by_approval), None)
    if target is None:
        pytest.skip("no unlocked line item with 2+ candidates in this fixture")

    first, second = target.candidates[0], target.candidates[1]
    service.apply_candidate(proj_copy, target.line_item_id, first, "integration-test", "first pass", approve=False)
    review_service.waive_activity_requirement(proj_copy, target.line_item_id, "integration-test", "not required for this test fixture")
    review_service.approve_item(proj_copy, target.line_item_id, "integration-test")

    with pytest.raises(service.RecommendationApplyBlockedError):
        service.apply_candidate(proj_copy, target.line_item_id, second, "integration-test", "trying to switch")

    row = next(r for r in review_service.build_effective_rows(proj_copy) if r["line_item_id"] == target.line_item_id)
    assert row["category"] == first.category
    assert row["selector"] == first.selector
    assert row["status"] == review_service.STATUS_APPROVED


def test_reusable_rule_creation_uses_real_phase35_workflow(tmp_path, real_project_dirs, real_db_path):
    project_dir = real_project_dirs[0]
    proj_copy = tmp_path / "proj_copy"
    shutil.copytree(project_dir, proj_copy)

    normalized_by_id = {
        i["line_item_id"]: i for i in json.loads((proj_copy / "mapping" / "normalized_estimate.json").read_text())
    }
    results = service.recommend_for_project(proj_copy, real_db_path, top=1)
    target = next((r for r in results if r.candidates and not r.locked_by_approval), None)
    if target is None:
        pytest.skip("no unlocked line item with a candidate in this fixture")

    row = next(r for r in review_service.build_effective_rows(proj_copy) if r["line_item_id"] == target.line_item_id)
    item = service.build_recommendation_input(row, normalized_by_id.get(target.line_item_id))
    candidate = target.candidates[0]

    catalog_path = tmp_path / "verified_catalog.yaml"
    backups_dir = tmp_path / "backups"
    record = service.save_recommendation_as_verified_rule(
        catalog_path,
        backups_dir,
        proj_copy,
        item,
        candidate,
        "integration-test",
        confirmations={"confirmed_category_selector": True, "confirmed_unit": True, "confirmed_price_context": True},
    )

    assert record.verification_status == vcs.VERIFICATION_STATUS_HUMAN_VERIFIED
    assert vcs.list_verified_catalog_backups(backups_dir)  # backup-before-write happened
    reloaded = vcs.load_verified_catalog(catalog_path)
    assert vcs.find_record(reloaded, record.category, record.selector) is not None


def test_applying_eligible_candidate_alone_does_not_grant_automation_readiness(tmp_path, real_project_dirs, real_db_path):
    """Applying (and even approving) a Phase 3.6 selector-catalog
    candidate must never, by itself, satisfy Phase 3.5's automation-
    readiness gate -- only a human_verified catalog record or an item-only
    verification can. See build spec 'A recommendation score alone must
    never satisfy automation readiness.'"""
    project_dir = real_project_dirs[0]
    proj_copy = tmp_path / "proj_copy"
    shutil.copytree(project_dir, proj_copy)

    results = service.recommend_for_project(proj_copy, real_db_path, top=1)
    target = next((r for r in results if r.candidates and not r.locked_by_approval), None)
    if target is None:
        pytest.skip("no unlocked line item with a candidate in this fixture")

    candidate = target.candidates[0]
    service.apply_candidate(proj_copy, target.line_item_id, candidate, "integration-test", "confirmed", approve=False)
    review_service.waive_activity_requirement(proj_copy, target.line_item_id, "integration-test", "not required for this test fixture")
    review_service.approve_item(proj_copy, target.line_item_id, "integration-test")

    row = next(r for r in review_service.build_effective_rows(proj_copy) if r["line_item_id"] == target.line_item_id)
    assert row["status"] == review_service.STATUS_APPROVED

    ready, reasons = vcs.is_automation_ready(row, proj_copy, records=[], group_reviewed=True)
    assert ready is False
    assert reasons
