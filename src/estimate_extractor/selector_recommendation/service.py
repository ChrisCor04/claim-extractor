"""Orchestrates the selector-recommendation layer: builds inputs from
Phase 2's existing mapping output (never rereads a PDF), generates and
scores candidates, merges in Phase 3.5 verified-catalog / Phase 2
placeholder priority, applies a chosen candidate through the existing
audited review_service (never a direct JSON write), records feedback
events, and evaluates recommendations against real (non-synthetic)
approvals only.

Every write path here is one this repository already trusts:
review_service.edit_mapping_field / approve_item for applying a
candidate, verified_catalog_service.add_record / apply_verified_match for
saving a reusable rule. This module adds no new way to mutate a project's
mapping state -- see build spec "Do not bypass human-verification
confirmation requirements."
"""

from __future__ import annotations

import dataclasses
import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from estimate_extractor.selector_catalog import database
from estimate_extractor.selector_catalog.image_inventory import resolve_screenshot_path
from estimate_extractor.selector_catalog.models import SelectorRecord
from estimate_extractor.selector_recommendation import candidate_generation, query, ranker, scoring
from estimate_extractor.selector_recommendation.candidate_generation import RecommendationRules
from estimate_extractor.selector_recommendation.models import (
    CANDIDATE_SOURCE_PLACEHOLDER_MAPPING,
    CANDIDATE_SOURCE_VERIFIED_CATALOG,
    RECOMMENDATION_STATE_POSSIBLE,
    RECOMMENDATION_STATE_STRONG,
    RECOMMENDATION_STATE_WEAK,
    Candidate,
    RecommendationInput,
    RecommendationResult,
)
from estimate_extractor.selector_recommendation.scoring import ScoringConfig
from estimate_extractor.ui import review_service
from estimate_extractor.ui import verified_catalog_service as vcs

DEFAULT_RULES_PATH = candidate_generation.DEFAULT_RULES_PATH
DEFAULT_SCORING_PATH = scoring.DEFAULT_SCORING_PATH

logger = logging.getLogger("estimate_extractor")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class RecommendationServiceError(Exception):
    pass


class RecommendationApplyBlockedError(RecommendationServiceError):
    """Raised when applying a candidate would silently change an
    already-approved item's category/selector -- mirrors
    verified_catalog_service.ApprovalOverrideBlockedError. Never allowed
    without an explicit override confirmation from the caller."""

    def __init__(self, line_item_id: str):
        super().__init__(
            f"{line_item_id} is already approved with a different category/selector. "
            f"Pass allow_override_approved=True (with an explicit reviewer reason) to override it -- "
            f"an approved mapping is never overwritten silently."
        )
        self.line_item_id = line_item_id


def _read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


# ---------------------------------------------------------------------------
# Input construction (from existing Phase 2 outputs -- never a PDF re-read)
# ---------------------------------------------------------------------------


def load_normalized_items(project_dir: Path) -> dict[str, dict]:
    """Public read-only accessor for normalized_estimate.json, keyed by
    line_item_id -- used by both this module and the review UI so callers
    never need their own JSON-loading logic for it."""
    return {i["line_item_id"]: i for i in _read_json(project_dir / "mapping" / "normalized_estimate.json", [])}


def build_recommendation_input(row: dict, normalized_item: dict | None) -> RecommendationInput:
    """`row` is one entry from review_service.build_effective_rows() --
    the existing machine+override merged view. `normalized_item` is the
    matching raw normalized_estimate.json entry, used only for the
    free-form attributes bag (not duplicated onto effective rows)."""
    attributes = {}
    if normalized_item:
        attributes = dict((normalized_item.get("normalized") or {}).get("attributes") or {})
    return RecommendationInput(
        line_item_id=row["line_item_id"],
        original_description=row.get("original_description"),
        normalized_description=None,
        action=row.get("normalized_action"),
        trade=row.get("normalized_trade"),
        component=row.get("normalized_component"),
        material=row.get("normalized_material"),
        attributes=attributes,
        source_unit=row.get("original_unit") or row.get("unit"),
        quantity=row.get("quantity"),
        section=row.get("section_name"),
        area=row.get("area_name"),
        coverage=row.get("coverage_id"),
        existing_mapping_status=row.get("mapping_status"),
        # Both drawn from the SAME source (Phase 2's untouched machine
        # best_match) -- never mixed with a reviewer override, which would
        # silently combine an original category with an overridden
        # selector (or vice versa) into a nonsensical placeholder pair.
        existing_category=row.get("machine_category"),
        existing_selector=row.get("machine_selector"),
        review_status=row.get("status"),
    )


# ---------------------------------------------------------------------------
# Verified-catalog / placeholder candidate construction
# ---------------------------------------------------------------------------


def _row_shape_for_verified_match(item: RecommendationInput) -> dict:
    return {
        "normalized_trade": item.trade,
        "normalized_component": item.component,
        "unit": item.source_unit,
        "normalized_action": item.action,
        "original_description": item.original_description,
    }


def _verified_candidates(item: RecommendationInput, verified_records: list) -> list[Candidate]:
    if not verified_records:
        return []
    row = _row_shape_for_verified_match(item)
    matches = vcs.find_verified_matches(row, verified_records)
    candidates: list[Candidate] = []
    for m in matches:
        if m.record.verification_status != vcs.VERIFICATION_STATUS_HUMAN_VERIFIED:
            continue
        candidates.append(
            Candidate(
                category=m.record.category,
                selector=m.record.selector,
                description=m.record.description,
                source_needs_review=False,
                score=1.0,
                rank=0,
                match_reasons=[f"Phase 3.5 human-verified catalog rule ({m.match_reason})"],
                penalties=[],
                source_image=None,
                ocr_confidence=None,
                source=CANDIDATE_SOURCE_VERIFIED_CATALOG,
            )
        )
    return candidates


def _has_verified_backing(record: SelectorRecord, verified_records: list) -> bool:
    match = vcs.find_record(verified_records, record.category, record.selector)
    return match is not None and match.verification_status == vcs.VERIFICATION_STATUS_HUMAN_VERIFIED


def _placeholder_candidate(item: RecommendationInput) -> Candidate | None:
    if not item.existing_category or not item.existing_selector:
        return None
    return Candidate(
        category=item.existing_category,
        selector=item.existing_selector,
        description=item.original_description or "",
        source_needs_review=False,
        score=0.0,
        rank=0,
        match_reasons=["Phase 2 placeholder catalog mapping (lowest priority; not selector-catalog backed)"],
        penalties=["placeholder mapping is not backed by the Phase 3.6 selector catalog or a verified rule"],
        source_image=None,
        ocr_confidence=None,
        source=CANDIDATE_SOURCE_PLACEHOLDER_MAPPING,
    )


# ---------------------------------------------------------------------------
# Per-item / per-project recommendation
# ---------------------------------------------------------------------------


def recommend_for_item(
    item: RecommendationInput,
    conn: sqlite3.Connection,
    rules: RecommendationRules,
    scoring_config: ScoringConfig,
    *,
    verified_records: list | None = None,
    include_uncertain: bool = False,
    locked_by_approval: bool = False,
) -> RecommendationResult:
    start = time.monotonic()
    verified_records = verified_records or []

    verified_candidates = _verified_candidates(item, verified_records)
    verified_keys = {(c.category, c.selector) for c in verified_candidates}

    pool, hinted_categories = candidate_generation.generate_candidate_pool(
        item,
        conn,
        rules,
        max_category_pool=scoring_config.max_category_pool,
        prefilter_limit=max(scoring_config.max_candidates_returned * 25, 100),
        include_uncertain=include_uncertain,
    )

    scored = []
    for record in pool:
        if record.key in verified_keys:
            continue
        verified_evidence = _has_verified_backing(record, verified_records)
        scored.append(
            scoring.score_candidate(
                item,
                record,
                hinted_categories,
                rules,
                scoring_config,
                verified_evidence=verified_evidence,
                include_uncertain=include_uncertain,
            )
        )
    selector_candidates = ranker.rank_scored_candidates(scored, scoring_config)
    placeholder_candidate = _placeholder_candidate(item)

    candidates, state = ranker.merge_and_rank(verified_candidates, selector_candidates, placeholder_candidate, scoring_config)

    latency_ms = (time.monotonic() - start) * 1000
    return RecommendationResult(
        line_item_id=item.line_item_id,
        state=state,
        candidates=candidates,
        locked_by_approval=locked_by_approval,
        include_uncertain=include_uncertain,
        latency_ms=round(latency_ms, 3),
    )


def recommend_for_project(
    project_dir: Path,
    db_path: Path,
    *,
    rules_path: Path = DEFAULT_RULES_PATH,
    scoring_path: Path = DEFAULT_SCORING_PATH,
    verified_catalog_path: Path | None = None,
    include_uncertain: bool = False,
    top: int | None = None,
    line_item_ids: list[str] | None = None,
) -> list[RecommendationResult]:
    rules = candidate_generation.load_recommendation_rules(rules_path)
    scoring_config = scoring.load_scoring_config(scoring_path)
    if top is not None:
        scoring_config = dataclasses.replace(scoring_config, max_candidates_returned=top)

    normalized_by_id = load_normalized_items(project_dir)
    rows = review_service.build_effective_rows(project_dir, line_item_ids=line_item_ids)

    verified_records: list = []
    if verified_catalog_path and verified_catalog_path.exists():
        verified_records = vcs.load_verified_catalog(verified_catalog_path)

    if not db_path.exists():
        raise RecommendationServiceError(f"No selector database found at {db_path}. Run `selectors import` first.")

    conn = database.create_database(db_path)
    try:
        results = []
        for row in rows:
            item = build_recommendation_input(row, normalized_by_id.get(row["line_item_id"]))
            locked = row["status"] == review_service.STATUS_APPROVED
            result = recommend_for_item(
                item,
                conn,
                rules,
                scoring_config,
                verified_records=verified_records,
                include_uncertain=include_uncertain,
                locked_by_approval=locked,
            )
            results.append(result)
    finally:
        conn.close()
    return results


def recommend_for_single_item(
    project_dir: Path,
    db_path: Path,
    line_item_id: str,
    *,
    rules_path: Path = DEFAULT_RULES_PATH,
    scoring_path: Path = DEFAULT_SCORING_PATH,
    verified_catalog_path: Path | None = None,
    include_uncertain: bool = False,
    top: int | None = None,
) -> RecommendationResult | None:
    """Convenience wrapper for interactive callers (the review UI) that
    only need one item's recommendations -- avoids making the UI layer
    re-derive the project-loading logic in recommend_for_project."""
    results = recommend_for_project(
        project_dir,
        db_path,
        rules_path=rules_path,
        scoring_path=scoring_path,
        verified_catalog_path=verified_catalog_path,
        include_uncertain=include_uncertain,
        top=top,
        line_item_ids=[line_item_id],
    )
    return results[0] if results else None


def _recommendations_path(project_dir: Path) -> Path:
    return project_dir / "review" / "selector_recommendations.json"


def write_recommendations(project_dir: Path, results: list[RecommendationResult]) -> Path:
    path = _recommendations_path(project_dir)
    _write_json(path, [r.to_dict() for r in results])
    return path


def load_recommendations(project_dir: Path) -> list[RecommendationResult]:
    raw = _read_json(_recommendations_path(project_dir), [])
    return [RecommendationResult.from_dict(r) for r in raw]


def resolve_candidate_screenshot(candidate: Candidate, extracted_root: Path) -> Path | None:
    """Resolves a candidate's source screenshot for preview. Never
    crashes and never embeds the image into project state (see build spec
    "Source screenshot access" / "Do not embed thousands of screenshots
    into project state") -- on any failure it logs a warning, marks
    ``candidate.screenshot_available = False``, and leaves the candidate
    itself untouched so the caller can still show it with a "provenance
    unavailable" note."""
    if not candidate.source_image:
        candidate.screenshot_available = False
        return None
    try:
        path = resolve_screenshot_path(extracted_root, candidate.source_image)
    except Exception:
        candidate.screenshot_available = False
        logger.warning(
            "Could not resolve source screenshot for %s/%s (source_image=%r)",
            candidate.category, candidate.selector, candidate.source_image,
        )
        return None
    if not path.exists():
        candidate.screenshot_available = False
        logger.warning(
            "Source screenshot missing on disk for %s/%s: %s", candidate.category, candidate.selector, path
        )
        return None
    candidate.screenshot_available = True
    return path


# ---------------------------------------------------------------------------
# Applying a candidate (audited, routed entirely through review_service)
# ---------------------------------------------------------------------------


def apply_candidate(
    project_dir: Path,
    line_item_id: str,
    candidate: Candidate,
    reviewer: str,
    reason: str,
    *,
    approve: bool = False,
    allow_override_approved: bool = False,
) -> list[dict]:
    if not reason or not reason.strip():
        raise RecommendationServiceError("A reason is required to apply a recommended selector.")

    rows = review_service.build_effective_rows(project_dir, line_item_ids=[line_item_id])
    if not rows:
        raise RecommendationServiceError(f"Unknown line_item_id {line_item_id!r}.")
    row = rows[0]

    if row["status"] == review_service.STATUS_APPROVED and not allow_override_approved:
        if (row["category"], row["selector"]) != (candidate.category, candidate.selector):
            raise RecommendationApplyBlockedError(line_item_id)

    events = []
    field_map = {"category": candidate.category, "selector": candidate.selector, "mapped_description": candidate.description}
    for field_name, value in field_map.items():
        events.append(review_service.edit_mapping_field(project_dir, line_item_id, field_name, value, reviewer, reason))

    action = "accepted"
    if approve:
        events.append(review_service.approve_item(project_dir, line_item_id, reviewer, note=reason))
        action = "accepted_and_approved"

    record_recommendation_event(
        project_dir,
        line_item_id,
        candidate,
        action,
        reviewer,
        reason=reason,
        selected_category=candidate.category,
        selected_selector=candidate.selector,
    )
    return events


def reject_candidate(project_dir: Path, line_item_id: str, candidate: Candidate, reviewer: str, reason: str = "") -> dict:
    return record_recommendation_event(project_dir, line_item_id, candidate, "rejected", reviewer, reason=reason)


def mark_candidate_irrelevant(project_dir: Path, line_item_id: str, candidate: Candidate, reviewer: str, reason: str = "") -> dict:
    return record_recommendation_event(project_dir, line_item_id, candidate, "marked_irrelevant", reviewer, reason=reason)


def save_recommendation_as_verified_rule(
    catalog_path: Path,
    backups_dir: Path,
    project_dir: Path,
    item: RecommendationInput,
    candidate: Candidate,
    reviewer: str,
    confirmations: dict[str, bool],
    reviewer_note: str = "",
):
    """Delegates entirely to the existing Phase 3.5 verified-catalog
    workflow -- same three required confirmations, same backup-before-
    write, same audit trail. This function adds no new verification
    bypass; see build spec "Save as reusable verified rule must use the
    Phase 3.5 verification workflow and confirmations."."""
    fields = {
        "category": candidate.category,
        "selector": candidate.selector,
        "description": candidate.description,
        "unit": item.source_unit or "",
        "trade": item.trade or "unknown",
        "component": item.component or "unknown",
        "aliases": [item.original_description] if item.original_description else [],
        "supported_actions": [item.action] if item.action and item.action != "unknown" else [],
    }
    record = vcs.add_record(
        catalog_path,
        backups_dir,
        project_dir,
        fields,
        reviewer,
        verification_status=vcs.VERIFICATION_STATUS_HUMAN_VERIFIED,
        confirmations=confirmations,
        reviewer_note=reviewer_note,
    )
    vcs.apply_verified_match(
        project_dir, item.line_item_id, record, reviewer, reviewer_note or "saved as reusable rule from selector recommendation"
    )
    record_recommendation_event(
        project_dir,
        item.line_item_id,
        candidate,
        "saved_reusable_rule",
        reviewer,
        reason=reviewer_note,
        selected_category=record.category,
        selected_selector=record.selector,
    )
    return record


# ---------------------------------------------------------------------------
# Feedback-event capture (audited, append-only -- never retrains anything)
# ---------------------------------------------------------------------------


def _events_path(project_dir: Path) -> Path:
    return project_dir / "review" / "selector_recommendation_events.json"


def get_recommendation_events(project_dir: Path) -> list[dict]:
    return _read_json(_events_path(project_dir), [])


def record_recommendation_event(
    project_dir: Path,
    line_item_id: str,
    candidate: Candidate | None,
    action: str,
    reviewer: str,
    *,
    reason: str = "",
    selected_category: str | None = None,
    selected_selector: str | None = None,
) -> dict:
    events = get_recommendation_events(project_dir)
    event = {
        "event_id": f"rec_event_{len(events) + 1:03d}",
        "timestamp": utc_now_iso(),
        "line_item_id": line_item_id,
        "candidate_category": candidate.category if candidate else None,
        "candidate_selector": candidate.selector if candidate else None,
        "candidate_score": candidate.score if candidate else None,
        "candidate_rank": candidate.rank if candidate else None,
        "action": action,
        "reviewer": reviewer,
        "reason": reason or "",
        "selected_category": selected_category,
        "selected_selector": selected_selector,
        "source_project": project_dir.name,
    }
    events.append(event)
    _write_json(_events_path(project_dir), events)
    return event


def compute_recommendation_stats(project_dir: Path) -> dict:
    results = load_recommendations(project_dir)
    events = get_recommendation_events(project_dir)
    by_state: dict[str, int] = {}
    for r in results:
        by_state[r.state] = by_state.get(r.state, 0) + 1
    by_action: dict[str, int] = {}
    for e in events:
        by_action[e["action"]] = by_action.get(e["action"], 0) + 1
    candidate_counts = [len(r.candidates) for r in results]
    return {
        "items_with_recommendations": len(results),
        "by_state": by_state,
        "average_candidates_per_item": round(sum(candidate_counts) / len(candidate_counts), 3) if candidate_counts else 0.0,
        "feedback_events": len(events),
        "events_by_action": by_action,
    }


# ---------------------------------------------------------------------------
# Evaluation framework
# ---------------------------------------------------------------------------

_SYNTHETIC_MARKERS = ("benchmark", "simulated", "synthetic")


def _is_synthetic_item_state(item_state: dict) -> bool:
    """Real Phase 3 benchmark runs in this repository stamp reviewer
    "benchmark-run" and notes like "simulated verification" -- see
    docs/selector-recommendation.md "Evaluation ground truth". Anything
    matching those markers is excluded from ground truth per build spec
    "Do not treat synthetic Phase 3 benchmark approvals as genuine ground
    truth."."""
    reviewer = (item_state.get("reviewer") or "").lower()
    note = (item_state.get("reviewer_note") or "").lower()
    if any(marker in reviewer for marker in _SYNTHETIC_MARKERS):
        return True
    if any(marker in note for marker in _SYNTHETIC_MARKERS):
        return True
    for override in (item_state.get("overrides") or {}).values():
        if any(marker in str(override.get("review_reason", "")).lower() for marker in _SYNTHETIC_MARKERS):
            return True
    return False


def real_ground_truth(project_dir: Path) -> dict[str, tuple[str, str]]:
    """Returns {line_item_id: (category, selector)} for items that are
    APPROVED and whose approval is not attributable to a synthetic
    benchmark run -- the only rows this module treats as real ground
    truth. Empty when a project has none (see build spec "no ground
    truth")."""
    state = _read_json(project_dir / "review" / "review_state.json", {})
    rows_by_id = {r["line_item_id"]: r for r in review_service.build_effective_rows(project_dir)}
    ground_truth: dict[str, tuple[str, str]] = {}
    for line_item_id, item_state in state.items():
        if item_state.get("status") != review_service.STATUS_APPROVED:
            continue
        if _is_synthetic_item_state(item_state):
            continue
        row = rows_by_id.get(line_item_id)
        if not row or not row.get("category") or not row.get("selector"):
            continue
        ground_truth[line_item_id] = (row["category"], row["selector"])
    return ground_truth


@dataclass(slots=True)
class EvaluationResult:
    fixture: str
    items_evaluated: int = 0
    items_with_candidates: int = 0
    strong_candidates: int = 0
    possible_candidates: int = 0
    weak_candidates: int = 0
    no_candidates: int = 0
    ground_truth_items: int = 0
    top1_agreement: float | None = None
    top3_agreement: float | None = None
    verified_rule_recall: float | None = None
    false_strong_candidate_count: int = 0
    average_candidates_per_item: float = 0.0
    average_latency_ms: float = 0.0


def evaluate_project(
    project_dir: Path,
    db_path: Path,
    *,
    rules_path: Path = DEFAULT_RULES_PATH,
    scoring_path: Path = DEFAULT_SCORING_PATH,
    verified_catalog_path: Path | None = None,
) -> EvaluationResult:
    results = recommend_for_project(
        project_dir,
        db_path,
        rules_path=rules_path,
        scoring_path=scoring_path,
        verified_catalog_path=verified_catalog_path,
        include_uncertain=False,
    )
    ground_truth = real_ground_truth(project_dir)

    verified_records: list = []
    if verified_catalog_path and verified_catalog_path.exists():
        verified_records = vcs.load_verified_catalog(verified_catalog_path)
    rows_by_id = {r["line_item_id"]: r for r in review_service.build_effective_rows(project_dir)}

    ev = EvaluationResult(fixture=project_dir.name, items_evaluated=len(results))
    latencies = []
    candidate_counts = []
    verified_applicable_count = 0  # independent check: a verified record genuinely matches this item
    verified_surfaced_count = 0  # of those, how many actually show a verified_catalog candidate
    top1_hits = 0
    top3_hits = 0

    for r in results:
        candidate_counts.append(len(r.candidates))
        if r.latency_ms is not None:
            latencies.append(r.latency_ms)
        if r.candidates:
            ev.items_with_candidates += 1
        if r.state == RECOMMENDATION_STATE_STRONG:
            ev.strong_candidates += 1
        elif r.state == RECOMMENDATION_STATE_POSSIBLE:
            ev.possible_candidates += 1
        elif r.state == RECOMMENDATION_STATE_WEAK:
            ev.weak_candidates += 1
        else:
            ev.no_candidates += 1

        row = rows_by_id.get(r.line_item_id)
        if row is not None and verified_records:
            item_matches = [
                m
                for m in vcs.find_verified_matches(row, verified_records)
                if m.record.verification_status == vcs.VERIFICATION_STATUS_HUMAN_VERIFIED
            ]
            if item_matches:
                verified_applicable_count += 1
                if any(c.source == CANDIDATE_SOURCE_VERIFIED_CATALOG for c in r.candidates):
                    verified_surfaced_count += 1

        truth = ground_truth.get(r.line_item_id)
        if truth is not None:
            top1 = (r.candidates[0].category, r.candidates[0].selector) == truth if r.candidates else False
            top3 = any((c.category, c.selector) == truth for c in r.candidates[:3])
            top1_hits += int(top1)
            top3_hits += int(top3)
            if r.state == RECOMMENDATION_STATE_STRONG and not top1:
                ev.false_strong_candidate_count += 1

    ev.ground_truth_items = len(ground_truth)
    ev.average_candidates_per_item = round(sum(candidate_counts) / len(candidate_counts), 3) if candidate_counts else 0.0
    ev.average_latency_ms = round(sum(latencies) / len(latencies), 3) if latencies else 0.0
    if ground_truth:
        ev.top1_agreement = round(top1_hits / len(ground_truth), 4)
        ev.top3_agreement = round(top3_hits / len(ground_truth), 4)
    else:
        ev.top1_agreement = None
        ev.top3_agreement = None
        ev.false_strong_candidate_count = 0  # not meaningful without ground truth

    ev.verified_rule_recall = (
        round(verified_surfaced_count / verified_applicable_count, 4) if verified_applicable_count else None
    )
    return ev
