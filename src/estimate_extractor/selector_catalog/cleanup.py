"""Phase 3.6 QA cleanup pass: fixes historical data produced before the
Unit-column-contamination, malformed-selector-shape, and title-bar-
corroboration fixes existed in row_parser.py/pipeline.py -- without
rebuilding the catalog from scratch. See docs/selector-catalog.md
"QA cleanup" for the investigation and evidence behind each fix.

Nothing here silently "corrects" a value:

- Unit-column contamination is only removed from a description when a
  fresh, coordinate-based re-parse of the actual source screenshot
  independently produces a different (shorter) description -- never by
  string-suffix stripping. A description that re-parses identically is
  left untouched (the trailing token was legitimate).
- A malformed-shaped selector is only resolved into an existing well-
  formed record when that record has an EXACT (not fuzzy) matching
  description, from a DIFFERENT source screenshot -- "identical evidence
  from another source screenshot", per the build spec. Anything short of
  that stays exactly as OCR'd, flagged for human review.
- Category-mismatch flags are recomputed from the existing per-screenshot
  title-bar readings already on disk (no re-OCR needed for this part).
"""

from __future__ import annotations

import shutil
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from estimate_extractor.selector_catalog import database, exporter
from estimate_extractor.selector_catalog.image_inventory import resolve_screenshot_path
from estimate_extractor.selector_catalog.models import (
    REVIEW_REASON_MALFORMED_SELECTOR,
    REVIEW_REASON_UNIT_CONTAMINATION,
    SelectorRecord,
    is_selector_plausible,
    normalize_description,
    probable_unit_contamination,
    utc_now_iso,
)
from estimate_extractor.selector_catalog.ocr_engine import TesseractWordBoxEngine, WordBoxOCREngine
from estimate_extractor.selector_catalog.pipeline import (
    apply_category_mismatch_corroboration,
    compute_corroborated_mismatches,
    load_all_cache_entries,
)
from estimate_extractor.selector_catalog.row_parser import parse_rows
from estimate_extractor.selector_catalog.table_region import detect_table_region

BACKUP_PREFIX = "master_selectors"


def backup_database(db_path: Path, backups_dir: Path) -> Path:
    backups_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_path = backups_dir / f"{BACKUP_PREFIX}_{timestamp}.db"
    if db_path.exists():
        shutil.copy2(db_path, backup_path)
    return backup_path


@dataclass(slots=True)
class CleanupReport:
    records_inspected: int = 0

    unit_contamination_candidates: int = 0
    descriptions_corrected: int = 0
    unit_contamination_confirmed_legitimate: int = 0
    unit_contamination_unresolved: int = 0

    malformed_selectors_total: int = 0
    malformed_resolved_from_overlap: int = 0
    malformed_still_review: int = 0

    category_mismatches_before: int = 0
    category_mismatches_after: int = 0

    final_record_count: int = 0
    final_needs_review_count: int = 0

    backup_path: Path | None = None
    corrected_examples: list[tuple[str, str, str, str]] = field(default_factory=list)
    resolved_examples: list[tuple[str, str, str]] = field(default_factory=list)


def _find_matching_row(rows: list[dict], selector: str) -> dict | None:
    for row in rows:
        if row["selector_raw"] == selector:
            return row
    return None


def run_unit_contamination_cleanup(
    records: list[SelectorRecord],
    extracted_root: Path,
    engine: WordBoxOCREngine,
    report: CleanupReport,
) -> None:
    """Re-derives the description for every probable-contamination
    candidate directly from its source screenshot's word coordinates,
    using the fixed (center-based) column-assignment logic. Mutates
    `records` in place."""
    rows_cache: dict[str, list[dict] | None] = {}

    for record in records:
        if probable_unit_contamination(record.description_original) is None:
            continue
        report.unit_contamination_candidates += 1

        source_image = record.primary_source_image
        row = None
        if source_image is not None:
            if source_image not in rows_cache:
                rows_cache[source_image] = _reparse_screenshot(extracted_root, source_image, engine)
            parsed_rows = rows_cache[source_image]
            if parsed_rows is not None:
                row = _find_matching_row(parsed_rows, record.selector)

        if row is None:
            report.unit_contamination_unresolved += 1
            if REVIEW_REASON_UNIT_CONTAMINATION not in record.review_reasons:
                record.review_reasons.append(REVIEW_REASON_UNIT_CONTAMINATION)
            record.needs_review = True
            continue

        new_description = row["description_raw"]
        matched_token = probable_unit_contamination(record.description_original)
        is_exact_trailing_token_removal = record.description_original == f"{new_description} {matched_token}"

        if new_description == record.description_original:
            # Coordinate re-parse independently reproduces the SAME
            # description -- the trailing token genuinely belongs to the
            # description column (e.g. "...over 180 SF"). Left untouched,
            # never flagged: this is the "legitimate descriptions ending
            # in unit-like text are not incorrectly stripped" guarantee.
            report.unit_contamination_confirmed_legitimate += 1
        elif is_exact_trailing_token_removal:
            # The re-parse differs from the stored description in EXACTLY
            # one way: the trailing unit token is gone, nothing else
            # changed. This precise check (not "any difference") is what
            # makes the fix safe for merged records: a record's stored
            # description can come from a DIFFERENT source screenshot than
            # its primary_source_image (deduplicator.py picks the "best"
            # candidate independently of source order), so re-parsing
            # primary_source_image can legitimately disagree with the
            # stored text for reasons that have nothing to do with Unit-
            # column contamination -- confirmed against real data during
            # this QA pass (e.g. a merged "SWR>" record's re-parsed
            # primary source showed an entirely different SF range than
            # its stored description). Only an exact "description = new +
            # ' ' + token" relationship is trusted as a genuine, isolated
            # contamination fix; anything else is left alone and flagged.
            report.descriptions_corrected += 1
            report.corrected_examples.append(
                (record.category, record.selector, record.description_original, new_description)
            )
            record.description_original = new_description
            record.description_normalized = normalize_description(new_description)
            record.updated_at = utc_now_iso()
        else:
            # The re-parse differs in some OTHER way (not just a clean
            # trailing-token removal) -- can't be trusted as a safe,
            # isolated contamination fix. Leave the stored description
            # untouched and flag for human review instead of guessing.
            report.unit_contamination_unresolved += 1
            if REVIEW_REASON_UNIT_CONTAMINATION not in record.review_reasons:
                record.review_reasons.append(REVIEW_REASON_UNIT_CONTAMINATION)
            record.needs_review = True


def _reparse_screenshot(extracted_root: Path, source_image: str, engine: WordBoxOCREngine) -> list[dict] | None:
    try:
        image_path = resolve_screenshot_path(extracted_root, source_image)
        if not image_path.exists():
            return None
        words = engine.extract_words(image_path)
        region = detect_table_region(words)
        parsed_rows = parse_rows(words, region)
    except Exception:  # noqa: BLE001 -- one bad screenshot must never abort the cleanup pass
        return None
    return [{"selector_raw": r.selector_raw, "description_raw": r.description_raw} for r in parsed_rows]


def run_malformed_selector_cleanup(records: list[SelectorRecord], report: CleanupReport) -> list[SelectorRecord]:
    """Returns a new records list with resolved malformed-selector
    duplicates removed (their provenance merged into the matching well-
    formed record). See module docstring for the exact resolution bar."""
    by_category: dict[str, list[SelectorRecord]] = defaultdict(list)
    for r in records:
        by_category[r.category].append(r)

    to_remove: set[tuple[str, str]] = set()

    for record in records:
        if is_selector_plausible(record.selector):
            continue
        report.malformed_selectors_total += 1

        malformed_sources = {s.source_image for s in record.source_images}
        resolved = None
        for candidate in by_category[record.category]:
            if candidate is record or candidate.key in to_remove:
                continue
            if not is_selector_plausible(candidate.selector):
                continue  # candidate is itself malformed -- not valid corroborating evidence
            if candidate.description_normalized != record.description_normalized:
                continue  # must be an EXACT match, not fuzzy -- "identical evidence"
            candidate_sources = {s.source_image for s in candidate.source_images}
            if not (candidate_sources - malformed_sources):
                continue  # must come from an INDEPENDENT source screenshot
            resolved = candidate
            break

        if resolved is not None:
            resolved.source_images.extend(record.source_images)
            report.malformed_resolved_from_overlap += 1
            report.resolved_examples.append((record.category, record.selector, resolved.selector))
            to_remove.add(record.key)
        else:
            report.malformed_still_review += 1
            if REVIEW_REASON_MALFORMED_SELECTOR not in record.review_reasons:
                record.review_reasons.append(REVIEW_REASON_MALFORMED_SELECTOR)
            record.needs_review = True

    return [r for r in records if r.key not in to_remove]


def run_full_cleanup(
    data_dir: Path,
    extracted_root: Path,
    backups_dir: Path,
    *,
    engine: WordBoxOCREngine | None = None,
) -> CleanupReport:
    engine = engine or TesseractWordBoxEngine()
    db_path = data_dir / "master_selectors.db"

    report = CleanupReport()
    report.backup_path = backup_database(db_path, backups_dir)

    conn = database.create_database(db_path)
    records = database.load_all_records(conn)
    conn.close()
    report.records_inspected = len(records)
    report.category_mismatches_before = sum(1 for r in records if r.category_mismatch)

    # 1) Category-mismatch corroboration recompute -- no re-OCR needed,
    # reuses the per-screenshot title-bar readings already cached on disk.
    cache_entries = load_all_cache_entries(data_dir)
    screenshot_title_votes = {
        (e.folder_category, e.relative_path): e.title_bar_category for e in cache_entries
    }
    corroborated = compute_corroborated_mismatches(screenshot_title_votes)
    apply_category_mismatch_corroboration(records, corroborated)
    report.category_mismatches_after = sum(1 for r in records if r.category_mismatch)

    # 2) Unit-column contamination: targeted re-OCR of only the affected
    # source screenshots, coordinate-based re-derivation.
    run_unit_contamination_cleanup(records, extracted_root, engine, report)

    # 3) Malformed-selector shapes: in-memory cross-screenshot resolution.
    records = run_malformed_selector_cleanup(records, report)

    report.final_record_count = len(records)
    report.final_needs_review_count = sum(1 for r in records if r.needs_review)

    conn = database.create_database(db_path)
    database.replace_all_records(conn, records)
    conn.close()

    exporter.write_master_selectors_csv(records, data_dir / "master_selectors.csv")
    exporter.write_master_selectors_json(records, data_dir / "master_selectors.json")

    return report
