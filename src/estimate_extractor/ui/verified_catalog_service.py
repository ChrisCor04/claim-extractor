"""Verified Xactimate catalog: stable selector identity, separated from
price-list observations, built exclusively from a human reviewer manually
transcribing what they personally verified in their own licensed Xactimate
environment (or, for seed/test rows, from a screenshot supplied during
this phase's build spec -- see verification_status below).

This module never scrapes, automates, or reverse-engineers Xactimate. It
only stores and validates reviewer-entered records and matches them
against normalized line items using deterministic, safety-gated
compatibility rules (trade + component + unit + action + negative
patterns) -- text similarity alone is never sufficient to produce a
match. See docs/verified-catalog-builder.md.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml

VERIFICATION_STATUS_HUMAN_VERIFIED = "human_verified"
VERIFICATION_STATUS_SCREENSHOT_TRANSCRIBED = "screenshot_transcribed"
VERIFICATION_STATUS_PLACEHOLDER = "placeholder"
VALID_VERIFICATION_STATUSES = frozenset(
    {VERIFICATION_STATUS_HUMAN_VERIFIED, VERIFICATION_STATUS_SCREENSHOT_TRANSCRIBED, VERIFICATION_STATUS_PLACEHOLDER}
)

# Only these statuses may ever back an automation-ready item -- see
# is_automation_ready(). screenshot_transcribed and placeholder never do.
AUTOMATION_READY_STATUSES = frozenset({VERIFICATION_STATUS_HUMAN_VERIFIED})

BACKUP_PREFIX = "verified_xactimate_catalog"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class VerifiedCatalogError(Exception):
    pass


class RecordValidationError(VerifiedCatalogError):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


class VerificationConfirmationError(VerifiedCatalogError):
    """Raised when a caller tries to create/upgrade a human_verified record
    without all three required confirmations -- see build spec 'Require
    explicit checkboxes'."""


class DuplicateRecordError(VerifiedCatalogError):
    def __init__(self, category: str, selector: str):
        super().__init__(f"A catalog record for category={category!r} selector={selector!r} already exists.")
        self.category = category
        self.selector = selector


class RecordNotFoundError(VerifiedCatalogError):
    pass


class ApprovalOverrideBlockedError(VerifiedCatalogError):
    """Raised when applying a verified match would silently change an
    already-approved item's category/selector -- never allowed without an
    explicit override confirmation."""

    def __init__(self, line_item_id: str):
        super().__init__(
            f"{line_item_id} is already approved with a different category/selector. "
            f"Pass allow_override_approved=True (with an explicit reviewer reason) to override it -- "
            f"it is never overwritten silently."
        )
        self.line_item_id = line_item_id


REQUIRED_CONFIRMATIONS = ("confirmed_category_selector", "confirmed_unit", "confirmed_price_context")


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class PriceObservation:
    observation_id: str
    price_list: str
    location: str | None = None
    price_list_date: str | None = None
    language: str | None = None
    type: str | None = None
    unit_price: float | None = None
    observed_at: str | None = None
    observed_by: str | None = None

    def to_dict(self) -> dict:
        return {
            "observation_id": self.observation_id,
            "price_list": self.price_list,
            "location": self.location,
            "price_list_date": self.price_list_date,
            "language": self.language,
            "type": self.type,
            "unit_price": self.unit_price,
            "observed_at": self.observed_at,
            "observed_by": self.observed_by,
        }

    @staticmethod
    def from_dict(data: dict) -> PriceObservation:
        return PriceObservation(
            observation_id=data["observation_id"],
            price_list=data.get("price_list"),
            location=data.get("location"),
            price_list_date=data.get("price_list_date"),
            language=data.get("language"),
            type=data.get("type"),
            unit_price=data.get("unit_price"),
            observed_at=data.get("observed_at"),
            observed_by=data.get("observed_by"),
        )


@dataclass(slots=True)
class VerifiedCatalogRecord:
    catalog_record_id: str
    category: str
    selector: str
    description: str
    unit: str
    activity_raw: str | None = None
    activity_interpretation: str | None = None
    green_indicator: bool | None = None  # tri-state: None=unknown
    aliases: list[str] = field(default_factory=list)
    trade: str = "unknown"
    component: str = "unknown"
    material: str | None = None
    supported_actions: list[str] = field(default_factory=list)
    negative_patterns: list[str] = field(default_factory=list)
    verification_status: str = VERIFICATION_STATUS_PLACEHOLDER
    verified_at: str | None = None
    verified_by: str | None = None
    verification_method: str | None = None
    verification_notes: str | None = None
    price_observations: list[PriceObservation] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "catalog_record_id": self.catalog_record_id,
            "category": self.category,
            "selector": self.selector,
            "description": self.description,
            "unit": self.unit,
            "activity_raw": self.activity_raw,
            "activity_interpretation": self.activity_interpretation,
            "green_indicator": self.green_indicator,
            "aliases": list(self.aliases),
            "trade": self.trade,
            "component": self.component,
            "material": self.material,
            "supported_actions": list(self.supported_actions),
            "negative_patterns": list(self.negative_patterns),
            "verification_status": self.verification_status,
            "verified_at": self.verified_at,
            "verified_by": self.verified_by,
            "verification_method": self.verification_method,
            "verification_notes": self.verification_notes,
            "price_observations": [o.to_dict() for o in self.price_observations],
        }

    @staticmethod
    def from_dict(data: dict) -> VerifiedCatalogRecord:
        return VerifiedCatalogRecord(
            catalog_record_id=data["catalog_record_id"],
            category=data["category"],
            selector=data["selector"],
            description=data.get("description", ""),
            unit=data.get("unit", ""),
            activity_raw=data.get("activity_raw"),
            activity_interpretation=data.get("activity_interpretation"),
            green_indicator=data.get("green_indicator"),
            aliases=list(data.get("aliases") or []),
            trade=data.get("trade", "unknown"),
            component=data.get("component", "unknown"),
            material=data.get("material"),
            supported_actions=list(data.get("supported_actions") or []),
            negative_patterns=list(data.get("negative_patterns") or []),
            verification_status=data.get("verification_status", VERIFICATION_STATUS_PLACEHOLDER),
            verified_at=data.get("verified_at"),
            verified_by=data.get("verified_by"),
            verification_method=data.get("verification_method"),
            verification_notes=data.get("verification_notes"),
            price_observations=[PriceObservation.from_dict(o) for o in (data.get("price_observations") or [])],
        )


@dataclass(slots=True)
class VerifiedMatchCandidate:
    record: VerifiedCatalogRecord
    match_reason: str


# ---------------------------------------------------------------------------
# Load / save
# ---------------------------------------------------------------------------


def load_verified_catalog(path: Path) -> list[VerifiedCatalogRecord]:
    if not path.exists():
        return []
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    records = [VerifiedCatalogRecord.from_dict(item) for item in raw]
    seen: set[tuple[str, str]] = set()
    for r in records:
        key = (r.category, r.selector)
        if key in seen:
            raise VerifiedCatalogError(f"Duplicate (category, selector) in catalog file: {key!r}")
        seen.add(key)
    return records


def _write_catalog(path: Path, records: list[VerifiedCatalogRecord]) -> None:
    path.write_text(yaml.safe_dump([r.to_dict() for r in records], sort_keys=False), encoding="utf-8")


def find_record(records: list[VerifiedCatalogRecord], category: str, selector: str) -> VerifiedCatalogRecord | None:
    for r in records:
        if r.category == category and r.selector == selector:
            return r
    return None


def _file_sha256(path: Path) -> str:
    if not path.exists():
        return hashlib.sha256(b"").hexdigest()
    return hashlib.sha256(path.read_bytes()).hexdigest()


def backup_verified_catalog(catalog_path: Path, backups_dir: Path) -> Path:
    backups_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_path = backups_dir / f"{BACKUP_PREFIX}_{timestamp}.yaml"
    if catalog_path.exists():
        shutil.copy2(catalog_path, backup_path)
    else:
        backup_path.write_text("[]\n", encoding="utf-8")
    return backup_path


def list_verified_catalog_backups(backups_dir: Path) -> list[Path]:
    if not backups_dir.exists():
        return []
    return sorted(backups_dir.glob(f"{BACKUP_PREFIX}_*.yaml"))


def restore_last_verified_catalog_backup(catalog_path: Path, backups_dir: Path) -> Path:
    backups = list_verified_catalog_backups(backups_dir)
    if not backups:
        raise VerifiedCatalogError("No verified-catalog backups exist to restore.")
    latest = backups[-1]
    shutil.copy2(latest, catalog_path)
    return latest


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

_ID_SAFE_RE = re.compile(r"[^a-zA-Z0-9]+")


def _slugify_id(text: str) -> str:
    return _ID_SAFE_RE.sub("_", text).strip("_").lower()


def generate_catalog_record_id(category: str, selector: str) -> str:
    return f"xactimate_{_slugify_id(category)}_{_slugify_id(selector)}"


def validate_record_fields(fields: dict, existing_records: list[VerifiedCatalogRecord]) -> list[str]:
    from estimate_extractor.ui.catalog_service import ALLOWED_UNITS

    errors: list[str] = []
    for required in ("category", "selector", "description", "unit"):
        if not fields.get(required):
            errors.append(f"Missing required field: {required!r}.")
    if errors:
        return errors

    if find_record(existing_records, fields["category"], fields["selector"]) is not None:
        errors.append(
            f"A catalog record already exists for category={fields['category']!r} selector={fields['selector']!r} "
            f"(category+selector is a compound unique key)."
        )

    status = fields.get("verification_status", VERIFICATION_STATUS_PLACEHOLDER)
    if status not in VALID_VERIFICATION_STATUSES:
        errors.append(f"Unknown verification_status {status!r}.")

    if str(fields["unit"]).upper() not in ALLOWED_UNITS:
        errors.append(f"Unsupported unit {fields['unit']!r}.")

    return errors


def _require_confirmations(confirmations: dict[str, bool] | None) -> None:
    confirmations = confirmations or {}
    missing = [c for c in REQUIRED_CONFIRMATIONS if not confirmations.get(c)]
    if missing:
        raise VerificationConfirmationError(
            f"Cannot mark this record human_verified without all required confirmations; missing: {missing}."
        )


def _require_reviewer_identity(reviewer: str | None) -> None:
    if not reviewer or not reviewer.strip():
        raise VerificationConfirmationError(
            "A human_verified record must record who verified it (reviewer/workstation name); "
            "verification provenance cannot be empty."
        )


# ---------------------------------------------------------------------------
# Mutations (all backup-before-write + audited via catalog_service's shared
# review/catalog_changes.json, target='verified_catalog')
# ---------------------------------------------------------------------------


def _append_change(project_dir: Path, record: dict) -> None:
    from estimate_extractor.ui.catalog_service import append_catalog_change_record

    record["target"] = "verified_catalog"
    append_catalog_change_record(project_dir, record)


def add_record(
    catalog_path: Path,
    backups_dir: Path,
    project_dir: Path,
    fields: dict,
    reviewer: str,
    *,
    verification_status: str = VERIFICATION_STATUS_PLACEHOLDER,
    confirmations: dict[str, bool] | None = None,
    reviewer_note: str = "",
) -> VerifiedCatalogRecord:
    """Adds a new stable catalog record. `verification_status` controls
    the gate: VERIFICATION_STATUS_HUMAN_VERIFIED requires all three
    REQUIRED_CONFIRMATIONS to be True (raises VerificationConfirmationError
    otherwise); screenshot_transcribed/placeholder do not."""
    existing = load_verified_catalog(catalog_path)
    validation_fields = {**fields, "verification_status": verification_status}
    errors = validate_record_fields(validation_fields, existing)
    if errors:
        raise RecordValidationError(errors)

    if verification_status == VERIFICATION_STATUS_HUMAN_VERIFIED:
        _require_confirmations(confirmations)
        _require_reviewer_identity(reviewer)

    record = VerifiedCatalogRecord(
        catalog_record_id=fields.get("catalog_record_id") or generate_catalog_record_id(fields["category"], fields["selector"]),
        category=fields["category"],
        selector=fields["selector"],
        description=fields["description"],
        unit=fields["unit"],
        activity_raw=fields.get("activity_raw"),
        activity_interpretation=fields.get("activity_interpretation"),
        green_indicator=fields.get("green_indicator"),
        aliases=list(fields.get("aliases") or []),
        trade=fields.get("trade", "unknown"),
        component=fields.get("component", "unknown"),
        material=fields.get("material"),
        supported_actions=list(fields.get("supported_actions") or []),
        negative_patterns=list(fields.get("negative_patterns") or []),
        verification_status=verification_status,
        verified_at=utc_now_iso() if verification_status == VERIFICATION_STATUS_HUMAN_VERIFIED else None,
        verified_by=reviewer if verification_status == VERIFICATION_STATUS_HUMAN_VERIFIED else None,
        verification_method="xactimate_selector_browser" if verification_status == VERIFICATION_STATUS_HUMAN_VERIFIED else fields.get("verification_method"),
        verification_notes=fields.get("verification_notes"),
    )

    price_list = fields.get("price_list")
    if price_list:
        record.price_observations.append(
            PriceObservation(
                observation_id=f"obs_{record.catalog_record_id}_{len(record.price_observations) + 1:03d}",
                price_list=price_list,
                location=fields.get("price_list_location"),
                price_list_date=fields.get("price_list_date"),
                language=fields.get("price_list_language"),
                type=fields.get("price_list_type"),
                unit_price=fields.get("unit_price"),
                observed_at=utc_now_iso(),
                observed_by=reviewer,
            )
        )

    previous_hash = _file_sha256(catalog_path)
    backup_path = backup_verified_catalog(catalog_path, backups_dir)
    existing.append(record)
    _write_catalog(catalog_path, existing)
    new_hash = _file_sha256(catalog_path)

    _append_change(
        project_dir,
        {
            "timestamp": utc_now_iso(),
            "action": "add_verified_record",
            "mapping_id": record.catalog_record_id,
            "previous_hash": previous_hash,
            "new_hash": new_hash,
            "backup_path": str(backup_path),
            "affected_line_items": [],
            "reviewer": reviewer,
            "reviewer_note": reviewer_note,
            "verification_status": verification_status,
        },
    )
    return record


def upgrade_to_human_verified(
    catalog_path: Path,
    backups_dir: Path,
    project_dir: Path,
    category: str,
    selector: str,
    reviewer: str,
    confirmations: dict[str, bool],
    reviewer_note: str = "",
) -> VerifiedCatalogRecord:
    """Promotes an existing screenshot_transcribed/placeholder record to
    human_verified in place. Requires the same three confirmations as
    creating a new human_verified record."""
    _require_confirmations(confirmations)
    _require_reviewer_identity(reviewer)

    existing = load_verified_catalog(catalog_path)
    record = find_record(existing, category, selector)
    if record is None:
        raise RecordNotFoundError(f"No catalog record for category={category!r} selector={selector!r}.")

    previous_hash = _file_sha256(catalog_path)
    backup_path = backup_verified_catalog(catalog_path, backups_dir)

    old_status = record.verification_status
    record.verification_status = VERIFICATION_STATUS_HUMAN_VERIFIED
    record.verified_at = utc_now_iso()
    record.verified_by = reviewer
    record.verification_method = "xactimate_selector_browser"

    _write_catalog(catalog_path, existing)
    new_hash = _file_sha256(catalog_path)

    _append_change(
        project_dir,
        {
            "timestamp": utc_now_iso(),
            "action": "upgrade_verification_status",
            "mapping_id": record.catalog_record_id,
            "previous_hash": previous_hash,
            "new_hash": new_hash,
            "backup_path": str(backup_path),
            "affected_line_items": [],
            "reviewer": reviewer,
            "reviewer_note": reviewer_note,
            "verification_status": f"{old_status} -> {VERIFICATION_STATUS_HUMAN_VERIFIED}",
        },
    )
    return record


def add_price_observation(
    catalog_path: Path,
    backups_dir: Path,
    project_dir: Path,
    category: str,
    selector: str,
    observation_fields: dict,
    reviewer: str,
    reviewer_note: str = "",
) -> PriceObservation:
    existing = load_verified_catalog(catalog_path)
    record = find_record(existing, category, selector)
    if record is None:
        raise RecordNotFoundError(f"No catalog record for category={category!r} selector={selector!r}.")
    if not observation_fields.get("price_list"):
        raise RecordValidationError(["price_list is required for a price observation."])

    previous_hash = _file_sha256(catalog_path)
    backup_path = backup_verified_catalog(catalog_path, backups_dir)

    observation = PriceObservation(
        observation_id=f"obs_{record.catalog_record_id}_{len(record.price_observations) + 1:03d}",
        price_list=observation_fields["price_list"],
        location=observation_fields.get("location"),
        price_list_date=observation_fields.get("price_list_date"),
        language=observation_fields.get("language"),
        type=observation_fields.get("type"),
        unit_price=observation_fields.get("unit_price"),
        observed_at=utc_now_iso(),
        observed_by=reviewer,
    )
    record.price_observations.append(observation)
    _write_catalog(catalog_path, existing)
    new_hash = _file_sha256(catalog_path)

    _append_change(
        project_dir,
        {
            "timestamp": utc_now_iso(),
            "action": "add_price_observation",
            "mapping_id": record.catalog_record_id,
            "previous_hash": previous_hash,
            "new_hash": new_hash,
            "backup_path": str(backup_path),
            "affected_line_items": [],
            "reviewer": reviewer,
            "reviewer_note": reviewer_note,
        },
    )
    return observation


# ---------------------------------------------------------------------------
# Item-only verification (Save for this item only -- not a reusable rule)
# ---------------------------------------------------------------------------


def _selector_verifications_path(project_dir: Path) -> Path:
    return project_dir / "review" / "selector_verifications.json"


def get_item_only_verifications(project_dir: Path) -> dict:
    path = _selector_verifications_path(project_dir)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def record_item_only_verification(
    project_dir: Path,
    line_item_id: str,
    fields: dict,
    reviewer: str,
    confirmations: dict[str, bool],
    notes: str = "",
) -> dict:
    """A verification scoped to exactly one line item -- never promoted to
    a reusable catalog rule. Still requires the same three confirmations
    since it can make the item automation-ready."""
    _require_confirmations(confirmations)
    _require_reviewer_identity(reviewer)
    for required in ("category", "selector", "description", "unit"):
        if not fields.get(required):
            raise RecordValidationError([f"Missing required field: {required!r}."])

    verifications = get_item_only_verifications(project_dir)
    entry = {
        "line_item_id": line_item_id,
        "category": fields["category"],
        "selector": fields["selector"],
        "description": fields["description"],
        "unit": fields["unit"],
        "activity_raw": fields.get("activity_raw"),
        "price_list": fields.get("price_list"),
        "price_list_location": fields.get("price_list_location"),
        "unit_price": fields.get("unit_price"),
        "verification_status": VERIFICATION_STATUS_HUMAN_VERIFIED,
        "verified_at": utc_now_iso(),
        "verified_by": reviewer,
        "verification_method": "xactimate_selector_browser",
        "confirmations": confirmations,
        "notes": notes,
    }
    verifications[line_item_id] = entry
    path = _selector_verifications_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(verifications, indent=2, default=str), encoding="utf-8")
    return entry


# ---------------------------------------------------------------------------
# Matching (deterministic, safety-gated -- never text-similarity alone)
# ---------------------------------------------------------------------------


def _is_compatible(row: dict, record: VerifiedCatalogRecord) -> bool:
    if record.trade and record.trade not in ("unknown",) and row.get("normalized_trade") != record.trade:
        return False
    if record.component and record.component not in ("unknown",) and row.get("normalized_component") != record.component:
        return False
    row_unit = (row.get("unit") or "").upper()
    if record.unit and row_unit and record.unit.upper() != row_unit:
        return False
    if record.supported_actions and row.get("normalized_action") not in record.supported_actions:
        return False
    desc = (row.get("original_description") or "").lower()
    if any(neg.lower() in desc for neg in record.negative_patterns if neg):
        return False
    return True


def _text_identifies(row: dict, record: VerifiedCatalogRecord) -> bool:
    desc = (row.get("original_description") or "").lower()
    if not desc:
        return False
    candidates = [record.description] + list(record.aliases)
    for c in candidates:
        if not c:
            continue
        c_lower = c.lower()
        if c_lower in desc or desc in c_lower:
            return True
    return False


def find_verified_matches(row: dict, records: list[VerifiedCatalogRecord]) -> list[VerifiedMatchCandidate]:
    """Deterministic, compatibility-gated candidate search. A record is
    only returned when trade/component/unit/action/negative-patterns are
    all compatible AND the description or an alias textually identifies
    it -- text similarity alone (without the compatibility gate) is never
    sufficient, per the build spec's matching-safety requirement."""
    matches = []
    for record in records:
        if not _is_compatible(row, record):
            continue
        if not _text_identifies(row, record):
            continue
        matches.append(VerifiedMatchCandidate(record=record, match_reason="compatible_and_text_identified"))
    return matches


def apply_verified_match(
    project_dir: Path,
    line_item_id: str,
    record: VerifiedCatalogRecord,
    reviewer: str,
    reason: str,
    *,
    allow_override_approved: bool = False,
) -> list[dict]:
    """Copies a verified record's category/selector/activity/description
    onto a line item as reviewed overrides, via the existing, tested
    review_service.edit_mapping_field() -- never bypasses it, never writes
    to mapped_estimate.json directly. Blocks (raises
    ApprovalOverrideBlockedError) rather than silently changing an already
    -approved item with a *different* category/selector, unless the caller
    explicitly passes allow_override_approved=True."""
    from estimate_extractor.ui import review_service

    rows = review_service.build_effective_rows(project_dir, line_item_ids=[line_item_id])
    if not rows:
        raise RecordNotFoundError(f"Unknown line_item_id {line_item_id!r}.")
    row = rows[0]

    if row["status"] == review_service.STATUS_APPROVED and not allow_override_approved:
        if (row["category"], row["selector"]) != (record.category, record.selector):
            raise ApprovalOverrideBlockedError(line_item_id)

    events = []
    field_map = {
        "category": record.category,
        "selector": record.selector,
        "activity": record.activity_raw,
        "mapped_description": record.description,
    }
    for field_name, value in field_map.items():
        events.append(review_service.edit_mapping_field(project_dir, line_item_id, field_name, value, reviewer, reason))
    return events


# ---------------------------------------------------------------------------
# Automation readiness
# ---------------------------------------------------------------------------


def is_automation_ready(
    row: dict,
    project_dir: Path,
    records: list[VerifiedCatalogRecord],
    group_reviewed: bool,
) -> tuple[bool, list[str]]:
    from estimate_extractor.ui import review_service

    ok, reasons = review_service.can_approve(row)
    if not ok:
        return False, reasons

    if row["status"] != review_service.STATUS_APPROVED:
        return False, ["Item is not approved."]

    backing_record = find_record(records, row["category"], row["selector"])
    backed_by_verified_catalog = backing_record is not None and backing_record.verification_status in AUTOMATION_READY_STATUSES

    item_only = get_item_only_verifications(project_dir).get(row["line_item_id"])
    backed_by_item_only = (
        item_only is not None
        and item_only.get("verification_status") == VERIFICATION_STATUS_HUMAN_VERIFIED
        and item_only.get("category") == row["category"]
        and item_only.get("selector") == row["selector"]
    )

    if not backed_by_verified_catalog and not backed_by_item_only:
        return False, ["No human_verified catalog record or item-only verification backs this category/selector pair."]

    if not group_reviewed:
        return False, ["Xactimate group for this item's section has not been reviewed (or explicitly allowed to stay custom)."]

    return True, []


# ---------------------------------------------------------------------------
# Coverage stats
# ---------------------------------------------------------------------------


def compute_coverage_stats(
    rows: list[dict],
    records: list[VerifiedCatalogRecord],
    project_dir: Path,
    group_reviewed_sections: set[str],
) -> dict:
    total_extracted = len(rows)
    normalized_items = sum(1 for r in rows if r["normalized_trade"] != "unknown" or r["normalized_component"] != "unknown")

    verified_matches_count = 0
    placeholder_only_count = 0
    no_match_count = 0
    incompatible_unit_count = 0
    awaiting_verification_count = 0
    automation_ready_count = 0

    for row in rows:
        verified_candidates = find_verified_matches(row, records)
        has_verified = any(c.record.verification_status == VERIFICATION_STATUS_HUMAN_VERIFIED for c in verified_candidates)
        has_placeholder = row["mapping_status"] in ("partially_mapped", "mapped")

        if has_verified:
            verified_matches_count += 1
        elif has_placeholder:
            placeholder_only_count += 1
        elif row["mapping_status"] == "unmapped":
            no_match_count += 1

        same_concept_diff_unit = any(
            r.trade == row["normalized_trade"] and r.component == row["normalized_component"] and r.unit.upper() != (row["unit"] or "").upper()
            for r in records
            if row["normalized_trade"] != "unknown"
        )
        if same_concept_diff_unit:
            incompatible_unit_count += 1

        if row["status"] == "unreviewed":
            awaiting_verification_count += 1

        section_name = row.get("section_name")
        group_reviewed = section_name in group_reviewed_sections
        ready, _ = is_automation_ready(row, project_dir, records, group_reviewed)
        if ready:
            automation_ready_count += 1

    by_status: dict[str, int] = {}
    by_category: dict[str, int] = {}
    by_trade: dict[str, int] = {}
    by_price_list: dict[str, int] = {}
    for r in records:
        by_status[r.verification_status] = by_status.get(r.verification_status, 0) + 1
        by_category[r.category] = by_category.get(r.category, 0) + 1
        by_trade[r.trade] = by_trade.get(r.trade, 0) + 1
        for obs in r.price_observations:
            by_price_list[obs.price_list] = by_price_list.get(obs.price_list, 0) + 1

    return {
        "total_extracted_items": total_extracted,
        "normalized_items": normalized_items,
        "items_matching_verified_selector": verified_matches_count,
        "items_matching_only_placeholder": placeholder_only_count,
        "items_no_catalog_match": no_match_count,
        "items_incompatible_units": incompatible_unit_count,
        "items_awaiting_reviewer_verification": awaiting_verification_count,
        "automation_ready_items": automation_ready_count,
        "verified_catalog_record_count": by_status.get(VERIFICATION_STATUS_HUMAN_VERIFIED, 0),
        "screenshot_transcribed_record_count": by_status.get(VERIFICATION_STATUS_SCREENSHOT_TRANSCRIBED, 0),
        "placeholder_record_count": by_status.get(VERIFICATION_STATUS_PLACEHOLDER, 0),
        "verified_records_by_category": by_category,
        "verified_records_by_trade": by_trade,
        "verified_records_by_price_list": by_price_list,
    }


def count_prior_successful_uses(catalog_record_id: str, category: str, selector: str, projects_dir: Path) -> int:
    """Scans every local project's approvals for items currently approved
    with this exact category/selector -- computed live rather than stored,
    so it can never go stale."""
    from estimate_extractor.ui import review_service

    if not projects_dir.exists():
        return 0
    count = 0
    for project_dir in projects_dir.iterdir():
        if not project_dir.is_dir():
            continue
        if not (project_dir / "mapping" / "mapped_estimate.json").exists():
            continue
        try:
            rows = review_service.build_effective_rows(project_dir)
        except Exception:
            continue
        for row in rows:
            if row["status"] == review_service.STATUS_APPROVED and row["category"] == category and row["selector"] == selector:
                count += 1
    return count


# ---------------------------------------------------------------------------
# Rule-save preview (new/changed matches, approved-item conflicts)
# ---------------------------------------------------------------------------


def preview_record_effect(fields: dict, rows: list[dict]) -> dict:
    """Simulates adding `fields` as a catalog record and reports, against
    the currently loaded rows: which would newly match, which currently-
    approved items have a *different* category/selector (a possible
    conflict this record's addition does not resolve automatically), and
    which already match with the exact same category/selector (no-ops)."""
    from estimate_extractor.ui import review_service

    trial_record = VerifiedCatalogRecord(
        catalog_record_id="preview",
        category=fields.get("category", ""),
        selector=fields.get("selector", ""),
        description=fields.get("description", ""),
        unit=fields.get("unit", ""),
        aliases=list(fields.get("aliases") or []),
        trade=fields.get("trade", "unknown"),
        component=fields.get("component", "unknown"),
        supported_actions=list(fields.get("supported_actions") or []),
        negative_patterns=list(fields.get("negative_patterns") or []),
        verification_status=VERIFICATION_STATUS_HUMAN_VERIFIED,
    )

    new_matches = []
    changed_matches = []
    already_approved_conflicts = []

    for row in rows:
        candidates = find_verified_matches(row, [trial_record])
        if not candidates:
            continue
        if row["status"] == review_service.STATUS_APPROVED:
            if (row["category"], row["selector"]) != (trial_record.category, trial_record.selector):
                already_approved_conflicts.append(row["line_item_id"])
            continue
        if row["category"] == trial_record.category and row["selector"] == trial_record.selector:
            continue  # already matches identically -- not a new or changed match
        if row["category"] or row["selector"]:
            changed_matches.append(row["line_item_id"])
        else:
            new_matches.append(row["line_item_id"])

    return {
        "new_matches": new_matches,
        "changed_matches": changed_matches,
        "already_approved_conflicts": already_approved_conflicts,
    }
