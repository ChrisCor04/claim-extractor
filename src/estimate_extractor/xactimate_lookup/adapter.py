"""Clean adapter boundary for (future) Xactimate desktop automation.

``XactimateAdapter`` is the only interface orchestrator.py talks to for
anything resembling desktop control. Recommendation/ranking/persistence/
review logic (this package's other modules) never imports a concrete
adapter and never assumes one exists -- see build spec "The adapter
should never contain business logic. Phrase generation, ranking,
persistence, registry lookup, and review remain outside the adapter."

``FakeXactimateAdapter`` is the only adapter registered by default. It
performs no real desktop interaction whatsoever; it plays back a
caller-supplied script of dropdown results and reports
``supports_live_execution = False`` -- orchestrator.py refuses to commit
against any adapter that doesn't explicitly declare live-execution
support (see build spec "Do not fabricate successful automation.").

A real adapter (Windows UI Automation, an accessibility API, etc.) would
subclass ``XactimateAdapter``, implement every method against the real
desktop, and set ``supports_live_execution = True``. It is deliberately
NOT provided here -- see build spec "Live desktop execution must remain
off by default." / "the only remaining work is implementing a real
WindowsXactimateAdapter."
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from estimate_extractor.xactimate_lookup.models import DropdownResult, PopulatedFields


class AdapterError(Exception):
    """Raised by an adapter on any failure -- orchestrator.py always
    catches this and stops safely (see build spec "stop safely on
    error")."""


class UnexpectedDialogError(AdapterError):
    """Raised when the adapter detects a dialog/prompt it did not expect
    (a license warning, an unsaved-changes prompt, a crash reporter,
    etc.). Always a hard stop -- orchestrator.py never attempts to
    dismiss or reason about an unexpected dialog itself."""


class UnsupportedAdapterError(AdapterError):
    """Raised (by orchestrator.py, not the adapter) when a live run is
    requested against an adapter that does not declare
    ``supports_live_execution = True``. This is what keeps a live run
    from silently no-op'ing against the Fake adapter or a partially
    implemented one."""


class ProtectedCommittedRowError(AdapterError):
    """Phase 5.5D: raised instead of performing a destructive action
    (cancel/delete) that would reduce a group's row count below the
    number of rows Execute has already successfully committed and
    protected there this session -- see destructive_audit.py's
    ProtectedRowLedger. A live incident showed rows Execute had just
    committed disappearing during an UNRELATED group's verification
    cleanup; this is the hard stop that makes that structurally
    impossible instead of merely less likely. Never caught and
    silently swallowed anywhere in this codebase -- a caller seeing
    this must stop, not retry the same destructive call."""


class XactimateAdapter(ABC):
    #: Must be True for a concrete adapter before orchestrator.py will
    #: ever call select_candidate/enter_quantity/commit_item for a live
    #: (non-dry-run) execution. FakeXactimateAdapter leaves this False.
    supports_live_execution: bool = False

    #: Phase 5.5C: True only if this adapter's ensure_group() can
    #: reliably create MORE THAN TWO Xactimate groups as true siblings
    #: within a single session. Live investigation (see docs/build-
    #: estimate.md Phase 5.5C) found Xactimate's "New Group" command
    #: attaches to the most-recently-created group regardless of click
    #: target or dialog button; a state-reset workaround (switching
    #: Estimate Items tabs) reliably fixes exactly the 2nd group but NOT
    #: a 3rd+ in the same session, reproduced across three different
    #: reset strategies. Default False -- build_estimate_panel.py uses
    #: this to gate whether the UI allows a multi-group plan or forces
    #: the one-group-at-a-time fallback ("Run one group at a time.").
    #: FakeXactimateAdapter leaves this False; a real adapter sets it
    #: True only once N>2 sibling creation is independently verified.
    multi_group_creation_available: bool = False

    @abstractmethod
    def verify_application(self) -> bool:
        """Confirms the Xactimate application itself is running and
        responsive. Must return False rather than raise when uncertain --
        orchestrator.py treats False as a hard stop."""

    @abstractmethod
    def verify_project(self) -> bool:
        """Confirms the correct Xactimate project/estimate is the active
        context (separate from verify_application: the application can be
        running with the wrong -- or no -- project open)."""

    @abstractmethod
    def focus_search(self) -> None: ...

    @abstractmethod
    def clear_search(self) -> None: ...

    @abstractmethod
    def search_by_description(self, phrase: str) -> None:
        """Enters a generated description search phrase into the top search box."""

    @abstractmethod
    def search_by_category_selector(self, category: str, selector: str) -> None:
        """Enters a known CAT/SEL directly (the fast path once a mapping is trusted)."""

    @abstractmethod
    def capture_dropdown(self):
        """Waits for and captures the RAW dropdown state (implementation-
        defined -- UI Automation elements, raw text, a screenshot region,
        etc.). Deliberately separate from parsing (see parse_dropdown) so
        a real adapter's capture step can be tested/logged independently
        of its parsing step."""

    @abstractmethod
    def parse_dropdown(self, raw) -> list[DropdownResult]:
        """Parses whatever capture_dropdown() returned into structured
        DropdownResult rows. Never ranks or judges them -- that is
        ranking.py's job, strictly outside the adapter."""

    @abstractmethod
    def select_candidate(self, candidate: DropdownResult) -> None: ...

    @abstractmethod
    def read_populated_fields(self) -> PopulatedFields:
        """Reads back CAT/SEL/description/unit/action/item number after a
        selection -- used by orchestrator.py's integrity check against
        the candidate that was actually selected."""

    @abstractmethod
    def enter_quantity(self, quantity: float) -> None: ...

    @abstractmethod
    def commit_item(self) -> None: ...

    @abstractmethod
    def capture_evidence(self) -> str:
        """Returns an evidence reference (e.g. a screenshot path or log
        identifier) -- never the evidence content itself embedded inline."""

    @abstractmethod
    def recover(self) -> None:
        """Best-effort return to a safe, known state after any stop or
        error (e.g. close an unexpected dialog, clear a partially-entered
        search). Never raises; a recover() that itself fails should log
        and return, since it runs during already-degraded conditions.
        Orchestrator.py calls this after any AdapterError and before
        surfacing the stop to the caller."""


@dataclass(slots=True)
class AdapterCallLog:
    calls: list[tuple[str, tuple, dict]] = field(default_factory=list)

    def record(self, name: str, *args, **kwargs) -> None:
        self.calls.append((name, args, kwargs))


class FakeXactimateAdapter(XactimateAdapter):
    """Deterministic, scripted adapter for tests and dry-run planning --
    performs no real desktop interaction. Construct with a mapping from
    search input (phrase or "CAT SEL" string) to the list of
    DropdownResult rows it should "return" for that query.
    ``supports_live_execution`` stays False; nothing built against this
    adapter can ever commit a live item."""

    supports_live_execution = False

    def __init__(
        self,
        dropdown_script: dict[str, list[DropdownResult]] | None = None,
        *,
        application_verified: bool = True,
        project_verified: bool = True,
        fail_on_capture_for: set[str] | None = None,
        raise_unexpected_dialog_for: set[str] | None = None,
        populated_unit: str | None = None,
    ) -> None:
        self.dropdown_script = dropdown_script or {}
        self.application_verified = application_verified
        self.project_verified = project_verified
        self.fail_on_capture_for = fail_on_capture_for or set()
        self.raise_unexpected_dialog_for = raise_unexpected_dialog_for or set()
        #: lets tests simulate Xactimate reporting a specific populated
        #: unit after selection (real DropdownResult/selector-catalog
        #: data carries no unit -- see build spec "Unit handling" in
        #: docs/selector-recommendation.md), so a unit_mismatch stop can
        #: be exercised deterministically.
        self.populated_unit = populated_unit
        self.log = AdapterCallLog()
        self._current_query: str | None = None
        self._selected: DropdownResult | None = None
        self._quantity: float | None = None
        self._committed = False

    def verify_application(self) -> bool:
        self.log.record("verify_application")
        return self.application_verified

    def verify_project(self) -> bool:
        self.log.record("verify_project")
        return self.project_verified

    def focus_search(self) -> None:
        self.log.record("focus_search")

    def clear_search(self) -> None:
        self.log.record("clear_search")
        self._current_query = None
        self._selected = None

    def search_by_description(self, phrase: str) -> None:
        self.log.record("search_by_description", phrase)
        self._current_query = phrase

    def search_by_category_selector(self, category: str, selector: str) -> None:
        query = f"{category} {selector}"
        self.log.record("search_by_category_selector", category, selector)
        self._current_query = query

    def capture_dropdown(self):
        self.log.record("capture_dropdown")
        if self._current_query in self.raise_unexpected_dialog_for:
            raise UnexpectedDialogError(f"Simulated unexpected dialog while capturing dropdown for {self._current_query!r}.")
        if self._current_query in self.fail_on_capture_for:
            raise AdapterError(f"Simulated dropdown-extraction failure for query {self._current_query!r}.")
        return list(self.dropdown_script.get(self._current_query or "", []))

    def parse_dropdown(self, raw) -> list[DropdownResult]:
        self.log.record("parse_dropdown")
        return list(raw)  # already-structured for the Fake adapter -- identity pass-through

    def select_candidate(self, candidate: DropdownResult) -> None:
        self.log.record("select_candidate", candidate.raw_text)
        self._selected = candidate

    def read_populated_fields(self) -> PopulatedFields:
        self.log.record("read_populated_fields")
        if self._selected is None:
            return PopulatedFields(category=None, selector=None, description=None, unit=None, action=None, item_number=None)
        return PopulatedFields(
            category=self._selected.category,
            selector=self._selected.selector,
            description=self._selected.description,
            unit=self.populated_unit,
            action=None,
            item_number=self._selected.item_number,
        )

    def enter_quantity(self, quantity: float) -> None:
        self.log.record("enter_quantity", quantity)
        self._quantity = quantity

    def commit_item(self) -> None:
        self.log.record("commit_item")
        self._committed = True

    def capture_evidence(self) -> str:
        self.log.record("capture_evidence")
        return f"fake-evidence://{self._current_query}/{self._selected.raw_text if self._selected else 'none'}"

    def recover(self) -> None:
        self.log.record("recover")
        self._current_query = None
        self._selected = None
