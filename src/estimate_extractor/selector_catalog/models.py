"""Data models for the Phase 3.6 canonical Xactimate selector catalog.

Canonical identity is (category, selector) -- see
docs/selector-catalog.md. This is deliberately independent of Phase 3.5's
``verified_catalog_service`` models (which track unit/activity/price
observations for the *mapping* catalog); this module's job is narrower: a
permanent, provenance-tracked Category/Selector/Description reference
built once from reviewer-supplied screenshots, with no pricing data.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone

SCREENSHOT_STATUS_PENDING = "pending"
SCREENSHOT_STATUS_PROCESSED = "processed"
SCREENSHOT_STATUS_SKIPPED = "skipped"
SCREENSHOT_STATUS_FAILED = "failed"
VALID_SCREENSHOT_STATUSES = frozenset(
    {SCREENSHOT_STATUS_PENDING, SCREENSHOT_STATUS_PROCESSED, SCREENSHOT_STATUS_SKIPPED, SCREENSHOT_STATUS_FAILED}
)

# The one folder in the reference library that is never a selector table --
# always skipped, but always logged (see build spec "no screenshot is
# silently ignored").
NON_CAT_FOLDER = "_NON_CAT_PROJECT_UI"

# Known Xactimate unit-column abbreviations, observed directly in the
# reference screenshots (Sel | Description | Unit | Act | Unit Price |
# Green). Used only to flag *probable* Unit-column bleed-through into
# Description for human review -- never to strip text. A description
# ending in one of these tokens is NOT proof of contamination: real
# descriptions legitimately end in "...over 180 SF" or "...per LF" often
# (confirmed against the real catalog -- see
# docs/selector-catalog.md "Unit-column contamination"), so this is a
# conservative *candidate* filter, resolved definitively only by
# coordinate-based re-inspection of the source screenshot.
KNOWN_UNIT_TOKENS = frozenset(
    {"EA", "SF", "SQ", "LF", "LB", "HR", "DA", "CY", "SY", "GAL", "MO", "BX", "PR", "RL", "PS"}
)

REVIEW_REASON_UNIT_CONTAMINATION = "possible_unit_column_contamination"
REVIEW_REASON_MALFORMED_SELECTOR = "malformed_selector_candidate"
REVIEW_REASON_CATEGORY_MISMATCH = "category_mismatch"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def probable_unit_contamination(description: str) -> str | None:
    """Returns the matched unit token (uppercased) if `description`'s last
    whitespace-delimited word exactly equals a known Unit-column
    abbreviation, else None. A *candidate* signal only -- see
    KNOWN_UNIT_TOKENS docstring. Never mutates or strips anything itself."""
    if not description:
        return None
    parts = description.strip().split()
    if not parts:
        return None
    last = parts[-1].strip(".,;:").upper()
    return last if last in KNOWN_UNIT_TOKENS else None


def is_selector_plausible(selector: str) -> bool:
    """Real Xactimate selector codes in this reference library are always
    upper-case-with-punctuation (confirmed across thousands of legible
    examples: FLPIPE, GCR240H, ISO3>, ST++, ...). A selector containing
    any lowercase letter is a strong, well-evidenced signal of OCR
    corruption (verified examples: 'stiR+' should read 'ST!R+'-shaped
    garbage, 'Bites' is not a real selector shape) -- not proof of a
    specific correct value, just proof the current reading is wrong.
    Empty selectors are handled separately (see review-queue routing);
    this function only judges shape, so an empty string is trivially
    "plausible" here (neither confirms nor denies malformation)."""
    if not selector:
        return True
    return not any(c.islower() for c in selector)


def normalize_description(text: str) -> str:
    """Search-only normalization -- never applied to description_original.
    Lowercase, unicode punctuation normalization (curly quotes/dashes ->
    plain ASCII), whitespace collapse, repeated-punctuation cleanup."""
    if not text:
        return ""
    normalized = unicodedata.normalize("NFKC", text)
    normalized = normalized.translate(
        str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-", "…": "..."})
    )
    normalized = normalized.lower()
    normalized = re.sub(r"\s+", " ", normalized).strip()
    normalized = re.sub(r"\.{2,}", "...", normalized)
    normalized = re.sub(r"-{2,}", "-", normalized)
    return normalized


def is_truncated(text: str) -> bool:
    stripped = text.rstrip()
    return stripped.endswith("...") or stripped.endswith("..") or stripped.endswith("…")


@dataclass(frozen=True, slots=True)
class ScreenshotRef:
    """One screenshot file discovered in the extracted reference library."""

    folder_category: str  # directory name under Screenshots_By_CAT/
    sequence: int  # numeric sequence within the folder, parsed from filename
    filename: str
    relative_path: str  # relative to the extracted library root
    absolute_path: str

    @property
    def is_non_cat(self) -> bool:
        return self.folder_category == NON_CAT_FOLDER


@dataclass(slots=True)
class TableRegion:
    """Detected header row + column boundaries for one screenshot, plus
    whatever the title bar OCR'd to (before any folder/title comparison)."""

    header_top: int
    header_bottom: int
    selector_col_left: int
    selector_col_right: int
    description_col_left: int
    description_col_right: int
    title_bar_category: str | None
    title_bar_raw_text: str | None


@dataclass(slots=True)
class ExtractedRow:
    """One selector row OCR'd from one screenshot, before deduplication."""

    selector_raw: str
    description_raw: str
    row_confidence: float | None  # 0.0-1.0, mean word confidence for this row
    truncated: bool
    y_top: int


@dataclass(slots=True)
class SourceReference:
    source_image: str  # relative path within the extracted library
    source_folder: str
    source_sequence: int
    ocr_confidence: float | None
    row_index: int  # which row within that screenshot, for traceability

    def to_dict(self) -> dict:
        return {
            "source_image": self.source_image,
            "source_folder": self.source_folder,
            "source_sequence": self.source_sequence,
            "ocr_confidence": self.ocr_confidence,
            "row_index": self.row_index,
        }

    @staticmethod
    def from_dict(data: dict) -> SourceReference:
        return SourceReference(
            source_image=data["source_image"],
            source_folder=data["source_folder"],
            source_sequence=data["source_sequence"],
            ocr_confidence=data.get("ocr_confidence"),
            row_index=data.get("row_index", 0),
        )


@dataclass(slots=True)
class SelectorRecord:
    """One canonical (category, selector) record -- the unit persisted to
    master_selectors.{csv,json,db}. Deliberately has no price field."""

    category: str
    selector: str
    description_original: str
    description_normalized: str
    needs_review: bool = False
    review_reasons: list[str] = field(default_factory=list)
    ocr_confidence: float | None = None
    title_bar_category: str | None = None
    category_mismatch: bool = False
    conflicting_descriptions: list[str] = field(default_factory=list)
    source_images: list[SourceReference] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)

    @property
    def primary_source_image(self) -> str | None:
        return self.source_images[0].source_image if self.source_images else None

    @property
    def key(self) -> tuple[str, str]:
        return (self.category, self.selector)

    def to_dict(self) -> dict:
        return {
            "category": self.category,
            "selector": self.selector,
            "description_original": self.description_original,
            "description_normalized": self.description_normalized,
            "needs_review": self.needs_review,
            "review_reasons": list(self.review_reasons),
            "ocr_confidence": self.ocr_confidence,
            "title_bar_category": self.title_bar_category,
            "category_mismatch": self.category_mismatch,
            "conflicting_descriptions": list(self.conflicting_descriptions),
            "primary_source_image": self.primary_source_image,
            "source_images": [s.to_dict() for s in self.source_images],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @staticmethod
    def from_dict(data: dict) -> SelectorRecord:
        return SelectorRecord(
            category=data["category"],
            selector=data["selector"],
            description_original=data["description_original"],
            description_normalized=data["description_normalized"],
            needs_review=data.get("needs_review", False),
            review_reasons=list(data.get("review_reasons") or []),
            ocr_confidence=data.get("ocr_confidence"),
            title_bar_category=data.get("title_bar_category"),
            category_mismatch=data.get("category_mismatch", False),
            conflicting_descriptions=list(data.get("conflicting_descriptions") or []),
            source_images=[SourceReference.from_dict(s) for s in (data.get("source_images") or [])],
            created_at=data.get("created_at", utc_now_iso()),
            updated_at=data.get("updated_at", utc_now_iso()),
        )


@dataclass(slots=True)
class ScreenshotManifestEntry:
    """Provenance + processing status for one screenshot -- persisted to
    screenshot_processing_manifest.json and used to make `selectors import`
    resumable (see build spec: "no screenshot is silently ignored")."""

    relative_path: str
    folder_category: str
    sequence: int
    status: str  # one of VALID_SCREENSHOT_STATUSES
    rows_extracted: int = 0
    title_bar_category: str | None = None
    category_mismatch: bool = False
    error: str | None = None
    skip_reason: str | None = None
    processed_at: str | None = None

    def to_dict(self) -> dict:
        return {
            "relative_path": self.relative_path,
            "folder_category": self.folder_category,
            "sequence": self.sequence,
            "status": self.status,
            "rows_extracted": self.rows_extracted,
            "title_bar_category": self.title_bar_category,
            "category_mismatch": self.category_mismatch,
            "error": self.error,
            "skip_reason": self.skip_reason,
            "processed_at": self.processed_at,
        }

    @staticmethod
    def from_dict(data: dict) -> ScreenshotManifestEntry:
        return ScreenshotManifestEntry(
            relative_path=data["relative_path"],
            folder_category=data["folder_category"],
            sequence=data["sequence"],
            status=data["status"],
            rows_extracted=data.get("rows_extracted", 0),
            title_bar_category=data.get("title_bar_category"),
            category_mismatch=data.get("category_mismatch", False),
            error=data.get("error"),
            skip_reason=data.get("skip_reason"),
            processed_at=data.get("processed_at"),
        )
