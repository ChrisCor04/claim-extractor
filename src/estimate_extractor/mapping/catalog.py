"""Loads config/mapping_catalog.yaml into typed, validated catalog entries.

The catalog is intentionally separate from code (per the build spec) so new
mappings can be added without a code change. See docs/mapping-engine.md
"How to add mappings".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass(frozen=True, slots=True)
class XactimateInfo:
    category: str | None
    selector: str | None
    activity: str | None
    description: str | None


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    mapping_id: str
    canonical_terms: tuple[str, ...]
    trade: str
    component: str
    allowed_actions: tuple[str, ...]
    allowed_units: tuple[str, ...]
    xactimate: XactimateInfo
    confidence_base: float
    requires_review: bool
    notes: tuple[str, ...] = field(default_factory=tuple)
    # Optional: attribute values this entry requires (e.g. {"felt_included":
    # false} for a "without felt" shingle entry). A normalized item whose
    # extracted attribute value disagrees is an attribute conflict -- see
    # matcher.py. Empty by default (most entries don't constrain attributes).
    required_attributes: dict = field(default_factory=dict)


class DuplicateMappingIdError(ValueError):
    pass


def load_catalog(path: Path) -> list[CatalogEntry]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    entries: list[CatalogEntry] = []
    seen_ids: set[str] = set()
    for item in raw:
        mapping_id = item["mapping_id"]
        if mapping_id in seen_ids:
            raise DuplicateMappingIdError(f"Duplicate mapping_id in catalog: {mapping_id!r}")
        seen_ids.add(mapping_id)
        xactimate_raw = item.get("xactimate", {}) or {}
        entries.append(
            CatalogEntry(
                mapping_id=mapping_id,
                canonical_terms=tuple(t.lower() for t in item.get("canonical_terms", [])),
                trade=item.get("trade", "unknown"),
                component=item.get("component", "unknown"),
                allowed_actions=tuple(item.get("allowed_actions", [])),
                allowed_units=tuple(u.upper() for u in item.get("allowed_units", [])),
                xactimate=XactimateInfo(
                    category=xactimate_raw.get("category"),
                    selector=xactimate_raw.get("selector"),
                    activity=xactimate_raw.get("activity"),
                    description=xactimate_raw.get("description"),
                ),
                confidence_base=float(item.get("confidence_base", 0.5)),
                requires_review=bool(item.get("requires_review", True)),
                notes=tuple(item.get("notes", [])),
                required_attributes=dict(item.get("required_attributes", {}) or {}),
            )
        )
    return entries
