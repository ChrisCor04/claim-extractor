"""Typed models for the Phase 3.8 description-first lookup workflow.

Reuses `selector_recommendation.models.RecommendationInput` as the
per-line-item input shape -- Phase 3.8 does not re-read Phase 2 output
differently than Phase 3.7 does, so there is no reason to duplicate that
model (see build spec "Preserve Phase 3.7").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from estimate_extractor.selector_recommendation.models import RecommendationInput  # noqa: F401  (re-exported)

DECISION_AUTO_SELECT = "AUTO_SELECT"
DECISION_REVIEW_REQUIRED = "REVIEW_REQUIRED"
DECISION_NO_MATCH = "NO_MATCH"
VALID_DECISIONS = frozenset({DECISION_AUTO_SELECT, DECISION_REVIEW_REQUIRED, DECISION_NO_MATCH})

MAPPING_STATUS_APPROVED = "approved"
MAPPING_STATUS_DISABLED = "disabled"
MAPPING_STATUS_REJECTED = "rejected"
VALID_MAPPING_STATUSES = frozenset({MAPPING_STATUS_APPROVED, MAPPING_STATUS_DISABLED, MAPPING_STATUS_REJECTED})

# Only mappings in this status set may ever be reused automatically (the
# fast CAT/SEL path) -- see build spec "Only genuinely approved records
# may be reused automatically."
REUSABLE_MAPPING_STATUSES = frozenset({MAPPING_STATUS_APPROVED})

LOOKUP_PATH_TRUSTED = "trusted_cat_sel"
LOOKUP_PATH_DESCRIPTION_SEARCH = "description_search"

STOP_REASON_NO_RESULTS = "no_results"
STOP_REASON_EXTRACTION_FAILED = "dropdown_extraction_failed"
STOP_REASON_QUANTITY_CONFIRMATION_FAILED = "quantity_confirmation_failed"
STOP_REASON_AMBIGUOUS = "ambiguous_candidates"
STOP_REASON_HARD_CONFLICT = "hard_conflict"
STOP_REASON_FIELD_MISMATCH = "populated_fields_mismatch"
STOP_REASON_UNIT_QUANTITY_INVALID = "unit_or_quantity_invalid"
STOP_REASON_CONTEXT_UNVERIFIED = "xactimate_context_unverified"
STOP_REASON_ADAPTER_ERROR = "adapter_error"
STOP_REASON_UNIT_MISMATCH = "unit_mismatch"
STOP_REASON_UNSUPPORTED_ADAPTER = "unsupported_adapter"
STOP_REASON_UNEXPECTED_DIALOG = "unexpected_dialog"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class PhraseResult:
    """Output of phrase_generator.generate_search_phrase() -- every term
    kept or dropped is explained, per build spec 'deterministic,
    explainable, configurable'."""

    phrase: str
    terms: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class DropdownResult:
    """One visible row from Xactimate's search dropdown, as captured (by
    a real or fake adapter) before ranking."""

    raw_text: str
    row_position: int
    category: str | None = None
    selector: str | None = None
    description: str | None = None
    item_number: str | None = None
    extraction_confidence: float = 1.0
    #: Phase 5.6 Stage 3: the dropdown's own price text, exact UI
    #: Automation text (never OCR) -- previously captured in the raw
    #: read (_RawDropdownRow.price_text) and then silently discarded by
    #: parse_dropdown(). Informational/audit-table use only; no unit is
    #: embedded in it (confirmed live against real Xactimate dropdown
    #: rows -- price_text is a bare dollar amount like "$11.56", never
    #: "$11.56/LF"), so this does not feed unit verification.
    price_text: str | None = None


@dataclass(slots=True)
class RankedCandidate:
    dropdown: DropdownResult
    score: float
    match_reasons: list[str] = field(default_factory=list)
    conflict_reasons: list[str] = field(default_factory=list)

    @property
    def has_hard_conflict(self) -> bool:
        return bool(self.conflict_reasons)


@dataclass(slots=True)
class InternalMappingRecord:
    """One learned/approved item-signature -> CAT/SEL resolution. Only
    ``status == approved`` records are ever reused automatically -- see
    REUSABLE_MAPPING_STATUSES. Denormalized material/size/grade/action/
    unit fields exist so a reuse check can catch a conflict even if two
    genuinely different items happen to hash to the same coarse
    signature (see build spec 'Do not reuse ... mappings with conflicting
    material, size, action, grade, or unit')."""

    mapping_id: str
    item_signature: str
    source_description: str
    search_phrase: str
    category: str
    selector: str
    xactimate_description: str
    unit: str | None
    action: str | None
    reviewer: str
    approval_reason: str
    evidence_reference: str | None = None
    xactimate_item_number: str | None = None
    material: str | None = None
    size_key: str | None = None
    grade_key: str | None = None
    #: readable rendering of the item's normalized fields (trade/
    #: component/material/action) -- distinct from source_description
    #: (verbatim carrier text) and search_phrase (bucketed for Xactimate
    #: search); see signature.compute_normalized_description.
    normalized_description: str | None = None
    #: the raw Activity-column symbol/text as actually observed in the
    #: Xactimate dropdown/detail (e.g. "+", "R&R") -- copied verbatim,
    #: never inferred, mirroring verified_catalog_service's
    #: activity_raw/activity_interpretation split. Distinct from `action`,
    #: which is Phase 2's semantic normalized action.
    xactimate_activity_raw: str | None = None
    status: str = MAPPING_STATUS_APPROVED
    usage_count: int = 0
    success_count: int = 0
    rejection_count: int = 0
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)

    @property
    def is_reusable(self) -> bool:
        return self.status in REUSABLE_MAPPING_STATUSES

    def to_dict(self) -> dict:
        return {
            "mapping_id": self.mapping_id,
            "item_signature": self.item_signature,
            "source_description": self.source_description,
            "search_phrase": self.search_phrase,
            "category": self.category,
            "selector": self.selector,
            "xactimate_description": self.xactimate_description,
            "xactimate_item_number": self.xactimate_item_number,
            "unit": self.unit,
            "action": self.action,
            "material": self.material,
            "size_key": self.size_key,
            "grade_key": self.grade_key,
            "normalized_description": self.normalized_description,
            "xactimate_activity_raw": self.xactimate_activity_raw,
            "reviewer": self.reviewer,
            "approval_reason": self.approval_reason,
            "evidence_reference": self.evidence_reference,
            "status": self.status,
            "usage_count": self.usage_count,
            "success_count": self.success_count,
            "rejection_count": self.rejection_count,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @staticmethod
    def from_dict(data: dict) -> InternalMappingRecord:
        return InternalMappingRecord(
            mapping_id=data["mapping_id"],
            item_signature=data["item_signature"],
            source_description=data.get("source_description", ""),
            search_phrase=data.get("search_phrase", ""),
            category=data["category"],
            selector=data["selector"],
            xactimate_description=data.get("xactimate_description", ""),
            xactimate_item_number=data.get("xactimate_item_number"),
            unit=data.get("unit"),
            action=data.get("action"),
            material=data.get("material"),
            size_key=data.get("size_key"),
            grade_key=data.get("grade_key"),
            normalized_description=data.get("normalized_description"),
            xactimate_activity_raw=data.get("xactimate_activity_raw"),
            reviewer=data.get("reviewer", ""),
            approval_reason=data.get("approval_reason", ""),
            evidence_reference=data.get("evidence_reference"),
            status=data.get("status", MAPPING_STATUS_APPROVED),
            usage_count=data.get("usage_count", 0),
            success_count=data.get("success_count", 0),
            rejection_count=data.get("rejection_count", 0),
            created_at=data.get("created_at", utc_now_iso()),
            updated_at=data.get("updated_at", utc_now_iso()),
        )


@dataclass(slots=True)
class LookupPlan:
    line_item_id: str
    path: str  # LOOKUP_PATH_TRUSTED | LOOKUP_PATH_DESCRIPTION_SEARCH
    item_signature: str
    search_input: str  # "CAT/SEL" for trusted path, phrase text for description search
    trusted_mapping: InternalMappingRecord | None = None
    phrase_result: PhraseResult | None = None

    def to_dict(self) -> dict:
        return {
            "line_item_id": self.line_item_id,
            "path": self.path,
            "item_signature": self.item_signature,
            "search_input": self.search_input,
            "trusted_mapping_id": self.trusted_mapping.mapping_id if self.trusted_mapping else None,
            "phrase": self.phrase_result.phrase if self.phrase_result else None,
        }


@dataclass(slots=True)
class PopulatedFields:
    """What the adapter reads back from Xactimate after selecting a
    dropdown result -- compared against the selected candidate as an
    integrity check (see build spec safety-stop 'populated fields differ
    from the selected result')."""

    category: str | None
    selector: str | None
    description: str | None
    unit: str | None
    action: str | None
    item_number: str | None


@dataclass(slots=True)
class LookupOutcome:
    line_item_id: str
    decision: str
    plan: LookupPlan
    candidates: list[RankedCandidate] = field(default_factory=list)
    selected: RankedCandidate | None = None
    stop_reason: str | None = None
    stop_detail: str | None = None
    evidence_reference: str | None = None
    committed: bool = False
    #: True as soon as the adapter positively observes the row-count
    #: increase caused by candidate activation.  This is deliberately
    #: separate from ``committed``: a later OCR/quantity/commit error
    #: must not make a physically-created row look like nothing landed.
    physical_item_created: bool = False
    #: The adapter's independent post-commit verification result (Phase
    #: 4.8's `CommitVerification`, from `WindowsXactimateAdapter.
    #: verify_commit()`), when the adapter supports it. Deliberately
    #: untyped here (duck-typed via `.trust_state` in to_dict()) --
    #: xactimate_lookup stays adapter-agnostic and must not import a
    #: concrete adapter's types. None whenever the adapter doesn't
    #: support commit verification, or nothing was committed.
    verification: object | None = None
    #: Phase 5.5: the `PopulatedFields` read back right after
    #: `select_candidate()` (see orchestrator.execute_plan()), set on
    #: the outcome that is ultimately returned on a successful commit.
    #: A field-mismatch/unit-mismatch safety stop returns a separate,
    #: fresh outcome (via `_stop()`) and never carries this. Exists so
    #: callers that need the Quick-Entry-populated `action` (e.g.
    #: execution_runner.py recording an "observed activity" for an
    #: originally-unmapped row) don't need their own separate read;
    #: nothing about execute_plan()'s own decision logic reads this
    #: field.
    populated_fields: PopulatedFields | None = None

    def to_dict(self) -> dict:
        return {
            "line_item_id": self.line_item_id,
            "decision": self.decision,
            "plan": self.plan.to_dict(),
            "candidate_count": len(self.candidates),
            "selected": (
                {
                    "category": self.selected.dropdown.category,
                    "selector": self.selected.dropdown.selector,
                    "description": self.selected.dropdown.description,
                    "item_number": self.selected.dropdown.item_number,
                    "score": self.selected.score,
                    "match_reasons": list(self.selected.match_reasons),
                }
                if self.selected
                else None
            ),
            "stop_reason": self.stop_reason,
            "stop_detail": self.stop_detail,
            "evidence_reference": self.evidence_reference,
            "committed": self.committed,
            "verification_trust_state": getattr(self.verification, "trust_state", None),
        }
