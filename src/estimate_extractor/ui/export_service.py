"""Builds approved_estimate.json, automation_input.json, and
approved_line_items.csv from a project's review state.

Only line items with review status "approved" AND a populated
category+selector (re-checked here, not just trusted from the stored
status, in case review_state.json was hand-edited) are ever written into
automation_input.json / approved_line_items.csv. Everything else is
reported in "excluded_items" with a reason -- export never silently drops
an item without saying why, and never raises just because zero items are
approved yet (see write_automation_input()).
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path

from estimate_extractor.ui.review_service import STATUS_APPROVED, build_effective_rows, can_approve

APPROVED_CSV_COLUMNS = [
    "line_item_id",
    "coverage_id",
    "area_name",
    "section_name",
    "original_description",
    "category",
    "selector",
    "activity",
    "quantity",
    "unit",
    "mapped_description",
    "reviewer_note",
    "source_page",
]


class ExportServiceError(Exception):
    pass


def _read_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _claim_project_meta(project_dir: Path) -> dict:
    canonical_path = project_dir / "extraction" / "canonical_estimate.json"
    canonical = _read_json(canonical_path, {})
    claim = canonical.get("claim", {}) or {}

    def val(key):
        f = claim.get(key) or {}
        return f.get("value")

    address = claim.get("property_address") or {}
    return {
        "claim_number": val("claim_number"),
        "insured": val("insured_name"),
        "property_address": address.get("line1"),
    }


def build_approved_estimate(project_dir: Path) -> dict:
    """Every line item, with both the machine mapping suggestion and the
    final reviewed values preserved side by side -- see the build spec's
    approved_estimate.json example."""
    rows = build_effective_rows(project_dir)
    meta = _claim_project_meta(project_dir)
    items = []
    for row in rows:
        items.append(
            {
                "line_item_id": row["line_item_id"],
                "original": {
                    "description": row["original_description"],
                    "quantity": row["original_quantity"],
                    "unit": row["original_unit"],
                },
                "normalized": {
                    "action": row["machine_action"],
                    "trade": row["machine_trade"],
                    "component": row["machine_component"],
                    "material": row["machine_material"],
                },
                "machine_mapping": {
                    "category": row["machine_category"],
                    "selector": row["machine_selector"],
                    "activity": row["machine_activity"],
                    "description": row["machine_mapped_description"],
                    "confidence": row["confidence"],
                },
                "reviewed_mapping": {
                    "category": row["category"],
                    "selector": row["selector"],
                    "activity": row["activity"],
                    "description": row["mapped_description"],
                    "approved": row["approved"],
                    "status": row["status"],
                    "reviewer_note": row["reviewer_note"],
                },
                "coverage_id": row["coverage_id"],
                "area_name": row["area_name"],
                "section_name": row["section_name"],
                "source_pages": row["source_pages"],
            }
        )
    return {
        "schema_version": "1.0.0",
        "project": meta,
        "items": items,
        "review_history_ref": "review/review_history.json",
    }


def write_approved_estimate(project_dir: Path) -> Path:
    data = build_approved_estimate(project_dir)
    path = project_dir / "review" / "approved_estimate.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    return path


def build_automation_input(project_dir: Path) -> tuple[dict, list[dict]]:
    """Returns (automation_input_dict, excluded_items). Excludes anything
    not approved, and re-validates approved items against can_approve()
    (defense in depth) so a stale/hand-edited status can never smuggle an
    unqualified item into the export."""
    rows = build_effective_rows(project_dir)
    meta = _claim_project_meta(project_dir)

    sections: dict[str, dict] = {}
    excluded: list[dict] = []
    for row in rows:
        if row["status"] != STATUS_APPROVED:
            if row["status"] not in (None, "unreviewed"):
                excluded.append({"line_item_id": row["line_item_id"], "reason": f"Item status is {row['status']!r}, not approved."})
            continue
        ok, reasons = can_approve(row)
        if not ok:
            excluded.append({"line_item_id": row["line_item_id"], "reason": "; ".join(reasons)})
            continue
        section_key = row["section_name"] or "Unassigned"
        section = sections.setdefault(section_key, {"name": section_key, "coverage_id": row["coverage_id"], "items": []})
        section["items"].append(
            {
                "line_item_id": row["line_item_id"],
                "category": row["category"],
                "selector": row["selector"],
                "activity": row["activity"],
                "quantity": row["quantity"],
                "unit": row["unit"],
                "description": row["mapped_description"] or row["original_description"],
                "source_page": row["source_page"],
            }
        )

    data = {
        "schema_version": "1.0.0",
        "project": meta,
        "sections": list(sections.values()),
        "excluded_items": excluded,
    }
    return data, excluded


@dataclass(slots=True)
class AutomationExportResult:
    exported_count: int
    excluded_count: int
    automation_input_path: Path
    approved_csv_path: Path


def write_approved_line_items_csv(project_dir: Path, automation_data: dict) -> Path:
    rows_by_id = {row["line_item_id"]: row for row in build_effective_rows(project_dir)}
    path = project_dir / "exports" / "approved_line_items.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(APPROVED_CSV_COLUMNS)
        for section in automation_data["sections"]:
            for item in section["items"]:
                row = rows_by_id.get(item["line_item_id"], {})
                writer.writerow(
                    [
                        item["line_item_id"],
                        row.get("coverage_id"),
                        row.get("area_name"),
                        row.get("section_name"),
                        row.get("original_description"),
                        item["category"],
                        item["selector"],
                        item["activity"],
                        item["quantity"],
                        item["unit"],
                        item["description"],
                        row.get("reviewer_note"),
                        item["source_page"],
                    ]
                )
    return path


def write_automation_input(project_dir: Path) -> AutomationExportResult:
    """Fails safely: if there are zero approved-and-qualified items, this
    still writes a valid automation_input.json with an empty sections list
    (never raises) -- the UI is responsible for showing an 'Export
    blocked' / nothing-to-export state, not this function."""
    data, excluded = build_automation_input(project_dir)
    exported_count = sum(len(s["items"]) for s in data["sections"])

    automation_path = project_dir / "exports" / "automation_input.json"
    automation_path.parent.mkdir(parents=True, exist_ok=True)
    automation_path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")

    csv_path = write_approved_line_items_csv(project_dir, data)

    return AutomationExportResult(
        exported_count=exported_count,
        excluded_count=len(excluded),
        automation_input_path=automation_path,
        approved_csv_path=csv_path,
    )
