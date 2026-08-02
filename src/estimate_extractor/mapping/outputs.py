"""Writers for normalized_estimate.json, mapped_estimate.json,
mapping_report.json, and mapping_review.csv."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from estimate_extractor.mapping.pipeline import MappingResult

CSV_COLUMNS = [
    "line_item_id",
    "original_description",
    "coverage_id",
    "area_name",
    "section_name",
    "quantity",
    "unit",
    "normalized_action",
    "normalized_trade",
    "normalized_component",
    "normalized_material",
    "mapping_status",
    "mapping_id",
    "category",
    "selector",
    "activity",
    "mapped_description",
    "confidence",
    "needs_review",
    "review_reasons",
    "alternative_count",
]


def _dump(data, pretty: bool) -> str:
    return json.dumps(data, indent=2 if pretty else None, default=str)


def write_normalized_estimate(result: MappingResult, path: Path, pretty: bool = True) -> None:
    data = [item.model_dump(mode="json") for item in result.normalized_items]
    path.write_text(_dump(data, pretty), encoding="utf-8")


def write_mapped_estimate(result: MappingResult, path: Path, pretty: bool = True) -> None:
    data = [item.model_dump(mode="json") for item in result.mapped_items]
    path.write_text(_dump(data, pretty), encoding="utf-8")


def write_mapping_report(result: MappingResult, path: Path, pretty: bool = True) -> None:
    path.write_text(_dump(result.report.model_dump(mode="json"), pretty), encoding="utf-8")


def write_mapping_review_csv(result: MappingResult, path: Path) -> None:
    normalized_by_id = {n.line_item_id: n for n in result.normalized_items}
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, quoting=csv.QUOTE_MINIMAL)
        writer.writerow(CSV_COLUMNS)
        for mapped in result.mapped_items:
            normalized = normalized_by_id.get(mapped.line_item_id)
            original = normalized.original if normalized else None
            best = mapped.mapping.best_match
            writer.writerow(
                [
                    mapped.line_item_id,
                    original.description if original else None,
                    mapped.coverage_id,
                    original.area_name if original else None,
                    original.section_name if original else None,
                    original.quantity if original else None,
                    original.unit_of_measure if original else None,
                    mapped.normalization.action.value,
                    mapped.normalization.trade.value,
                    mapped.normalization.component,
                    mapped.normalization.material,
                    mapped.mapping.status.value,
                    best.mapping_id if best else None,
                    best.category if best else None,
                    best.selector if best else None,
                    best.activity if best else None,
                    best.description if best else None,
                    best.confidence if best else None,
                    mapped.mapping.needs_review,
                    "; ".join(mapped.mapping.review_reasons),
                    len(mapped.mapping.alternatives),
                ]
            )


def write_all_mapping_outputs(result: MappingResult, output_dir: Path, pretty: bool = True) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    write_normalized_estimate(result, output_dir / "normalized_estimate.json", pretty)
    write_mapped_estimate(result, output_dir / "mapped_estimate.json", pretty)
    write_mapping_report(result, output_dir / "mapping_report.json", pretty)
    write_mapping_review_csv(result, output_dir / "mapping_review.csv")
