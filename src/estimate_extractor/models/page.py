"""Internal page/layout representations and the page-classification model.

The ``Word``/``Line``/``ParsedPage``/``ParsedDocument`` dataclasses are the
working representation produced by the PDF reading layer (``pdf/reader.py``,
``pdf/layout.py``). They are not part of the stable canonical output
contract, so they are plain dataclasses rather than pydantic models.

``PageClassification`` and ``PageRecord`` ARE part of the output contract
(they are written to ``document_pages.json`` and embedded in
``canonical_estimate.json`` as ``source_pages``), so they are pydantic
models.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class PageClassification(str, Enum):
    CARRIER_LETTER = "carrier_letter"
    SETTLEMENT_NOTICE = "settlement_notice"
    INSTRUCTIONAL_SAMPLE = "instructional_sample"
    FAQ = "faq"
    CLAIM_METADATA = "claim_metadata"
    COVERAGE_SUMMARY = "coverage_summary"
    ESTIMATE_DETAIL = "estimate_detail"
    ESTIMATE_DETAIL_CONTINUATION = "estimate_detail_continuation"
    ROOF_DIAGRAM = "roof_diagram"
    MEASUREMENT_SUMMARY = "measurement_summary"
    TAX_RECAP = "tax_recap"
    OVERHEAD_PROFIT_RECAP = "overhead_profit_recap"
    RECAP_BY_ROOM = "recap_by_room"
    DEPRECIATION_GUIDE = "depreciation_guide"
    REPLACEMENT_COST_EXPLANATION = "replacement_cost_explanation"
    ATTACHMENT = "attachment"
    BLANK = "blank"
    UNKNOWN = "unknown"


# Classifications whose page content must never be used as a source of real
# claim metadata or real line items, even if it superficially matches the
# patterns used to extract those things.
NON_ESTIMATE_CLASSIFICATIONS: frozenset[PageClassification] = frozenset(
    {
        PageClassification.CARRIER_LETTER,
        PageClassification.SETTLEMENT_NOTICE,
        PageClassification.INSTRUCTIONAL_SAMPLE,
        PageClassification.FAQ,
        PageClassification.DEPRECIATION_GUIDE,
        PageClassification.REPLACEMENT_COST_EXPLANATION,
        PageClassification.ATTACHMENT,
        PageClassification.BLANK,
        PageClassification.UNKNOWN,
    }
)

# Classifications that contain estimate line-item rows.
ESTIMATE_DETAIL_CLASSIFICATIONS: frozenset[PageClassification] = frozenset(
    {
        PageClassification.ESTIMATE_DETAIL,
        PageClassification.ESTIMATE_DETAIL_CONTINUATION,
    }
)


class PageRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page: int
    classification: PageClassification
    include_in_estimate: bool
    confidence: float = Field(ge=0.0, le=1.0)
    reasons: list[str] = Field(default_factory=list)


@dataclass(frozen=True, slots=True)
class Word:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float


@dataclass(frozen=True, slots=True)
class Line:
    """A physical line of text on a page, in reading order."""

    text: str
    y0: float
    y1: float
    x0: float
    x1: float
    words: tuple[Word, ...] = ()


@dataclass(slots=True)
class ParsedPage:
    page_number: int  # 1-indexed
    width: float
    height: float
    raw_text: str
    lines: list[Line] = field(default_factory=list)
    source: str = "native"  # "native" or "ocr"
    # Text belonging to a repeating multi-column position grid (e.g. a
    # sketch's per-opening annotation table), detected purely from layout
    # position -- see pdf/layout.py:find_grid_annotation_texts. Empty for
    # OCR-sourced pages (no positional span data available).
    grid_annotation_texts: frozenset[str] = field(default_factory=frozenset)

    @property
    def char_count(self) -> int:
        return len(self.raw_text.strip())

    @property
    def has_native_text(self) -> bool:
        return self.char_count > 0


@dataclass(slots=True)
class ParsedDocument:
    source_path: Path
    sha256: str
    page_count: int
    pages: list[ParsedPage] = field(default_factory=list)

    def full_text(self) -> str:
        return "\n".join(p.raw_text for p in self.pages)
