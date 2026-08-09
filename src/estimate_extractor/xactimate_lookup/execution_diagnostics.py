"""Phase 5.9: an append-only, timestamped ledger of every task's row
lifecycle transitions during a live execution run -- built in response to
a live incident report where the user directly observed a group's final
line item's results dropdown still open when the automation moved on to
the next group's setup, and committed rows appearing to disappear later
in the run (see docs/build-estimate.md Phase 5.9).

Purely observational: recording an event here never changes control flow,
ranking, routing, or commit behavior. A task's status in ExecutionPlan
(TASK_COMPLETED, etc.) reflects what the Python-level code BELIEVES
happened; this ledger, cross-referenced against DestructiveActionAuditor's
log (destructive_audit.py) and an independent post-run grid re-inventory,
is what proves what ACTUALLY happened on screen -- see Phase 5.9 Stage 11:
"Do NOT trust task status as persistence proof."
"""

from __future__ import annotations

import json
import time
from pathlib import Path

#: The ordered row-lifecycle states a single task can pass through.
#: Not every task reaches every state (e.g. a NO_MATCH task never
#: reaches CANDIDATE_SELECTED onward) -- see RowLifecycleLedger.record()'s
#: docstring for how a caller records only the states it actually
#: reached.
ROW_LIFECYCLE_STATES = (
    "PLANNED",
    "SEARCH_STARTED",
    "POPUP_OBSERVED",
    "CANDIDATES_CAPTURED",
    "DECISION_MADE",
    "CANDIDATE_SELECTED",
    "QUICK_ENTRY_POPULATED",
    "QUANTITY_ENTERED",
    "COMMIT_STARTED",
    "COMMIT_RETURNED",
    "ROW_OBSERVED_IN_GRID",
    "VERIFIED",
    "TERMINAL",
)


class RowLifecycleLedger:
    """Appends one JSON line per lifecycle event to `path` (created on
    first use) AND keeps an in-memory list for same-process
    reconstruction (e.g. building the Phase 5.9 reconciliation table
    right after a run finishes, without re-parsing the file). Never
    raises on its own I/O failure -- an inability to persist an
    observational event must never affect the caller's own already-
    decided behavior, matching DestructiveActionAuditor's own
    contract (destructive_audit.py)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.events: list[dict] = []

    def record(
        self,
        *,
        run_id: str | None,
        task_id: str | None,
        source_row: str | None,
        group: str | None,
        event: str,
        **detail,
    ) -> None:
        entry = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "run_id": run_id,
            "task_id": task_id,
            "source_row": source_row,
            "group": group,
            "event": event,
            **detail,
        }
        self.events.append(entry)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        except Exception:
            pass

    def events_for_task(self, task_id: str) -> list[dict]:
        return [e for e in self.events if e["task_id"] == task_id]

    def last_event_for_task(self, task_id: str) -> dict | None:
        matches = self.events_for_task(task_id)
        return matches[-1] if matches else None
