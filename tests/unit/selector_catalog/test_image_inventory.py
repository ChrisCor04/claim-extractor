from __future__ import annotations

import pytest

from estimate_extractor.selector_catalog.image_inventory import (
    ImageInventoryError,
    find_screenshots_root,
    inventory_screenshots,
)


def _make_library(tmp_path, top_level_name="Xactimate_Reference_Library_v3"):
    root = tmp_path / top_level_name / "Screenshots_By_CAT"
    (root / "RFG").mkdir(parents=True)
    (root / "RFG" / "RFG_001_aaaa.png").write_bytes(b"fake")
    (root / "RFG" / "RFG_002_bbbb.png").write_bytes(b"fake")
    (root / "RFG" / "RFG_image_index.csv").write_text("cat,sequence\n")  # must be ignored (not .png)
    (root / "_NON_CAT_PROJECT_UI").mkdir(parents=True)
    (root / "_NON_CAT_PROJECT_UI" / "_NON_CAT_PROJECT_UI_001_cccc.png").write_bytes(b"fake")
    return tmp_path


def test_find_screenshots_root_locates_nested_directory(tmp_path):
    _make_library(tmp_path)
    found = find_screenshots_root(tmp_path)
    assert found.name == "Screenshots_By_CAT"
    assert found.is_dir()


def test_find_screenshots_root_raises_when_missing(tmp_path):
    with pytest.raises(ImageInventoryError):
        find_screenshots_root(tmp_path)


def test_inventory_excludes_non_png_files(tmp_path):
    _make_library(tmp_path)
    refs = inventory_screenshots(tmp_path)
    assert all(r.filename.endswith(".png") for r in refs)
    assert not any("image_index" in r.filename for r in refs)


def test_inventory_folder_category_detection(tmp_path):
    _make_library(tmp_path)
    refs = inventory_screenshots(tmp_path)
    rfg_refs = [r for r in refs if r.folder_category == "RFG"]
    assert len(rfg_refs) == 2


def test_inventory_sequence_parsed_from_filename(tmp_path):
    _make_library(tmp_path)
    refs = {r.filename: r for r in inventory_screenshots(tmp_path)}
    assert refs["RFG_001_aaaa.png"].sequence == 1
    assert refs["RFG_002_bbbb.png"].sequence == 2


def test_inventory_handles_underscore_prefixed_folder_name(tmp_path):
    """_NON_CAT_PROJECT_UI itself contains underscores -- sequence parsing
    must strip the whole folder-category prefix, not naively split on the
    first underscore."""
    _make_library(tmp_path)
    refs = inventory_screenshots(tmp_path)
    non_cat = [r for r in refs if r.folder_category == "_NON_CAT_PROJECT_UI"]
    assert len(non_cat) == 1
    assert non_cat[0].sequence == 1
    assert non_cat[0].is_non_cat is True


def test_inventory_is_deterministically_ordered(tmp_path):
    _make_library(tmp_path)
    refs_a = inventory_screenshots(tmp_path)
    refs_b = inventory_screenshots(tmp_path)
    assert [r.relative_path for r in refs_a] == [r.relative_path for r in refs_b]
