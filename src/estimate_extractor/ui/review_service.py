"""Human-review state: approvals, rejections, field overrides, and the
append-only review-history log. Everything here reads/writes plain JSON
files under a project's review/ directory and never touches the machine
outputs written by pipeline_service.py (normalized_estimate.json /
mapped_estimate.json / canonical_estimate.json stay byte-for-byte what the
extractor and mapper produced).

Key invariant: a "reviewed" value is always stored as an override layered
on top of the untouched machine value, never as an in-place edit. See
build_effective_rows() for how the two are merged for display, and
export_service.py for how machine vs. reviewed values both surface in
approved_estimate.json.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

STATUS_UNREVIEWED = "unreviewed"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_NEEDS_MORE_INFO = "needs_more_information"
VALID_STATUSES = frozenset({STATUS_UNREVIEWED, STATUS_APPROVED, STATUS_REJECTED, STATUS_NEEDS_MORE_INFO})

# The reviewer may edit these normalized/mapping fields. Everything else on
# a line item (line_item_id, original description, original quantity,
# original unit, source page) is permanently immutable in the mapping
# review screen -- editing them is not offered by any function in this
# module.
EDITABLE_MAPPING_FIELDS = frozenset(
    {"action", "trade", "component", "material", "category", "selector", "activity", "mapped_description"}
)

# In the extraction review screen, only attribution fields (which the
# hardening-phase coverage-attribution work already documents as
# sometimes genuinely ambiguous) may be corrected by a human -- the raw
# description/quantity/unit/source page a carrier printed are never
# editable anywhere in this UI.
EDITABLE_EXTRACTION_FIELDS = frozenset({"coverage_id", "area_name", "section_name"})


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ReviewServiceError(Exception):
    pass


class ApprovalBlockedError(ReviewServiceError):
    def __init__(self, line_item_id: str, reasons: list[str]):
        super().__init__(f"Cannot approve {line_item_id}: {'; '.join(reasons)}")
        self.line_item_id = line_item_id
        self.reasons = reasons


def _read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ReviewServiceError(f"Malformed JSON in {path}: {exc}") from exc


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


def _review_dir(project_dir: Path) -> Path:
    return project_dir / "review"


def _state_path(project_dir: Path) -> Path:
    return _review_dir(project_dir) / "review_state.json"


def _history_path(project_dir: Path) -> Path:
    return _review_dir(project_dir) / "review_history.json"


def _extraction_overrides_path(project_dir: Path) -> Path:
    return _review_dir(project_dir) / "extraction_overrides.json"


def _new_item_state() -> dict:
    return {
        "status": STATUS_UNREVIEWED,
        "overrides": {},
        "reviewer_note": "",
        "activity_required_waived": False,
        "updated_at": None,
        "reviewer": None,
    }


def _load_state(project_dir: Path) -> dict:
    return _read_json(_state_path(project_dir), {})


def _save_state(project_dir: Path, state: dict) -> None:
    _write_json(_state_path(project_dir), state)


def _append_history(
    project_dir: Path,
    line_item_id: str | None,
    action: str,
    field_changes: dict,
    note: str,
    reviewer: str,
) -> dict:
    history = _read_json(_history_path(project_dir), [])
    event = {
        "event_id": f"review_event_{len(history) + 1:03d}",
        "timestamp": utc_now_iso(),
        "line_item_id": line_item_id,
        "action": action,
        "field_changes": field_changes,
        "note": note or "",
        "reviewer": reviewer,
    }
    history.append(event)
    _write_json(_history_path(project_dir), history)
    return event


def get_review_history(project_dir: Path) -> list[dict]:
    return _read_json(_history_path(project_dir), [])


# ---------------------------------------------------------------------------
# Machine-value lookups (read-only access into normalized/mapped outputs)
# ---------------------------------------------------------------------------


def _index_normalized(project_dir: Path) -> dict:
    items = _read_json(project_dir / "mapping" / "normalized_estimate.json", [])
    return {i["line_item_id"]: i for i in items}


def _index_mapped(project_dir: Path) -> dict:
    items = _read_json(project_dir / "mapping" / "mapped_estimate.json", [])
    return {i["line_item_id"]: i for i in items}


def _machine_value_for_mapping_field(norm_item: dict, mapped_item: dict, field: str):
    normalized_block = norm_item["normalized"]
    best = (mapped_item["mapping"].get("best_match") or {}) if mapped_item else {}
    if field in ("action", "trade", "component", "material"):
        return normalized_block.get(field)
    if field == "mapped_description":
        return best.get("description")
    return best.get(field)  # category / selector / activity


def _machine_value_for_extraction_field(norm_item: dict, field: str):
    original = norm_item["original"]
    if field == "coverage_id":
        return original.get("coverage_id")
    if field == "area_name":
        return original.get("area_name")
    if field == "section_name":
        return original.get("section_name")
    raise ReviewServiceError(f"Unknown extraction field {field!r}.")


# ---------------------------------------------------------------------------
# Mapping-field overrides (action/trade/component/material/category/
# selector/activity/mapped_description) and approval status
# ---------------------------------------------------------------------------


def edit_mapping_field(
    project_dir: Path,
    line_item_id: str,
    field: str,
    new_value,
    reviewer: str,
    reason: str,
) -> dict:
    if field not in EDITABLE_MAPPING_FIELDS:
        raise ReviewServiceError(f"Field {field!r} is not editable on the mapping review screen.")
    if not reason or not reason.strip():
        raise ReviewServiceError("A reason is required to edit a mapping field.")

    normalized_by_id = _index_normalized(project_dir)
    mapped_by_id = _index_mapped(project_dir)
    norm_item = normalized_by_id.get(line_item_id)
    if norm_item is None:
        raise ReviewServiceError(f"Unknown line_item_id {line_item_id!r} (not present in normalized_estimate.json).")
    machine_value = _machine_value_for_mapping_field(norm_item, mapped_by_id.get(line_item_id), field)

    state = _load_state(project_dir)
    item_state = state.setdefault(line_item_id, _new_item_state())
    before = item_state["overrides"].get(field, {}).get("reviewed_value", machine_value)
    item_state["overrides"][field] = {
        "original_machine_value": machine_value,
        "reviewed_value": new_value,
        "reviewed_at": utc_now_iso(),
        "review_reason": reason,
    }
    item_state["updated_at"] = utc_now_iso()
    item_state["reviewer"] = reviewer
    _save_state(project_dir, state)

    return _append_history(
        project_dir, line_item_id, "edit_field", {field: {"before": before, "after": new_value}}, reason, reviewer
    )


def override_extraction_field(
    project_dir: Path,
    line_item_id: str,
    field: str,
    new_value,
    reviewer: str,
    reason: str,
) -> dict:
    if field not in EDITABLE_EXTRACTION_FIELDS:
        raise ReviewServiceError(
            f"Field {field!r} is not an editable extraction field "
            f"(only {sorted(EDITABLE_EXTRACTION_FIELDS)} may be overridden -- the extractor's raw facts are immutable)."
        )
    if not reason or not reason.strip():
        raise ReviewServiceError("A reason is required to override an extracted value.")

    normalized_by_id = _index_normalized(project_dir)
    norm_item = normalized_by_id.get(line_item_id)
    if norm_item is None:
        raise ReviewServiceError(f"Unknown line_item_id {line_item_id!r}.")
    machine_value = _machine_value_for_extraction_field(norm_item, field)

    overrides = _read_json(_extraction_overrides_path(project_dir), {})
    item_overrides = overrides.setdefault(line_item_id, {})
    before = item_overrides.get(field, {}).get("reviewed_value", machine_value)
    item_overrides[field] = {
        "original_machine_value": machine_value,
        "reviewed_value": new_value,
        "reviewed_at": utc_now_iso(),
        "review_reason": reason,
    }
    _write_json(_extraction_overrides_path(project_dir), overrides)

    return _append_history(
        project_dir,
        line_item_id,
        "extraction_override",
        {field: {"before": before, "after": new_value}},
        reason,
        reviewer,
    )


def can_approve(effective_row: dict) -> tuple[bool, list[str]]:
    """Approval rules (spec-mandated, not configurable): category,
    selector, quantity, and unit must be present; activity must be present
    or explicitly waived; no fatal mapping error. A null coverage_id never
    blocks approval -- it stays visible but is not a blocker."""
    reasons: list[str] = []
    if not effective_row.get("category"):
        reasons.append("Missing category.")
    if not effective_row.get("selector"):
        reasons.append("Missing selector.")
    if not effective_row.get("activity") and not effective_row.get("activity_required_waived"):
        reasons.append("Missing activity (or explicitly mark activity as not required).")
    if effective_row.get("quantity") in (None, ""):
        reasons.append("Missing quantity.")
    if not effective_row.get("unit"):
        reasons.append("Missing unit.")
    if effective_row.get("mapping_status") == "error":
        reasons.append("Unresolved fatal mapping error.")
    return (len(reasons) == 0, reasons)


def set_status(project_dir: Path, line_item_id: str, status: str, reviewer: str, note: str | None = None) -> dict:
    if status not in VALID_STATUSES:
        raise ReviewServiceError(f"Invalid status {status!r}; must be one of {sorted(VALID_STATUSES)}.")

    if status == STATUS_APPROVED:
        rows = build_effective_rows(project_dir, line_item_ids=[line_item_id])
        if not rows:
            raise ReviewServiceError(f"Unknown line_item_id {line_item_id!r}.")
        ok, reasons = can_approve(rows[0])
        if not ok:
            raise ApprovalBlockedError(line_item_id, reasons)

    state = _load_state(project_dir)
    item_state = state.setdefault(line_item_id, _new_item_state())
    before_status = item_state.get("status", STATUS_UNREVIEWED)
    item_state["status"] = status
    if note is not None:
        item_state["reviewer_note"] = note
    item_state["updated_at"] = utc_now_iso()
    item_state["reviewer"] = reviewer
    _save_state(project_dir, state)

    action_name = {
        STATUS_APPROVED: "approve_mapping",
        STATUS_REJECTED: "reject_mapping",
        STATUS_NEEDS_MORE_INFO: "mark_needs_more_information",
        STATUS_UNREVIEWED: "reset_status",
    }[status]
    return _append_history(
        project_dir, line_item_id, action_name, {"status": {"before": before_status, "after": status}}, note or "", reviewer
    )


def approve_item(project_dir: Path, line_item_id: str, reviewer: str, note: str | None = None) -> dict:
    return set_status(project_dir, line_item_id, STATUS_APPROVED, reviewer, note)


def reject_item(project_dir: Path, line_item_id: str, reviewer: str, note: str | None = None) -> dict:
    return set_status(project_dir, line_item_id, STATUS_REJECTED, reviewer, note)


def mark_needs_more_info(project_dir: Path, line_item_id: str, reviewer: str, note: str | None = None) -> dict:
    return set_status(project_dir, line_item_id, STATUS_NEEDS_MORE_INFO, reviewer, note)


def set_reviewer_note(project_dir: Path, line_item_id: str, note: str, reviewer: str) -> dict:
    state = _load_state(project_dir)
    item_state = state.setdefault(line_item_id, _new_item_state())
    before = item_state.get("reviewer_note", "")
    item_state["reviewer_note"] = note
    item_state["updated_at"] = utc_now_iso()
    item_state["reviewer"] = reviewer
    _save_state(project_dir, state)
    return _append_history(
        project_dir, line_item_id, "set_reviewer_note", {"reviewer_note": {"before": before, "after": note}}, note, reviewer
    )


def waive_activity_requirement(project_dir: Path, line_item_id: str, reviewer: str, reason: str) -> dict:
    if not reason or not reason.strip():
        raise ReviewServiceError("A reason is required to waive the activity requirement.")
    state = _load_state(project_dir)
    item_state = state.setdefault(line_item_id, _new_item_state())
    item_state["activity_required_waived"] = True
    item_state["updated_at"] = utc_now_iso()
    item_state["reviewer"] = reviewer
    _save_state(project_dir, state)
    return _append_history(
        project_dir, line_item_id, "waive_activity_requirement", {"activity_required_waived": {"before": False, "after": True}}, reason, reviewer
    )


# ---------------------------------------------------------------------------
# Bulk actions
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class BulkResult:
    applied: list[str]
    blocked: dict[str, list[str]]


def bulk_set_status(project_dir: Path, line_item_ids: list[str], status: str, reviewer: str, note: str | None = None) -> BulkResult:
    applied: list[str] = []
    blocked: dict[str, list[str]] = {}
    for lid in line_item_ids:
        try:
            set_status(project_dir, lid, status, reviewer, note)
            applied.append(lid)
        except ApprovalBlockedError as exc:
            blocked[lid] = exc.reasons
    return BulkResult(applied=applied, blocked=blocked)


def bulk_assign_field(
    project_dir: Path, line_item_ids: list[str], field: str, value, reviewer: str, reason: str
) -> BulkResult:
    applied: list[str] = []
    blocked: dict[str, list[str]] = {}
    for lid in line_item_ids:
        try:
            edit_mapping_field(project_dir, lid, field, value, reviewer, reason)
            applied.append(lid)
        except ReviewServiceError as exc:
            blocked[lid] = [str(exc)]
    return BulkResult(applied=applied, blocked=blocked)


# ---------------------------------------------------------------------------
# Effective (machine + override merged) rows for the mapping review table
# ---------------------------------------------------------------------------


def _apply_override(overrides: dict, field: str, machine_value):
    ov = overrides.get(field)
    return ov["reviewed_value"] if ov else machine_value


def build_effective_rows(project_dir: Path, line_item_ids: list[str] | None = None) -> list[dict]:
    normalized_by_id = _index_normalized(project_dir)
    mapped_by_id = _index_mapped(project_dir)
    extraction_overrides = _read_json(_extraction_overrides_path(project_dir), {})
    state = _load_state(project_dir)

    ids = line_item_ids if line_item_ids is not None else list(normalized_by_id.keys())
    rows: list[dict] = []
    for lid in ids:
        norm = normalized_by_id.get(lid)
        mapped = mapped_by_id.get(lid)
        if norm is None or mapped is None:
            continue

        original = norm["original"]
        normalized_block = norm["normalized"]
        mapping_block = mapped["mapping"]
        best = mapping_block.get("best_match") or {}

        item_state = state.get(lid, {})
        overrides = item_state.get("overrides", {})
        eo = extraction_overrides.get(lid, {})

        row = {
            "line_item_id": lid,
            "original_description": original.get("description"),
            "original_quantity": original.get("quantity"),
            "original_unit": original.get("unit_of_measure"),
            "coverage_id": _apply_override(eo, "coverage_id", original.get("coverage_id")),
            "area_name": _apply_override(eo, "area_name", original.get("area_name")),
            "section_name": _apply_override(eo, "section_name", original.get("section_name")),
            "quantity": original.get("quantity"),
            "unit": original.get("unit_of_measure"),
            "normalized_action": _apply_override(overrides, "action", normalized_block.get("action")),
            "normalized_trade": _apply_override(overrides, "trade", normalized_block.get("trade")),
            "normalized_component": _apply_override(overrides, "component", normalized_block.get("component")),
            "normalized_material": _apply_override(overrides, "material", normalized_block.get("material")),
            "machine_action": normalized_block.get("action"),
            "machine_trade": normalized_block.get("trade"),
            "machine_component": normalized_block.get("component"),
            "machine_material": normalized_block.get("material"),
            "mapping_status": mapping_block.get("status"),
            "category": _apply_override(overrides, "category", best.get("category")),
            "selector": _apply_override(overrides, "selector", best.get("selector")),
            "activity": _apply_override(overrides, "activity", best.get("activity")),
            "mapped_description": _apply_override(overrides, "mapped_description", best.get("description")),
            "machine_category": best.get("category"),
            "machine_selector": best.get("selector"),
            "machine_activity": best.get("activity"),
            "machine_mapped_description": best.get("description"),
            "confidence": best.get("confidence"),
            "needs_review": mapping_block.get("needs_review"),
            "review_reasons": mapping_block.get("review_reasons", []),
            "status": item_state.get("status", STATUS_UNREVIEWED),
            "approved": item_state.get("status") == STATUS_APPROVED,
            "rejected": item_state.get("status") == STATUS_REJECTED,
            "reviewer_note": item_state.get("reviewer_note", ""),
            "activity_required_waived": item_state.get("activity_required_waived", False),
            "source_page": (original.get("source_pages") or [None])[0],
            "source_pages": original.get("source_pages", []),
            "extraction_confidence": original.get("extraction_confidence"),
            "extraction_needs_review": original.get("extraction_needs_review"),
        }
        ok, reasons = can_approve(row)
        row["can_approve"] = ok
        row["approval_block_reasons"] = reasons
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# Project-summary (used by the Projects screen)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ProjectSummary:
    slug: str
    source_filename: str
    carrier: str | None
    claim_number: str | None
    insured_name: str | None
    last_processed_at: str | None
    extraction_status: str | None
    mapping_status: str | None
    total_line_items: int
    approved_count: int
    unresolved_count: int


def get_project_summary(project_dir: Path, record) -> ProjectSummary:
    canonical_path = project_dir / "extraction" / "canonical_estimate.json"
    mapping_report_path = project_dir / "mapping" / "mapping_report.json"

    carrier = claim_number = insured_name = extraction_status = None
    total_line_items = 0
    if canonical_path.exists():
        canonical = _read_json(canonical_path, {})
        document = canonical.get("document") or {}
        carrier = document.get("carrier_detected")
        extraction_status = document.get("extraction_status")
        claim = canonical.get("claim") or {}
        claim_number = (claim.get("claim_number") or {}).get("value")
        insured_name = (claim.get("insured_name") or {}).get("value")
        total_line_items = len(canonical.get("line_items", []))

    mapping_status = None
    unresolved_count = 0
    if mapping_report_path.exists():
        report = _read_json(mapping_report_path, {})
        mapping_status = report.get("status")
        summary = report.get("summary", {})
        unresolved_count = summary.get("needs_review", 0) + summary.get("unmapped", 0)

    state = _load_state(project_dir)
    approved_count = sum(1 for v in state.values() if v.get("status") == STATUS_APPROVED)

    return ProjectSummary(
        slug=record.slug,
        source_filename=record.source_filename,
        carrier=carrier,
        claim_number=claim_number,
        insured_name=insured_name,
        last_processed_at=record.last_processed_at,
        extraction_status=extraction_status,
        mapping_status=mapping_status,
        total_line_items=total_line_items,
        approved_count=approved_count,
        unresolved_count=unresolved_count,
    )
