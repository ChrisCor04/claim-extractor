"""Safe, audited mutation of config/mapping_catalog.yaml from the review
UI. Every write is preceded by a timestamped backup and followed by a
review/catalog_changes.json audit entry recording before/after content
hashes -- see docs/local-review-ui.md "Reusable mapping-rule creation" and
"Catalog safety".

This module never edits normalization_rules.yaml or mapping_scoring.yaml;
only the catalog (which line items get a category/selector) is
UI-editable, matching the build spec's "Reusable mapping-rule creation"
workflow.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml

from estimate_extractor.mapping.catalog import CatalogEntry, load_catalog
from estimate_extractor.mapping.models import ActionType, TradeType

# Conservative, explicit unit vocabulary. Extending this list is itself a
# deliberate, reviewable code change (not something the UI can silently
# widen), consistent with "do not invent" -- see docs/local-review-ui.md.
ALLOWED_UNITS = frozenset(
    {"SQ", "LF", "SF", "EA", "HR", "CY", "SY", "GAL", "LB", "DA", "MO", "BX", "PR", "RL", "PS"}
)

REQUIRED_RULE_FIELDS = ("mapping_id", "canonical_terms", "trade", "component", "allowed_actions", "allowed_units", "xactimate", "confidence_base")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class CatalogServiceError(Exception):
    pass


class CatalogValidationError(CatalogServiceError):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def validate_rule_dict(rule: dict, existing_catalog: list[CatalogEntry]) -> list[str]:
    """Returns a list of human-readable errors; empty list means the rule
    is safe to save. Never raises -- callers decide what to do with the
    error list (the UI shows them inline before the Confirm button is
    enabled)."""
    errors: list[str] = []

    for f in REQUIRED_RULE_FIELDS:
        if f not in rule or rule[f] in (None, "", []):
            errors.append(f"Missing required field: {f!r}.")
    if errors:
        return errors  # remaining checks assume required fields exist

    mapping_id = rule["mapping_id"]
    if mapping_id in {e.mapping_id for e in existing_catalog}:
        errors.append(f"mapping_id {mapping_id!r} already exists in the catalog (duplicate mapping IDs are rejected).")

    known_actions = {a.value for a in ActionType}
    for action in rule["allowed_actions"]:
        if action not in known_actions:
            errors.append(f"Unknown action {action!r} -- not in the supported action vocabulary.")

    known_trades = {t.value for t in TradeType}
    if rule["trade"] not in known_trades:
        errors.append(f"Unknown trade {rule['trade']!r} -- not in the supported trade vocabulary.")

    for unit in rule["allowed_units"]:
        if str(unit).upper() not in ALLOWED_UNITS:
            errors.append(f"Unsupported unit {unit!r}.")

    confidence_base = rule["confidence_base"]
    if not isinstance(confidence_base, (int, float)) or not (0.0 <= float(confidence_base) <= 1.0):
        errors.append("confidence_base must be a number between 0.0 and 1.0.")

    xactimate = rule["xactimate"] or {}
    selector = xactimate.get("selector")
    if selector and not rule.get("selector_confirmed"):
        errors.append(
            "A selector cannot be saved without explicit human confirmation "
            "(check 'I have verified this selector against a licensed Xactimate price list')."
        )

    shape = (
        rule["trade"],
        rule["component"],
        tuple(sorted(rule["allowed_actions"])),
        tuple(sorted(str(u).upper() for u in rule["allowed_units"])),
    )
    for entry in existing_catalog:
        existing_shape = (entry.trade, entry.component, tuple(sorted(entry.allowed_actions)), tuple(sorted(entry.allowed_units)))
        if existing_shape == shape:
            errors.append(
                f"Conflicts with existing exact rule {entry.mapping_id!r}, which already covers the same "
                f"trade/component/allowed_actions/allowed_units combination."
            )
            break

    return errors


def preview_affected_items(rule: dict, effective_rows: list[dict]) -> list[str]:
    """Which line_item_ids in the currently-open project would newly match
    this rule's trade/component + at least one canonical term. Best-effort
    substring match against the original description -- shown to the
    reviewer before they confirm the save, never used to auto-apply
    anything."""
    trade = rule.get("trade")
    component = rule.get("component")
    terms = [t.lower() for t in rule.get("canonical_terms", [])]
    affected = []
    for row in effective_rows:
        if row.get("normalized_trade") != trade or row.get("normalized_component") != component:
            continue
        desc = (row.get("original_description") or "").lower()
        if any(t in desc for t in terms):
            affected.append(row["line_item_id"])
    return affected


def rule_to_yaml_preview(rule: dict) -> str:
    entry = _rule_to_catalog_entry_dict(rule)
    return yaml.safe_dump([entry], sort_keys=False)


def _rule_to_catalog_entry_dict(rule: dict) -> dict:
    entry = {
        "mapping_id": rule["mapping_id"],
        "canonical_terms": list(rule["canonical_terms"]),
        "trade": rule["trade"],
        "component": rule["component"],
        "allowed_actions": list(rule["allowed_actions"]),
        "allowed_units": [str(u).upper() for u in rule["allowed_units"]],
        "xactimate": {
            "category": rule["xactimate"].get("category"),
            "selector": rule["xactimate"].get("selector"),
            "activity": rule["xactimate"].get("activity"),
            "description": rule["xactimate"].get("description"),
        },
        "confidence_base": float(rule["confidence_base"]),
        "requires_review": bool(rule.get("requires_review", True)),
    }
    if rule.get("notes"):
        entry["notes"] = list(rule["notes"])
    if rule.get("required_attributes"):
        entry["required_attributes"] = dict(rule["required_attributes"])
    return entry


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def backup_catalog(catalog_path: Path, backups_dir: Path) -> Path:
    backups_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_path = backups_dir / f"mapping_catalog_{timestamp}.yaml"
    try:
        shutil.copy2(catalog_path, backup_path)
    except OSError as exc:
        raise CatalogServiceError(f"Failed to back up catalog before writing: {exc}") from exc
    return backup_path


def list_backups(backups_dir: Path) -> list[Path]:
    if not backups_dir.exists():
        return []
    return sorted(backups_dir.glob("mapping_catalog_*.yaml"))


def restore_last_backup(catalog_path: Path, backups_dir: Path) -> Path:
    backups = list_backups(backups_dir)
    if not backups:
        raise CatalogServiceError("No catalog backups exist to restore.")
    latest = backups[-1]
    shutil.copy2(latest, catalog_path)
    return latest


def append_catalog_change_record(project_dir: Path, record: dict) -> None:
    """Appends one entry to review/catalog_changes.json. Shared by both the
    Phase 3 placeholder-catalog rule builder (target: mapping_catalog,
    implicit for backward compatibility) and the Phase 3.5 verified-catalog
    builder (target: verified_catalog) -- see verified_catalog_service.py."""
    record.setdefault("target", "mapping_catalog")
    path = project_dir / "review" / "catalog_changes.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    changes = []
    if path.exists():
        try:
            changes = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            changes = []
    changes.append(record)
    path.write_text(json.dumps(changes, indent=2, default=str), encoding="utf-8")


def get_catalog_changes(project_dir: Path) -> list[dict]:
    path = project_dir / "review" / "catalog_changes.json"
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []


@dataclass(slots=True)
class SaveRuleResult:
    change_record: dict
    backup_path: Path


def save_rule(
    catalog_path: Path,
    backups_dir: Path,
    project_dir: Path,
    rule: dict,
    reviewer: str,
    reviewer_note: str,
    affected_line_items: list[str],
) -> SaveRuleResult:
    """Draft rule -> validate -> preview -> confirm -> backup -> write ->
    audit. Callers must have already run validate_rule_dict() and
    preview_affected_items() and gotten explicit user confirmation before
    calling this -- this function performs the actual, irreversible-without-
    a-restore catalog write."""
    existing_catalog = load_catalog(catalog_path)
    errors = validate_rule_dict(rule, existing_catalog)
    if errors:
        raise CatalogValidationError(errors)

    previous_hash = _file_sha256(catalog_path)
    backup_path = backup_catalog(catalog_path, backups_dir)

    raw = yaml.safe_load(catalog_path.read_text(encoding="utf-8")) or []
    raw.append(_rule_to_catalog_entry_dict(rule))
    try:
        catalog_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    except OSError as exc:
        raise CatalogServiceError(f"Failed to write catalog: {exc}") from exc

    new_hash = _file_sha256(catalog_path)

    change_record = {
        "target": "mapping_catalog",
        "timestamp": utc_now_iso(),
        "action": "add_rule",
        "mapping_id": rule["mapping_id"],
        "previous_hash": previous_hash,
        "new_hash": new_hash,
        "backup_path": str(backup_path),
        "affected_line_items": affected_line_items,
        "reviewer": reviewer,
        "reviewer_note": reviewer_note,
    }
    append_catalog_change_record(project_dir, change_record)
    return SaveRuleResult(change_record=change_record, backup_path=backup_path)
