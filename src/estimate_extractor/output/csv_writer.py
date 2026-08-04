"""Writer for line_items.csv -- see estimate_extractor.output's package
docstring."""

from __future__ import annotations

import csv
from pathlib import Path

from estimate_extractor.models.canonical import CanonicalEstimate

CSV_COLUMNS = [
    "line_item_id",
    "source_line_number",
    "coverage_id",
    "area_id",
    "area_name",
    "section_id",
    "section_name",
    "category_heading",
    "description",
    "quantity",
    "unit_of_measure",
    "unit_price",
    "tax",
    "overhead_and_profit",
    "replacement_cost_value",
    "depreciation_type",
    "depreciation_amount",
    "actual_cash_value",
    "confidence_overall",
    "needs_review",
    "review_reasons",
]


def write_line_items_csv(canonical: CanonicalEstimate, path: Path) -> None:
    area_names = {area.area_id: area.name for area in canonical.areas}
    section_names = {section.section_id: section.name for section in canonical.sections}

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, quoting=csv.QUOTE_MINIMAL)
        writer.writerow(CSV_COLUMNS)
        for item in canonical.line_items:
            writer.writerow(
                [
                    item.line_item_id,
                    item.source_line_number,
                    item.coverage_id,
                    item.area_id,
                    area_names.get(item.area_id) if item.area_id else None,
                    item.section_id,
                    section_names.get(item.section_id) if item.section_id else None,
                    item.category_heading,
                    item.description,
                    item.quantity,
                    item.unit_of_measure,
                    item.unit_price,
                    item.tax,
                    item.overhead_and_profit,
                    item.replacement_cost_value,
                    item.depreciation_type.value,
                    item.depreciation_amount,
                    item.actual_cash_value,
                    item.confidence.overall,
                    item.needs_review,
                    "; ".join(item.review_reasons),
                ]
            )
