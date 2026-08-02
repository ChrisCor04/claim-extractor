"""Orchestrates one document's normalization + mapping run: reads a
canonical_estimate.json (dict, already parsed), never the source PDF, and
produces normalized items, mapped items, and a mapping report.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from estimate_extractor.mapping.catalog import CatalogEntry, load_catalog
from estimate_extractor.mapping.matcher import match_line_item
from estimate_extractor.mapping.models import (
    MappedLineItem,
    MappingReport,
    MappingReportSummary,
    MappingStatus,
    NormalizedLineItem,
    OriginalItem,
)
from estimate_extractor.mapping.normalizer import normalize_line_item
from estimate_extractor.mapping.rules_config import NormalizationRules, load_normalization_rules
from estimate_extractor.mapping.scorer import ScoringConfig, load_scoring_config
from estimate_extractor.mapping.validator import validate_mapping

DEFAULT_CONFIG_DIR = Path(__file__).resolve().parents[3] / "config"


@dataclass(frozen=True, slots=True)
class MappingEngineConfig:
    rules: NormalizationRules
    catalog: list[CatalogEntry]
    scoring: ScoringConfig


def load_mapping_engine_config(config_dir: Path = DEFAULT_CONFIG_DIR) -> MappingEngineConfig:
    return MappingEngineConfig(
        rules=load_normalization_rules(config_dir / "normalization_rules.yaml"),
        catalog=load_catalog(config_dir / "mapping_catalog.yaml"),
        scoring=load_scoring_config(config_dir / "mapping_scoring.yaml"),
    )


@dataclass(slots=True)
class MappingResult:
    normalized_items: list[NormalizedLineItem]
    mapped_items: list[MappedLineItem]
    report: MappingReport


def load_canonical_estimate(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _build_original_item(line_item: dict, sections_by_id: dict, areas_by_id: dict) -> OriginalItem:
    section = sections_by_id.get(line_item.get("section_id"))
    area = areas_by_id.get(line_item.get("area_id"))
    source = line_item.get("source") or {}
    pages = sorted({p for p in (source.get("page_start"), source.get("page_end")) if p is not None})
    confidence = line_item.get("confidence") or {}
    return OriginalItem(
        description=line_item["description"],
        quantity=line_item.get("quantity"),
        unit_of_measure=line_item.get("unit_of_measure"),
        coverage_id=line_item.get("coverage_id"),
        section_name=section["name"] if section else None,
        area_name=area["name"] if area else None,
        source_pages=pages,
        notes=[n.get("text", "") for n in line_item.get("notes", [])],
        extraction_confidence=confidence.get("overall"),
        extraction_needs_review=bool(line_item.get("needs_review", False)),
        extraction_warnings=list(line_item.get("review_reasons", [])),
    )


def _summary(mapped_items: list[MappedLineItem], normalized_items: list[NormalizedLineItem]) -> MappingReportSummary:
    total = len(mapped_items)
    counts = Counter(m.mapping.status for m in mapped_items)
    unresolved_coverage = sum(1 for n in normalized_items if n.original.coverage_id is None)
    confidences = [m.mapping.best_match.confidence for m in mapped_items if m.mapping.best_match is not None]
    avg = round(sum(confidences) / len(confidences), 4) if confidences else None
    return MappingReportSummary(
        total_items=total,
        mapped=counts.get(MappingStatus.MAPPED, 0),
        partially_mapped=counts.get(MappingStatus.PARTIALLY_MAPPED, 0),
        needs_review=counts.get(MappingStatus.NEEDS_REVIEW, 0),
        unmapped=counts.get(MappingStatus.UNMAPPED, 0),
        unresolved_coverage=unresolved_coverage,
        average_confidence=avg,
    )


def run_mapping(canonical: dict, engine_config: MappingEngineConfig) -> MappingResult:
    sections_by_id = {s["section_id"]: s for s in canonical.get("sections", [])}
    areas_by_id = {a["area_id"]: a for a in canonical.get("areas", [])}

    normalized_items: list[NormalizedLineItem] = []
    mapped_items: list[MappedLineItem] = []

    for line_item in canonical.get("line_items", []):
        original = _build_original_item(line_item, sections_by_id, areas_by_id)

        normalized, confidence, needs_review, review_reasons = normalize_line_item(
            original.description, original.quantity, original.unit_of_measure, engine_config.rules
        )
        normalized_item = NormalizedLineItem(
            line_item_id=line_item["line_item_id"],
            original=original,
            normalized=normalized,
            confidence=confidence,
            needs_review=needs_review,
            review_reasons=review_reasons,
        )
        normalized_items.append(normalized_item)

        outcome = match_line_item(normalized, engine_config.catalog, engine_config.scoring)
        mapped_items.append(
            MappedLineItem(
                line_item_id=line_item["line_item_id"],
                coverage_id=original.coverage_id,
                normalization=normalized,
                mapping=outcome.mapping,
            )
        )

    issues = validate_mapping(engine_config.catalog, normalized_items, mapped_items)
    summary = _summary(mapped_items, normalized_items)
    status = "needs_review" if (summary.needs_review + summary.unmapped) > 0 else "mapped"
    if summary.total_items == 0:
        status = "needs_review"

    report = MappingReport(status=status, summary=summary, issues=issues)
    return MappingResult(normalized_items=normalized_items, mapped_items=mapped_items, report=report)
