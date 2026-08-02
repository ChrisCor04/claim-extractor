from __future__ import annotations

import yaml

from estimate_extractor.ui import group_name_service as gns

REAL_CONFIG_PATH = None  # set in test that needs the real shipped file


def _write_config(tmp_path, groups=None, aliases=None):
    path = tmp_path / "xactimate_group_names.yaml"
    data = {"groups": groups or ["Kitchen", "Roof", "Master Bedroom", "Garage"], "aliases": aliases or {}}
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_exact_known_group_matches_with_full_confidence(tmp_path):
    path = _write_config(tmp_path)
    config = gns.load_group_names(path)
    suggestion = gns.suggest_group_name("Kitchen", config)
    assert suggestion.suggested_group_name == "Kitchen"
    assert suggestion.confidence == 1.0
    assert suggestion.method == "exact"


def test_alias_suggestion(tmp_path):
    path = _write_config(tmp_path, aliases={"main dwelling roof": "Roof"})
    config = gns.load_group_names(path)
    suggestion = gns.suggest_group_name("Main Dwelling Roof", config)
    assert suggestion.suggested_group_name == "Roof"
    assert suggestion.method == "alias"


def test_fuzzy_suggestion_for_close_but_not_exact_name(tmp_path):
    path = _write_config(tmp_path)
    config = gns.load_group_names(path)
    suggestion = gns.suggest_group_name("Master Bedrom", config)  # typo
    assert suggestion.suggested_group_name == "Master Bedroom"
    assert suggestion.method in ("fuzzy", "substring")


def test_original_name_always_preserved_on_suggestion(tmp_path):
    path = _write_config(tmp_path)
    config = gns.load_group_names(path)
    suggestion = gns.suggest_group_name("Main Dwelling Roof - Rear Slope", config)
    assert suggestion.original_section_name == "Main Dwelling Roof - Rear Slope"


def test_no_forced_replacement_when_confidence_is_low(tmp_path):
    path = _write_config(tmp_path)
    config = gns.load_group_names(path)
    suggestion = gns.suggest_group_name("Xyzzy Nonexistent Zone 42", config)
    assert suggestion.suggested_group_name is None
    assert suggestion.method == "no_match"


def test_custom_name_recorded_without_being_in_vocabulary(tmp_path):
    project_dir = tmp_path / "proj"
    (project_dir / "review").mkdir(parents=True)
    config = gns.load_group_names(_write_config(tmp_path))
    suggestion = gns.suggest_group_name("Weird One-Off Section", config)

    entry = gns.set_group_name_review(project_dir, "Weird One-Off Section", suggestion, "tester", reviewed_group_name=None, allow_custom=True)
    assert entry["allow_custom"] is True
    assert entry["reviewed_xactimate_group_name"] is None
    assert gns.is_section_reviewed(project_dir, "Weird One-Off Section") is True


def test_reusable_group_alias_persists_and_is_used_on_next_suggestion(tmp_path):
    path = _write_config(tmp_path)
    backups_dir = tmp_path / "backups"

    config = gns.load_group_names(path)
    assert gns.suggest_group_name("Upper Rear Slope Section", config).method == "no_match"

    gns.save_reusable_group_alias(path, backups_dir, "Upper Rear Slope Section", "Roof")

    reloaded = gns.load_group_names(path)
    suggestion = gns.suggest_group_name("Upper Rear Slope Section", reloaded)
    assert suggestion.suggested_group_name == "Roof"
    assert suggestion.method == "alias"


def test_section_not_reviewed_until_explicitly_reviewed(tmp_path):
    project_dir = tmp_path / "proj"
    (project_dir / "review").mkdir(parents=True)
    assert gns.is_section_reviewed(project_dir, "Kitchen") is False
    assert gns.is_section_reviewed(project_dir, None) is False


def test_full_import_of_supplied_group_name_vocabulary():
    from pathlib import Path

    real_path = Path(__file__).resolve().parents[2] / "config" / "xactimate_group_names.yaml"
    config = gns.load_group_names(real_path)
    assert len(config.groups) == 129
    for expected in ("Kitchen", "Master Bedroom", "Roof", "Attic", "Garage", "Front Elevation", "Bathroom (full)"):
        assert expected in config.groups
