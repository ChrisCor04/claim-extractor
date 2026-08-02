"""Writers for the selector catalog's output files: master_selectors.csv,
master_selectors.json, screenshot_processing_manifest.json,
selector_review_queue.csv, and the human-readable extraction report.
SQLite is handled separately in database.py.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path

from estimate_extractor.selector_catalog.models import ScreenshotManifestEntry, SelectorRecord

CSV_COLUMNS = [
    "category",
    "selector",
    "description_original",
    "description_normalized",
    "needs_review",
    "review_reasons",
    "ocr_confidence",
    "title_bar_category",
    "category_mismatch",
    "conflicting_descriptions",
    "primary_source_image",
    "source_image_count",
    "created_at",
    "updated_at",
]

REVIEW_QUEUE_COLUMNS = [
    "category",
    "selector",
    "description_original",
    "reason",
    "in_database",
    "source_image",
    "ocr_confidence",
]


@dataclass(slots=True)
class ReviewQueueRow:
    category: str
    selector: str
    description_original: str
    reason: str
    in_database: bool
    source_image: str
    ocr_confidence: float | None


def write_master_selectors_csv(records: list[SelectorRecord], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(CSV_COLUMNS)
        for r in records:
            writer.writerow(
                [
                    r.category,
                    r.selector,
                    r.description_original,
                    r.description_normalized,
                    r.needs_review,
                    ";".join(r.review_reasons),
                    r.ocr_confidence,
                    r.title_bar_category,
                    r.category_mismatch,
                    ";".join(r.conflicting_descriptions),
                    r.primary_source_image,
                    len(r.source_images),
                    r.created_at,
                    r.updated_at,
                ]
            )


def write_master_selectors_json(records: list[SelectorRecord], path: Path, pretty: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = [r.to_dict() for r in records]
    path.write_text(json.dumps(data, indent=2 if pretty else None, default=str), encoding="utf-8")


def write_manifest(manifest_entries: list[ScreenshotManifestEntry], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "total_screenshots": len(manifest_entries),
        "entries": [e.to_dict() for e in sorted(manifest_entries, key=lambda e: e.relative_path)],
    }
    path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


def load_manifest(path: Path) -> list[ScreenshotManifestEntry]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return [ScreenshotManifestEntry.from_dict(e) for e in data.get("entries", [])]


def write_review_queue_csv(rows: list[ReviewQueueRow], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(REVIEW_QUEUE_COLUMNS)
        for row in rows:
            writer.writerow(
                [
                    row.category,
                    row.selector,
                    row.description_original,
                    row.reason,
                    row.in_database,
                    row.source_image,
                    row.ocr_confidence,
                ]
            )


def write_extraction_report(report_markdown: str, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report_markdown, encoding="utf-8")
