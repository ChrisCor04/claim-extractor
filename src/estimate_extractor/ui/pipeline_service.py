"""Runs the existing, unmodified extraction + mapping pipeline against a
project's source PDF and writes outputs into that project's extraction/
and mapping/ subdirectories.

This module never reimplements extraction or mapping logic -- it only
calls ``estimate_extractor.pipeline.run_extraction`` and
``estimate_extractor.mapping.pipeline.run_mapping`` (the same functions the
``process`` CLI command uses) and points their output writers at a
project directory instead of ``output/<slug>/``.

Progress stages: the extractor and mapper are not instrumented for
sub-step progress (deliberately -- Phase 3 must not rewrite Phase 1/2
internals), so the stage labels below are coarse "about to start this
blocking call" / "finished" markers around the two existing entry points,
not live intra-function progress. See docs/local-review-ui.md "Upload and
processing" for the honest explanation shown in the UI.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from estimate_extractor.config import Config
from estimate_extractor.errors import EstimateExtractorError, OCRDependencyMissingError
from estimate_extractor.mapping.outputs import write_all_mapping_outputs
from estimate_extractor.mapping.pipeline import MappingEngineConfig, MappingResult, run_mapping
from estimate_extractor.output.csv_writer import write_line_items_csv
from estimate_extractor.output.json_writer import write_canonical_estimate, write_document_pages
from estimate_extractor.output.report_writer import write_extraction_report
from estimate_extractor.pipeline import ExtractionResult, run_extraction

ProgressCallback = Callable[[str, str | None], None]

STAGES: tuple[str, ...] = (
    "Reading PDF",
    "Classifying pages",
    "Extracting line items",
    "Validating totals",
    "Normalizing descriptions",
    "Mapping candidates",
    "Preparing review",
)


class PipelineServiceError(Exception):
    """Wraps a fatal extraction/mapping error with the stage it happened
    in. The underlying exception is preserved on .cause so the UI's debug
    expander can show the real traceback -- errors are never hidden."""

    def __init__(self, stage: str, message: str, cause: Exception | None = None):
        super().__init__(message)
        self.stage = stage
        self.cause = cause


@dataclass(slots=True)
class PipelineRunResult:
    extraction: ExtractionResult
    mapping: MappingResult


def _emit(callback: ProgressCallback | None, stage: str, detail: str | None = None) -> None:
    if callback is not None:
        callback(stage, detail)


def run_pipeline_for_project(
    pdf_path: Path,
    project_dir: Path,
    config: Config,
    engine_config: MappingEngineConfig,
    *,
    enable_ocr: bool = False,
    carrier_override: str | None = None,
    progress_callback: ProgressCallback | None = None,
) -> PipelineRunResult:
    """Run extraction then mapping for one project, writing all eight
    stage output files. Raises PipelineServiceError only on a fatal
    extraction error or an unexpected mapping-stage crash -- unresolved or
    low-confidence mappings are never an error (they come out as
    needs_review/unmapped status, per the mapper's own contract)."""

    extraction_dir = project_dir / "extraction"
    mapping_dir = project_dir / "mapping"
    extraction_dir.mkdir(parents=True, exist_ok=True)
    mapping_dir.mkdir(parents=True, exist_ok=True)

    _emit(progress_callback, "Reading PDF")
    _emit(progress_callback, "Classifying pages")
    _emit(progress_callback, "Extracting line items")
    _emit(progress_callback, "Validating totals")
    try:
        extraction_result = run_extraction(pdf_path, config, enable_ocr=enable_ocr, carrier_override=carrier_override)
    except OCRDependencyMissingError as exc:
        raise PipelineServiceError("Reading PDF", f"OCR dependency missing: {exc}", exc) from exc
    except EstimateExtractorError as exc:
        raise PipelineServiceError("Extracting line items", f"Extraction failed: {exc}", exc) from exc

    canonical = extraction_result.canonical
    write_canonical_estimate(canonical, extraction_dir / "canonical_estimate.json", pretty=config.output.pretty_json)
    write_extraction_report(extraction_result.report, extraction_dir / "extraction_report.json", pretty=config.output.pretty_json)
    write_line_items_csv(canonical, extraction_dir / "line_items.csv")
    write_document_pages(canonical.source_pages, extraction_dir / "document_pages.json", pretty=config.output.pretty_json)

    _emit(progress_callback, "Normalizing descriptions")
    _emit(progress_callback, "Mapping candidates")
    canonical_dict = json.loads((extraction_dir / "canonical_estimate.json").read_text(encoding="utf-8"))
    try:
        mapping_result = run_mapping(canonical_dict, engine_config)
    except Exception as exc:
        raise PipelineServiceError("Mapping candidates", f"Mapping failed: {exc}", exc) from exc

    write_all_mapping_outputs(mapping_result, mapping_dir, pretty=config.output.pretty_json)

    _emit(progress_callback, "Preparing review")
    return PipelineRunResult(extraction=extraction_result, mapping=mapping_result)


def rerun_mapping_for_project(
    project_dir: Path,
    engine_config: MappingEngineConfig,
    *,
    pretty: bool = True,
) -> MappingResult:
    """Re-run normalization + mapping against the project's already-saved
    canonical_estimate.json (no PDF re-read, no re-extraction) -- used
    after a catalog change. Overwrites normalized_estimate.json /
    mapped_estimate.json / mapping_report.json / mapping_review.csv;
    review-state (approvals, overrides, notes) lives in a separate file and
    is untouched by this call -- see review_service.py."""
    canonical_path = project_dir / "extraction" / "canonical_estimate.json"
    if not canonical_path.exists():
        raise PipelineServiceError("Mapping candidates", "No canonical_estimate.json found -- run extraction first.")
    canonical_dict = json.loads(canonical_path.read_text(encoding="utf-8"))
    mapping_result = run_mapping(canonical_dict, engine_config)
    write_all_mapping_outputs(mapping_result, project_dir / "mapping", pretty=pretty)
    return mapping_result
