from __future__ import annotations

import yaml

from estimate_extractor.mapping.catalog import load_catalog
from estimate_extractor.ui import catalog_service
from estimate_extractor.ui.catalog_service import CatalogValidationError

BASE_CATALOG = [
    {
        "mapping_id": "rfg_existing_rule",
        "canonical_terms": ["laminated composition shingles"],
        "trade": "roofing",
        "component": "composition_shingles",
        "allowed_actions": ["install"],
        "allowed_units": ["SQ"],
        "xactimate": {"category": "RFG", "selector": None, "activity": "install", "description": "test"},
        "confidence_base": 0.9,
        "requires_review": True,
    }
]


def _write_catalog(tmp_path, entries=None) -> tuple:
    catalog_path = tmp_path / "mapping_catalog.yaml"
    catalog_path.write_text(yaml.safe_dump(entries if entries is not None else BASE_CATALOG, sort_keys=False), encoding="utf-8")
    backups_dir = tmp_path / "backups"
    return catalog_path, backups_dir


def _valid_rule(**overrides) -> dict:
    rule = {
        "mapping_id": "gut_new_rule",
        "canonical_terms": ["aluminum gutter"],
        "trade": "gutters",
        "component": "gutter",
        "allowed_actions": ["remove_and_replace"],
        "allowed_units": ["LF"],
        "xactimate": {"category": "GUT", "selector": None, "activity": "install", "description": "Aluminum gutter"},
        "confidence_base": 0.8,
        "requires_review": True,
    }
    rule.update(overrides)
    return rule


def test_validate_rule_missing_required_fields(tmp_path):
    catalog_path, _ = _write_catalog(tmp_path)
    existing = load_catalog(catalog_path)
    errors = catalog_service.validate_rule_dict({"mapping_id": "x"}, existing)
    assert errors
    assert any("Missing required field" in e for e in errors)


def test_validate_rule_duplicate_mapping_id_rejected(tmp_path):
    catalog_path, _ = _write_catalog(tmp_path)
    existing = load_catalog(catalog_path)
    rule = _valid_rule(mapping_id="rfg_existing_rule")
    errors = catalog_service.validate_rule_dict(rule, existing)
    assert any("already exists" in e for e in errors)


def test_validate_rule_unknown_action_rejected(tmp_path):
    catalog_path, _ = _write_catalog(tmp_path)
    existing = load_catalog(catalog_path)
    rule = _valid_rule(allowed_actions=["not_a_real_action"])
    errors = catalog_service.validate_rule_dict(rule, existing)
    assert any("Unknown action" in e for e in errors)


def test_validate_rule_unsupported_unit_rejected(tmp_path):
    catalog_path, _ = _write_catalog(tmp_path)
    existing = load_catalog(catalog_path)
    rule = _valid_rule(allowed_units=["FURLONGS"])
    errors = catalog_service.validate_rule_dict(rule, existing)
    assert any("Unsupported unit" in e for e in errors)


def test_validate_rule_selector_requires_explicit_confirmation(tmp_path):
    catalog_path, _ = _write_catalog(tmp_path)
    existing = load_catalog(catalog_path)
    rule = _valid_rule(xactimate={"category": "GUT", "selector": "GUT100", "activity": "install", "description": "x"})
    errors = catalog_service.validate_rule_dict(rule, existing)
    assert any("confirmation" in e for e in errors)

    rule["selector_confirmed"] = True
    errors = catalog_service.validate_rule_dict(rule, existing)
    assert errors == []


def test_validate_rule_conflicts_with_existing_exact_shape(tmp_path):
    catalog_path, _ = _write_catalog(tmp_path)
    existing = load_catalog(catalog_path)
    rule = _valid_rule(
        mapping_id="rfg_conflicting_rule",
        trade="roofing",
        component="composition_shingles",
        allowed_actions=["install"],
        allowed_units=["SQ"],
    )
    errors = catalog_service.validate_rule_dict(rule, existing)
    assert any("Conflicts with existing exact rule" in e for e in errors)


def test_validate_rule_valid_rule_has_no_errors(tmp_path):
    catalog_path, _ = _write_catalog(tmp_path)
    existing = load_catalog(catalog_path)
    errors = catalog_service.validate_rule_dict(_valid_rule(), existing)
    assert errors == []


def test_preview_affected_items_matches_trade_component_and_term():
    rule = {"trade": "roofing", "component": "composition_shingles", "canonical_terms": ["laminated"]}
    rows = [
        {"line_item_id": "line_0001", "normalized_trade": "roofing", "normalized_component": "composition_shingles", "original_description": "Laminated shingles"},
        {"line_item_id": "line_0002", "normalized_trade": "roofing", "normalized_component": "composition_shingles", "original_description": "3-tab shingles"},
        {"line_item_id": "line_0003", "normalized_trade": "gutters", "normalized_component": "gutter", "original_description": "Laminated gutter guard"},
    ]
    affected = catalog_service.preview_affected_items(rule, rows)
    assert affected == ["line_0001"]


def test_backup_catalog_creates_timestamped_copy(tmp_path):
    catalog_path, backups_dir = _write_catalog(tmp_path)
    backup_path = catalog_service.backup_catalog(catalog_path, backups_dir)
    assert backup_path.exists()
    assert backup_path.read_text(encoding="utf-8") == catalog_path.read_text(encoding="utf-8")
    assert backup_path.parent == backups_dir


def test_restore_last_backup_restores_content(tmp_path):
    catalog_path, backups_dir = _write_catalog(tmp_path)
    original_content = catalog_path.read_text(encoding="utf-8")

    catalog_service.backup_catalog(catalog_path, backups_dir)
    catalog_path.write_text("- mapping_id: corrupted\n", encoding="utf-8")
    assert catalog_path.read_text(encoding="utf-8") != original_content

    restored = catalog_service.restore_last_backup(catalog_path, backups_dir)
    assert restored.exists()
    assert catalog_path.read_text(encoding="utf-8") == original_content


def test_restore_last_backup_raises_when_no_backups_exist(tmp_path):
    catalog_path, backups_dir = _write_catalog(tmp_path)
    try:
        catalog_service.restore_last_backup(catalog_path, backups_dir)
        assert False, "expected CatalogServiceError"
    except catalog_service.CatalogServiceError:
        pass


def test_save_rule_backs_up_writes_catalog_and_records_audit_entry(tmp_path):
    catalog_path, backups_dir = _write_catalog(tmp_path)
    project_dir = tmp_path / "aranda-insurance"
    project_dir.mkdir()

    result = catalog_service.save_rule(
        catalog_path, backups_dir, project_dir, _valid_rule(), "tester", "adding gutter rule", ["line_0001"]
    )

    assert result.backup_path.exists()
    after = load_catalog(catalog_path)
    assert len(after) == len(BASE_CATALOG) + 1
    assert any(e.mapping_id == "gut_new_rule" for e in after)

    changes = catalog_service.get_catalog_changes(project_dir)
    assert len(changes) == 1
    assert changes[0]["mapping_id"] == "gut_new_rule"
    assert changes[0]["action"] == "add_rule"
    assert changes[0]["affected_line_items"] == ["line_0001"]
    assert changes[0]["previous_hash"] != changes[0]["new_hash"]


def test_save_rule_raises_and_does_not_write_when_invalid(tmp_path):
    catalog_path, backups_dir = _write_catalog(tmp_path)
    project_dir = tmp_path / "aranda-insurance"
    project_dir.mkdir()

    before_content = catalog_path.read_text(encoding="utf-8")
    try:
        catalog_service.save_rule(catalog_path, backups_dir, project_dir, {"mapping_id": "x"}, "tester", "", [])
        assert False, "expected CatalogValidationError"
    except CatalogValidationError:
        pass

    assert catalog_path.read_text(encoding="utf-8") == before_content
    assert catalog_service.get_catalog_changes(project_dir) == []
