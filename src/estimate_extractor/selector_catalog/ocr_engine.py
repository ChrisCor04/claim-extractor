"""Word-level OCR engine for selector-screenshot table parsing.

Distinct from ``estimate_extractor.pdf.ocr.OCREngine`` (which returns flat
page text for the Layer-3 PDF OCR fallback) -- table row/column parsing
needs per-word pixel bounding boxes, a fundamentally different data shape,
so this module defines its own narrow Protocol rather than forcing an
incompatible interface onto the existing one. It follows the same
conventions as ``pdf/ocr.py``: a Protocol for swappability (tests inject a
fake engine), and the same ``OCRDependencyMissingError`` for a consistent,
actionable error message.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from estimate_extractor.errors import OCRDependencyMissingError


@dataclass(frozen=True, slots=True)
class OCRWord:
    text: str
    left: int
    top: int
    width: int
    height: int
    confidence: float | None  # 0.0-1.0, None if unavailable
    block_num: int
    par_num: int
    line_num: int
    word_num: int

    @property
    def right(self) -> int:
        return self.left + self.width

    @property
    def bottom(self) -> int:
        return self.top + self.height

    @property
    def center_x(self) -> float:
        return self.left + self.width / 2.0

    @property
    def center_y(self) -> float:
        return self.top + self.height / 2.0

    @property
    def line_key(self) -> tuple[int, int, int]:
        return (self.block_num, self.par_num, self.line_num)


class WordBoxOCREngine(Protocol):
    def extract_words(self, image_path: Path) -> list[OCRWord]: ...
    def extract_title_bar_text(self, image_path: Path) -> str: ...


class TesseractWordBoxEngine:
    """Local Tesseract OCR via ``pytesseract.image_to_data``, returning one
    ``OCRWord`` per recognized word with its pixel bounding box (in
    *original*-image pixel space).

    Xactimate selector-browser screenshots pack an entire data table into
    a fairly small dialog (row height as low as ~18-20px), which is well
    below Tesseract's sweet spot. Measured on a real sample screenshot,
    upscaling 3x (LANCZOS) with ``--psm 6`` (treat the dialog as one
    uniform text block, which it visually is) raised mean word confidence
    from ~50-60 to ~90+ with no configuration beyond that -- so this is
    applied unconditionally rather than left as an untuned default.
    """

    UPSCALE_FACTOR = 3
    TESSERACT_CONFIG = "--psm 6"

    def __init__(self) -> None:
        try:
            import pytesseract  # noqa: F401
            from PIL import Image  # noqa: F401
        except ImportError as exc:
            raise OCRDependencyMissingError(
                "Selector-catalog OCR requires pytesseract/Pillow. Install with "
                "`pip install -r requirements-dev.txt` (or `pip install .[ocr]`) "
                "and ensure the `tesseract` binary is on PATH. See README "
                "'Optional OCR setup'."
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

    def extract_words(self, image_path: Path) -> list[OCRWord]:
        image = self._Image.open(image_path).convert("RGB")
        scale = self.UPSCALE_FACTOR
        upscaled = image.resize((image.width * scale, image.height * scale), self._Image.LANCZOS)

        data = self._pytesseract.image_to_data(
            upscaled, output_type=self._pytesseract.Output.DICT, config=self.TESSERACT_CONFIG
        )
        words: list[OCRWord] = []
        n = len(data.get("text", []))
        for i in range(n):
            text = data["text"][i]
            if not text or not text.strip():
                continue
            try:
                conf_raw = float(data["conf"][i])
            except (TypeError, ValueError):
                conf_raw = -1.0
            confidence = conf_raw / 100.0 if conf_raw >= 0 else None
            words.append(
                OCRWord(
                    text=text,
                    left=round(int(data["left"][i]) / scale),
                    top=round(int(data["top"][i]) / scale),
                    width=round(int(data["width"][i]) / scale),
                    height=round(int(data["height"][i]) / scale),
                    confidence=confidence,
                    block_num=int(data["block_num"][i]),
                    par_num=int(data["par_num"][i]),
                    line_num=int(data["line_num"][i]),
                    word_num=int(data["word_num"][i]),
                )
            )
        return words

    def extract_title_bar_text(self, image_path: Path, band_height: int = 40) -> str:
        """OCRs just the top title-bar strip of the screenshot.

        Xactimate renders an *unfocused* dialog's title bar in a lighter
        gray than a focused one's; measured on a real unfocused-window
        screenshot, plain upscaling left it unreadable, but a tight
        grayscale threshold (pixel < 220 -> black) after a 4x upscale
        recovered it perfectly (mean word confidence irrelevant here --
        this returns a plain string for a single regex-parse pass) --
        this dedicated pass is used unconditionally for title-bar
        detection since it also works fine for focused-window (dark
        text) screenshots.
        """
        image = self._Image.open(image_path).convert("L")
        band = image.crop((0, 0, image.width, min(band_height, image.height)))
        scale = 4
        band = band.resize((band.width * scale, band.height * scale), self._Image.LANCZOS)
        thresholded = band.point(lambda p: 0 if p < 220 else 255)
        return self._pytesseract.image_to_string(thresholded, config="--psm 6").strip()
