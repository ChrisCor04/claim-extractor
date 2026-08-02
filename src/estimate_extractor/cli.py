"""Command-line interface.

Exit codes:
  0  success
  1  completed with review warnings
  2  extraction failure
  3  invalid arguments
  4  dependency/configuration problem
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import click

from estimate_extractor.config import Config
from estimate_extractor.errors import EstimateExtractorError, OCRDependencyMissingError, SchemaValidationError
from estimate_extractor.logging_config import configure_logging
from estimate_extractor.models.canonical import CanonicalEstimate, ExtractionStatus
from estimate_extractor.output.csv_writer import write_line_items_csv
from estimate_extractor.output.json_writer import (
    write_canonical_estimate,
    write_debug_pages,
    write_document_pages,
    write_raw_text_pages,
)
from estimate_extractor.output.report_writer import write_extraction_report
from estimate_extractor.pdf.reader import read_pdf
from estimate_extractor.pdf.tables import classify_line
from estimate_extractor.pipeline import run_extraction, slugify

logger = logging.getLogger("estimate_extractor")

_CARRIER_KEYS = ("state_farm", "travelers", "usaa", "farmers", "allstate", "generic")

_DEFAULT_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"
_DEFAULT_VERIFIED_CATALOG_PATH = _DEFAULT_CONFIG_DIR / "verified_xactimate_catalog.yaml"
_DEFAULT_BACKUPS_DIR = _DEFAULT_CONFIG_DIR / "backups"


def _load_config(config_path: Path | None) -> Config:
    if config_path is None:
        return Config.default()
    try:
        return Config.from_yaml(config_path)
    except Exception as exc:
        click.echo(f"Failed to load config '{config_path}': {exc}", err=True)
        sys.exit(4)


def _find_pdfs(path: Path, recursive: bool) -> list[Path]:
    if path.is_file():
        return [path]
    pattern = "**/*.pdf" if recursive else "*.pdf"
    return sorted(p for p in path.glob(pattern) if p.is_file())


@click.group()
def cli() -> None:
    """estimate_extractor: multi-carrier property insurance estimate PDF extractor."""


@cli.command()
@click.argument("path", type=click.Path(exists=True, path_type=Path))
@click.option("--output-dir", default="output", type=click.Path(path_type=Path), help="Output root directory.")
@click.option("--enable-ocr", is_flag=True, default=False, help="Fall back to local Tesseract OCR for image-only pages.")
@click.option("--carrier", default=None, type=click.Choice(_CARRIER_KEYS), help="Force a specific carrier adapter instead of auto-detecting.")
@click.option("--strict", is_flag=True, default=False, help="Treat 'needs_review' status as a failure (exit 1) even without errors.")
@click.option("--debug", is_flag=True, default=False, help="Write full per-page debug JSON alongside the standard outputs.")
@click.option("--log-level", default="INFO", show_default=True)
@click.option("--overwrite/--no-overwrite", default=True, help="Overwrite an existing output directory for a document.")
@click.option("--recursive", is_flag=True, default=False, help="When PATH is a directory, search it recursively for PDFs.")
@click.option("--redact-debug-output", is_flag=True, default=False, help="Redact emails/phones/zips in debug JSON and raw text output.")
@click.option("--config", "config_path", default=None, type=click.Path(exists=True, path_type=Path), help="Path to a config YAML (defaults to built-in defaults).")
def extract(
    path: Path,
    output_dir: Path,
    enable_ocr: bool,
    carrier: str | None,
    strict: bool,
    debug: bool,
    log_level: str,
    overwrite: bool,
    recursive: bool,
    redact_debug_output: bool,
    config_path: Path | None,
) -> None:
    """Extract one PDF, or every PDF in a directory, into canonical JSON."""
    configure_logging(log_level)
    config = _load_config(config_path)

    pdfs = _find_pdfs(path, recursive)
    if not pdfs:
        click.echo(f"No PDF files found at '{path}'.", err=True)
        sys.exit(3)

    worst_exit = 0
    for pdf_path in pdfs:
        slug = slugify(pdf_path.name)
        doc_out_dir = output_dir / slug
        if doc_out_dir.exists() and not overwrite and any(doc_out_dir.iterdir()):
            click.echo(f"Skipping '{pdf_path.name}': output directory already exists (--no-overwrite).")
            continue

        click.echo(f"Processing {pdf_path.name} ...")
        try:
            result = run_extraction(pdf_path, config, enable_ocr=enable_ocr, carrier_override=carrier)
        except OCRDependencyMissingError as exc:
            click.echo(f"  Dependency error: {exc}", err=True)
            worst_exit = max(worst_exit, 4)
            continue
        except EstimateExtractorError as exc:
            click.echo(f"  Extraction failed: {exc}", err=True)
            worst_exit = max(worst_exit, 2)
            continue

        canonical = result.canonical
        report = result.report

        doc_out_dir.mkdir(parents=True, exist_ok=True)
        write_canonical_estimate(
            canonical, doc_out_dir / "canonical_estimate.json", pretty=config.output.pretty_json
        )
        write_extraction_report(
            report, doc_out_dir / "extraction_report.json", pretty=config.output.pretty_json
        )
        write_line_items_csv(canonical, doc_out_dir / "line_items.csv")
        write_document_pages(
            canonical.source_pages, doc_out_dir / "document_pages.json", pretty=config.output.pretty_json
        )
        if config.output.write_raw_text:
            write_raw_text_pages(result.document, doc_out_dir / "raw_text")
        if config.output.write_debug_json or debug:
            write_debug_pages(
                result.document,
                canonical.source_pages,
                doc_out_dir / "debug",
                pretty=config.output.pretty_json,
                redact=redact_debug_output,
            )

        reconciliation_label = (
            "PASS"
            if report.reconciliation.within_tolerance
            else ("FAIL" if report.reconciliation.within_tolerance is False else "N/A")
        )
        click.echo(f"  Carrier: {canonical.document.carrier_detected} ({canonical.document.carrier_confidence:.2f})")
        click.echo(f"  Pages: {canonical.document.page_count}")
        click.echo(f"  Estimate pages: {len(canonical.document.estimate_page_numbers)}")
        click.echo(f"  Excluded pages: {len(canonical.document.excluded_page_numbers)}")
        click.echo(f"  Coverages: {len(canonical.coverages)}")
        click.echo(f"  Sections: {len(canonical.sections)}")
        click.echo(f"  Line items: {len(canonical.line_items)}")
        click.echo(f"  Warnings: {report.summary.warnings}")
        click.echo(f"  Reconciliation: {reconciliation_label}")
        click.echo(f"  Status: {canonical.document.extraction_status.value}")
        click.echo(f"  Output: {doc_out_dir}/")
        click.echo()

        if canonical.document.extraction_status == ExtractionStatus.FAILED:
            worst_exit = max(worst_exit, 2)
        elif canonical.document.extraction_status == ExtractionStatus.NEEDS_REVIEW:
            worst_exit = max(worst_exit, 1)
        elif strict and report.summary.warnings > 0:
            worst_exit = max(worst_exit, 1)

    sys.exit(worst_exit)


@cli.command()
@click.argument("json_path", type=click.Path(exists=True, path_type=Path))
def validate(json_path: Path) -> None:
    """Validate a canonical_estimate.json file against the schema."""
    import json

    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
        CanonicalEstimate.model_validate(data)
    except Exception as exc:
        click.echo(f"Schema validation FAILED: {exc}", err=True)
        raise SystemExit(2) from exc
    click.echo(f"Schema validation OK: {json_path}")
    sys.exit(0)


@cli.command()
@click.argument("pdf_path", type=click.Path(exists=True, path_type=Path))
@click.option("--page", "page_number", type=int, required=True, help="1-indexed page number to inspect.")
@click.option("--enable-ocr", is_flag=True, default=False)
def inspect(pdf_path: Path, page_number: int, enable_ocr: bool) -> None:
    """Print classification + raw text + row-token breakdown for one page."""
    from estimate_extractor.classification.pages import classify_page

    config = Config.default()
    document = read_pdf(
        pdf_path,
        enable_ocr=enable_ocr,
        min_native_text_characters=config.ocr.minimum_native_text_characters,
        ocr_dpi=config.ocr.dpi,
    )
    if not (1 <= page_number <= document.page_count):
        click.echo(f"Page {page_number} out of range (document has {document.page_count} pages).", err=True)
        sys.exit(3)

    page = document.pages[page_number - 1]
    record = classify_page(page)

    click.echo(f"Page {page_number} of {document.page_count}")
    click.echo(f"Classification: {record.classification.value} (confidence {record.confidence:.2f})")
    click.echo(f"include_in_estimate: {record.include_in_estimate}")
    click.echo("Reasons:")
    for r in record.reasons:
        click.echo(f"  - {r}")
    click.echo(f"Source: {page.source}, chars: {page.char_count}")
    click.echo("\n--- Raw text ---")
    click.echo(page.raw_text)
    click.echo("\n--- Row token stream ---")
    for line in page.raw_text.splitlines():
        if not line.strip():
            continue
        tok = classify_line(line)
        click.echo(f"[{tok.kind.value:>24}] {line.strip()}")
    sys.exit(0)


@cli.command(name="map")
@click.argument("canonical_json_path", type=click.Path(exists=True, path_type=Path))
@click.option(
    "--output-dir",
    default=None,
    type=click.Path(path_type=Path),
    help="Where to write mapping outputs (defaults to the canonical_estimate.json's own directory).",
)
def map_command(canonical_json_path: Path, output_dir: Path | None) -> None:
    """Normalize + map an existing canonical_estimate.json (Phase 2 only, no PDF re-read)."""
    from estimate_extractor.mapping.outputs import write_all_mapping_outputs
    from estimate_extractor.mapping.pipeline import load_canonical_estimate, load_mapping_engine_config, run_mapping

    canonical = load_canonical_estimate(canonical_json_path)
    try:
        engine_config = load_mapping_engine_config()
    except Exception as exc:
        click.echo(f"Failed to load mapping engine configuration: {exc}", err=True)
        sys.exit(4)

    result = run_mapping(canonical, engine_config)
    target_dir = output_dir or canonical_json_path.parent
    write_all_mapping_outputs(result, target_dir)

    s = result.report.summary
    click.echo(f"Mapping status: {result.report.status}")
    click.echo(f"  Total items: {s.total_items}")
    click.echo(f"  Mapped: {s.mapped}")
    click.echo(f"  Partially mapped: {s.partially_mapped}")
    click.echo(f"  Needs review: {s.needs_review}")
    click.echo(f"  Unmapped: {s.unmapped}")
    click.echo(f"  Unresolved coverage: {s.unresolved_coverage}")
    click.echo(f"  Average confidence: {s.average_confidence}")
    click.echo(f"Output: {target_dir}/")
    sys.exit(0 if result.report.status == "mapped" else 1)


@cli.command()
@click.argument("path", type=click.Path(exists=True, path_type=Path))
@click.option("--output-dir", default="output", type=click.Path(path_type=Path), help="Output root directory.")
@click.option("--enable-ocr", is_flag=True, default=False, help="Fall back to local Tesseract OCR for image-only pages.")
@click.option("--carrier", default=None, type=click.Choice(_CARRIER_KEYS), help="Force a specific carrier adapter instead of auto-detecting.")
@click.option("--recursive", is_flag=True, default=False, help="When PATH is a directory, search it recursively for PDFs.")
@click.option("--overwrite/--no-overwrite", default=True, help="Overwrite an existing output directory for a document.")
@click.option("--debug", is_flag=True, default=False, help="Write full per-page debug JSON alongside the standard outputs.")
@click.option("--log-level", default="INFO", show_default=True)
@click.option("--redact-debug-output", is_flag=True, default=False, help="Redact emails/phones/zips in debug JSON and raw text output.")
@click.option("--config", "config_path", default=None, type=click.Path(exists=True, path_type=Path), help="Path to a config YAML.")
def process(
    path: Path,
    output_dir: Path,
    enable_ocr: bool,
    carrier: str | None,
    recursive: bool,
    overwrite: bool,
    debug: bool,
    log_level: str,
    redact_debug_output: bool,
    config_path: Path | None,
) -> None:
    """Full pipeline: extract -> normalize -> map -> validate mapping -> write outputs.

    Stops only on a fatal EXTRACTION error for a given document; unresolved
    or low-confidence mappings produce status "needs_review", never a
    pipeline failure.
    """
    configure_logging(log_level)
    config = _load_config(config_path)

    from estimate_extractor.mapping.outputs import write_all_mapping_outputs
    from estimate_extractor.mapping.pipeline import load_mapping_engine_config, run_mapping

    pdfs = _find_pdfs(path, recursive)
    if not pdfs:
        click.echo(f"No PDF files found at '{path}'.", err=True)
        sys.exit(3)

    try:
        engine_config = load_mapping_engine_config()
    except Exception as exc:
        click.echo(f"Failed to load mapping engine configuration: {exc}", err=True)
        sys.exit(4)

    worst_exit = 0
    for pdf_path in pdfs:
        slug = slugify(pdf_path.name)
        doc_out_dir = output_dir / slug
        if doc_out_dir.exists() and not overwrite and any(doc_out_dir.iterdir()):
            click.echo(f"Skipping '{pdf_path.name}': output directory already exists (--no-overwrite).")
            continue

        click.echo(f"Processing {pdf_path.name} ...")
        try:
            result = run_extraction(pdf_path, config, enable_ocr=enable_ocr, carrier_override=carrier)
        except OCRDependencyMissingError as exc:
            click.echo(f"  Dependency error: {exc}", err=True)
            worst_exit = max(worst_exit, 4)
            continue
        except EstimateExtractorError as exc:
            click.echo(f"  Extraction failed: {exc}", err=True)
            worst_exit = max(worst_exit, 2)
            continue

        canonical = result.canonical
        report = result.report

        doc_out_dir.mkdir(parents=True, exist_ok=True)
        write_canonical_estimate(canonical, doc_out_dir / "canonical_estimate.json", pretty=config.output.pretty_json)
        write_extraction_report(report, doc_out_dir / "extraction_report.json", pretty=config.output.pretty_json)
        write_line_items_csv(canonical, doc_out_dir / "line_items.csv")
        write_document_pages(canonical.source_pages, doc_out_dir / "document_pages.json", pretty=config.output.pretty_json)
        if config.output.write_raw_text:
            write_raw_text_pages(result.document, doc_out_dir / "raw_text")
        if config.output.write_debug_json or debug:
            write_debug_pages(
                result.document,
                canonical.source_pages,
                doc_out_dir / "debug",
                pretty=config.output.pretty_json,
                redact=redact_debug_output,
            )

        # Mapping runs regardless of extraction status -- a fatal
        # EXTRACTION error stops the loop (caught above); a merely
        # needs_review extraction status must still be mapped, per the
        # requirement that unresolved items never crash the pipeline.
        canonical_dict = json.loads((doc_out_dir / "canonical_estimate.json").read_text(encoding="utf-8"))
        mapping_result = run_mapping(canonical_dict, engine_config)
        write_all_mapping_outputs(mapping_result, doc_out_dir, pretty=config.output.pretty_json)

        s = mapping_result.report.summary
        click.echo(f"  Carrier: {canonical.document.carrier_detected} ({canonical.document.carrier_confidence:.2f})")
        click.echo(f"  Line items: {len(canonical.line_items)}")
        click.echo(f"  Extraction status: {canonical.document.extraction_status.value}")
        click.echo(
            f"  Mapping: mapped={s.mapped} partial={s.partially_mapped} review={s.needs_review} "
            f"unmapped={s.unmapped} unresolved_coverage={s.unresolved_coverage} avg_conf={s.average_confidence}"
        )
        click.echo(f"  Output: {doc_out_dir}/")
        click.echo()

        if canonical.document.extraction_status == ExtractionStatus.FAILED:
            worst_exit = max(worst_exit, 2)
        elif (
            canonical.document.extraction_status == ExtractionStatus.NEEDS_REVIEW
            or mapping_result.report.status == "needs_review"
        ):
            worst_exit = max(worst_exit, 1)

    sys.exit(worst_exit)


@cli.command()
@click.option(
    "--projects-dir",
    default="projects",
    type=click.Path(path_type=Path),
    help="Directory to store local review projects in (created if missing).",
)
@click.option("--port", default=8501, type=int, help="Local port for the Streamlit server.")
def ui(projects_dir: Path, port: int) -> None:
    """Launch the local review UI (Streamlit). Binds to 127.0.0.1 only -- never 0.0.0.0."""
    import subprocess

    app_path = Path(__file__).resolve().parent / "ui" / "app.py"
    resolved_projects_dir = Path(projects_dir).expanduser().resolve()
    resolved_projects_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(app_path),
        "--server.address",
        "127.0.0.1",
        "--server.port",
        str(port),
        "--browser.gatherUsageStats",
        "false",
        "--",
        "--projects-dir",
        str(resolved_projects_dir),
    ]
    try:
        subprocess.run(cmd, check=True)
    except FileNotFoundError:
        click.echo("Streamlit is not installed. Install it with: pip install -r requirements-ui.txt", err=True)
        sys.exit(4)
    except subprocess.CalledProcessError as exc:
        sys.exit(exc.returncode or 1)


@cli.group()
def catalog() -> None:
    """Manage the verified Xactimate catalog (config/verified_xactimate_catalog.yaml).

    Ordinary review workflows should use the local review UI's "Verified
    Catalog" tab; these commands are for inspection, scripting, and
    recovery so nobody has to hand-edit the YAML file directly.
    """


@catalog.command(name="list")
@click.option("--status", "status_filter", default=None, help="Filter by verification_status.")
@click.option("--category", "category_filter", default=None, help="Filter by Xactimate category.")
@click.option("--catalog-path", default=None, type=click.Path(path_type=Path), help="Override the catalog file path.")
def catalog_list(status_filter: str | None, category_filter: str | None, catalog_path: Path | None) -> None:
    """List verified catalog records."""
    from estimate_extractor.ui.verified_catalog_service import load_verified_catalog

    path = catalog_path or _DEFAULT_VERIFIED_CATALOG_PATH
    records = load_verified_catalog(path)
    if status_filter:
        records = [r for r in records if r.verification_status == status_filter]
    if category_filter:
        records = [r for r in records if r.category == category_filter]

    if not records:
        click.echo("No matching catalog records.")
        return
    click.echo(f"{'Category':<10} {'Selector':<20} {'Unit':<6} {'Status':<22} {'Description'}")
    for r in records:
        click.echo(f"{r.category:<10} {r.selector:<20} {r.unit:<6} {r.verification_status:<22} {r.description}")
    click.echo(f"\n{len(records)} record(s).")


@catalog.command()
@click.argument("query")
@click.option("--catalog-path", default=None, type=click.Path(path_type=Path))
def search(query: str, catalog_path: Path | None) -> None:
    """Search the verified catalog by description, alias, selector, or category."""
    from estimate_extractor.ui.verified_catalog_service import load_verified_catalog

    path = catalog_path or _DEFAULT_VERIFIED_CATALOG_PATH
    records = load_verified_catalog(path)
    q = query.lower()
    matches = [
        r
        for r in records
        if q in r.description.lower()
        or q in r.selector.lower()
        or q in r.category.lower()
        or any(q in a.lower() for a in r.aliases)
    ]
    if not matches:
        click.echo(f"No catalog records match {query!r}.")
        return
    for r in matches:
        click.echo(f"[{r.verification_status}] {r.category}/{r.selector} -- {r.description} ({r.unit})")
    click.echo(f"\n{len(matches)} match(es).")


@catalog.command()
@click.option("--catalog-path", default=None, type=click.Path(path_type=Path))
def validate(catalog_path: Path | None) -> None:
    """Validate the verified catalog file for structural/vocabulary errors."""
    from estimate_extractor.ui.verified_catalog_service import (
        VALID_VERIFICATION_STATUSES,
        VerifiedCatalogError,
        load_verified_catalog,
    )
    from estimate_extractor.ui.catalog_service import ALLOWED_UNITS

    path = catalog_path or _DEFAULT_VERIFIED_CATALOG_PATH
    try:
        records = load_verified_catalog(path)
    except VerifiedCatalogError as exc:
        click.echo(f"Catalog validation FAILED: {exc}", err=True)
        sys.exit(2)

    errors: list[str] = []
    for r in records:
        if r.verification_status not in VALID_VERIFICATION_STATUSES:
            errors.append(f"{r.category}/{r.selector}: unknown verification_status {r.verification_status!r}")
        if r.unit.upper() not in ALLOWED_UNITS:
            errors.append(f"{r.category}/{r.selector}: unsupported unit {r.unit!r}")
        if r.verification_status == "human_verified" and not (r.verified_by and r.verified_at):
            errors.append(f"{r.category}/{r.selector}: human_verified but missing verified_by/verified_at")

    if errors:
        click.echo(f"Catalog validation FAILED ({len(errors)} issue(s)):", err=True)
        for e in errors:
            click.echo(f"  - {e}", err=True)
        sys.exit(2)
    click.echo(f"Catalog validation OK: {len(records)} record(s), {path}")
    sys.exit(0)


@catalog.command()
@click.option("--catalog-path", default=None, type=click.Path(path_type=Path))
def stats(catalog_path: Path | None) -> None:
    """Print aggregate counts for the verified catalog (by status/category/trade)."""
    from estimate_extractor.ui.verified_catalog_service import load_verified_catalog

    path = catalog_path or _DEFAULT_VERIFIED_CATALOG_PATH
    records = load_verified_catalog(path)

    by_status: dict[str, int] = {}
    by_category: dict[str, int] = {}
    by_trade: dict[str, int] = {}
    for r in records:
        by_status[r.verification_status] = by_status.get(r.verification_status, 0) + 1
        by_category[r.category] = by_category.get(r.category, 0) + 1
        by_trade[r.trade] = by_trade.get(r.trade, 0) + 1

    click.echo(f"Total records: {len(records)}")
    click.echo("By verification status:")
    for k, v in sorted(by_status.items()):
        click.echo(f"  {k}: {v}")
    click.echo("By category:")
    for k, v in sorted(by_category.items()):
        click.echo(f"  {k}: {v}")
    click.echo("By trade:")
    for k, v in sorted(by_trade.items()):
        click.echo(f"  {k}: {v}")


@catalog.command(name="export-review")
@click.option("--output", "output_path", default="config/verified_catalog_review.csv", type=click.Path(path_type=Path))
@click.option("--catalog-path", default=None, type=click.Path(path_type=Path))
def export_review(output_path: Path, catalog_path: Path | None) -> None:
    """Export the verified catalog as a CSV for offline review."""
    import csv

    from estimate_extractor.ui.verified_catalog_service import load_verified_catalog

    path = catalog_path or _DEFAULT_VERIFIED_CATALOG_PATH
    records = load_verified_catalog(path)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            ["category", "selector", "description", "unit", "activity_raw", "trade", "component", "verification_status", "verified_by", "verified_at", "price_lists_observed"]
        )
        for r in records:
            writer.writerow(
                [
                    r.category,
                    r.selector,
                    r.description,
                    r.unit,
                    r.activity_raw,
                    r.trade,
                    r.component,
                    r.verification_status,
                    r.verified_by,
                    r.verified_at,
                    ";".join(sorted({o.price_list for o in r.price_observations})),
                ]
            )
    click.echo(f"Exported {len(records)} record(s) to {output_path}")


@catalog.command(name="restore-latest")
@click.option("--catalog-path", default=None, type=click.Path(path_type=Path))
@click.option("--backups-dir", default=None, type=click.Path(path_type=Path))
def restore_latest(catalog_path: Path | None, backups_dir: Path | None) -> None:
    """Restore the verified catalog from its most recent backup."""
    from estimate_extractor.ui.verified_catalog_service import VerifiedCatalogError, restore_last_verified_catalog_backup

    path = catalog_path or _DEFAULT_VERIFIED_CATALOG_PATH
    backups = backups_dir or _DEFAULT_BACKUPS_DIR
    try:
        restored_from = restore_last_verified_catalog_backup(path, backups)
    except VerifiedCatalogError as exc:
        click.echo(f"Restore failed: {exc}", err=True)
        sys.exit(2)
    click.echo(f"Restored {path} from {restored_from}")


_DEFAULT_REFERENCE_DIR = _DEFAULT_CONFIG_DIR.parent / "fixtures" / "reference"
_DEFAULT_EXTRACTED_DIR = _DEFAULT_REFERENCE_DIR / "extracted"
_DEFAULT_SELECTOR_DATA_DIR = _DEFAULT_REFERENCE_DIR / "data"
_DEFAULT_SELECTOR_REPORTS_DIR = _DEFAULT_REFERENCE_DIR / "reports"
_DEFAULT_SELECTOR_DB_PATH = _DEFAULT_SELECTOR_DATA_DIR / "master_selectors.db"
_DEFAULT_PROJECTS_DIR = _DEFAULT_CONFIG_DIR.parent / "projects"


@cli.group()
def selectors() -> None:
    """Build and query the canonical Xactimate selector catalog
    (Category, Selector, Description) from reviewer-supplied selector-
    browser screenshots. See docs/selector-catalog.md.
    """


@selectors.command(name="import")
@click.argument("source_path", type=click.Path(exists=True, path_type=Path))
@click.option("--force", is_flag=True, default=False, help="Reprocess every screenshot, ignoring the existing manifest/cache.")
@click.option("--category", "category_filter", default=None, help="Only process one CAT folder (debugging aid).")
@click.option("--limit", type=int, default=None, help="Only process the first N screenshots (debugging aid).")
@click.option("--extract-to", type=click.Path(path_type=Path), default=None, help="Where to extract the ZIP (default fixtures/reference/extracted/).")
@click.option("--data-dir", type=click.Path(path_type=Path), default=None)
@click.option("--reports-dir", type=click.Path(path_type=Path), default=None)
def selectors_import(
    source_path: Path,
    force: bool,
    category_filter: str | None,
    limit: int | None,
    extract_to: Path | None,
    data_dir: Path | None,
    reports_dir: Path | None,
) -> None:
    """Import selector screenshots from a ZIP (or an already-extracted
    directory) into the canonical selector catalog. Resumable: without
    --force, a re-run skips screenshots already recorded as processed in
    screenshot_processing_manifest.json rather than re-OCRing them."""
    from estimate_extractor.selector_catalog.image_inventory import ImageInventoryError, find_screenshots_root
    from estimate_extractor.selector_catalog.pipeline import run_import, write_outputs

    extracted_root = extract_to or _DEFAULT_EXTRACTED_DIR
    data_dir = data_dir or _DEFAULT_SELECTOR_DATA_DIR
    reports_dir = reports_dir or _DEFAULT_SELECTOR_REPORTS_DIR

    if source_path.suffix.lower() == ".zip":
        try:
            find_screenshots_root(extracted_root)
            click.echo(f"Already extracted at {extracted_root} (use a fresh --extract-to to force re-extraction).")
        except ImageInventoryError:
            click.echo(f"Extracting {source_path} -> {extracted_root} ...")
            import zipfile

            extracted_root.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(source_path) as zf:
                zf.extractall(extracted_root)
    else:
        extracted_root = source_path

    def _progress(i: int, n: int, relative_path: str) -> None:
        if i == 1 or i % 25 == 0 or i == n:
            click.echo(f"  [{i}/{n}] {relative_path}")

    result = run_import(
        extracted_root, data_dir, force=force, category_filter=category_filter, limit=limit, progress_callback=_progress
    )
    paths = write_outputs(result, data_dir, reports_dir)

    click.echo()
    click.echo(f"Total screenshots: {result.total_screenshots}")
    click.echo(f"  Processed: {result.processed}  From cache: {result.from_cache}  Skipped: {result.skipped}  Failed: {result.failed}")
    click.echo(f"Unique selector records: {len(result.records)}")
    click.echo(f"Records needing review: {sum(1 for r in result.records if r.needs_review)}")
    click.echo(f"Conflicting-description groups: {len(result.conflicts)}")
    click.echo(f"Review queue rows: {len(result.review_queue)}")
    click.echo(f"Validation: {'PASSED' if result.validation.passed else 'FAILED'}")
    for name, path in paths.items():
        click.echo(f"  {name}: {path}")

    if not result.validation.passed:
        sys.exit(2)
    sys.exit(0)


@selectors.command(name="cleanup")
@click.option("--data-dir", type=click.Path(path_type=Path), default=None)
@click.option("--extracted-root", type=click.Path(path_type=Path), default=None)
@click.option("--backups-dir", type=click.Path(path_type=Path), default=None)
def selectors_cleanup(data_dir: Path | None, extracted_root: Path | None, backups_dir: Path | None) -> None:
    """Targeted QA cleanup pass: fixes Unit-column description
    contamination (coordinate re-verification, never string stripping),
    flags/resolves malformed-shaped selectors, and recomputes category-
    mismatch flags using cross-screenshot corroboration instead of a
    single noisy title-bar read. Backs up the database first. See
    docs/selector-catalog.md "QA cleanup"."""
    from estimate_extractor.selector_catalog.cleanup import run_full_cleanup

    resolved_data_dir = data_dir or _DEFAULT_SELECTOR_DATA_DIR
    resolved_extracted_root = extracted_root or _DEFAULT_EXTRACTED_DIR
    resolved_backups_dir = backups_dir or _DEFAULT_BACKUPS_DIR

    db_path = resolved_data_dir / "master_selectors.db"
    if not db_path.exists():
        click.echo(f"No selector database found at {db_path}. Run `selectors import` first.", err=True)
        sys.exit(4)

    report = run_full_cleanup(resolved_data_dir, resolved_extracted_root, resolved_backups_dir)

    click.echo(f"Backup written: {report.backup_path}")
    click.echo(f"Records inspected: {report.records_inspected}")
    click.echo()
    click.echo("Unit-column contamination:")
    click.echo(f"  Candidates (heuristic pre-filter): {report.unit_contamination_candidates}")
    click.echo(f"  Descriptions corrected (coordinate-confirmed): {report.descriptions_corrected}")
    click.echo(f"  Confirmed legitimate (no change): {report.unit_contamination_confirmed_legitimate}")
    click.echo(f"  Unresolved (flagged for review): {report.unit_contamination_unresolved}")
    click.echo()
    click.echo("Malformed selectors:")
    click.echo(f"  Total malformed-shaped: {report.malformed_selectors_total}")
    click.echo(f"  Resolved from overlapping screenshots: {report.malformed_resolved_from_overlap}")
    click.echo(f"  Still awaiting review: {report.malformed_still_review}")
    click.echo()
    click.echo("Category mismatches:")
    click.echo(f"  Before corroboration filter: {report.category_mismatches_before}")
    click.echo(f"  After corroboration filter: {report.category_mismatches_after}")
    click.echo()
    click.echo(f"Final record count: {report.final_record_count}")
    click.echo(f"Final needs-review count: {report.final_needs_review_count}")
    sys.exit(0)


@selectors.command(name="validate")
@click.option("--db-path", type=click.Path(path_type=Path), default=None)
@click.option("--extracted-root", type=click.Path(path_type=Path), default=None)
def selectors_validate(db_path: Path | None, extracted_root: Path | None) -> None:
    """Validate the persisted selector catalog against the completion
    invariants (no empty keys, no duplicates, full provenance, full
    screenshot-manifest coverage)."""
    from estimate_extractor.selector_catalog import database, exporter
    from estimate_extractor.selector_catalog.image_inventory import ImageInventoryError, inventory_screenshots
    from estimate_extractor.selector_catalog.validator import validate_catalog

    path = db_path or _DEFAULT_SELECTOR_DB_PATH
    if not path.exists():
        click.echo(f"No selector database found at {path}. Run `selectors import` first.", err=True)
        sys.exit(4)

    conn = database.create_database(path)
    records = database.load_all_records(conn)
    conn.close()

    manifest_dir = db_path.parent if db_path else _DEFAULT_SELECTOR_DATA_DIR
    manifest_entries = exporter.load_manifest(manifest_dir / "screenshot_processing_manifest.json")

    try:
        refs = inventory_screenshots(extracted_root or _DEFAULT_EXTRACTED_DIR)
        all_relative_paths = {r.relative_path for r in refs}
    except ImageInventoryError:
        all_relative_paths = {e.relative_path for e in manifest_entries}

    report = validate_catalog(records, manifest_entries, all_relative_paths)
    click.echo(f"Records: {len(records)}")
    click.echo(f"Validation: {'PASSED' if report.passed else 'FAILED'} ({len(report.errors)} errors, {len(report.warnings)} warnings)")
    for issue in report.errors:
        click.echo(f"  ERROR [{issue.code}] {issue.message}")
    for issue in report.warnings:
        click.echo(f"  WARN  [{issue.code}] {issue.message}")
    sys.exit(0 if report.passed else 2)


@selectors.command(name="stats")
@click.option("--db-path", type=click.Path(path_type=Path), default=None)
def selectors_stats(db_path: Path | None) -> None:
    """Print aggregate counts for the selector catalog."""
    from estimate_extractor.selector_catalog import database

    path = db_path or _DEFAULT_SELECTOR_DB_PATH
    if not path.exists():
        click.echo(f"No selector database found at {path}. Run `selectors import` first.", err=True)
        sys.exit(4)
    conn = database.create_database(path)
    records = database.load_all_records(conn)
    conn.close()

    by_category: dict[str, int] = {}
    needs_review_count = 0
    for r in records:
        by_category[r.category] = by_category.get(r.category, 0) + 1
        if r.needs_review:
            needs_review_count += 1

    click.echo(f"Total records: {len(records)}")
    click.echo(f"Needs review: {needs_review_count}")
    click.echo(f"Categories: {len(by_category)}")
    for cat, count in sorted(by_category.items()):
        click.echo(f"  {cat}: {count}")


@selectors.command(name="search")
@click.argument("query")
@click.option("--category", "category_filter", default=None)
@click.option("--fuzzy", is_flag=True, default=False, help="Rank by description similarity instead of substring match.")
@click.option(
    "--include-uncertain", is_flag=True, default=False,
    help="Also show needs_review=1 records (visibly labeled), for manual review of otherwise-excluded matches.",
)
@click.option("--db-path", type=click.Path(path_type=Path), default=None)
def selectors_search(query: str, category_filter: str | None, fuzzy: bool, include_uncertain: bool, db_path: Path | None) -> None:
    """Search the selector catalog: substring match by default, or
    similarity-ranked with --fuzzy. Eligible (needs_review=0) records only
    by default -- pass --include-uncertain to also see flagged records.
    Read-only -- never modifies records. Backs the manual selector
    browser used alongside `selectors recommend`."""
    from estimate_extractor.selector_catalog import database
    from estimate_extractor.selector_recommendation import query as recommendation_query

    path = db_path or _DEFAULT_SELECTOR_DB_PATH
    if not path.exists():
        click.echo(f"No selector database found at {path}. Run `selectors import` first.", err=True)
        sys.exit(4)
    conn = database.create_database(path)

    if fuzzy:
        results = recommendation_query.manual_search(
            conn, text=query, category=category_filter, include_uncertain=include_uncertain, fuzzy=True
        )
        conn.close()
        for record in results:
            flag = " [NEEDS REVIEW]" if record.needs_review else ""
            click.echo(f"{record.category}/{record.selector} -- {record.description_original}{flag}")
    else:
        results = recommendation_query.manual_search(
            conn, text=query, category=category_filter, include_uncertain=include_uncertain, limit=10_000
        )
        conn.close()
        for record in results:
            flag = " [NEEDS REVIEW]" if record.needs_review else ""
            click.echo(f"{record.category}/{record.selector} -- {record.description_original}{flag}")
        click.echo(f"\n{len(results)} match(es).")


@selectors.command(name="export-csv")
@click.option("--db-path", type=click.Path(path_type=Path), default=None)
@click.option("--output", type=click.Path(path_type=Path), default=None)
def export_csv(db_path: Path | None, output: Path | None) -> None:
    """Re-export master_selectors.csv from the current database state."""
    from estimate_extractor.selector_catalog import database, exporter

    path = db_path or _DEFAULT_SELECTOR_DB_PATH
    if not path.exists():
        click.echo(f"No selector database found at {path}. Run `selectors import` first.", err=True)
        sys.exit(4)
    conn = database.create_database(path)
    records = database.load_all_records(conn)
    conn.close()

    out_path = output or (_DEFAULT_SELECTOR_DATA_DIR / "master_selectors.csv")
    exporter.write_master_selectors_csv(records, out_path)
    click.echo(f"Exported {len(records)} record(s) to {out_path}")


@selectors.command(name="review-queue")
@click.option("--path", "queue_path", type=click.Path(path_type=Path), default=None)
def review_queue(queue_path: Path | None) -> None:
    """Print a summary of the current selector_review_queue.csv."""
    import csv as csv_module

    path = queue_path or (_DEFAULT_SELECTOR_DATA_DIR / "selector_review_queue.csv")
    if not path.exists():
        click.echo(f"No review queue found at {path}. Run `selectors import` first.", err=True)
        sys.exit(4)
    with path.open(newline="", encoding="utf-8") as f:
        rows = list(csv_module.DictReader(f))
    click.echo(f"Review queue: {len(rows)} row(s)")
    by_reason: dict[str, int] = {}
    for row in rows:
        for reason in row["reason"].split(";"):
            by_reason[reason] = by_reason.get(reason, 0) + 1
    for reason, count in sorted(by_reason.items(), key=lambda kv: -kv[1]):
        click.echo(f"  {reason}: {count}")


@selectors.command(name="recommend")
@click.argument("mapped_estimate_path", type=click.Path(exists=True, path_type=Path))
@click.option("--top", type=int, default=None, help="Maximum candidates to show per item (default: from config).")
@click.option(
    "--include-uncertain", is_flag=True, default=False,
    help="Also surface needs_review selector-catalog records, always visibly labeled and never auto-applied.",
)
@click.option("--db-path", type=click.Path(path_type=Path), default=None)
@click.option("--verified-catalog-path", type=click.Path(path_type=Path), default=None)
@click.option("--write/--no-write", default=True, help="Write projects/<project>/review/selector_recommendations.json.")
def selectors_recommend(
    mapped_estimate_path: Path,
    top: int | None,
    include_uncertain: bool,
    db_path: Path | None,
    verified_catalog_path: Path | None,
    write: bool,
) -> None:
    """Recommend candidate Xactimate CAT/SEL selectors for every line item
    in a project's mapped_estimate.json (Phase 3.7). Suggestions only --
    never writes to mapped_estimate.json, never approves anything. Eligible
    (needs_review=0) selector-catalog records only by default; pass
    --include-uncertain to also see clearly-labeled uncertain references."""
    from estimate_extractor.selector_recommendation import service

    project_dir = mapped_estimate_path.parent.parent
    resolved_db_path = db_path or _DEFAULT_SELECTOR_DB_PATH
    resolved_verified_path = verified_catalog_path or _DEFAULT_VERIFIED_CATALOG_PATH

    try:
        results = service.recommend_for_project(
            project_dir,
            resolved_db_path,
            verified_catalog_path=resolved_verified_path,
            include_uncertain=include_uncertain,
            top=top,
        )
    except service.RecommendationServiceError as exc:
        click.echo(str(exc), err=True)
        sys.exit(4)

    for r in results:
        locked_flag = " [LOCKED: already approved]" if r.locked_by_approval else ""
        click.echo(f"{r.line_item_id}  state={r.state}{locked_flag}")
        if not r.candidates:
            click.echo("  (no defensible candidate)")
        for c in r.candidates:
            uncertain_flag = " [UNCERTAIN]" if c.source_needs_review else ""
            verified_flag = " [VERIFIED]" if c.source == "verified_catalog" else ""
            placeholder_flag = " [PLACEHOLDER]" if c.source == "placeholder_mapping" else ""
            click.echo(
                f"  #{c.rank} {c.category}/{c.selector} (score={c.score:.2f}){verified_flag}{uncertain_flag}{placeholder_flag} -- {c.description}"
            )
            for reason in c.match_reasons:
                click.echo(f"      + {reason}")
            for penalty in c.penalties:
                click.echo(f"      - {penalty}")

    if write:
        out_path = service.write_recommendations(project_dir, results)
        click.echo(f"\nWrote {out_path}")
    sys.exit(0)


@selectors.command(name="evaluate")
@click.option("--projects-dir", type=click.Path(path_type=Path), default=None)
@click.option("--db-path", type=click.Path(path_type=Path), default=None)
@click.option("--verified-catalog-path", type=click.Path(path_type=Path), default=None)
def selectors_evaluate(projects_dir: Path | None, db_path: Path | None, verified_catalog_path: Path | None) -> None:
    """Benchmark the recommendation system against every local project
    under `projects/`. Prints per-fixture candidate-state counts and, only
    where a project has real (non-synthetic) approved ground truth, top-1/
    top-3 agreement -- N/A otherwise. See build spec 'Do not treat
    synthetic Phase 3 benchmark approvals as genuine ground truth.'"""
    from estimate_extractor.selector_recommendation import service

    resolved_projects_dir = projects_dir or _DEFAULT_PROJECTS_DIR
    resolved_db_path = db_path or _DEFAULT_SELECTOR_DB_PATH
    resolved_verified_path = verified_catalog_path or _DEFAULT_VERIFIED_CATALOG_PATH

    if not resolved_db_path.exists():
        click.echo(f"No selector database found at {resolved_db_path}. Run `selectors import` first.", err=True)
        sys.exit(4)
    if not resolved_projects_dir.exists():
        click.echo(f"No projects directory found at {resolved_projects_dir}.", err=True)
        sys.exit(4)

    project_dirs = sorted(
        p for p in resolved_projects_dir.iterdir() if p.is_dir() and (p / "mapping" / "mapped_estimate.json").exists()
    )
    if not project_dirs:
        click.echo(f"No processed projects found under {resolved_projects_dir}.", err=True)
        sys.exit(4)

    click.echo(
        f"{'Fixture':<32} {'Items':>6} {'Cand':>5} {'Strong':>7} {'Poss':>6} {'Weak':>5} {'None':>5} "
        f"{'Top1':>8} {'Top3':>8} {'AvgCand':>8} {'AvgMs':>8}"
    )
    for project_dir in project_dirs:
        ev = service.evaluate_project(project_dir, resolved_db_path, verified_catalog_path=resolved_verified_path)
        top1 = f"{ev.top1_agreement:.2f}" if ev.top1_agreement is not None else "N/A"
        top3 = f"{ev.top3_agreement:.2f}" if ev.top3_agreement is not None else "N/A"
        click.echo(
            f"{ev.fixture:<32} {ev.items_evaluated:>6} {ev.items_with_candidates:>5} {ev.strong_candidates:>7} "
            f"{ev.possible_candidates:>6} {ev.weak_candidates:>5} {ev.no_candidates:>5} {top1:>8} {top3:>8} "
            f"{ev.average_candidates_per_item:>8.2f} {ev.average_latency_ms:>8.2f}"
        )
    sys.exit(0)


@selectors.command(name="recommendation-stats")
@click.argument("project_dir", type=click.Path(path_type=Path), required=False, default=None)
@click.option("--projects-dir", type=click.Path(path_type=Path), default=None)
def selectors_recommendation_stats(project_dir: Path | None, projects_dir: Path | None) -> None:
    """Print aggregate stats for previously generated recommendations
    (`selectors recommend`) and recorded reviewer feedback events, for one
    project (positional arg) or every local project with recommendations
    on disk."""
    from estimate_extractor.selector_recommendation import service

    if project_dir is not None:
        targets = [project_dir]
    else:
        root = projects_dir or _DEFAULT_PROJECTS_DIR
        targets = sorted(
            p for p in root.iterdir() if p.is_dir() and (p / "review" / "selector_recommendations.json").exists()
        ) if root.exists() else []
        if not targets:
            click.echo(f"No selector_recommendations.json found under {root}. Run `selectors recommend` first.", err=True)
            sys.exit(4)

    for target in targets:
        stats = service.compute_recommendation_stats(target)
        click.echo(f"{target.name}:")
        click.echo(f"  items_with_recommendations: {stats['items_with_recommendations']}")
        for state, count in sorted(stats["by_state"].items()):
            click.echo(f"    {state}: {count}")
        click.echo(f"  average_candidates_per_item: {stats['average_candidates_per_item']}")
        click.echo(f"  feedback_events: {stats['feedback_events']}")
        for action, count in sorted(stats["events_by_action"].items()):
            click.echo(f"    {action}: {count}")
    sys.exit(0)


def main() -> None:
    try:
        cli()
    except click.UsageError as exc:
        click.echo(str(exc), err=True)
        sys.exit(3)


if __name__ == "__main__":
    main()
