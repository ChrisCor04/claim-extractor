"""Resumable orchestration for building the canonical selector catalog:
inventory -> per-screenshot OCR/table-detect/row-parse (cached to disk so
a restart never re-runs expensive OCR on already-processed screenshots)
-> merge/deduplicate -> validate -> persist (db/csv/json/manifest/
review-queue/report).
"""

from __future__ import annotations

import json
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from estimate_extractor.selector_catalog import database, exporter
from estimate_extractor.selector_catalog.deduplicator import merge_records
from estimate_extractor.selector_catalog.image_inventory import inventory_screenshots
from estimate_extractor.selector_catalog.models import (
    NON_CAT_FOLDER,
    REVIEW_REASON_CATEGORY_MISMATCH,
    REVIEW_REASON_MALFORMED_SELECTOR,
    REVIEW_REASON_UNIT_CONTAMINATION,
    SCREENSHOT_STATUS_FAILED,
    SCREENSHOT_STATUS_PROCESSED,
    SCREENSHOT_STATUS_SKIPPED,
    ScreenshotManifestEntry,
    ScreenshotRef,
    SelectorRecord,
    SourceReference,
    is_selector_plausible,
    normalize_description,
    probable_unit_contamination,
    utc_now_iso,
)
from estimate_extractor.selector_catalog.ocr_engine import TesseractWordBoxEngine, WordBoxOCREngine
from estimate_extractor.selector_catalog.row_parser import parse_rows
from estimate_extractor.selector_catalog.table_region import (
    TableRegionNotFoundError,
    detect_table_region,
    detect_title_bar_category,
)
from estimate_extractor.selector_catalog.validator import ValidationReport, validate_catalog

# Calibrated against real reference-library screenshots: correctly-read
# rows cluster at ~0.90-0.96 mean word confidence; character-confusion
# misreads (I/1, O/0 -- a known Tesseract limitation, not something this
# pipeline attempts to correct heuristically, since that risks silently
# inventing a selector) cluster at ~0.78-0.85. This threshold catches most
# of the latter for human review without over-flagging correct reads.
LOW_CONFIDENCE_THRESHOLD = 0.85

CANDIDATES_SUBDIR = ".candidates"

ProgressCallback = Callable[[int, int, str], None]


@dataclass(slots=True)
class ScreenshotCacheEntry:
    """One screenshot's OCR results, cached to disk so a resumed import
    never re-OCRs an already-processed screenshot."""

    relative_path: str
    folder_category: str
    sequence: int
    title_bar_category: str | None
    category_mismatch: bool
    rows: list[dict]  # {"selector_raw", "description_raw", "row_confidence", "truncated"}

    def to_dict(self) -> dict:
        return {
            "relative_path": self.relative_path,
            "folder_category": self.folder_category,
            "sequence": self.sequence,
            "title_bar_category": self.title_bar_category,
            "category_mismatch": self.category_mismatch,
            "rows": self.rows,
        }

    @staticmethod
    def from_dict(data: dict) -> ScreenshotCacheEntry:
        return ScreenshotCacheEntry(
            relative_path=data["relative_path"],
            folder_category=data["folder_category"],
            sequence=data["sequence"],
            title_bar_category=data.get("title_bar_category"),
            category_mismatch=data.get("category_mismatch", False),
            rows=data.get("rows", []),
        )


def _candidate_cache_path(data_dir: Path, relative_path: str) -> Path:
    return data_dir / CANDIDATES_SUBDIR / (relative_path.replace("/", "__") + ".json")


def _save_cache_entry(data_dir: Path, entry: ScreenshotCacheEntry) -> None:
    path = _candidate_cache_path(data_dir, entry.relative_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entry.to_dict(), indent=2), encoding="utf-8")


def _load_cache_entry(data_dir: Path, relative_path: str) -> ScreenshotCacheEntry | None:
    path = _candidate_cache_path(data_dir, relative_path)
    if not path.exists():
        return None
    return ScreenshotCacheEntry.from_dict(json.loads(path.read_text(encoding="utf-8")))


def load_all_cache_entries(data_dir: Path) -> list[ScreenshotCacheEntry]:
    """Enumerates every cached per-screenshot OCR result on disk -- used by
    the QA cleanup pass to recompute category-mismatch corroboration from
    the original per-screenshot title-bar readings without any re-OCR."""
    cache_dir = data_dir / CANDIDATES_SUBDIR
    if not cache_dir.exists():
        return []
    entries = []
    for path in sorted(cache_dir.glob("*.json")):
        entries.append(ScreenshotCacheEntry.from_dict(json.loads(path.read_text(encoding="utf-8"))))
    return entries


@dataclass(slots=True)
class ImportResult:
    total_screenshots: int
    processed: int
    skipped: int
    failed: int
    from_cache: int
    records: list[SelectorRecord]
    conflicts: list[tuple[str, str, list[str]]]
    review_queue: list[exporter.ReviewQueueRow]
    manifest_entries: list[ScreenshotManifestEntry] = field(default_factory=list)
    validation: ValidationReport = field(default_factory=ValidationReport)
    duration_seconds: float = 0.0


def _process_screenshot(ref: ScreenshotRef, engine: WordBoxOCREngine) -> tuple[ScreenshotCacheEntry | None, str | None]:
    """Returns (cache_entry, error) -- error is None on success. Never
    raises: any OCR/decoding failure is captured as a manifest "failed"
    entry so one bad screenshot can't abort the whole import."""
    try:
        image_path = Path(ref.absolute_path)
        words = engine.extract_words(image_path)
        region = detect_table_region(words)
        title_category, _title_raw = detect_title_bar_category(image_path, engine)
        rows = parse_rows(words, region)
    except TableRegionNotFoundError as exc:
        return None, str(exc)
    except Exception as exc:  # noqa: BLE001 -- deliberately broad: any per-image failure must not crash the import
        return None, f"{type(exc).__name__}: {exc}"

    category_mismatch = title_category is not None and title_category != ref.folder_category
    row_dicts = [
        {
            "selector_raw": r.selector_raw,
            "description_raw": r.description_raw,
            "row_confidence": r.row_confidence,
            "truncated": r.truncated,
        }
        for r in rows
    ]
    entry = ScreenshotCacheEntry(
        relative_path=ref.relative_path,
        folder_category=ref.folder_category,
        sequence=ref.sequence,
        title_bar_category=title_category,
        category_mismatch=category_mismatch,
        rows=row_dicts,
    )
    return entry, None


def _row_to_candidate_or_review(
    entry: ScreenshotCacheEntry, row: dict, row_index: int
) -> tuple[SelectorRecord | None, exporter.ReviewQueueRow | None]:
    selector = row["selector_raw"]
    description = row["description_raw"]
    confidence = row["row_confidence"]
    truncated = row["truncated"]

    if not selector or not description:
        return None, exporter.ReviewQueueRow(
            category=entry.folder_category,
            selector=selector,
            description_original=description,
            reason="empty_selector" if not selector else "empty_description",
            in_database=False,
            source_image=entry.relative_path,
            ocr_confidence=confidence,
        )

    reasons = []
    if truncated:
        reasons.append("description_truncated_in_screenshot")
    if confidence is not None and confidence < LOW_CONFIDENCE_THRESHOLD:
        reasons.append("low_ocr_confidence")
    # NOTE: category_mismatch is deliberately NOT decided here from a
    # single screenshot's raw title-bar disagreement (that produced real
    # false positives -- see docs/selector-catalog.md "Title-bar
    # corroboration"). title_bar_category is still recorded unconditionally
    # as supporting metadata; run_import() applies
    # _apply_category_mismatch_corroboration() to the final, deduplicated
    # records, requiring a folder-wide majority of independent screenshots
    # before treating a title-bar disagreement as a genuine mismatch.
    if not is_selector_plausible(selector):
        reasons.append(REVIEW_REASON_MALFORMED_SELECTOR)
    if probable_unit_contamination(description) is not None:
        reasons.append(REVIEW_REASON_UNIT_CONTAMINATION)

    record = SelectorRecord(
        category=entry.folder_category,
        selector=selector,
        description_original=description,
        description_normalized=normalize_description(description),
        needs_review=bool(reasons),
        review_reasons=reasons,
        ocr_confidence=confidence,
        title_bar_category=entry.title_bar_category,
        category_mismatch=False,  # decided post-merge; see note above
        source_images=[
            SourceReference(
                source_image=entry.relative_path,
                source_folder=entry.folder_category,
                source_sequence=entry.sequence,
                ocr_confidence=confidence,
                row_index=row_index,
            )
        ],
    )
    return record, None


# A folder's title-bar readings must show a clear majority (>50%) pointing
# to the SAME alternate category, from at least this many independently-
# voting SCREENSHOTS (not rows -- a screenshot with many rows must not
# dominate the tally), before a category disagreement is trusted as a
# genuine mismatch rather than isolated OCR noise. Calibrated against real
# data during Phase 3.6 QA: the confirmed-real ELE-folder/FNC-title
# mismatch showed 6/9 (67%) independent screenshots agreeing; the false-
# positive DOR-folder/ELE-title (8/18, 44%) and DMO-folder/DRY-title
# (2/4, 50%) cases both failed to clear a majority -- see
# docs/selector-catalog.md "Title-bar corroboration".
MISMATCH_CORROBORATION_MIN_COUNT = 2
MISMATCH_CORROBORATION_MIN_RATIO = 0.5


def compute_corroborated_mismatches(screenshot_title_votes: dict[tuple[str, str], str | None]) -> dict[str, str]:
    """Tallies title-bar readings once per distinct SOURCE SCREENSHOT.
    `screenshot_title_votes` maps (folder_category, source_image) ->
    title_bar_category (or None) -- exactly one entry per screenshot, so a
    screenshot with many rows can never dominate the tally. Returns
    {folder_category: corroborated_alternate} only for folders whose
    readings clear both the count and ratio bars in
    MISMATCH_CORROBORATION_MIN_COUNT/RATIO. The folder name itself is
    always authoritative -- see build spec "Keep the CAT folder as the
    authoritative category" -- so a folder whose own name is the majority
    reading (or the plurality) never appears in the result."""
    by_folder: dict[str, list[str]] = defaultdict(list)
    for (folder, _screenshot), title in screenshot_title_votes.items():
        if title:
            by_folder[folder].append(title)

    corroborated: dict[str, str] = {}
    for folder, readings in by_folder.items():
        counts = Counter(readings)
        top_category, top_count = counts.most_common(1)[0]
        if top_category == folder:
            continue  # folder's own consensus agrees with itself -- no systemic mismatch
        if top_count >= MISMATCH_CORROBORATION_MIN_COUNT and (top_count / len(readings)) > MISMATCH_CORROBORATION_MIN_RATIO:
            corroborated[folder] = top_category
    return corroborated


def _screenshot_title_votes_from_candidates(candidates: list[SelectorRecord]) -> dict[tuple[str, str], str | None]:
    """Reduces per-row candidates (many rows share one screenshot's single
    title reading) down to one vote per (folder, source_image)."""
    votes: dict[tuple[str, str], str | None] = {}
    for candidate in candidates:
        for source in candidate.source_images:
            votes[(source.source_folder, source.source_image)] = candidate.title_bar_category
    return votes


def apply_category_mismatch_corroboration(records: list[SelectorRecord], corroborated: dict[str, str]) -> int:
    """Mutates `records` in place: sets category_mismatch=True (and adds
    REVIEW_REASON_CATEGORY_MISMATCH) only when a record's own
    title_bar_category equals its folder's corroborated alternate;
    clears it otherwise (so a stale/incorrect prior flag is also
    corrected). Returns how many records' category_mismatch value
    actually changed."""
    changed = 0
    for record in records:
        alternate = corroborated.get(record.category)
        should_mismatch = alternate is not None and record.title_bar_category == alternate
        if should_mismatch != record.category_mismatch:
            changed += 1
        record.category_mismatch = should_mismatch
        if should_mismatch:
            if REVIEW_REASON_CATEGORY_MISMATCH not in record.review_reasons:
                record.review_reasons.append(REVIEW_REASON_CATEGORY_MISMATCH)
        elif REVIEW_REASON_CATEGORY_MISMATCH in record.review_reasons:
            record.review_reasons.remove(REVIEW_REASON_CATEGORY_MISMATCH)
        record.needs_review = bool(record.review_reasons)
    return changed


def _candidates_from_cache_entry(entry: ScreenshotCacheEntry) -> tuple[list[SelectorRecord], list[exporter.ReviewQueueRow]]:
    candidates: list[SelectorRecord] = []
    review_rows: list[exporter.ReviewQueueRow] = []
    for idx, row in enumerate(entry.rows):
        candidate, review_row = _row_to_candidate_or_review(entry, row, idx)
        if candidate is not None:
            candidates.append(candidate)
        if review_row is not None:
            review_rows.append(review_row)
    return candidates, review_rows


def run_import(
    extracted_root: Path,
    data_dir: Path,
    *,
    engine: WordBoxOCREngine | None = None,
    force: bool = False,
    category_filter: str | None = None,
    limit: int | None = None,
    progress_callback: ProgressCallback | None = None,
) -> ImportResult:
    start = time.time()
    engine = engine or TesseractWordBoxEngine()

    refs = inventory_screenshots(extracted_root)
    if category_filter:
        refs = [r for r in refs if r.folder_category == category_filter]
    if limit:
        refs = refs[:limit]

    manifest_path = data_dir / "screenshot_processing_manifest.json"
    existing_manifest = {} if force else {e.relative_path: e for e in exporter.load_manifest(manifest_path)}

    manifest_entries: dict[str, ScreenshotManifestEntry] = dict(existing_manifest)
    all_candidates: list[SelectorRecord] = []
    review_only_rows: list[exporter.ReviewQueueRow] = []

    processed = skipped = failed = from_cache = 0

    for i, ref in enumerate(refs):
        if progress_callback:
            progress_callback(i + 1, len(refs), ref.relative_path)

        if ref.is_non_cat:
            manifest_entries[ref.relative_path] = ScreenshotManifestEntry(
                relative_path=ref.relative_path,
                folder_category=ref.folder_category,
                sequence=ref.sequence,
                status=SCREENSHOT_STATUS_SKIPPED,
                skip_reason="non_cat_project_ui_folder",
                processed_at=utc_now_iso(),
            )
            skipped += 1
            continue

        existing = existing_manifest.get(ref.relative_path)
        if not force and existing is not None and existing.status == SCREENSHOT_STATUS_PROCESSED:
            cache_entry = _load_cache_entry(data_dir, ref.relative_path)
            if cache_entry is not None:
                from_cache += 1
                candidates, review_rows = _candidates_from_cache_entry(cache_entry)
                all_candidates.extend(candidates)
                review_only_rows.extend(review_rows)
                manifest_entries[ref.relative_path] = existing
                continue
            # Manifest says processed but the cache file is gone -- fall
            # through and reprocess rather than silently losing data.

        cache_entry, error = _process_screenshot(ref, engine)
        if error is not None or cache_entry is None:
            manifest_entries[ref.relative_path] = ScreenshotManifestEntry(
                relative_path=ref.relative_path,
                folder_category=ref.folder_category,
                sequence=ref.sequence,
                status=SCREENSHOT_STATUS_FAILED,
                error=error,
                processed_at=utc_now_iso(),
            )
            failed += 1
            exporter.write_manifest(list(manifest_entries.values()), manifest_path)
            continue

        _save_cache_entry(data_dir, cache_entry)
        manifest_entries[ref.relative_path] = ScreenshotManifestEntry(
            relative_path=ref.relative_path,
            folder_category=ref.folder_category,
            sequence=ref.sequence,
            status=SCREENSHOT_STATUS_PROCESSED,
            rows_extracted=len(cache_entry.rows),
            title_bar_category=cache_entry.title_bar_category,
            category_mismatch=cache_entry.category_mismatch,
            processed_at=utc_now_iso(),
        )
        processed += 1

        candidates, review_rows = _candidates_from_cache_entry(cache_entry)
        all_candidates.extend(candidates)
        review_only_rows.extend(review_rows)

        # Persist incrementally -- an interruption here still leaves every
        # already-processed screenshot resumable without re-OCRing it.
        exporter.write_manifest(list(manifest_entries.values()), manifest_path)

    # Unconditional final flush: the in-loop writes above are skipped by
    # the non-CAT-folder and reused-from-cache branches (both `continue`
    # before reaching them), so if the *last* screenshots in inventory
    # order hit either of those branches -- as happens for real, since
    # "_NON_CAT_PROJECT_UI" sorts after every real (uppercase) CAT folder
    # name -- their manifest entries would otherwise never reach disk.
    exporter.write_manifest(list(manifest_entries.values()), manifest_path)

    dedup_result = merge_records(all_candidates)

    corroborated_mismatches = compute_corroborated_mismatches(_screenshot_title_votes_from_candidates(all_candidates))
    apply_category_mismatch_corroboration(dedup_result.records, corroborated_mismatches)

    for record in dedup_result.records:
        if record.needs_review:
            review_only_rows.append(
                exporter.ReviewQueueRow(
                    category=record.category,
                    selector=record.selector,
                    description_original=record.description_original,
                    reason=";".join(record.review_reasons),
                    in_database=True,
                    source_image=record.primary_source_image or "",
                    ocr_confidence=record.ocr_confidence,
                )
            )

    all_relative_paths = {r.relative_path for r in refs}
    validation = validate_catalog(dedup_result.records, list(manifest_entries.values()), all_relative_paths)

    return ImportResult(
        total_screenshots=len(refs),
        processed=processed,
        skipped=skipped,
        failed=failed,
        from_cache=from_cache,
        records=dedup_result.records,
        conflicts=dedup_result.conflicts,
        review_queue=review_only_rows,
        manifest_entries=list(manifest_entries.values()),
        validation=validation,
        duration_seconds=time.time() - start,
    )


def write_outputs(result: ImportResult, data_dir: Path, reports_dir: Path) -> dict[str, Path]:
    db_path = data_dir / "master_selectors.db"
    csv_path = data_dir / "master_selectors.csv"
    json_path = data_dir / "master_selectors.json"
    review_path = data_dir / "selector_review_queue.csv"
    report_path = reports_dir / "selector_extraction_report.md"

    conn = database.create_database(db_path)
    try:
        database.replace_all_records(conn, result.records)
    finally:
        conn.close()

    exporter.write_master_selectors_csv(result.records, csv_path)
    exporter.write_master_selectors_json(result.records, json_path)
    exporter.write_review_queue_csv(result.review_queue, review_path)
    exporter.write_extraction_report(build_report_markdown(result), report_path)

    return {"database": db_path, "csv": csv_path, "json": json_path, "review_queue": review_path, "report": report_path}


def build_report_markdown(result: ImportResult) -> str:
    by_cat: dict[str, dict] = defaultdict(lambda: {"screenshots": 0, "raw_rows": 0, "unique_selectors": 0, "review_rows": 0})
    for e in result.manifest_entries:
        if e.folder_category == NON_CAT_FOLDER:
            continue
        by_cat[e.folder_category]["screenshots"] += 1
        by_cat[e.folder_category]["raw_rows"] += e.rows_extracted
    for r in result.records:
        by_cat[r.category]["unique_selectors"] += 1
        if r.needs_review:
            by_cat[r.category]["review_rows"] += 1

    lines = ["# Selector Extraction Report", ""]
    lines.append(f"- Total screenshots: {result.total_screenshots}")
    lines.append(f"- Processed: {result.processed}")
    lines.append(f"- From cache (resumed): {result.from_cache}")
    lines.append(f"- Skipped: {result.skipped}")
    lines.append(f"- Failed: {result.failed}")
    lines.append(f"- Unique selector records: {len(result.records)}")
    lines.append(f"- Records needing review: {sum(1 for r in result.records if r.needs_review)}")
    lines.append(f"- Conflicting-description groups: {len(result.conflicts)}")
    lines.append(f"- Review queue rows: {len(result.review_queue)}")
    lines.append(
        f"- Validation: {'PASSED' if result.validation.passed else 'FAILED'} "
        f"({len(result.validation.errors)} errors, {len(result.validation.warnings)} warnings)"
    )
    lines.append(f"- Duration: {result.duration_seconds:.1f}s")
    lines.append("")
    lines.append("| CAT | Screenshots | Raw Rows | Unique Selectors | Review Rows | Status |")
    lines.append("|---|---|---|---|---|---|")
    for cat in sorted(by_cat):
        stats = by_cat[cat]
        status = "OK" if stats["review_rows"] == 0 else "REVIEW"
        lines.append(f"| {cat} | {stats['screenshots']} | {stats['raw_rows']} | {stats['unique_selectors']} | {stats['review_rows']} | {status} |")
    lines.append("")

    failed_entries = [e for e in result.manifest_entries if e.status == SCREENSHOT_STATUS_FAILED]
    if failed_entries:
        lines.append("## Failed screenshots")
        for e in failed_entries:
            lines.append(f"- {e.relative_path}: {e.error}")
        lines.append("")

    if result.conflicts:
        lines.append("## CAT/SEL description conflicts")
        for category, selector, descriptions in result.conflicts:
            lines.append(f"- {category}/{selector}:")
            for d in descriptions:
                lines.append(f"  - {d!r}")
        lines.append("")

    if result.validation.errors:
        lines.append("## Validation errors")
        for issue in result.validation.errors:
            lines.append(f"- [{issue.code}] {issue.message}")
        lines.append("")

    return "\n".join(lines)
