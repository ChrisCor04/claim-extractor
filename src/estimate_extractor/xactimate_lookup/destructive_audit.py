"""Phase 5.5D: an explicit ledger of committed estimate rows and an
audit trail for every call that can delete/cancel a grid row, built in
response to a live incident where rows successfully committed by
Execute were later removed by an unrelated cleanup cycle (see
docs/build-estimate.md Phase 5.5D).

Two separate things live here:

1. ``ProtectedRowLedger`` -- records every row Execute has successfully
   committed this session, grouped by the Xactimate group it landed in.
   Any caller about to reduce a group's row count below its protected
   count must consult this first and refuse (raise
   ``ProtectedCommittedRowError``) rather than guess.
2. ``DestructiveActionAuditor`` -- appends one JSON line per destructive
   call (attempted or refused) to a gitignored evidence file, so every
   deletion this codebase performs is independently reconstructable
   after the fact -- never just trusted.

Neither class talks to Xactimate directly; both are plain bookkeeping
that ``WindowsXactimateAdapter`` owns one instance of each and consults
before doing anything destructive."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

#: The only reasons a destructive call may declare (Phase 5.5D). A
#: vague reason ("cleanup", "recovery", "reset") is refused outright --
#: see DestructiveActionAuditor.record()'s docstring.
DESTRUCTIVE_REASONS = frozenset(
    {
        "pending_uncommitted_selection",
        "disposable_group_probe",
        "user_requested_test_cleanup",
        "explicit_baseline_restore",
    }
)


class InvalidDestructiveReason(Exception):
    """Raised when a destructive call declares a reason outside
    DESTRUCTIVE_REASONS -- refuses to log (or perform) the action
    rather than accept a vague, unauditable justification."""


@dataclass(slots=True)
class ProtectedRowRecord:
    task_id: str | None
    source_row: str | None
    group: str | None
    category: str | None
    selector: str | None
    description: str | None
    quantity: float | None
    unit: str | None
    xactimate_item_number: str | None
    committed_row_identity: tuple[str | None, str | None]
    row_count_after_commit: int | None
    timestamp: str
    verification_state: str | None


class ProtectedRowLedger:
    """Per-group record of every row Execute has successfully
    committed this session. Deliberately in-memory only (one adapter
    instance = one live session) -- a fresh adapter starts with an
    empty ledger, matching the fact that a fresh Xactimate session has
    no rows it could accidentally know about yet."""

    def __init__(self) -> None:
        self._by_group: dict[str, list[ProtectedRowRecord]] = {}

    def record(self, group: str | None, record: ProtectedRowRecord) -> None:
        key = group or ""
        self._by_group.setdefault(key, []).append(record)

    def records_for_group(self, group: str | None) -> list[ProtectedRowRecord]:
        return list(self._by_group.get(group or "", []))

    def count_for_group(self, group: str | None) -> int:
        return len(self._by_group.get(group or "", []))

    def all_records(self) -> list[ProtectedRowRecord]:
        return [r for records in self._by_group.values() for r in records]


@dataclass(slots=True)
class ExecutionContext:
    """Mutable "what's happening right now" the runner sets on the
    adapter before each task/group -- purely descriptive, used only to
    label audit entries and identify the active group for protection
    checks. Never influences ranking, routing, or commit behavior."""

    run_id: str | None = None
    task_id: str | None = None
    source_row: str | None = None
    group: str | None = None


class DestructiveActionAuditor:
    """Appends one JSON line per destructive call to `path` (created on
    first use). Never raises on its own I/O failure -- an audit-logging
    failure must never mask or block the caller's own success/failure,
    but a caller passing an invalid `reason` IS a hard stop (see
    record()'s docstring): that is a programming error in this
    codebase, not a live/IO condition to swallow."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def record(
        self,
        *,
        context: ExecutionContext,
        method: str,
        reason: str,
        caller: str,
        target_type: str,
        target_identity: str | None,
        row_count_before: int | None,
        row_identities_before: list | None,
        row_count_after: int | None,
        row_identities_after: list | None,
        result: str,
        exception: str | None = None,
    ) -> None:
        """`reason` must be one of DESTRUCTIVE_REASONS -- raises
        InvalidDestructiveReason (not a silent log-and-continue)
        otherwise, since an unaudited-reason destructive call is
        exactly the failure mode Phase 5.5D exists to close off."""
        if reason not in DESTRUCTIVE_REASONS:
            raise InvalidDestructiveReason(
                f"{method}(): reason {reason!r} is not one of {sorted(DESTRUCTIVE_REASONS)} -- "
                f"refusing to log (or trust) a vague destructive-call reason."
            )
        entry = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "run_id": context.run_id,
            "task_id": context.task_id,
            "source_row": context.source_row,
            "group": context.group,
            "method": method,
            "reason": reason,
            "caller": caller,
            "target_type": target_type,
            "target_identity": target_identity,
            "row_count_before": row_count_before,
            "row_identities_before": row_identities_before,
            "row_count_after": row_count_after,
            "row_identities_after": row_identities_after,
            "result": result,
            "exception": exception,
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        except Exception:
            pass
