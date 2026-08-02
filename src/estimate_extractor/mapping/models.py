"""Typed models for the normalization + mapping pipeline.

Deliberately decoupled from ``estimate_extractor.models.canonical`` (the
Phase 1 extraction schema) beyond the plain values copied out of it in
``OriginalItem`` -- Phase 2 reads Phase 1's output file, it does not import
Phase 1's internal models, so the two stages can evolve independently.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class ActionType(str, Enum):
    REMOVE = "remove"
    INSTALL = "install"
    REPLACE = "replace"
    REMOVE_AND_REPLACE = "remove_and_replace"
    REPAIR = "repair"
    DETACH = "detach"
    RESET = "reset"
    DETACH_AND_RESET = "detach_and_reset"
    CLEAN = "clean"
    PRESSURE_WASH = "pressure_wash"
    PAINT = "paint"
    PRIME = "prime"
    SEAL = "seal"
    STAIN = "stain"
    INSPECT = "inspect"
    LABOR_MINIMUM = "labor_minimum"
    HAUL = "haul"
    DISPOSE = "dispose"
    CUSTOM = "custom"
    UNKNOWN = "unknown"


class TradeType(str, Enum):
    ROOFING = "roofing"
    GUTTERS = "gutters"
    SIDING = "siding"
    WINDOWS = "windows"
    DOORS = "doors"
    DRYWALL = "drywall"
    PAINTING = "painting"
    FLOORING = "flooring"
    INSULATION = "insulation"
    FENCING = "fencing"
    CARPENTRY = "carpentry"
    CABINETS = "cabinets"
    MASONRY = "masonry"
    CONCRETE = "concrete"
    ELECTRICAL = "electrical"
    PLUMBING = "plumbing"
    HVAC = "hvac"
    APPLIANCES = "appliances"
    CONTENTS = "contents"
    CLEANING = "cleaning"
    DEMOLITION = "demolition"
    DEBRIS_REMOVAL = "debris_removal"
    GENERAL_LABOR = "general_labor"
    OTHER = "other"
    UNKNOWN = "unknown"


class MappingStatus(str, Enum):
    MAPPED = "mapped"
    PARTIALLY_MAPPED = "partially_mapped"
    NEEDS_REVIEW = "needs_review"
    UNMAPPED = "unmapped"
    UNSUPPORTED = "unsupported"
    ERROR = "error"


class MappingSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


# ---------------------------------------------------------------------------
# normalized_estimate.json
# ---------------------------------------------------------------------------


class OriginalItem(Strict):
    """Verbatim facts copied from the Phase 1 canonical line item. Never
    altered by the normalizer -- see docs/mapping-engine.md "Do not modify
    extracted facts."""

    description: str
    quantity: float | None = None
    unit_of_measure: str | None = None
    coverage_id: str | None = None
    section_name: str | None = None
    area_name: str | None = None
    source_pages: list[int] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    extraction_confidence: float | None = None
    extraction_needs_review: bool = False
    extraction_warnings: list[str] = Field(default_factory=list)


class NormalizedAttributes(Strict):
    """Free-form, extensible attribute bag (dimensions, material grade,
    felt/no-felt, story count, slope range, etc.) -- deliberately a dict
    rather than a fixed field set so new attributes don't require a schema
    change; see config/normalization_rules.yaml."""

    model_config = ConfigDict(extra="allow")


class Normalized(Strict):
    action: ActionType = ActionType.UNKNOWN
    trade: TradeType = TradeType.UNKNOWN
    component: str = "unknown"
    material: str | None = None
    attributes: dict[str, str | float | bool | None] = Field(default_factory=dict)
    quantity: float | None = None
    unit_of_measure: str | None = None


class NormalizationConfidence(Strict):
    overall: float = Field(ge=0.0, le=1.0)
    action: float = Field(ge=0.0, le=1.0)
    trade: float = Field(ge=0.0, le=1.0)
    component: float = Field(ge=0.0, le=1.0)
    material: float = Field(ge=0.0, le=1.0)


class NormalizedLineItem(Strict):
    line_item_id: str
    original: OriginalItem
    normalized: Normalized
    confidence: NormalizationConfidence
    needs_review: bool = False
    review_reasons: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# mapped_estimate.json
# ---------------------------------------------------------------------------


class XactimateRef(Strict):
    category: str | None = None
    selector: str | None = None
    activity: str | None = None
    description: str | None = None


class MappingCandidate(Strict):
    mapping_id: str
    category: str | None = None
    selector: str | None = None
    activity: str | None = None
    description: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)


class MappingOutcome(Strict):
    status: MappingStatus
    best_match: MappingCandidate | None = None
    alternatives: list[MappingCandidate] = Field(default_factory=list)
    needs_review: bool = False
    review_reasons: list[str] = Field(default_factory=list)


class MappedLineItem(Strict):
    line_item_id: str
    coverage_id: str | None = None
    normalization: Normalized
    mapping: MappingOutcome


# ---------------------------------------------------------------------------
# mapping_report.json
# ---------------------------------------------------------------------------


class MappingIssue(Strict):
    issue_id: str
    line_item_id: str | None = None
    severity: MappingSeverity
    code: str
    message: str
    suggested_action: str | None = None


class MappingReportSummary(Strict):
    total_items: int
    mapped: int
    partially_mapped: int
    needs_review: int
    unmapped: int
    unresolved_coverage: int
    average_confidence: float | None = None


class MappingReport(Strict):
    status: str
    summary: MappingReportSummary
    issues: list[MappingIssue] = Field(default_factory=list)
