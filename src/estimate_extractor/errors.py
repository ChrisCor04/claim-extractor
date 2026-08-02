"""Typed, explicit error hierarchy for the extraction pipeline.

A single malformed page should not necessarily abort extraction of an
entire document. Callers that can continue safely should catch the
page/line-item-scoped errors below, record an issue in the extraction
report, and keep going. Document-scoped errors (encrypted PDF, no
readable text at all) are fatal and abort the run for that document.
"""

from __future__ import annotations


class EstimateExtractorError(Exception):
    """Base class for all typed errors raised by estimate_extractor."""


class UnsupportedPDFError(EstimateExtractorError):
    """The file is not a PDF, or is a PDF version/structure we cannot open."""


class EncryptedPDFError(EstimateExtractorError):
    """The PDF is password-protected or otherwise encrypted."""


class NoReadableTextError(EstimateExtractorError):
    """No native text could be extracted from any page (document-wide)."""


class OCRDependencyMissingError(EstimateExtractorError):
    """OCR was requested but pytesseract/Pillow/tesseract are unavailable."""


class CarrierDetectionError(EstimateExtractorError):
    """Carrier detection could not run (e.g. no pages to inspect)."""


class PageClassificationError(EstimateExtractorError):
    """A page could not be classified. Scoped to a single page."""

    def __init__(self, message: str, page: int) -> None:
        super().__init__(message)
        self.page = page


class LineItemParseError(EstimateExtractorError):
    """A line item row could not be parsed. Scoped to a single page."""

    def __init__(self, message: str, page: int) -> None:
        super().__init__(message)
        self.page = page


class SchemaValidationError(EstimateExtractorError):
    """The assembled canonical document failed pydantic schema validation."""


class ReconciliationError(EstimateExtractorError):
    """Arithmetic reconciliation could not be attempted at all (config error)."""
