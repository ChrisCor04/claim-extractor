"""Writers for canonical_estimate.json, document_pages.json, raw_text/, and
debug/ -- see estimate_extractor.output's package docstring."""

from __future__ import annotations

import json
from pathlib import Path

from estimate_extractor.models.canonical import CanonicalEstimate
from estimate_extractor.models.page import ParsedDocument, PageRecord
from estimate_extractor.normalization.redact import redact_text


def _dump(data, pretty: bool) -> str:
    return json.dumps(data, indent=2 if pretty else None, default=str)


def write_canonical_estimate(canonical: CanonicalEstimate, path: Path, pretty: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_dump(canonical.model_dump(mode="json"), pretty), encoding="utf-8")


def write_document_pages(source_pages: list[PageRecord], path: Path, pretty: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = [record.model_dump(mode="json") for record in source_pages]
    path.write_text(_dump(data, pretty), encoding="utf-8")


def write_raw_text_pages(document: ParsedDocument, dir_path: Path) -> None:
    """One plain-text file per page: ``page_0001.txt``, ``page_0002.txt``, ..."""
    dir_path.mkdir(parents=True, exist_ok=True)
    for page in document.pages:
        (dir_path / f"page_{page.page_number:04d}.txt").write_text(page.raw_text, encoding="utf-8")


def write_debug_pages(
    document: ParsedDocument,
    source_pages: list[PageRecord],
    dir_path: Path,
    pretty: bool = True,
    redact: bool = False,
) -> None:
    """One JSON file per page merging the working ``ParsedPage`` (raw text,
    lines, OCR source) with its matching ``PageRecord`` (classification,
    confidence, reasons) -- everything needed to debug a single page's
    extraction without re-running the whole pipeline. Full raw text is only
    ever written here (and to ``raw_text/``), never at ordinary log level --
    ``redact=True`` strips emails/phones/zips from the text fields before
    writing, matching the same policy raw_text/ and logging follow."""
    dir_path.mkdir(parents=True, exist_ok=True)
    records_by_page = {record.page: record for record in source_pages}
    for page in document.pages:
        record = records_by_page.get(page.page_number)
        raw_text = redact_text(page.raw_text) if redact else page.raw_text
        data = {
            "page_number": page.page_number,
            "width": page.width,
            "height": page.height,
            "source": page.source,
            "char_count": page.char_count,
            "raw_text": raw_text,
            "lines": [
                {
                    "text": redact_text(line.text) if redact else line.text,
                    "x0": line.x0,
                    "y0": line.y0,
                    "x1": line.x1,
                    "y1": line.y1,
                }
                for line in page.lines
            ],
            "grid_annotation_texts": sorted(page.grid_annotation_texts),
            "classification": record.model_dump(mode="json") if record else None,
        }
        (dir_path / f"page_{page.page_number:04d}.json").write_text(_dump(data, pretty), encoding="utf-8")
