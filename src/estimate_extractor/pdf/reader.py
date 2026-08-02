"""Layer 1: native PDF text/word extraction via PyMuPDF, with an optional
OCR fallback (Layer 3) for pages with little or no native text.
"""

from __future__ import annotations

import hashlib
import logging
import tempfile
from pathlib import Path

import fitz  # PyMuPDF

from estimate_extractor.errors import EncryptedPDFError, UnsupportedPDFError
from estimate_extractor.models.page import Line, ParsedDocument, ParsedPage
from estimate_extractor.pdf.layout import build_lines, find_grid_annotation_texts
from estimate_extractor.pdf.ocr import OCREngine, render_page_to_image

logger = logging.getLogger(__name__)


def compute_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def open_document(path: Path) -> fitz.Document:
    if not path.exists():
        raise UnsupportedPDFError(f"File not found: {path}")
    try:
        doc = fitz.open(str(path))
    except Exception as exc:  # PyMuPDF raises its own exception types
        raise UnsupportedPDFError(f"Could not open PDF '{path.name}': {exc}") from exc
    if doc.needs_pass:
        raise EncryptedPDFError(f"'{path.name}' is password-protected/encrypted.")
    return doc


def read_pdf(
    path: Path,
    *,
    enable_ocr: bool = False,
    ocr_engine: OCREngine | None = None,
    min_native_text_characters: int = 50,
    ocr_dpi: int = 300,
) -> ParsedDocument:
    """Read a PDF into a ParsedDocument.

    For each page: extract native text and positional lines. If native text
    is shorter than ``min_native_text_characters`` and ``enable_ocr`` is
    True, fall back to OCR for that page only; the resulting text replaces
    the (empty/near-empty) native text and the page is marked
    ``source="ocr"``.
    """
    sha256 = compute_sha256(path)
    doc = open_document(path)
    pages: list[ParsedPage] = []

    ocr_tmp_dir: Path | None = None
    try:
        for i, page in enumerate(doc):
            page_number = i + 1
            raw_text = page.get_text("text")
            lines: list[Line] = build_lines(page)
            grid_annotation_texts = find_grid_annotation_texts(page)
            source = "native"

            if enable_ocr and len(raw_text.strip()) < min_native_text_characters:
                if ocr_engine is None:
                    from estimate_extractor.pdf.ocr import TesseractOCREngine

                    ocr_engine = TesseractOCREngine()
                if ocr_tmp_dir is None:
                    ocr_tmp_dir = Path(tempfile.mkdtemp(prefix="estimate_extractor_ocr_"))
                image_path = ocr_tmp_dir / f"page_{page_number:03d}.png"
                render_page_to_image(page, ocr_dpi, image_path)
                result = ocr_engine.extract_page(image_path)
                if result.text.strip():
                    raw_text = result.text
                    source = "ocr"
                    logger.info(
                        "OCR fallback used for %s page %d (native text: %d chars, "
                        "OCR confidence: %s)",
                        path.name,
                        page_number,
                        len(page.get_text("text").strip()),
                        f"{result.mean_confidence:.2f}" if result.mean_confidence is not None else "n/a",
                    )

            pages.append(
                ParsedPage(
                    page_number=page_number,
                    width=page.rect.width,
                    height=page.rect.height,
                    raw_text=raw_text,
                    lines=lines,
                    source=source,
                    grid_annotation_texts=grid_annotation_texts if source == "native" else frozenset(),
                )
            )
    finally:
        doc.close()

    return ParsedDocument(source_path=path, sha256=sha256, page_count=len(pages), pages=pages)
