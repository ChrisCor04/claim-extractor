"""Offline Phase 3 shadow Quick Entry plan generation."""

from __future__ import annotations

import json
from collections import OrderedDict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .offline_catalog_mapper import OfflineCatalogMapper, SourceLineContext


@dataclass(slots=True)
class ShadowPlanItem:
    line_item_id: str
    source_order: int
    group: str
    section: str
    original_description: str
    catalog_search_text: str
    quantity: float | None
    unit: str | None
    source_pricing: dict[str, Any]
    source_action: str | None
    trade_hint: str | None
    resolution: str
    category: str | None
    selector: str | None
    catalog_description: str | None
    execution_category: str
    execution_selector: str
    execution_description: str
    score: float
    margin: float
    reason: str
    execution_state: str
    top_candidates: list[dict[str, Any]]


def _pricing(line: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "unit_price", "tax", "overhead_and_profit", "replacement_cost_value",
        "depreciation_amount", "actual_cash_value",
    )
    return {key: line.get(key) for key in keys if line.get(key) is not None}


def _action_from_line(line: dict[str, Any], normalized: dict[str, Any]) -> str | None:
    action = normalized.get("action")
    if action and action != "unknown":
        return str(action)
    flags = line.get("flags") or {}
    for flag, value in (
        ("detach_reset", flags.get("is_detach_and_reset")),
        ("remove_replace", flags.get("is_remove_and_replace")),
        ("remove", flags.get("is_remove_only")),
        ("replace", flags.get("is_replace_only")),
    ):
        if value:
            return flag
    return None


def build_shadow_plan(project_dir: Path, mapper: OfflineCatalogMapper | None = None) -> dict[str, Any]:
    mapper = mapper or OfflineCatalogMapper()
    canonical_path = project_dir / "extraction" / "canonical_estimate.json"
    mapped_path = project_dir / "mapping" / "mapped_estimate.json"
    canonical = json.loads(canonical_path.read_text(encoding="utf-8"))
    mapped_rows = json.loads(mapped_path.read_text(encoding="utf-8")) if mapped_path.exists() else []
    normalized_by_id = {row["line_item_id"]: row.get("normalization", {}) for row in mapped_rows}
    section_names = {row["section_id"]: row["name"] for row in canonical.get("sections", [])}

    items: list[ShadowPlanItem] = []
    grouped: OrderedDict[str, list[str]] = OrderedDict()
    for source_order, line in enumerate(canonical["line_items"], start=1):
        normalized = normalized_by_id.get(line["line_item_id"], {})
        section = section_names.get(line.get("section_id"), line.get("category_heading") or "Ungrouped")
        action = _action_from_line(line, normalized)
        context = SourceLineContext(
            description=line["description"], section=section, group=section,
            quantity=line.get("quantity"), unit=line.get("unit_of_measure"),
            activity=action, trade_hint=normalized.get("trade"), pricing=_pricing(line),
        )
        result = mapper.map_line(context)
        if result.resolution == "resolved":
            execution_state = "fast_normal_item_ready"
            execution_category = result.category or ""
            execution_selector = result.selector or ""
            execution_description = result.catalog_description or line["description"]
        elif result.resolution == "bid_item_fallback":
            execution_state = "fast_bid_item_unsupported_tab_order_unknown"
            execution_category, execution_selector = "DOR", "BIDITM"
            execution_description = line["description"]
        else:
            execution_state = "review_required_with_bid_item_fallback"
            execution_category, execution_selector = "DOR", "BIDITM"
            execution_description = line["description"]
        candidates = [candidate.to_dict() for candidate in result.candidates] if result.resolution != "resolved" else []
        item = ShadowPlanItem(
            line_item_id=line["line_item_id"], source_order=source_order,
            group=section, section=section, original_description=line["description"],
            catalog_search_text=result.catalog_search_text,
            quantity=line.get("quantity"), unit=line.get("unit_of_measure"),
            source_pricing=_pricing(line), source_action=result.source_activity, trade_hint=normalized.get("trade"),
            resolution=result.resolution, category=result.category, selector=result.selector,
            catalog_description=result.catalog_description, score=result.final_score,
            execution_category=execution_category, execution_selector=execution_selector,
            execution_description=execution_description,
            margin=result.margin, reason=result.reason, execution_state=execution_state,
            top_candidates=candidates,
        )
        if result.resolution == "resolved":
            assert (item.category, item.selector) in mapper.catalog.by_identity
        if result.resolution == "bid_item_fallback":
            assert (item.category, item.selector) == ("DOR", "BIDITM")
        items.append(item)
        grouped.setdefault(section, []).append(line["line_item_id"])

    counts = {state: sum(item.resolution == state for item in items) for state in ("resolved", "ambiguous", "bid_item_fallback")}
    execution_fallbacks = sum(item.execution_category == "DOR" and item.execution_selector == "BIDITM" for item in items)
    return {
        "schema_version": "phase3-shadow-quick-entry-plan-v2",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "project": project_dir.name,
        "source_estimate": str(canonical_path.relative_to(project_dir)),
        "catalog": str(mapper.catalog.source_path),
        "catalog_row_count": len(mapper.catalog.records),
        "execution_mode": "offline_shadow_only",
        "bid_item_fast_execution": "unsupported_until_description_quantity_price_tab_order_is_verified",
        "summary": {"total_items": len(items), **counts, "execution_bid_item_fallback": execution_fallbacks},
        "group_first_future_layout": [
            {"group": group, "line_item_ids": line_ids} for group, line_ids in grouped.items()
        ],
        "items": [asdict(item) for item in items],
    }


def render_shadow_report(plan: dict[str, Any]) -> str:
    summary = plan["summary"]
    lines = [
        f"# Phase 3 shadow Quick Entry plan: {plan['project']}", "",
        f"- Total lines: {summary['total_items']}",
        f"- Resolved: {summary['resolved']}",
        f"- Ambiguous/review: {summary['ambiguous']}",
        f"- DOR/BIDITM fallback: {summary['bid_item_fallback']}",
        f"- Executable DOR/BIDITM fallback (includes unresolved review lines): {summary['execution_bid_item_fallback']}",
        "- Live execution: disabled", "",
        "| # | Group | Source description | Qty | Unit | Resolution | CAT/SEL | Score | Margin | Execution |",
        "|---:|---|---|---:|---|---|---|---:|---:|---|",
    ]
    for item in plan["items"]:
        identity = f"{item['category'] or '—'}/{item['selector'] or '—'}"
        description = item["original_description"].replace("|", "\\|")
        lines.append(
            f"| {item['source_order']} | {item['group']} | {description} | "
            f"{item['quantity'] if item['quantity'] is not None else '—'} | {item['unit'] or '—'} | "
            f"{item['resolution']} | {identity} | {item['score']:.4f} | {item['margin']:.4f} | "
            f"{item['execution_state']} |"
        )
        if item["top_candidates"]:
            choices = ", ".join(
                f"{candidate['category']}/{candidate['selector']} ({candidate['final_score']:.3f})"
                for candidate in item["top_candidates"][:5]
            )
            lines.append(f"|  |  | ↳ candidates: {choices} |  |  |  |  |  |  |  |")
    return "\n".join(lines) + "\n"


def write_shadow_plan(project_dir: Path, output_dir: Path, mapper: OfflineCatalogMapper | None = None) -> dict[str, Any]:
    plan = build_shadow_plan(project_dir, mapper)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "shadow_quick_entry_plan.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")
    (output_dir / "shadow_quick_entry_plan.md").write_text(render_shadow_report(plan), encoding="utf-8")
    return plan
