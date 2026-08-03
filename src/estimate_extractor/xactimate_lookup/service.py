"""Top-level orchestration used by the CLI and UI: per-project lookup
planning, recording a manually-resolved Xactimate result as a reusable
internal mapping, disabling/listing mappings, dry-run planning, and
lookup statistics. Every write path here is backup-before-write and
requires a non-empty reviewer + reason, mirroring
verified_catalog_service's conventions exactly.
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from estimate_extractor.mapping.pipeline import DEFAULT_CONFIG_DIR
from estimate_extractor.xactimate_lookup import orchestrator, phrase_generator, ranking, registry
from estimate_extractor.xactimate_lookup.adapter import XactimateAdapter
from estimate_extractor.xactimate_lookup.models import (
    LOOKUP_PATH_DESCRIPTION_SEARCH,
    LOOKUP_PATH_TRUSTED,
    MAPPING_STATUS_APPROVED,
    MAPPING_STATUS_DISABLED,
    MAPPING_STATUS_REJECTED,
    STOP_REASON_ADAPTER_ERROR,
    STOP_REASON_AMBIGUOUS,
    STOP_REASON_CONTEXT_UNVERIFIED,
    STOP_REASON_EXTRACTION_FAILED,
    STOP_REASON_FIELD_MISMATCH,
    STOP_REASON_HARD_CONFLICT,
    STOP_REASON_NO_RESULTS,
    STOP_REASON_UNEXPECTED_DIALOG,
    STOP_REASON_UNIT_MISMATCH,
    STOP_REASON_UNIT_QUANTITY_INVALID,
    STOP_REASON_UNSUPPORTED_ADAPTER,
    InternalMappingRecord,
    LookupOutcome,
    LookupPlan,
    RecommendationInput,
)
from estimate_extractor.selector_recommendation import service as recommendation_service
from estimate_extractor.ui import review_service
from estimate_extractor.ui import verified_catalog_service as vcs

DEFAULT_REGISTRY_DB_PATH = DEFAULT_CONFIG_DIR / "internal_lookup_registry.db"
DEFAULT_BACKUPS_DIR = DEFAULT_CONFIG_DIR / "backups"
DEFAULT_VERIFIED_CATALOG_PATH = DEFAULT_CONFIG_DIR / "verified_xactimate_catalog.yaml"
DEFAULT_EVENTS_PATH = DEFAULT_CONFIG_DIR / "internal_lookup_events.json"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class LookupServiceError(Exception):
    pass


class MappingConflictError(LookupServiceError):
    """Raised when recording a resolution would silently change an
    existing approved mapping's CAT/SEL for the same signature -- never
    allowed without an explicit override (see build spec 'Do not
    silently change approved mappings.')."""

    def __init__(self, item_signature: str, existing: InternalMappingRecord):
        super().__init__(
            f"An approved mapping already exists for signature {item_signature!r} "
            f"({existing.category}/{existing.selector}, mapping_id={existing.mapping_id}). "
            f"Pass allow_override=True with an explicit reason to change it."
        )
        self.item_signature = item_signature
        self.existing = existing


# ---------------------------------------------------------------------------
# Input construction (reuses Phase 3.7's item-building logic verbatim)
# ---------------------------------------------------------------------------


def build_lookup_input(row: dict, normalized_item: dict | None) -> RecommendationInput:
    return recommendation_service.build_recommendation_input(row, normalized_item)


def _load_items(project_dir: Path, line_item_ids: list[str] | None = None) -> list[RecommendationInput]:
    normalized_by_id = recommendation_service.load_normalized_items(project_dir)
    rows = review_service.build_effective_rows(project_dir, line_item_ids=line_item_ids)
    return [build_lookup_input(row, normalized_by_id.get(row["line_item_id"])) for row in rows]


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


def plan_for_project(
    project_dir: Path,
    registry_db_path: Path = DEFAULT_REGISTRY_DB_PATH,
    *,
    phrase_rules_path: Path = phrase_generator.DEFAULT_PHRASE_RULES_PATH,
    verified_catalog_path: Path | None = DEFAULT_VERIFIED_CATALOG_PATH,
    line_item_ids: list[str] | None = None,
) -> list[LookupPlan]:
    phrase_rules = phrase_generator.load_phrase_rules(phrase_rules_path)
    verified_records = []
    if verified_catalog_path and verified_catalog_path.exists():
        verified_records = vcs.load_verified_catalog(verified_catalog_path)

    conn = registry.create_database(registry_db_path)
    try:
        items = _load_items(project_dir, line_item_ids)
        return [orchestrator.build_lookup_plan(item, conn, phrase_rules, verified_records) for item in items]
    finally:
        conn.close()


def dry_run_for_project(
    project_dir: Path,
    adapter: XactimateAdapter,
    registry_db_path: Path = DEFAULT_REGISTRY_DB_PATH,
    *,
    phrase_rules_path: Path = phrase_generator.DEFAULT_PHRASE_RULES_PATH,
    ranking_path: Path = ranking.DEFAULT_RANKING_PATH,
    verified_catalog_path: Path | None = DEFAULT_VERIFIED_CATALOG_PATH,
    line_item_ids: list[str] | None = None,
) -> list[LookupOutcome]:
    """Runs the full plan -> search -> capture -> rank -> decide pipeline
    with dry_run=True (never selects/commits) -- see orchestrator.py."""
    phrase_rules = phrase_generator.load_phrase_rules(phrase_rules_path)
    ranking_config = ranking.load_ranking_config(ranking_path)
    verified_records = []
    if verified_catalog_path and verified_catalog_path.exists():
        verified_records = vcs.load_verified_catalog(verified_catalog_path)

    conn = registry.create_database(registry_db_path)
    try:
        items = _load_items(project_dir, line_item_ids)
        outcomes = []
        for item in items:
            plan = orchestrator.build_lookup_plan(item, conn, phrase_rules, verified_records)
            outcomes.append(orchestrator.execute_plan(plan, item, adapter, ranking_config, phrase_rules, dry_run=True))
        return outcomes
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Recording a resolved result (manual workflow, always after approval)
# ---------------------------------------------------------------------------


_ID_SAFE_RE = re.compile(r"[^a-zA-Z0-9]+")


def _generate_mapping_id(category: str, selector: str) -> str:
    slug = _ID_SAFE_RE.sub("_", f"{category}_{selector}").strip("_").lower()
    return f"lookup_{slug}_{uuid.uuid4().hex[:8]}"


def find_existing_mapping(conn: sqlite3.Connection, item_signature: str) -> InternalMappingRecord | None:
    """Any status (not just reusable) -- used for the conflict check when
    recording a new resolution."""
    records = registry.find_by_signature(conn, item_signature)
    approved = [r for r in records if r.status == MAPPING_STATUS_APPROVED]
    return approved[0] if approved else (records[0] if records else None)


class LookupApplyBlockedError(LookupServiceError):
    """Raised when recording a resolution would silently change an
    already-approved LINE ITEM's category/selector -- mirrors
    selector_recommendation.service.RecommendationApplyBlockedError.
    Never allowed without an explicit override (see build spec 'Do not
    silently change approved mappings.')."""

    def __init__(self, line_item_id: str):
        super().__init__(
            f"{line_item_id} is already approved with a different category/selector. "
            f"Pass allow_override=True (with an explicit reason) to override it."
        )
        self.line_item_id = line_item_id


def record_resolution(
    project_dir: Path,
    registry_db_path: Path,
    backups_dir: Path,
    item: RecommendationInput,
    item_signature: str,
    search_phrase: str,
    *,
    category: str,
    selector: str,
    xactimate_description: str,
    unit: str | None,
    action: str | None,
    xactimate_item_number: str | None,
    reviewer: str,
    approval_reason: str,
    evidence_reference: str | None = None,
    xactimate_activity_raw: str | None = None,
    allow_override: bool = False,
    approve: bool = False,
    save_as_reusable_mapping: bool = True,
) -> InternalMappingRecord | None:
    """Captures a manually-resolved Xactimate dropdown result for one
    line item: (1) applies category/selector/description to THAT item
    through the existing audited review_service (never a direct write --
    same pattern as selector_recommendation.service.apply_candidate),
    optionally approving it; (2) optionally also persists the resolution
    to the internal lookup registry as a reusable mapping for future
    line items with the same signature. Both steps require the same
    non-empty reviewer + reason and never silently overwrite an existing
    approved item or mapping (see build spec 'Do not silently change
    approved mappings.')."""
    if not reviewer or not reviewer.strip():
        raise LookupServiceError("A reviewer identity is required to record a resolved Xactimate result.")
    if not approval_reason or not approval_reason.strip():
        raise LookupServiceError("An approval reason is required to record a resolved Xactimate result.")
    if not category or not selector:
        raise LookupServiceError("Both category and selector are required to record a resolved Xactimate result.")

    rows = review_service.build_effective_rows(project_dir, line_item_ids=[item.line_item_id])
    if not rows:
        raise LookupServiceError(f"Unknown line_item_id {item.line_item_id!r} in {project_dir}.")
    row = rows[0]
    if row["status"] == review_service.STATUS_APPROVED and not allow_override:
        if (row["category"], row["selector"]) != (category, selector):
            raise LookupApplyBlockedError(item.line_item_id)

    for field_name, value in {"category": category, "selector": selector, "mapped_description": xactimate_description}.items():
        review_service.edit_mapping_field(project_dir, item.line_item_id, field_name, value, reviewer, approval_reason)
    if approve:
        review_service.approve_item(project_dir, item.line_item_id, reviewer, note=approval_reason)

    if not save_as_reusable_mapping:
        return None

    conn = registry.create_database(registry_db_path)
    try:
        existing = find_existing_mapping(conn, item_signature)
        if existing is not None and existing.status == MAPPING_STATUS_APPROVED and not allow_override:
            if (existing.category, existing.selector) != (category, selector):
                raise MappingConflictError(item_signature, existing)
            # Identical CAT/SEL re-approval -- treat as a no-op update, not a conflict.

        registry.backup_registry(registry_db_path, backups_dir)

        size_key = None
        grade_key = None
        normalized_description = None
        try:
            from estimate_extractor.xactimate_lookup import signature as signature_mod

            rules = phrase_generator.load_phrase_rules()
            size_key = signature_mod.compute_size_key(item.original_description or "")
            grade_key = signature_mod.compute_grade_key(item.original_description or "", rules)
            normalized_description = signature_mod.compute_normalized_description(item.trade, item.component, item.material, item.action)
        except Exception:
            pass

        mapping_id = existing.mapping_id if (existing is not None and allow_override) else _generate_mapping_id(category, selector)
        record = InternalMappingRecord(
            mapping_id=mapping_id,
            item_signature=item_signature,
            source_description=item.original_description or "",
            search_phrase=search_phrase,
            category=category,
            selector=selector,
            xactimate_description=xactimate_description,
            xactimate_item_number=xactimate_item_number,
            unit=unit,
            action=action,
            material=item.material,
            size_key=size_key,
            grade_key=grade_key,
            normalized_description=normalized_description,
            xactimate_activity_raw=xactimate_activity_raw,
            reviewer=reviewer,
            approval_reason=approval_reason,
            evidence_reference=evidence_reference,
            status=MAPPING_STATUS_APPROVED,
            created_at=existing.created_at if existing else utc_now_iso(),
            updated_at=utc_now_iso(),
        )
        registry.save_record(conn, record)
        _append_event(
            registry_db_path,
            {
                "timestamp": utc_now_iso(),
                "action": "record_resolution",
                "mapping_id": record.mapping_id,
                "item_signature": item_signature,
                "category": category,
                "selector": selector,
                "reviewer": reviewer,
                "reason": approval_reason,
                "line_item_id": item.line_item_id,
            }
        )
        return record
    finally:
        conn.close()


def disable_mapping(registry_db_path: Path, backups_dir: Path, mapping_id: str, reviewer: str, reason: str) -> InternalMappingRecord:
    return _change_mapping_status(registry_db_path, backups_dir, mapping_id, reviewer, reason, MAPPING_STATUS_DISABLED, "disable_mapping")


def reject_mapping(registry_db_path: Path, backups_dir: Path, mapping_id: str, reviewer: str, reason: str) -> InternalMappingRecord:
    return _change_mapping_status(registry_db_path, backups_dir, mapping_id, reviewer, reason, MAPPING_STATUS_REJECTED, "reject_mapping")


def _change_mapping_status(
    registry_db_path: Path, backups_dir: Path, mapping_id: str, reviewer: str, reason: str, new_status: str, action_name: str
) -> InternalMappingRecord:
    if not reviewer or not reviewer.strip():
        raise LookupServiceError(f"A reviewer identity is required to {action_name}.")
    if not reason or not reason.strip():
        raise LookupServiceError(f"A reason is required to {action_name}.")

    conn = registry.create_database(registry_db_path)
    try:
        record = registry.find_by_mapping_id(conn, mapping_id)
        if record is None:
            raise LookupServiceError(f"No internal mapping found for mapping_id {mapping_id!r}.")
        registry.backup_registry(registry_db_path, backups_dir)
        registry.set_status(conn, mapping_id, new_status)
        _append_event(
            registry_db_path,
            {
                "timestamp": utc_now_iso(), "action": action_name, "mapping_id": mapping_id,
                "reviewer": reviewer, "reason": reason,
                "status": f"{record.status} -> {new_status}",
            }
        )
        return registry.find_by_mapping_id(conn, mapping_id)
    finally:
        conn.close()


def record_reuse_outcome(registry_db_path: Path, mapping_id: str, *, success: bool) -> None:
    """Bumps usage/success/rejection counters -- called whenever a
    trusted-path mapping is actually applied to a line item, from
    whichever caller applies it (CLI/UI)."""
    conn = registry.create_database(registry_db_path)
    try:
        registry.record_usage(conn, mapping_id, success=success)
    finally:
        conn.close()


def list_mappings(registry_db_path: Path, *, active_only: bool = True) -> list[InternalMappingRecord]:
    conn = registry.create_database(registry_db_path)
    try:
        records = registry.load_all_records(conn)
    finally:
        conn.close()
    return [r for r in records if r.is_reusable] if active_only else records


# ---------------------------------------------------------------------------
# Audit log (registry is cross-project/global data -- its audit trail
# lives beside it in config/, not inside any one project's review/ dir)
# ---------------------------------------------------------------------------


def _events_path_for_registry(registry_db_path: Path) -> Path:
    """Colocated with whichever registry database is actually in use --
    a tmp_path registry (as every test uses) must never write its audit
    trail into the real repository's config/ directory. Only a caller
    using the real DEFAULT_REGISTRY_DB_PATH ends up at DEFAULT_EVENTS_PATH."""
    return registry_db_path.parent / "internal_lookup_events.json"


def _append_event(registry_db_path: Path, event: dict) -> None:
    events_path = _events_path_for_registry(registry_db_path)
    events = get_events(registry_db_path)
    events.append(event)
    events_path.parent.mkdir(parents=True, exist_ok=True)
    events_path.write_text(json.dumps(events, indent=2, default=str), encoding="utf-8")


def get_events(registry_db_path: Path = DEFAULT_REGISTRY_DB_PATH) -> list[dict]:
    events_path = _events_path_for_registry(registry_db_path)
    if not events_path.exists():
        return []
    try:
        return json.loads(events_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class LookupStats:
    items_evaluated: int = 0
    items_resolved_by_existing_mapping: int = 0
    items_requiring_description_search: int = 0
    learned_mapping_reuse_rate: float | None = None
    review_required_rate: str = "N/A (requires `lookup dry-run` against captured dropdown data)"
    no_match_rate: str = "N/A (requires `lookup dry-run` against captured dropdown data)"
    description_searches_resolved: str = "N/A (requires `lookup dry-run` against captured dropdown data)"
    total_registry_records: int = 0
    active_registry_records: int = 0
    top1_agreement: float | None = None
    top3_agreement: float | None = None
    ground_truth_items: int = 0


def compute_lookup_stats(
    project_dirs: list[Path],
    registry_db_path: Path = DEFAULT_REGISTRY_DB_PATH,
    *,
    phrase_rules_path: Path = phrase_generator.DEFAULT_PHRASE_RULES_PATH,
    verified_catalog_path: Path | None = DEFAULT_VERIFIED_CATALOG_PATH,
) -> LookupStats:
    stats = LookupStats()
    all_records = list_mappings(registry_db_path, active_only=False)
    stats.total_registry_records = len(all_records)
    stats.active_registry_records = sum(1 for r in all_records if r.is_reusable)

    ground_truth_hits_top1 = 0
    ground_truth_total = 0

    for project_dir in project_dirs:
        plans = plan_for_project(
            project_dir, registry_db_path, phrase_rules_path=phrase_rules_path, verified_catalog_path=verified_catalog_path
        )
        stats.items_evaluated += len(plans)
        stats.items_resolved_by_existing_mapping += sum(1 for p in plans if p.path == LOOKUP_PATH_TRUSTED)
        stats.items_requiring_description_search += sum(1 for p in plans if p.path == LOOKUP_PATH_DESCRIPTION_SEARCH)

        ground_truth = recommendation_service.real_ground_truth(project_dir)
        for plan in plans:
            truth = ground_truth.get(plan.line_item_id)
            if truth is None or plan.trusted_mapping is None:
                continue
            ground_truth_total += 1
            if (plan.trusted_mapping.category, plan.trusted_mapping.selector) == truth:
                ground_truth_hits_top1 += 1

    if stats.items_evaluated:
        stats.learned_mapping_reuse_rate = round(stats.items_resolved_by_existing_mapping / stats.items_evaluated, 4)

    stats.ground_truth_items = ground_truth_total
    if ground_truth_total:
        stats.top1_agreement = round(ground_truth_hits_top1 / ground_truth_total, 4)
        stats.top3_agreement = stats.top1_agreement  # trusted path is a single exact CAT/SEL -- top-1 and top-3 coincide
    return stats


# ---------------------------------------------------------------------------
# Automation plan / dry-run reporting (structured, richer than plain
# LookupPlan/LookupOutcome -- for `automation plan` / `automation
# dry-run` CLI presentation. Reuses build_lookup_plan / dry_run_for_project
# directly; adds no new orchestration logic of its own.)
# ---------------------------------------------------------------------------

ALL_STOP_REASONS = (
    STOP_REASON_NO_RESULTS,
    STOP_REASON_EXTRACTION_FAILED,
    STOP_REASON_AMBIGUOUS,
    STOP_REASON_HARD_CONFLICT,
    STOP_REASON_FIELD_MISMATCH,
    STOP_REASON_UNIT_QUANTITY_INVALID,
    STOP_REASON_CONTEXT_UNVERIFIED,
    STOP_REASON_ADAPTER_ERROR,
    STOP_REASON_UNIT_MISMATCH,
    STOP_REASON_UNSUPPORTED_ADAPTER,
    STOP_REASON_UNEXPECTED_DIALOG,
)


def describe_automation_plan_entry(item: RecommendationInput, plan: LookupPlan) -> dict:
    """The per-item structure `automation plan` prints: lookup method,
    CAT/SEL or description search phrase, expected dropdown terms,
    quantity, the fixed sequence of adapter calls execute_plan() would
    make, and every safety-stop condition that could interrupt it. This
    never touches an adapter -- it describes what WOULD happen."""
    if plan.path == LOOKUP_PATH_TRUSTED:
        trusted = plan.trusted_mapping
        expected_terms = [trusted.category, trusted.selector]
        search_phrase = None
        search_step = "search_by_category_selector"
    else:
        expected_terms = list(plan.phrase_result.terms) if plan.phrase_result else []
        search_phrase = plan.phrase_result.phrase if plan.phrase_result else plan.search_input
        search_step = "search_by_description"

    planned_actions = [
        "verify_application", "verify_project", "focus_search", "clear_search", search_step,
        "capture_dropdown", "parse_dropdown", "rank_candidates",
        "select_candidate (only if AUTO_SELECT)", "read_populated_fields (only if AUTO_SELECT)",
        "enter_quantity (only if AUTO_SELECT)", "commit_item (only if AUTO_SELECT)",
        "capture_evidence (only if AUTO_SELECT)",
    ]

    return {
        "line_item_id": item.line_item_id,
        "lookup_method": plan.path,
        "search_input": plan.search_input,
        "search_phrase": search_phrase,
        "expected_dropdown_terms": expected_terms,
        "quantity": item.quantity,
        "unit": item.source_unit,
        "planned_adapter_actions": planned_actions,
        "safety_checks": [
            "application_verified", "project_verified", "dropdown_results_present", "extraction_succeeded",
            "candidate_score_and_margin_sufficient", "no_hard_conflict", "extraction_confidence_reliable",
            "quantity_valid", "populated_fields_match_selection", "populated_unit_matches_source_unit",
            "adapter_supports_live_execution (live runs only)",
        ],
        "stop_conditions": list(ALL_STOP_REASONS),
    }


def build_automation_plan(
    project_dir: Path,
    registry_db_path: Path = DEFAULT_REGISTRY_DB_PATH,
    *,
    phrase_rules_path: Path = phrase_generator.DEFAULT_PHRASE_RULES_PATH,
    verified_catalog_path: Path | None = DEFAULT_VERIFIED_CATALOG_PATH,
    line_item_ids: list[str] | None = None,
) -> list[dict]:
    phrase_rules = phrase_generator.load_phrase_rules(phrase_rules_path)
    verified_records = []
    if verified_catalog_path and verified_catalog_path.exists():
        verified_records = vcs.load_verified_catalog(verified_catalog_path)

    conn = registry.create_database(registry_db_path)
    try:
        items = _load_items(project_dir, line_item_ids)
        entries = []
        for item in items:
            plan = orchestrator.build_lookup_plan(item, conn, phrase_rules, verified_records)
            entries.append(describe_automation_plan_entry(item, plan))
        return entries
    finally:
        conn.close()


@dataclass(slots=True)
class DiagnosticsReport:
    adapter_class: str
    supports_live_execution: bool
    application_verified: bool | None
    project_verified: bool | None
    registry_reachable: bool
    registry_record_count: int
    phrase_rules_loaded: bool
    ranking_config_loaded: bool
    warnings: list


def run_diagnostics(
    adapter: XactimateAdapter | None = None,
    registry_db_path: Path = DEFAULT_REGISTRY_DB_PATH,
    *,
    phrase_rules_path: Path = phrase_generator.DEFAULT_PHRASE_RULES_PATH,
    ranking_path: Path = ranking.DEFAULT_RANKING_PATH,
) -> DiagnosticsReport:
    """Read-only health check: never launches or connects to Xactimate --
    see build spec "Do not attempt to launch Xactimate." If no adapter is
    passed, checks against a fresh FakeXactimateAdapter and warns loudly
    that no real adapter is configured."""
    from estimate_extractor.xactimate_lookup.adapter import FakeXactimateAdapter

    warnings: list[str] = []
    if adapter is None:
        adapter = FakeXactimateAdapter()
        warnings.append("No real XactimateAdapter is configured -- diagnostics ran against FakeXactimateAdapter (no live Xactimate session exists).")
    if not adapter.supports_live_execution:
        warnings.append(f"{type(adapter).__name__} does not declare supports_live_execution=True; live automation runs are refused by orchestrator.execute_plan().")

    try:
        application_verified = adapter.verify_application()
        project_verified = adapter.verify_project()
    except Exception as exc:
        application_verified = None
        project_verified = None
        warnings.append(f"Adapter context verification raised: {exc}")

    registry_reachable = True
    registry_record_count = 0
    try:
        registry_record_count = len(list_mappings(registry_db_path, active_only=False))
    except Exception as exc:
        registry_reachable = False
        warnings.append(f"Could not read the internal lookup registry at {registry_db_path}: {exc}")

    phrase_rules_loaded = True
    try:
        phrase_generator.load_phrase_rules(phrase_rules_path)
    except Exception as exc:
        phrase_rules_loaded = False
        warnings.append(f"Could not load phrase rules from {phrase_rules_path}: {exc}")

    ranking_config_loaded = True
    try:
        ranking.load_ranking_config(ranking_path)
    except Exception as exc:
        ranking_config_loaded = False
        warnings.append(f"Could not load ranking config from {ranking_path}: {exc}")

    return DiagnosticsReport(
        adapter_class=type(adapter).__name__,
        supports_live_execution=adapter.supports_live_execution,
        application_verified=application_verified,
        project_verified=project_verified,
        registry_reachable=registry_reachable,
        registry_record_count=registry_record_count,
        phrase_rules_loaded=phrase_rules_loaded,
        ranking_config_loaded=ranking_config_loaded,
        warnings=warnings,
    )
