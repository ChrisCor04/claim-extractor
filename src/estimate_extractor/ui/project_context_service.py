"""Reusable data model for Xactimate project-level context (profile,
project type, price list, tax jurisdiction, policy type, ...), as
observed in the Xactimate "New Project" / "Claim Info" screens described
in the Phase 3.5 build spec.

This module only defines and persists reviewer-entered/confirmed values.
It does not open, drive, or automate Xactimate in any way -- see
docs/verified-catalog-builder.md "Project-context profiles". A price list
extracted from the source estimate may be offered as a *suggestion*, but
the field is never treated as confirmed until a reviewer explicitly
confirms it (the build spec: "the reviewer must confirm it before
automation readiness").
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

CONTEXT_FIELDS = (
    "profile",
    "project_type",
    "project_name",
    "price_list",
    "tax_jurisdiction",
    "timezone_label",
    "policy_type",
    "deductible_application",
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _context_path(project_dir: Path) -> Path:
    return project_dir / "review" / "xactimate_project_context.json"


def get_project_context(project_dir: Path) -> dict:
    path = _context_path(project_dir)
    if not path.exists():
        return {field: None for field in CONTEXT_FIELDS} | {"confirmed": False, "confirmed_at": None, "confirmed_by": None}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {field: None for field in CONTEXT_FIELDS} | {"confirmed": False, "confirmed_at": None, "confirmed_by": None}


def suggest_price_list_from_canonical(canonical: dict) -> str | None:
    """The only field this module will ever pre-fill from extracted
    evidence -- everything else must be reviewer-entered. Never marks the
    result as confirmed."""
    claim = (canonical or {}).get("claim") or {}
    price_list_field = claim.get("price_list") or {}
    value = price_list_field.get("value")
    return value or None


def set_project_context(project_dir: Path, fields: dict, reviewer: str, *, confirmed: bool = False) -> dict:
    current = get_project_context(project_dir)
    for key in CONTEXT_FIELDS:
        if key in fields:
            current[key] = fields[key]
    current["confirmed"] = confirmed
    current["confirmed_at"] = utc_now_iso() if confirmed else None
    current["confirmed_by"] = reviewer if confirmed else None

    path = _context_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(current, indent=2, default=str), encoding="utf-8")
    return current
