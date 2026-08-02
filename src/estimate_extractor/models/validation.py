"""Models backing extraction_report.json."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from estimate_extractor.models.canonical import ExtractionStatus


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    FATAL = "fatal"


class IssueCode(str, Enum):
    # Document-level
    PAGE_NOT_CLASSIFIED = "PAGE_NOT_CLASSIFIED"
    DUPLICATE_PAGE_NUMBER = "DUPLICATE_PAGE_NUMBER"
    NO_METADATA_SOURCE = "NO_METADATA_SOURCE"
    LOW_CARRIER_CONFIDENCE = "LOW_CARRIER_CONFIDENCE"
    PAGE_UNKNOWN_CLASSIFICATION = "PAGE_UNKNOWN_CLASSIFICATION"

    # Claim-level
    CONFLICTING_CLAIM_NUMBER = "CONFLICTING_CLAIM_NUMBER"
    CONFLICTING_INSURED_NAME = "CONFLICTING_INSURED_NAME"
    CONFLICTING_PROPERTY_ADDRESS = "CONFLICTING_PROPERTY_ADDRESS"
    INSTRUCTIONAL_SAMPLE_VALUE_SUSPECTED = "INSTRUCTIONAL_SAMPLE_VALUE_SUSPECTED"
    MISSING_CLAIM_NUMBER = "MISSING_CLAIM_NUMBER"
    MISSING_PRICE_LIST = "MISSING_PRICE_LIST"

    # Line-item-level
    DUPLICATE_LINE_ITEM_ID = "DUPLICATE_LINE_ITEM_ID"
    DUPLICATE_SOURCE_LINE_NUMBER = "DUPLICATE_SOURCE_LINE_NUMBER"
    MISSING_DESCRIPTION = "MISSING_DESCRIPTION"
    MISSING_QUANTITY = "MISSING_QUANTITY"
    MISSING_UNIT = "MISSING_UNIT"
    MALFORMED_MONEY_FIELD = "MALFORMED_MONEY_FIELD"
    ACV_GREATER_THAN_RCV = "ACV_GREATER_THAN_RCV"
    DEPRECIATION_GREATER_THAN_RCV = "DEPRECIATION_GREATER_THAN_RCV"
    UNEXPLAINED_NEGATIVE_VALUE = "UNEXPLAINED_NEGATIVE_VALUE"
    LINE_NUMBER_SEQUENCE_GAP = "LINE_NUMBER_SEQUENCE_GAP"
    AMBIGUOUS_LINE_CONTINUATION = "AMBIGUOUS_LINE_CONTINUATION"
    ORPHAN_CONTINUATION_ROW = "ORPHAN_CONTINUATION_ROW"
    UNRECOGNIZED_UNIT = "UNRECOGNIZED_UNIT"
    POSSIBLE_DESCRIPTION_WRAP = "POSSIBLE_DESCRIPTION_WRAP"
    UNRESOLVED_COVERAGE_ATTRIBUTION = "UNRESOLVED_COVERAGE_ATTRIBUTION"

    # Arithmetic
    LINE_ITEM_ARITHMETIC_MISMATCH = "LINE_ITEM_ARITHMETIC_MISMATCH"
    RCV_DEPRECIATION_ACV_MISMATCH = "RCV_DEPRECIATION_ACV_MISMATCH"
    SECTION_TOTAL_MISMATCH = "SECTION_TOTAL_MISMATCH"
    COVERAGE_TOTAL_MISMATCH = "COVERAGE_TOTAL_MISMATCH"
    GRAND_TOTAL_MISMATCH = "GRAND_TOTAL_MISMATCH"

    # OCR
    OCR_LOW_CONFIDENCE = "OCR_LOW_CONFIDENCE"

    # Generic
    EXTRACTION_ERROR = "EXTRACTION_ERROR"


class Issue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    issue_id: str
    severity: Severity
    code: IssueCode
    page: int | None = None
    line_item_id: str | None = None
    field: str | None = None
    message: str
    suggested_action: str | None = None


class ReportSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pages_total: int
    pages_classified: int
    pages_excluded: int
    line_items_extracted: int
    high_confidence_items: int
    review_items: int
    warnings: int
    fatal_errors: int


class Reconciliation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reported_line_item_total: float | None = None
    calculated_line_item_total: float | None = None
    # Sum of RCV across line items flagged is_code_upgrade=True (building-
    # code-upgrade items the carrier settles as "payable when incurred",
    # separate from the primary line-item total -- see docs). These items
    # are NOT removed from canonical_estimate.json's line_items; they are
    # only excluded from calculated_line_item_total so the PASS/FAIL
    # comparison is apples-to-apples with the document's own reported total.
    paid_when_incurred_total: float | None = None
    difference: float | None = None
    within_tolerance: bool | None = None
    note: str | None = None


class ExtractionReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: ExtractionStatus
    summary: ReportSummary
    reconciliation: Reconciliation
    issues: list[Issue] = Field(default_factory=list)
