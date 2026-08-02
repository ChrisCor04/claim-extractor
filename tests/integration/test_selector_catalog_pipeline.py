"""Integration tests: run the real, local Tesseract-based pipeline against
real reference-library screenshots (no fabricated fixtures) for the eight
categories the build spec requires (RFG, ELE, PLM, PNT, WTR, FNC, WDV,
XST), plus a resumability check and a full-catalog validation check.

The reference ZIP is large (140MB) and Xactimate-proprietary-adjacent, so
it is gitignored (see fixtures/reference/ in .gitignore); these tests skip
gracefully -- not fail -- when the extracted library isn't present
locally, matching this repo's existing convention for gitignored fixtures
(see tests/integration/test_fixtures.py).
"""

from __future__ import annotations

import shutil

import pytest

from estimate_extractor.selector_catalog.database import create_database, load_all_records
from estimate_extractor.selector_catalog.image_inventory import ImageInventoryError, find_screenshots_root
from estimate_extractor.selector_catalog.pipeline import run_import, write_outputs

REQUIRED_CATEGORIES = ["RFG", "ELE", "PLM", "PNT", "WTR", "FNC", "WDV", "XST"]

REPO_ROOT_RELATIVE_EXTRACTED = "fixtures/reference/extracted"


@pytest.fixture(scope="module")
def extracted_root(fixtures_dir):
    # fixtures_dir is fixtures/originals/ (see conftest.py); the reference
    # library lives one level up, under fixtures/reference/extracted/.
    root = fixtures_dir.parent / "reference" / "extracted"
    try:
        find_screenshots_root(root)
    except ImageInventoryError:
        pytest.skip("reference selector-screenshot library not extracted locally (gitignored, large ZIP)")
    return root


@pytest.mark.parametrize("category", REQUIRED_CATEGORIES)
def test_real_category_produces_records_with_provenance(extracted_root, category, tmp_path):
    data_dir = tmp_path / category / "data"
    result = run_import(extracted_root, data_dir, category_filter=category)

    assert result.total_screenshots > 0, f"no screenshots found for category {category!r}"
    assert result.failed == 0, f"{result.failed} screenshot(s) failed for {category}: " + "; ".join(
        f"{e.relative_path}: {e.error}" for e in result.manifest_entries if e.error
    )
    assert len(result.records) > 0

    for record in result.records:
        assert record.category  # never empty
        assert record.selector  # never empty
        assert record.description_original  # never empty
        assert record.source_images  # provenance always present
        assert record.primary_source_image is not None

    # No line item in this category set should ever silently vanish: every
    # extracted row either becomes a record or a review-queue row.
    assert result.validation.passed, [i.message for i in result.validation.errors]


def test_real_import_writes_all_required_output_files(extracted_root, tmp_path):
    data_dir = tmp_path / "data"
    reports_dir = tmp_path / "reports"
    result = run_import(extracted_root, data_dir, category_filter="XST")  # smallest real category (2 screenshots)
    paths = write_outputs(result, data_dir, reports_dir)

    for path in paths.values():
        assert path.exists()
        assert path.stat().st_size > 0

    conn = create_database(paths["database"])
    loaded = load_all_records(conn)
    conn.close()
    assert len(loaded) == len(result.records)


def test_resumed_import_against_real_screenshots_reuses_cache(extracted_root, tmp_path):
    data_dir = tmp_path / "data"
    first = run_import(extracted_root, data_dir, category_filter="WDV")  # 2 screenshots -- fast
    assert first.processed == 2
    assert first.failed == 0

    second = run_import(extracted_root, data_dir, category_filter="WDV")
    assert second.from_cache == 2
    assert second.processed == 0
    assert {r.key for r in second.records} == {r.key for r in first.records}


def test_folder_title_mismatch_is_detected_on_a_real_known_case(extracted_root, tmp_path):
    """The ELE folder's very first screenshot is confirmed (by direct
    visual inspection during this phase's build) to show a 'Selectors for
    FNC' title bar. Across the whole ELE folder, a clear majority (6/9,
    ~67%) of independent screenshots agree on "FNC" -- real, corroborated
    evidence of a genuine folder/title-bar disagreement the pipeline must
    flag, never silently resolve.

    Processing only the single first screenshot (as an earlier version of
    this test did) is deliberately NOT enough on its own to trigger
    category_mismatch anymore -- see
    docs/selector-catalog.md "QA cleanup pass": a lone screenshot's title
    disagreement, unlike this folder-wide majority, was found to also
    describe real false-positive cases (the DOR and DMO folders) and is no
    longer trusted alone."""
    data_dir = tmp_path / "data"
    result = run_import(extracted_root, data_dir, category_filter="ELE")
    assert result.processed > 1  # full folder, not a single screenshot

    fnc_titled = [r for r in result.records if r.title_bar_category == "FNC"]
    assert fnc_titled, "expected at least one record whose title-bar OCR read 'FNC'"

    mismatched = [r for r in result.records if r.category_mismatch]
    assert mismatched, "expected the folder-wide FNC majority to be corroborated as a genuine mismatch"
    for record in mismatched:
        assert record.category == "ELE"  # folder wins as the primary key
        assert record.title_bar_category == "FNC"
        assert "category_mismatch" in record.review_reasons
        assert record.needs_review is True


def test_no_screenshot_is_silently_ignored_across_all_required_categories(extracted_root, tmp_path):
    data_dir = tmp_path / "data"
    all_relative_paths: set[str] = set()
    all_manifest_paths: set[str] = set()

    for category in REQUIRED_CATEGORIES:
        result = run_import(extracted_root, data_dir, category_filter=category)
        for entry in result.manifest_entries:
            if entry.folder_category != category:
                continue
            all_manifest_paths.add(entry.relative_path)
            assert entry.status in ("processed", "skipped", "failed")
            if entry.status == "failed":
                assert entry.error
            if entry.status == "skipped":
                assert entry.skip_reason

    shutil.rmtree(data_dir, ignore_errors=True)
    assert len(all_manifest_paths) > 0
