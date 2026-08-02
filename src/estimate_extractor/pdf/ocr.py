"""OCR fallback layer (Layer 3).

Disabled by default. Only invoked for pages whose native text extraction
produced fewer than ``minimum_native_text_characters`` characters (e.g.
scanned/image-only pages), and only when the caller passes
``--enable-ocr``. Uses local Tesseract via ``pytesseract`` -- no cloud
service, no API key, works offline on macOS and Windows as long as the
``tesseract`` binary is installed (see README "Optional OCR setup").

The engine is behind a ``Protocol`` so it can be swapped out later without
touching the rest of the pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from estimate_extractor.errors import OCRDependencyMissingError


@dataclass(frozen=True, slots=True)
class OCRPageResult:
    text: str
    mean_confidence: float | None  # 0.0-1.0, None if the engine can't report one


class OCREngine(Protocol):
    def extract_page(self, image_path: Path) -> OCRPageResult: ...


class TesseractOCREngine:
    """Local Tesseract OCR engine, invoked via pytesseract."""

    def __init__(self) -> None:
        try:
            import pytesseract  # noqa: F401
            from PIL import Image  # noqa: F401
        except ImportError as exc:
            raise OCRDependencyMissingError(
                "OCR was requested (--enable-ocr) but pytesseract/Pillow are not "
                "installed. Install with `pip install -r requirements-dev.txt` "
                "(or `pip install .[ocr]`) and ensure the `tesseract` binary is "
                "on PATH. See README 'Optional OCR setup'."
            ) from exc
        self._pytesseract = pytesseract
        self._Image = Image
        try:
            pytesseract.get_tesseract_version()
        except Exception as exc:  # pytesseract raises various error types
            raise OCRDependencyMissingError(
                "pytesseract is installed but the `tesseract` binary could not be "
                "invoked. Install Tesseract OCR for your OS and ensure it is on "
                "PATH, or set the TESSERACT_CMD environment variable. See README "
                "'Optional OCR setup'."
            ) from exc

    def extract_page(self, image_path: Path) -> OCRPageResult:
        image = self._Image.open(image_path)
        data = self._pytesseract.image_to_data(
            image, output_type=self._pytesseract.Output.DICT
        )
        words = []
        confidences = []
        for text, conf in zip(data.get("text", []), data.get("conf", [])):
            if text and text.strip():
                words.append(text)
                try:
                    c = float(conf)
                except (TypeError, ValueError):
                    c = -1.0
                if c >= 0:
                    confidences.append(c)
        text_out = " ".join(words)
        mean_conf = (sum(confidences) / len(confidences) / 100.0) if confidences else None
        return OCRPageResult(text=text_out, mean_confidence=mean_conf)


def render_page_to_image(page, dpi: int, out_path: Path) -> Path:
    """Render a PyMuPDF page to a PNG at the given DPI for OCR input."""
    import fitz  # local import: OCR is optional, keep fitz import scoped here too

    zoom = dpi / 72.0
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat)
    pix.save(str(out_path))
    return out_path
