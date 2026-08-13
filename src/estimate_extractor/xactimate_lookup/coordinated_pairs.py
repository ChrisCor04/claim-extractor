"""Generalized, source-grounded detection of complementary REMOVE +
INSTALL/REPLACE source task pairs.

Some carrier estimates print an R&R (remove-and-replace) scope as TWO
separate priced line items -- "Remove [item]" and "[item]" -- rather than
one combined Xactimate R&R catalog entry. Today's execution runner treats
each as an independent task; if both independently searched and selected
a candidate, each would separately trigger Xactimate's own R&R -/+ row
pair, producing duplicate physical rows. This module only DETECTS that
shape from source-side evidence already produced by Phase 2 normalization
and execution-plan construction (trade/component/material/action/group/
unit/source order) -- it never talks to Xactimate, never reads a ranked
candidate, and never guesses from anything downstream of source data.

Deliberately conservative: a pair is confirmed only when the evidence is
UNIQUE and MUTUAL (see detect_coordinated_pairs()'s own docstring) --
every other shape (no candidate, multiple candidates, one-sided evidence)
is left unpaired, which is always safe: an unpaired task simply continues
through the existing, unmodified ordinary execution path.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: The one action value this module treats as "removes an existing
#: item" -- produced by normalization_rules.yaml's own action_rules
#: (e.g. "tear off", "remove "). Never a component/trade concept.
ACTION_REMOVE = "remove"

#: Actions a REMOVE task's partner may carry: an explicit "install"
#: action, or "unknown" -- the common real-world shape where a bare
#: replacement line ("3 tab - 25 yr. - comp. shingle roofing") carries
#: no recognized action verb at all (see normalization_rules.yaml's
#: action_rules -- nothing matches a description with no action word),
#: which is the ordinary, expected, "implicit install by omission"
#: case, not a defect. Never "remove" itself, and never any OTHER
#: recognized action (e.g. "clean", "paint") -- those are real,
#: different scopes that must never be treated as a replacement half.
_INSTALL_LIKE_ACTIONS = frozenset({"install", "unknown"})

#: How many positions apart, in EITHER direction, two tasks' source
#: order may be and still be considered for pairing. Deliberately
#: narrow and symmetric: every real pair used to calibrate this rule
#: (three pairs from a live claim, odom-insurance-v2) is exactly one
#: position apart, and nothing in the source architecture guarantees a
#: "Remove X" line is printed before its replacement "X" line -- see
#: _candidate_partners()'s own docstring. Widening this window without
#: real evidence a real claim needs it would trade false-positive risk
#: for coverage this module has no basis to claim; left as a single,
#: clearly-named constant so that tradeoff is a deliberate, visible
#: choice if it's ever revisited.
MAX_SOURCE_ORDER_DISTANCE = 1

REASON_PAIRED = "paired"
REASON_NO_CANDIDATES = "no_candidates"
REASON_AMBIGUOUS_MULTIPLE_PARTNERS = "ambiguous_multiple_partners"
REASON_PARTNER_NOT_MUTUALLY_UNIQUE = "partner_not_mutually_unique"

_REJECT_DIFFERENT_GROUP = "different_group"
_REJECT_TRADE = "trade_mismatch_or_unspecified"
_REJECT_COMPONENT = "component_mismatch_or_unspecified"
_REJECT_MATERIAL = "material_mismatch"
_REJECT_UNIT = "unit_mismatch"
_REJECT_PROXIMITY = "outside_source_proximity_window"
_REJECT_ACTION = "not_an_install_like_action"


@dataclass(frozen=True, slots=True)
class CandidateRejection:
    """Why one specific other task was NOT accepted as this remove
    task's partner -- persisted purely for audit/debugging, never
    consumed by detection logic itself."""

    task_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class PairDetection:
    """The complete, explainable outcome for one REMOVE task: either a
    confirmed pair, or exactly why no pair was formed."""

    remove_task_id: str
    replace_task_id: str | None
    paired: bool
    reason: str
    considered: tuple[CandidateRejection, ...] = field(default_factory=tuple)


def _group_key(task) -> tuple:
    return (task.area_name, task.section_name)


def _trade_compatible(a: str | None, b: str | None) -> bool:
    """Trade must be explicitly known and equal on both sides -- unlike
    material below, an unknown/missing trade is never treated as
    "compatible by omission": trade is the primary signal that the two
    tasks concern the same real-world scope at all, so silence here
    must fail closed, not pass by default."""
    if not a or a == "unknown" or not b or b == "unknown":
        return False
    return a == b


def _component_compatible(a: str | None, b: str | None) -> bool:
    """Same reasoning as trade: component is a required, positive
    signal, not an optional one."""
    if not a or a == "unknown" or not b or b == "unknown":
        return False
    return a == b


def _material_compatible(a: str | None, b: str | None) -> bool:
    """Material is compatible-by-omission when either side never
    specified one -- mirrors ranking.py's own established "unspecified
    is not a conflict, only an explicit different value is" principle
    (see score_dropdown_candidate()'s material handling). Only an
    explicit, different value on BOTH sides is a real mismatch."""
    if not a or not b:
        return True
    return a == b


def _unit_compatible(a: str | None, b: str | None) -> bool:
    """Deliberately strict (exact match only, case-insensitive) --
    unlike material, a missing unit on either side is NOT treated as
    compatible: every real pair in the calibration evidence states its
    own unit explicitly, and unit-conversion compatibility (e.g. SQ vs
    SF) is a distinct, unvalidated claim this module does not make."""
    if not a or not b:
        return False
    return a.strip().lower() == b.strip().lower()


def _evidence_match(remove_task, other_task) -> tuple[bool, str | None]:
    """The complete positive-evidence gate a candidate partner must
    clear, independent of which task is "first" in source order."""
    if _group_key(remove_task) != _group_key(other_task):
        return False, _REJECT_DIFFERENT_GROUP
    if not _trade_compatible(remove_task.normalized_trade, other_task.normalized_trade):
        return False, _REJECT_TRADE
    if not _component_compatible(remove_task.normalized_component, other_task.normalized_component):
        return False, _REJECT_COMPONENT
    if not _material_compatible(remove_task.normalized_material, other_task.normalized_material):
        return False, _REJECT_MATERIAL
    if not _unit_compatible(remove_task.source_unit, other_task.source_unit):
        return False, _REJECT_UNIT
    if abs(remove_task.source_order - other_task.source_order) > MAX_SOURCE_ORDER_DISTANCE:
        return False, _REJECT_PROXIMITY
    return True, None


def _candidate_partners(remove_task, tasks) -> tuple[list, list[CandidateRejection]]:
    """Every task that could plausibly complete `remove_task`'s pair.

    Deliberately order-agnostic: a task at source_order - 1 is
    considered exactly like one at source_order + 1. Nothing in the
    canonical extraction schema or this codebase's normalization layer
    asserts a removal line is always printed before its replacement --
    treating "remove first" as semantic here would be an unjustified
    assumption this module was explicitly told not to make."""
    accepted = []
    rejections: list[CandidateRejection] = []
    for t in tasks:
        if t.task_id == remove_task.task_id:
            continue
        if t.normalized_action not in _INSTALL_LIKE_ACTIONS:
            continue
        ok, reason = _evidence_match(remove_task, t)
        if ok:
            accepted.append(t)
        elif reason in (_REJECT_PROXIMITY,):
            # Never worth persisting as a "near miss" -- most of the
            # estimate is outside the proximity window by construction;
            # recording every one would swamp the real diagnostics.
            continue
        else:
            rejections.append(CandidateRejection(t.task_id, reason))
    return accepted, rejections


def _candidate_removers(other_task, tasks) -> list:
    """The mirror of _candidate_partners(): every REMOVE task that
    could plausibly claim `other_task` as ITS partner. Used only to
    prove mutual uniqueness -- see detect_coordinated_pairs()."""
    out = []
    for t in tasks:
        if t.task_id == other_task.task_id:
            continue
        if t.normalized_action != ACTION_REMOVE:
            continue
        ok, _ = _evidence_match(t, other_task)
        if ok:
            out.append(t)
    return out


def detect_coordinated_pairs(tasks) -> list[PairDetection]:
    """Pure, deterministic, offline pairing pass over a plan's tasks.

    A pair (R, P) is confirmed ONLY when the relationship is mutually
    unique: R's only qualifying partner is P, AND P's only qualifying
    remover is R. This single rule is what correctly handles every
    required shape without special-casing any of them:

    - a normal pair: mutual uniqueness holds, pairs.
    - reversed source order: _evidence_match() never looks at which
      task comes first, only at the bounded distance between them --
      pairs exactly the same as the normal order.
    - unrelated adjacent tasks: fail _evidence_match() (trade/
      component/material/unit/group), never even become candidates.
    - ambiguous many-to-one (two REMOVE tasks both adjacent to the same
      replacement): the shared replacement's candidate-removers list
      has 2 entries, so mutual uniqueness fails for BOTH removers --
      neither pairs.
    - one-to-many (one REMOVE task adjacent to two plausible
      replacements): _candidate_partners() itself returns 2, already
      rejected before the mutual check.

    Only REMOVE tasks are ever the entry point -- a task with no
    REMOVE-actioned neighbor at all is simply never mentioned in the
    returned list (equivalent to "not applicable", not "unpaired");
    only a task whose normalized_action IS "remove" produces a
    PairDetection record, paired or not.
    """
    results: list[PairDetection] = []
    remove_tasks = [t for t in tasks if t.normalized_action == ACTION_REMOVE]
    for remove_task in remove_tasks:
        partners, rejections = _candidate_partners(remove_task, tasks)
        if not partners:
            results.append(PairDetection(remove_task.task_id, None, False, REASON_NO_CANDIDATES, tuple(rejections)))
            continue
        if len(partners) > 1:
            results.append(
                PairDetection(remove_task.task_id, None, False, REASON_AMBIGUOUS_MULTIPLE_PARTNERS, tuple(rejections))
            )
            continue
        partner = partners[0]
        removers = _candidate_removers(partner, tasks)
        if len(removers) > 1:
            results.append(
                PairDetection(remove_task.task_id, None, False, REASON_PARTNER_NOT_MUTUALLY_UNIQUE, tuple(rejections))
            )
            continue
        results.append(PairDetection(remove_task.task_id, partner.task_id, True, REASON_PAIRED, tuple(rejections)))
    return results


def pair_id_for(remove_task_id: str, replace_task_id: str) -> str:
    """Deterministic, stable identity -- the same two task IDs always
    produce the same pair_id, regardless of how many times detection
    re-runs."""
    return f"pair_{remove_task_id}_{replace_task_id}"
