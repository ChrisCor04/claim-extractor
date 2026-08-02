"""The stable, carrier-agnostic canonical output schema.

This is the single most important contract in the codebase: everything
upstream (PDF parsing, carrier adapters) produces these objects, and
everything downstream (a future Xactimate-mapping stage, human reviewers)
consumes them. Carrier-specific quirks are captured via optional fields
(``carrier_code`` on ``Coverage``, free-text ``category_heading`` on
``LineItem``, etc.) rather than by changing the top-level shape per carrier.

Design rules enforced throughout:
  * Every extracted fact that could be wrong carries a confidence score and
    the source page(s) it came from.
  * Nothing is fabricated: absent data is ``None``, never guessed.
  * Money and quantity fields are plain JSON numbers (floats), computed with
    ``Decimal`` upstream in ``normalization/money.py`` and only converted to
    float at the moment a model is constructed.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from estimate_extractor.models.page import PageRecord

T = TypeVar("T")


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class ExtractionStatus(str, Enum):
    SUCCESS = "success"
    NEEDS_REVIEW = "needs_review"
    FAILED = "failed"


class DepreciationType(str, Enum):
    NONE = "none"
    RECOVERABLE = "recoverable"
    NONRECOVERABLE = "nonrecoverable"
    MIXED = "mixed"
    UNKNOWN = "unknown"


class SummaryType(str, Enum):
    SECTION_TOTAL = "section_total"
    AREA_TOTAL = "area_total"
    COVERAGE_SUMMARY = "coverage_summary"
    LINE_ITEM_TOTAL = "line_item_total"
    GRAND_TOTAL = "grand_total"
    TAX_RECAP = "tax_recap"
    OVERHEAD_PROFIT_RECAP = "overhead_profit_recap"
    RECAP_BY_ROOM = "recap_by_room"
    ADDITIONAL_COVERAGE_RECAP = "additional_coverage_recap"
    PAID_WHEN_INCURRED_SUMMARY = "paid_when_incurred_summary"


class ContactRole(str, Enum):
    CLAIM_REPRESENTATIVE = "claim_representative"
    ESTIMATOR = "estimator"
    ADJUSTER = "adjuster"
    AGENT = "agent"
    CLAIMS_PROFESSIONAL = "claims_professional"
    OTHER = "other"


class Strict(BaseModel):
    """Base for canonical models: forbid unknown fields, deterministic dumps."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Provenance primitives
# ---------------------------------------------------------------------------


class FieldValue(Strict, Generic[T]):
    """A single extracted fact with provenance and confidence.

    ``value`` is ``None`` when the field could not be determined at all but
    we still want to record that we looked (e.g. conflicting raw values).
    When a claim field is entirely absent from the document, the field on
    ``ClaimData`` itself is ``None`` rather than an empty ``FieldValue``.
    """

    value: T | None
    confidence: float = Field(ge=0.0, le=1.0)
    source_pages: list[int] = Field(default_factory=list)
    raw_values: list[str] = Field(default_factory=list)
    normalized: bool = False


class AddressValue(Strict):
    line1: str | None = None
    line2: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None
    country: str = "US"
    confidence: float = Field(ge=0.0, le=1.0)
    source_pages: list[int] = Field(default_factory=list)
    raw_values: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# document
# ---------------------------------------------------------------------------


class DocumentMeta(Strict):
    source_filename: str
    source_sha256: str
    carrier_detected: str
    carrier_confidence: float = Field(ge=0.0, le=1.0)
    document_type: str = "property_estimate"
    page_count: int
    estimate_page_numbers: list[int] = Field(default_factory=list)
    excluded_page_numbers: list[int] = Field(default_factory=list)
    extraction_status: ExtractionStatus
    created_at_utc: datetime = Field(default_factory=utc_now)
    extractor_version: str


# ---------------------------------------------------------------------------
# claim
# ---------------------------------------------------------------------------


class ClaimData(Strict):
    claim_number: FieldValue[str] | None = None
    estimate_number: FieldValue[str] | None = None
    policy_number: FieldValue[str] | None = None
    insured_name: FieldValue[str] | None = None
    claimant_name: FieldValue[str] | None = None
    property_address: AddressValue | None = None
    mailing_address: AddressValue | None = None
    type_of_loss: FieldValue[str] | None = None
    cause_of_loss: FieldValue[str] | None = None
    date_of_loss: FieldValue[str] | None = None
    date_contacted: FieldValue[str] | None = None
    date_received: FieldValue[str] | None = None
    date_inspected: FieldValue[str] | None = None
    date_entered: FieldValue[str] | None = None
    date_completed: FieldValue[str] | None = None
    price_list: FieldValue[str] | None = None
    insurance_company: FieldValue[str] | None = None
    estimate_name: FieldValue[str] | None = None
    member_number: FieldValue[str] | None = None
    lr_number: FieldValue[str] | None = None


# ---------------------------------------------------------------------------
# contacts
# ---------------------------------------------------------------------------


class Contact(Strict):
    contact_id: str
    role: ContactRole
    name: str | None = None
    company: str | None = None
    phone: str | None = None
    email: str | None = None
    license_number: str | None = None
    source_pages: list[int] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)


# ---------------------------------------------------------------------------
# coverages
# ---------------------------------------------------------------------------


class CoverageSummaryFinancials(Strict):
    line_item_total: float | None = None
    material_sales_tax: float | None = None
    cleaning_material_tax: float | None = None
    cleaning_sales_tax: float | None = None
    other_tax: float | None = None
    subtotal: float | None = None
    overhead: float | None = None
    profit: float | None = None
    replacement_cost_value: float | None = None
    recoverable_depreciation: float | None = None
    nonrecoverable_depreciation: float | None = None
    total_depreciation: float | None = None
    actual_cash_value: float | None = None
    deductible: float | None = None
    net_claim: float | None = None
    net_claim_if_depreciation_recovered: float | None = None


class Coverage(Strict):
    coverage_id: str
    code: str | None = None
    carrier_code: str | None = None
    name: str
    subcoverage: str | None = None
    deductible: float | None = None
    policy_limit: float | None = None
    paid_when_incurred: bool = False
    summary: CoverageSummaryFinancials = Field(default_factory=CoverageSummaryFinancials)
    source_pages: list[int] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)


# ---------------------------------------------------------------------------
# areas
# ---------------------------------------------------------------------------


class Area(Strict):
    area_id: str
    coverage_id: str | None = None
    name: str
    area_type: str | None = None
    parent_area_id: str | None = None
    source_pages: list[int] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)


# ---------------------------------------------------------------------------
# sections
# ---------------------------------------------------------------------------


class SectionMeasurements(Strict):
    surface_area_sf: float | None = None
    wall_area_sf: float | None = None
    ceiling_area_sf: float | None = None
    floor_area_sf: float | None = None
    walls_and_ceiling_sf: float | None = None
    number_of_squares: float | None = None
    perimeter_lf: float | None = None
    ridge_lf: float | None = None
    hip_lf: float | None = None
    floor_perimeter_lf: float | None = None
    ceiling_perimeter_lf: float | None = None


class Section(Strict):
    section_id: str
    coverage_id: str | None = None
    area_id: str | None = None
    name: str
    section_type: str | None = None
    parent_section_id: str | None = None
    continued_from_page: int | None = None
    source_pages: list[int] = Field(default_factory=list)
    measurements: SectionMeasurements = Field(default_factory=SectionMeasurements)
    confidence: float = Field(ge=0.0, le=1.0)


# ---------------------------------------------------------------------------
# line items
# ---------------------------------------------------------------------------


class Note(Strict):
    note_type: str
    text: str
    source_pages: list[int] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)


class LineItemFlags(Strict):
    is_remove_only: bool = False
    is_replace_only: bool = False
    is_remove_and_replace: bool = False
    is_detach_and_reset: bool = False
    is_labor_minimum: bool = False
    is_code_upgrade: bool = False


class BoundingBox(Strict):
    page: int
    x0: float
    y0: float
    x1: float
    y1: float


class LineItemSource(Strict):
    page_start: int
    page_end: int
    raw_text: str
    line_ranges: list[list[int]] = Field(default_factory=list)
    bounding_boxes: list[BoundingBox] = Field(default_factory=list)


class LineItemConfidence(Strict):
    overall: float = Field(ge=0.0, le=1.0)
    description: float = Field(ge=0.0, le=1.0)
    quantity: float = Field(ge=0.0, le=1.0)
    unit_of_measure: float = Field(ge=0.0, le=1.0)
    unit_price: float = Field(ge=0.0, le=1.0)
    tax: float = Field(ge=0.0, le=1.0)
    replacement_cost_value: float = Field(ge=0.0, le=1.0)
    depreciation: float = Field(ge=0.0, le=1.0)
    actual_cash_value: float = Field(ge=0.0, le=1.0)


class LineItem(Strict):
    line_item_id: str
    source_line_number: int | None = None
    coverage_id: str | None = None
    area_id: str | None = None
    section_id: str | None = None
    category_heading: str | None = None
    description: str
    description_normalized_whitespace: str
    quantity: float | None = None
    unit_of_measure: str | None = None
    unit_price: float | None = None
    tax: float | None = None
    overhead_and_profit: float | None = None
    replacement_cost_value: float | None = None
    age: str | None = None
    life_expectancy: str | None = None
    condition: str | None = None
    depreciation_percent: float | None = None
    depreciation_amount: float | None = None
    depreciation_type: DepreciationType = DepreciationType.UNKNOWN
    actual_cash_value: float | None = None
    notes: list[Note] = Field(default_factory=list)
    flags: LineItemFlags = Field(default_factory=LineItemFlags)
    source: LineItemSource
    confidence: LineItemConfidence
    needs_review: bool = False
    review_reasons: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# summary totals
# ---------------------------------------------------------------------------


class SummaryTotal(Strict):
    summary_id: str
    summary_type: SummaryType
    coverage_id: str | None = None
    area_id: str | None = None
    section_id: str | None = None
    label: str
    tax: float | None = None
    replacement_cost_value: float | None = None
    depreciation: float | None = None
    actual_cash_value: float | None = None
    source_page: int | None = None


# ---------------------------------------------------------------------------
# validation_state (embedded summary; full detail lives in
# extraction_report.json, written by output/report_writer.py)
# ---------------------------------------------------------------------------


class ValidationState(Strict):
    status: ExtractionStatus
    issue_count: int = 0
    warning_count: int = 0
    error_count: int = 0
    fatal_count: int = 0
    reconciled: bool | None = None


# ---------------------------------------------------------------------------
# top-level document
# ---------------------------------------------------------------------------


class CanonicalEstimate(Strict):
    schema_version: str = "1.0.0"
    document: DocumentMeta
    claim: ClaimData = Field(default_factory=ClaimData)
    contacts: list[Contact] = Field(default_factory=list)
    coverages: list[Coverage] = Field(default_factory=list)
    areas: list[Area] = Field(default_factory=list)
    sections: list[Section] = Field(default_factory=list)
    line_items: list[LineItem] = Field(default_factory=list)
    summary_totals: list[SummaryTotal] = Field(default_factory=list)
    source_pages: list[PageRecord] = Field(default_factory=list)
    validation_state: ValidationState
