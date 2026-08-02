"""End-to-end single-document extraction pipeline: PDF path in, a fully
assembled CanonicalEstimate + ExtractionReport out. This is the one place
that wires together PDF reading, page classification, carrier detection,
adapter-driven extraction, and validation -- the CLI is a thin shell around
this module.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from estimate_extractor.adapters.allstate import AllstateAdapter
from estimate_extractor.adapters.base import CarrierAdapter
from estimate_extractor.adapters.farmers import FarmersAdapter
from estimate_extractor.adapters.generic import GenericXactimateStyleAdapter
from estimate_extractor.adapters.state_farm import StateFarmAdapter
from estimate_extractor.adapters.travelers import TravelersAdapter
from estimate_extractor.adapters.usaa import USAAAdapter
from estimate_extractor.classification.carrier import detect_carrier
from estimate_extractor.classification.pages import classify_document
from estimate_extractor.config import Config
from estimate_extractor.models.canonical import (
    CanonicalEstimate,
    DocumentMeta,
    ExtractionStatus,
    ValidationState,
)
from estimate_extractor.models.page import ParsedDocument
from estimate_extractor.models.validation import ExtractionReport
from estimate_extractor.parsing.coverage_attribution import attribute_coverages
from estimate_extractor.parsing.state_machine import IdFactory
from estimate_extractor.pdf.reader import read_pdf
from estimate_extractor.validation.engine import run_validation

__version__ = "0.1.0"


def build_adapter_registry() -> dict[str, CarrierAdapter]:
    return {
        "state_farm": StateFarmAdapter(),
        "travelers": TravelersAdapter(),
        "usaa": USAAAdapter(),
        "farmers": FarmersAdapter(),
        "allstate": AllstateAdapter(),
        "generic": GenericXactimateStyleAdapter(),
    }


def slugify(name: str) -> str:
    stem = Path(name).stem
    slug = re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")
    return slug or "document"


def _build_boilerplate_set(claim, contacts, carrier_display_name: str) -> set[str]:
    values: set[str] = {carrier_display_name.lower()}
    for field in (
        claim.claim_number,
        claim.estimate_number,
        claim.policy_number,
        claim.insured_name,
        claim.claimant_name,
        claim.price_list,
        claim.insurance_company,
        claim.member_number,
        claim.lr_number,
    ):
        if field and field.value:
            values.add(field.value.strip().lower())
        if field:
            for rv in field.raw_values:
                values.add(rv.strip().lower())
    for addr in (claim.property_address, claim.mailing_address):
        if addr:
            if addr.line1:
                values.add(addr.line1.strip().lower())
            for rv in addr.raw_values:
                for part in rv.split("|"):
                    values.add(part.strip().lower())
    for c in contacts:
        if c.name:
            values.add(c.name.strip().lower())
        if c.phone:
            values.add(c.phone.strip().lower())
        if c.company:
            values.add(c.company.strip().lower())
    values.discard("")
    return values


_GENERIC_BOILERPLATE = {
    "restoration/service/remodel",
    "all amounts payable are subject to the terms, conditions and",
    "limits of your policy.",
}


def _detect_repeated_header_lines(document: ParsedDocument, page_records) -> set[str]:
    """Any short line that repeats verbatim across most estimate-detail
    pages is almost certainly a repeated letterhead/header line (carrier
    name, insured name, "Page: N", etc.), not real section content. This
    generalizes across carriers without hand-listing each one's letterhead
    text.

    Column headers (QUANTITY, RCV, DESCRIPTION, ...) are deliberately
    excluded even though they repeat just as often: they are already
    recognized structurally as TokenKind.HEADER_KEYWORD, and the state
    machine's section-boundary lookahead relies on seeing that token to
    know a new section is starting -- if it were also in the boilerplate
    set, the lookahead would skip straight past it looking for
    "real" content and walk off the end of the page.
    """
    from estimate_extractor.models.page import ESTIMATE_DETAIL_CLASSIFICATIONS
    from estimate_extractor.pdf.tables import TokenKind, classify_line

    detail_pages = [pr for pr in page_records if pr.classification in ESTIMATE_DETAIL_CLASSIFICATIONS]
    if len(detail_pages) < 3:
        return set()

    counts: dict[str, int] = {}
    for pr in detail_pages:
        page = document.pages[pr.page - 1]
        seen_this_page: set[str] = set()
        for line in page.raw_text.splitlines():
            s = line.strip().lower()
            if not s or len(s) > 60 or s in seen_this_page:
                continue
            if classify_line(line).kind in (
                TokenKind.HEADER_KEYWORD,
                TokenKind.ITEM_START,
                TokenKind.TOTALS_LABEL,
                TokenKind.QTY_UNIT,
            ):
                continue
            counts[s] = counts.get(s, 0) + 1
            seen_this_page.add(s)

    # Require near-unanimous repetition (all but at most one page) so a
    # legitimately-repeating category heading like "Windows" (which might
    # appear on 2-3 of many pages) is never mistaken for a page header.
    threshold = max(3, len(detail_pages) - 1)
    return {s for s, c in counts.items() if c >= threshold}


@dataclass(slots=True)
class ExtractionResult:
    canonical: CanonicalEstimate
    report: ExtractionReport
    document: ParsedDocument


def run_extraction(
    pdf_path: Path,
    config: Config,
    *,
    enable_ocr: bool = False,
    carrier_override: str | None = None,
) -> ExtractionResult:
    document = read_pdf(
        pdf_path,
        enable_ocr=enable_ocr,
        min_native_text_characters=config.ocr.minimum_native_text_characters,
        ocr_dpi=config.ocr.dpi,
    )

    page_records = classify_document(document.pages)

    adapters = build_adapter_registry()
    if carrier_override and carrier_override in adapters:
        adapter = adapters[carrier_override]
        carrier_confidence = 1.0
        carrier_key = carrier_override
    else:
        match = detect_carrier(document, adapters, config.extractor.carrier_detection_threshold)
        adapter = adapters[match.key]
        carrier_confidence = match.confidence
        carrier_key = match.key

    id_factory = IdFactory()
    claim = adapter.extract_claim_metadata(document, page_records)
    contacts = adapter.extract_contacts(document, page_records, id_factory)
    coverages = adapter.extract_coverages(document, page_records, id_factory)

    boilerplate = (
        _build_boilerplate_set(claim, contacts, adapter.display_name)
        | _GENERIC_BOILERPLATE
        | _detect_repeated_header_lines(document, page_records)
    )

    body = adapter.extract_estimate_body(
        document,
        page_records,
        coverages,
        id_factory,
        boilerplate,
        low_confidence_threshold=config.validation.low_confidence_threshold,
    )

    attribution = attribute_coverages(
        coverages,
        body.areas,
        body.sections,
        body.line_items,
        body.summary_totals,
        tolerance=config.validation.money_absolute_tolerance,
    )

    estimate_pages = sorted(pr.page for pr in page_records if pr.include_in_estimate)
    excluded_pages = sorted(pr.page for pr in page_records if not pr.include_in_estimate)

    document_meta = DocumentMeta(
        source_filename=pdf_path.name,
        source_sha256=document.sha256,
        carrier_detected=adapter.display_name,
        carrier_confidence=carrier_confidence,
        document_type="property_estimate",
        page_count=document.page_count,
        estimate_page_numbers=estimate_pages,
        excluded_page_numbers=excluded_pages,
        extraction_status=ExtractionStatus.NEEDS_REVIEW,
        extractor_version=__version__,
    )

    canonical = CanonicalEstimate(
        document=document_meta,
        claim=claim,
        contacts=contacts,
        coverages=coverages,
        areas=body.areas,
        sections=body.sections,
        line_items=body.line_items,
        summary_totals=body.summary_totals,
        source_pages=page_records,
        validation_state=ValidationState(status=ExtractionStatus.NEEDS_REVIEW),
    )

    report = run_validation(
        canonical,
        page_records,
        config.validation,
        config.extractor.carrier_detection_threshold,
        coverage_attribution_notes=attribution.notes,
    )

    canonical.document.extraction_status = report.status
    canonical.validation_state = ValidationState(
        status=report.status,
        issue_count=len(report.issues),
        warning_count=report.summary.warnings,
        error_count=sum(1 for i in report.issues if i.severity.value == "error"),
        fatal_count=report.summary.fatal_errors,
        reconciled=report.reconciliation.within_tolerance,
    )

    return ExtractionResult(canonical=canonical, report=report, document=document)
