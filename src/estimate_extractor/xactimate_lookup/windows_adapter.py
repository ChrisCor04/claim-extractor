"""WindowsXactimateAdapter -- a real, live-validated XactimateAdapter for
Windows desktop automation against Xactimate Online Estimate Writer.

This is not a pure UI-Automation adapter and not a pure vision/OCR
adapter -- it is a hybrid, because that is what the live investigation
(Phase 4.1 -> 4.2 -> 4.2B, see docs/xactimate-lookup.md) actually found
works:

- The application's *static* chrome (search box container, Quick Entry
  panel, the results grid) exposes **zero** UI Automation peers -- this
  was independently confirmed via raw UI Automation (all three tree
  views) and legacy MSAA (``IAccessible``). This appears to be a
  consequence of the app being a self-contained, trimmed .NET Core WPF
  ClickOnce publish (trimming is a documented cause of exactly this
  symptom). It means the search box, Quick Entry fields, and grid cells
  must be driven by verified screen coordinates, not accessibility APIs.
- The search-results dropdown, by contrast, is a **separate top-level
  owned popup window** that -- unlike the static chrome -- *is* a fully
  populated, standard WPF ``ListBox`` with real automation peers. Every
  row is read as exact UI Automation text (category/selector code,
  description, price), never OCR.
- ``PrintWindow`` against the main window's HWND cannot see that popup
  at all (it is a different top-level window). It must be located via
  window enumeration.
- Typing via the modern ``SendInput``-backed API (what ``pywinauto``
  uses by default) reliably enters text but never triggers the popup.
  The legacy ``keybd_event`` API does, reliably. Both are standard,
  documented Win32 input-injection APIs -- this is not an evasion
  technique, it is a real, reproducible discovery about which one this
  particular application's live-search binding responds to.
- Selecting a candidate is a **real mouse click** at the row's live
  (freshly re-read, immediately before clicking) UI Automation bounding
  rectangle center. ``LegacyIAccessiblePattern.DoDefaultAction()`` is
  supported by the row elements but was confirmed to be a safe no-op
  here -- it does not select anything, so it cannot be used as a
  coordinate-free selection substitute.
- Quantity is entered by clicking the grid row's own ``Quantity`` cell
  directly (the top "Quick Entry" panel is a separate "create new item"
  form that desyncs once a row already exists in the grid), typing via
  ``keybd_event``, and committing with ``Tab`` -- deliberately never
  ``Enter``.
- Committing is ``Ctrl+S``; the header's "Saved" / "Unsaved changes"
  text is the ground truth for whether it worked.

``supports_live_execution`` stays ``False`` until the pilot-gate
validation in docs/xactimate-lookup.md's Phase 4.3 section reports it
should be flipped -- see that document for the exact evidence.

All Windows-only dependencies (``ctypes``/``win32gui``/``win32ui``/
``comtypes``/``pywinauto``/``pytesseract``) are imported lazily inside
methods, never at module import time, so importing this module on a
non-Windows platform (e.g. the CI test suite) does not fail -- only
actually instantiating/using ``WindowsXactimateAdapter`` requires
Windows.
"""

from __future__ import annotations

import re
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from estimate_extractor.xactimate_lookup.adapter import (
    AdapterError,
    PhysicalStateUncertainError,
    ProtectedCommittedRowError,
    QuantityConfirmationError,
    TaskLocalRowReconciliationError,
    UnexpectedDialogError,
    XactimateAdapter,
)
from estimate_extractor.xactimate_lookup.destructive_audit import (
    DESTRUCTIVE_REASONS,
    DestructiveActionAuditor,
    ExecutionContext,
    InvalidDestructiveReason,
    ProtectedRowLedger,
    ProtectedRowRecord,
)
from estimate_extractor.xactimate_lookup.execution_diagnostics import (
    RRBindingDiagnosticLedger,
    RowLifecycleLedger,
    ZeroDeltaCommitDiagnosticLedger,
)
from estimate_extractor.xactimate_lookup.models import DropdownResult, PopulatedFields

#: Xactimate's HwndWrapper class names embed this literal substring for
#: every top-level window it owns (main window, popups, the "Loading"
#: overlay) -- confirmed live across dozens of window enumerations.
#: Matching on it avoids needing a process-name/PID lookup at all.
_APP_CLASS_MARKER = "Xactimate online Estimate Writer"

#: Client-relative pixel rects (left, top, right, bottom), calibrated
#: against the Estimate Items screen at 1920x1021 client size / 96 DPI
#: (100% scale) with the content pane scrolled to its default/top
#: position (see _reset_scroll_state). Live-drift-prone; every method
#: that clicks re-derives its target via _locate_anchor_offset() first
#: wherever the click matters for correctness (search box, OCR
#: label-relative crops) rather than trusting these blindly.
_ANCHORS = {
    "search_box": (508, 165, 826, 186),
    "search_button": (843, 165, 928, 186),
    "search_dropdown_arrow": (817, 165, 836, 186),
    "items_tab": (296, 78, 342, 98),
    #: Phase 5.5C: used only by _reset_group_creation_stickiness() to
    #: switch away from and back to the Items tab -- never used for
    #: reading Components content itself, so a generous rect centered
    #: on the live-verified click point (417, 88) is fine.
    "components_tab": (384, 78, 452, 98),
    "quick_entry_cat_label": (506, 461, 535, 475),
    #: The grid header's own "Cat" column label -- unlike the Quick Entry
    #: panel's "Cat:" label (which OCR intermittently fails to detect at
    #: all when its value box is empty, a real live-caught bug), the grid
    #: header is always present whenever there's a grid to read, and was
    #: found reliably in every sample tested. Used as the anchor-offset
    #: reference in preference to quick_entry_cat_label. Calibrated
    #: (dx=0/dy=0 reference frame) by back-solving from known-good
    #: offsets observed live -- see docs/xactimate-lookup.md Phase 4.4.
    "grid_header_cat_label": (540, 630, 561, 643),
    "quick_entry_cat_value": (563, 459, 608, 477),
    "quick_entry_sel_value": (663, 459, 738, 477),
    "quick_entry_act_value": (792, 459, 818, 477),
    "quick_entry_desc_box": (563, 488, 1044, 506),
    "quick_entry_calc_qty_box": (563, 514, 613, 532),
    "quick_entry_unit_dropdown": (744, 514, 771, 532),
    "grid_header": (506, 628, 1894, 645),
    "grid_row_1": (506, 654, 1894, 671),
}

#: Vertical distance between consecutive grid rows, pixels. Live-caught
#: (Phase 4.4 Stage 3): the original value of 17 was calibrated only
#: against single-row states, where _last_row_geometry()'s
#: (row_count - 1) * _GRID_ROW_HEIGHT term is always multiplied by
#: zero -- so the constant was never actually exercised until the
#: first live pilot item that reached two grid rows, where it silently
#: misaligned every crop for row 2+ by ~8px (garbled OCR: category='_',
#: selector='an', description=None). Remeasured directly via OCR
#: word-position of the "#" column across two real static rows
#: (410 at y=615, 412 at y=640) -- see docs/xactimate-lookup.md Phase
#: 4.4. Any adapter change that only ever exercised a single grid row
#: cannot validate this constant; re-verify against a 2+ row state
#: before trusting it again.
_GRID_ROW_HEIGHT = 25

#: Column x-ranges within a grid row, client-relative, matching the
#: header order (#, Cat, Sel, Act, Notes, Description, Coverage, Calc,
#: Quantity, Unit, Unit Price, Sales Tax, RCV, Depreciation, Factor, ACV).
#: Live-measured via OCR word-level bounding boxes against a real row
#: ("356 SFG GUTA & [notes icon] Gutter / downspout - aluminum - up to
#: 5" Dwelling 0 LF $11.56 ..."). The header also has a "Notes" column
#: (a small calendar-icon glyph, non-text) between Act and Description
#: that an earlier version of this table didn't account for, causing
#: the activity/description boundary to be wrong -- see
#: docs/xactimate-lookup.md Phase 4.3 for the exact live comparison
#: that caught it.
_GRID_COLUMNS = {
    "number": (506, 538),
    "category": (539, 559),
    # selector/activity/description live-caught (Phase 4.4 Stage 3): the
    # original boundaries were only ever exercised against a 4-character
    # selector code ("GUTA") and a description short enough not to hit
    # the right edge. A longer selector ("GUTAB>") visually overflows
    # column boundaries Xactimate doesn't clip to -- the trailing ">"
    # was cut off (selector) and bled into the next crop (activity read
    # a stray ">" instead of the real "&"/"-" symbol underneath it,
    # which OCR word-position measurement placed at x=626-636,
    # independent of selector length since Xactimate's activity column
    # itself doesn't move). Description's right edge similarly
    # truncated a longer description ("...aluminum - 7\" to 8\"" cut to
    # "...aluminum - 7"). Widened all three to fit the longest observed
    # real content with margin; see docs/xactimate-lookup.md Phase 4.4.
    "selector": (563, 620),
    "activity": (622, 648),
    "description": (682, 948),
    "quantity": (1020, 1080),
    # Live-caught (Phase 4.4 Stage 3): (1073, 1099) was calibrated
    # against the row highlighted by enter_quantity()'s own cell click
    # (Phase 4.3/4.4 Stage 1) -- but read_populated_fields() is actually
    # called by the real orchestrator.execute_plan() flow right after
    # select_candidate(), BEFORE enter_quantity() ever runs, i.e.
    # against the STATIC (unhighlighted) row. OCR word-position
    # measurement in that real state placed "SQ" at x=1095-1110, mostly
    # outside the old boundary -- widened to (1090, 1120).
    #
    # Live-caught (Phase 4.7): a row read immediately after
    # `commit_item()` (Ctrl+S) is back in something close to the
    # HIGHLIGHTED position -- live word-position measurement placed
    # "LF" at x=1078-1089, almost entirely to the LEFT of (1090, 1120)
    # (missed it by 1px, reading pure gridline instead of any text).
    # There are now three observed real-world states (highlighted-by-
    # quantity-click, static, highlighted-by-recent-commit) with
    # different unit positions and no reliable way to know which one
    # applies at read time -- widened to (1070, 1122) to cover all
    # three, relying on the unit-reading code to extract the real word
    # from whatever whitespace/gridline noise surrounds it rather than
    # a tight crop with zero margin for error. See
    # docs/xactimate-lookup.md Phase 4.7.
    "unit": (1070, 1122),
    "unit_price": (1131, 1197),
}


class StaleCandidateError(AdapterError):
    """Raised when a previously-parsed dropdown candidate can no longer
    be found (by matching text) in a freshly re-read popup -- the
    live investigation's core selection-safety rule ("never reuse stale
    coordinates") made concrete."""


class PopupNotFoundError(AdapterError):
    """Raised when the search-results popup window does not appear
    within the timeout, or disappears/changes between capture and
    selection."""


class ProbeCleanupFailedError(AdapterError):
    """Phase 5.9 Priority 1/2 (live-caught): raised by
    _cleanup_probe_item() when it cannot POSITIVELY confirm the
    disposable group-verification probe row is gone -- either because
    the probe was never observed becoming visible at all within the
    bounded settle window (a stale/slow grid read), or because the
    last row's identity never matched the probe's own CAT/SEL so
    nothing was safely deletable, or because the grid never returned
    to the expected baseline row count after cleanup attempts. Never
    silently swallowed by _verify_group_once()'s finally block --
    surfaces as a specific, diagnosable group-verification failure
    reason instead of a bare False. See docs/build-estimate.md
    Phase 5.9 Priority 1/2: a live incident found the ORIGINAL cleanup
    trusted a single immediate post-commit grid read, which could be
    stale and make cleanup silently declare victory without ever
    having removed the probe it just added -- leaving real garbage
    SFG/GUTA rows in the live estimate."""


class GroupTransitionUnsafeError(AdapterError):
    """Phase 5.9: raised by `_assert_group_transition_settled()` when
    ensure_group()/select_group()/verify_group()/the Components-tab
    click inside `_reset_group_creation_stickiness()` is about to run
    while the live UI still shows a results popup or an unexpected
    dialog after a bounded settle-wait -- i.e. the previous group's
    final task has not actually finished at the UI level, no matter
    what its Python-level return value claimed. Unlike
    `_dismiss_stray_results_popup()` (a self-heal for a DIFFERENT,
    already-understood benign condition -- a stray popup from some
    future code path that forgot to call `recover()`), this check
    NEVER dismisses anything itself: see docs/build-estimate.md
    Phase 5.9, "Do not dismiss an open results dropdown just to
    proceed." Caught the same way any other AdapterError is -- the
    ONE group this happens to is marked unverified/failed (its tasks
    routed to REVIEW_REQUIRED), never a reason to abort the whole
    run, matching every other per-group failure in this codebase."""


class PopupCaptureFailedError(AdapterError):
    """Phase 5.8: raised when the results popup window WAS found at
    least once, but no candidate rows could ever be confidently read
    from it -- even after the bounded stabilization/retry sequence in
    capture_dropdown(). Distinct from PopupNotFoundError (the popup
    window handle never appeared at all) and from a genuine zero-
    result search (see capture_dropdown()'s own "NO_RESULTS requires
    positive evidence" contract) -- live-caught screenshots showed a
    dropdown that visibly displayed real candidates, which must never
    be silently reported as if Xactimate found nothing."""


class ItemsTabVerificationError(AdapterError):
    """Raised before clicking when the live Items tab cannot be located,
    or after clicking when the Items search pane cannot be observed."""


class SearchFocusError(AdapterError):
    """Raised when the live Search field or its keyboard focus cannot be
    positively verified.  Callers must not clear or type after this."""


class RowOffscreenError(QuantityConfirmationError):
    """Phase 6.2 (live-caught): raised by enter_quantity() when the
    pending row's computed screen position still falls outside the
    captured client area after the bounded scroll-into-view sequence
    exhausts its attempts. Every row-position calculation in this file
    is document-coordinate arithmetic (row_1_top + row_index *
    _GRID_ROW_HEIGHT) with no awareness of the actual visible viewport
    -- fine as long as a group never grows past ~15 rows (true for
    every group in this project's history until Phase 6.0's clean-
    TEST benchmark first grew Roof past that boundary), but silently
    unsafe once it does: clicking a Y coordinate beyond the window's
    own client height either hits nothing or hits whatever else is at
    that pixel, live-reproduced as two RFG/STEEP logical items (#171,
    #172) both landing at quantity 0 instead of their real 33.66/35.67
    -- the click for each item's "+" row computed a Y past the visible
    window and missed the quantity cell entirely. Raising here instead
    of clicking blind is the actual fix: fail closed, never silently
    write into an unverified location."""


class QuantityNotConfirmedError(QuantityConfirmationError):
    """Phase 6.2 (live-caught): raised by enter_quantity() when the
    quantity cell, read back immediately after typing (before any
    caller can call commit_item()), does not show the value that was
    just typed. This is the safety net that catches ANY targeting
    failure -- not just the off-screen case RowOffscreenError guards
    against, but also a stale/wrong row position, a click that missed
    the cell for some other reason, or the value failing to take for
    any reason -- by requiring positive, read-back proof the source
    quantity is visible on the pending item BEFORE commit, rather than
    discovering a $0 row only after the fact (exactly what happened
    live to task_line_0018/0019: both committed successfully per the
    plan's own bookkeeping, with quantity silently left at 0)."""


@dataclass(slots=True)
class DropdownCaptureDiagnostics:
    """Phase 5.8: one search's popup-lifecycle timeline, in monotonic
    seconds (never wall-clock, so relative ordering can be asserted in
    tests without depending on real time). Purely observational --
    never consulted by ranking or commit logic, only surfaced for live
    diagnostics/tests. See capture_dropdown()'s docstring for what each
    field marks."""

    search_submitted_at: float | None = None
    popup_first_seen_at: float | None = None
    popup_stable_at: float | None = None
    popup_row_count: int | None = None
    popup_bounds: tuple[int, int, int, int] | None = None
    popup_closed_at: float | None = None
    candidate_parse_completed_at: float | None = None
    selection_clicked_at: float | None = None
    search_retries: int = 0
    #: One of "CANDIDATES_PARSED", "NO_RESULTS", "POPUP_CAPTURE_FAILED",
    #: "POPUP_DISAPPEARED" -- see capture_dropdown()'s docstring.
    outcome: str | None = None


@dataclass(slots=True)
class _RawDropdownRow:
    """Adapter-internal raw row -- carries the live popup handle and
    rect alongside the text, which DropdownResult (a shared, adapter-
    agnostic model used by ranking.py/registry.py/etc.) deliberately
    does not. Never leaks outside capture_dropdown()/parse_dropdown()/
    select_candidate()."""

    code_text: str
    description_text: str
    price_text: str
    row_position: int
    popup_hwnd: int
    rect_at_capture: tuple[int, int, int, int]


@dataclass(slots=True)
class _AdapterDiagnostics:
    main_window_found: bool
    main_window_hwnd: int | None
    main_window_title: str | None
    project_matches: bool
    foreground: bool
    dropdown_open: bool
    timestamp: str


@dataclass(slots=True)
class QuantityVerificationResult:
    """Result of verify_quantity_committed()'s bounded poll -- see that
    method's docstring. ``samples`` records every attempt (elapsed
    seconds since the poll started, whether a grid row was found that
    attempt, and the quantity value observed, if any) for timing
    diagnostics and regression assertions; never used for control flow
    itself, which is decided attempt-by-attempt as the poll runs."""

    matched: bool
    stop_reason: str  # "matched" | "timeout" | "wrong_context"
    expected: float
    observed: float | None
    attempts: int
    elapsed_s: float
    samples: list[tuple[float, bool, float | None]] = field(default_factory=list)


#: Known real Xactimate unit codes -- evidence-backed only, each one
#: live-confirmed this phase (or an earlier one) against a real
#: catalog item's populated Unit cell: LF (SFG/GUTA), SQ (RFG/ARMVN),
#: EA (PLM/TLT, "Toilet"), HR (PNT/LAB, "Painter - per hour"), DA
#: (TMP/GEN, "Generator ... per day"), SF (CLN/FCC, "Clean and
#: deodorize carpet"). Used to validate/vote among noisy OCR reads
#: (Phase 4.7 Stage 6) -- never to invent a value with no OCR support.
#: See docs/xactimate-lookup.md Phase 4.7.
_KNOWN_XACTIMATE_UNITS = frozenset({"LF", "SQ", "EA", "HR", "DA", "SF"})

#: A narrow, evidence-backed OCR-confusion correction applied ONLY to
#: raw OCR text before vocabulary matching -- NOT a semantic synonym
#: (see `_UNIT_SYNONYMS` for those). Live-caught (Phase 4.6/4.7):
#: Tesseract consistently misreads a real "LF" as "uF" (lowercase "u"
#: visually close to uppercase "L" at this font size) -- reproduced
#: live, stable across 5 of 6 (scale, PSM) combinations on the same
#: real cell. Scoped as narrowly as possible: only the literal
#: stripped string "UF" maps to "LF"; nothing else is in this map, and
#: none of the evidence-backed real units above start with "U", so
#: this cannot misfire against a genuinely different valid unit. See
#: docs/xactimate-lookup.md Phase 4.7 Stage 6.
_UNIT_OCR_CONFUSIONS = {"UF": "LF"}

#: Phase 5.25: live-caught -- a real R&R candidate activation (RFG/240)
#: produced a physical "+" row whose activity-cell OCR read the digit
#: "4" instead of the "+" glyph, stable across the read. Scoped as
#: narrowly as possible, exactly like _UNIT_OCR_CONFUSIONS above: this
#: set is consulted ONLY by _rr_pair_from_delta_with_plus_glyph_
#: tolerance() (windows_adapter.WindowsXactimateAdapter), and only
#: after every other structural precondition (exactly one clean "-"
#: row, exactly one complementary new row, matching identity, a
#: strictly additive two-row delta) already holds -- "4" is NEVER
#: globally treated as "+" anywhere else in this file (_activity_
#: token()/_is_one_logical_rr_multiset_delta() are untouched).
_ACTIVITY_PLUS_OCR_CONFUSIONS = frozenset({"4"})

#: Phase 5.28: live-caught -- three independent R&R candidate
#: activations (RFG/240 Roof, RFG/HIGH Roof, RFG/240 Shed), across two
#: different groups and three different CAT/SEL pairs in one session,
#: all produced a physical "-" row whose activity-cell OCR read "to"
#: instead of the "-" glyph. _read_activation_activity_at()'s crop
#: straddles the selector/activity boundary specifically because a
#: bare "-" is a notoriously thin/small glyph for OCR at this crop
#: size; when neither "-" nor "+" is found as a substring in either
#: PSM read, the raw (garbage) text is returned as-is rather than
#: None, and "to" was that garbage, stably, across all three
#: instances. Scoped as narrowly as possible, exactly like
#: _ACTIVITY_PLUS_OCR_CONFUSIONS above: this set is consulted ONLY by
#: _rr_pair_from_delta_with_minus_glyph_tolerance() (windows_adapter.
#: WindowsXactimateAdapter), and only after every other structural
#: precondition (exactly one clean "+" row, exactly one complementary
#: new row, matching identity, a strictly additive two-row delta)
#: already holds -- "to" is NEVER globally treated as "-" anywhere
#: else in this file (_activity_token()/_is_one_logical_rr_multiset_
#: delta() are untouched).
_ACTIVITY_MINUS_OCR_CONFUSIONS = frozenset({"to"})

#: Unit-normalization synonym map (Phase 4.7 Stage 2) -- semantically
#: identical spellings of the SAME unit, not OCR-noise correction and
#: not a dimensional conversion (see `_VERIFIED_UNIT_CONVERSIONS` for
#: that). Deliberately small and evidence/spec-backed only: each entry
#: normalizes to the real Xactimate-observed code confirmed live this
#: phase. Explicitly NOT included, per the build spec's own examples
#: of unsafe merges: SF/SQ, LF/SF, EA/LF, HR/EA -- these are genuinely
#: different units and must never normalize to each other.
_UNIT_SYNONYMS: dict[str, str] = {
    "EA": "EA", "EACH": "EA",
    "HR": "HR", "HOUR": "HR", "HOURS": "HR",
    "DA": "DA", "DAY": "DA",
    "WK": "WK", "WEEK": "WK",
    "MO": "MO", "MONTH": "MO",
    "LF": "LF", "SQ": "SQ", "SF": "SF",
}

#: Explicit, reviewer-approved unit conversions -- (from_unit,
#: to_unit) -> multiplicative factor. EMPTY BY DEFAULT (Phase 4.7
#: Stage 3 policy: conversions are disabled by default; a conversion
#: may be used ONLY when an explicit rule exists here, identifies
#: source and target units, and has been reviewer-approved). Nothing
#: in this phase populates this map -- no broad automatic conversions
#: are added. A converted quantity must always be recorded separately
#: from the original (see `verify_commit()`), never overwriting it.
#: See docs/xactimate-lookup.md Phase 4.7 Stage 3.
_VERIFIED_UNIT_CONVERSIONS: dict[tuple[str, str], float] = {}


@dataclass(slots=True)
class UnitVerificationResult:
    """Phase 4.7 Stage 1: unit concepts kept independent and never
    overwritten -- `observed_xactimate_unit` is the raw OCR text
    (whitespace-stripped only), `unit_normalized` is only ever set
    after synonym/OCR-confusion resolution, and neither is derived
    from or overwrites the other. `unit_match_state` is one of:
    "exact_match", "normalized_synonym", "verified_conversion",
    "source_unit_missing", "observed_unit_missing", "incompatible",
    "unreadable", "ambiguous". See docs/xactimate-lookup.md Phase
    4.7."""

    source_unit: str | None
    expected_xactimate_unit: str | None
    observed_xactimate_unit: str | None
    unit_normalized: str | None
    unit_match_state: str
    unit_match_reason: str


@dataclass(slots=True)
class CommitVerification:
    """Phase 4.8: the full result of `verify_commit()` -- identifies
    the committed row STRUCTURALLY (a rich, order-independent logical
    multiset delta from a before-commit snapshot), never by searching
    OCR text across every row (Phase 4.7's
    `verify_committed_row()`/`CommittedRowVerification`, retired --
    see docs/xactimate-lookup.md Phase 4.7 and 4.8). WHAT was intended
    is already certain before this ever runs (`select_candidate()`
    acts on UIA-exact dropdown text, `extraction_confidence=1.0`);
    this only answers WHERE it landed and whether the data there is
    correct.

    `trust_state` is one of "VERIFIED", "REVIEW_REQUIRED",
    "VERIFICATION_FAILED", "CONFLICTING_ROW", "UNIT_MISMATCH" -- see
    `verify_commit()`'s docstring for the exact precedence rules. A
    unit conflict is always a dedicated hard trust_state; it is never
    downgraded to "supporting" evidence. Phase 5.27: "QUANTITY_
    MISMATCH" is no longer produced by this method -- quantity OCR
    proved unreliable enough at production zoom/DPI to serve as a
    success gate (real, correctly-written quantities were repeatedly
    misread), so `quantity_observed`/`quantity_matched` below are now
    ADVISORY evidence only and never change `trust_state`.
    `category_observed`/`selector_observed`/`description_observed` are
    corroborating OCR reads at the structurally-identified row ONLY --
    they never decide row identity, and an unreadable category/
    selector never blocks "VERIFIED" when the structural and unit
    evidence agree. `samples` holds one dict per polling attempt for
    diagnostics."""

    trust_state: str
    reason: str
    row_count_before: int
    row_count_after: int | None
    row_index: int | None
    preexisting_rows_unchanged: bool | None
    category_expected: str
    selector_expected: str
    category_observed: str | None
    selector_observed: str | None
    category_selector_ocr_agrees: bool | None
    description_observed: str | None
    quantity_expected: float
    quantity_observed: float | None
    quantity_matched: bool
    unit: UnitVerificationResult | None
    compatibility: str  # "compatible" | "review_required" | "hard_stop" | "not_evaluated"
    compatibility_reason: str
    attempts: int
    elapsed_s: float
    samples: list[dict] = field(default_factory=list)
    evidence_path: str | None = None


@dataclass(slots=True)
class GroupRowSnapshot:
    """Phase 5.4: one grid row's identity + financial fields, captured
    for baseline/reconciliation purposes -- deliberately a superset of
    `snapshot_grid_identities()`'s (category, selector) tuples, since
    Phase 5.3 live-caught a row that read as visually empty/inactive by
    a structural (row-count) check alone while still carrying real
    financial value. `quantity_text`/`unit_text` are raw OCR reads
    (whitespace-stripped only), compared via fuzzy/substring tolerance
    like every other OCR'd field in this file -- never parsed to a
    silently-guessed number."""

    category: str | None
    selector: str | None
    quantity_text: str | None
    unit_text: str | None


@dataclass(slots=True, frozen=True)
class ActivationRowSnapshot:
    """Read-only identity evidence for one physical activation row."""

    category: str | None
    selector: str | None
    description: str | None
    activity: str | None


@dataclass(slots=True, frozen=True)
class CommitRowSnapshot:
    """Order-independent post-commit evidence for one physical row."""

    category: str | None
    selector: str | None
    description: str | None
    activity: str | None
    quantity: float | None
    unit: str | None


@dataclass(slots=True, frozen=True)
class PendingQuantityTarget:
    """Logical identity retained from the proven activation delta.

    ``after_index`` and ``activity_ordinal`` are corroborating position
    evidence from the same after-snapshot. Identity is re-read and
    verified immediately before quantity entry; after that binding is
    established, confirmation stays on the exact edited row/cell while
    separately proving the grid geometry did not materially change.
    """

    identity: tuple[str, str, str]
    activity: str | None
    after_index: int
    activity_ordinal: int
    #: One quantity-bearing ``+`` row may be either the complete physical
    #: representation or one half of an Xactimate-generated ``-/+`` pair.
    #: Recorded when activation is positively reconciled, before entry.
    physical_row_delta: int = 1
    #: True only for a uniquely selected ordinary row whose identity and
    #: activation delta were already proven independently of quantity. In
    #: that narrow case Xactimate's nonzero default is editable source data,
    #: not identity evidence. This flag controls causal ordinary-row
    #: relocation only; R&R targets retain their activity/occurrence logic.
    allow_initial_quantity_overwrite: bool = False
    #: True only for the fresh logical item created by the current candidate
    #: click and positively bound through its activation delta. Once that
    #: binding exists, the source quantity is written exactly once without
    #: consulting repaint-sensitive initial quantity OCR. This does not apply
    #: to arbitrary pre-existing/legacy targets.
    write_source_quantity_once: bool = False
    #: Rich pre-click snapshot retained for causal ordinary-row binding.
    #: Empty for legacy/R&R targets, which keep their established logic.
    activation_baseline_rows: tuple[ActivationRowSnapshot, ...] = ()


@dataclass(slots=True, frozen=True)
class QuantityEntryConfirmation:
    """Result of the single same-cell read immediately after entry.

    Identity is deliberately absent: it was proven before the write and
    retained by :class:`PendingQuantityTarget`. Phase 5.27 (policy
    change, live-caught repeatedly -- correct quantities were
    misread): a low-confidence OCR read is ADVISORY diagnostics only.
    When the bound row/cell and surrounding grid structure remain
    stable, the deterministic write to that positively-identified
    target is treated as authoritative on its own; callers no longer
    route an otherwise-successful commit to review solely because this
    confidence is LOW_CONFIDENCE. A genuinely FAILED write/edit (row
    disappeared, dialog interrupted, grid anchor moved, etc.) is a
    different, still-fatal condition -- see enter_quantity()'s own
    QuantityNotConfirmedError raise sites, unaffected by this change.

    Phase 5.29 (performance): since that same-cell OCR result already
    could not affect production success, a freshly, positively-bound
    write (`PendingQuantityTarget.write_source_quantity_once=True`)
    now skips the read entirely rather than paying its wall-clock cost
    for a result nothing consumes -- `confidence="SKIPPED"`,
    `observed=None` in that case. Never claims a numeric read happened
    when it did not.
    """

    expected: float
    observed: float | None
    confidence: str  # "CONFIRMED" | "LOW_CONFIDENCE" | "SKIPPED"
    review_required: bool
    reason: str


@dataclass(slots=True, frozen=True)
class PendingRRPairTarget:
    """Phase 6.3 (R&R Stage 3): one proven R&R activation's DUAL
    physical-row binding -- both halves of a single logical remove-
    and-replace item, built from the SAME one-time activation delta
    (see WindowsXactimateAdapter._pending_rr_pair_targets_from_delta()).

    Deliberately reuses PendingQuantityTarget TWICE rather than
    introducing a second row-targeting system: minus_target/plus_target
    are independently locatable through the exact same, already-proven
    machinery a single-row target uses (_locate_pending_quantity_row()/
    _quantity_target_candidates()), disambiguated by their own real
    activity glyph ("-"/"+") -- never by row order or position.
    __post_init__ enforces that invariant unconditionally, since a
    PendingRRPairTarget with a data bug here is worse than raising
    immediately."""

    minus_target: PendingQuantityTarget
    plus_target: PendingQuantityTarget
    #: Shared identity both targets' own .identity already agree with --
    #: retained separately purely as a convenience/assertion surface,
    #: never independently re-derived.
    identity: tuple[str, str, str]

    def __post_init__(self) -> None:
        if self.minus_target.activity != "-":
            raise ValueError("PendingRRPairTarget: minus_target must carry activity '-'.")
        if self.plus_target.activity != "+":
            raise ValueError("PendingRRPairTarget: plus_target must carry activity '+'.")
        if self.minus_target.identity != self.identity or self.plus_target.identity != self.identity:
            raise ValueError("PendingRRPairTarget: minus_target/plus_target must share the pair's own identity.")


@dataclass(slots=True, frozen=True)
class RRPairQuantityResult:
    """The complete, structured outcome of writing and verifying BOTH
    halves of one R&R pair -- returned to the caller (a future Stage 4
    runner) to persist as CoordinatedPair checkpoints. This module
    never persists execution-plan state itself; see write_and_verify_
    rr_pair_quantities()'s own docstring."""

    minus_confirmation: QuantityEntryConfirmation
    plus_confirmation: QuantityEntryConfirmation


class RRPairQuantityError(QuantityNotConfirmedError):
    """Phase 6.3 (R&R Stage 3): raised by write_and_verify_rr_pair_
    quantities() when either half's write/verify fails. `side` names
    which half failed ("minus" | "plus"). `minus_confirmation` is set
    only when the minus side had ALREADY been positively written and
    confirmed before a later failure on the plus side -- so a caller
    can still persist that partial progress (e.g. minus_verified=True)
    even though the pair call did not complete; never fabricated, and
    always None when the minus side itself is what failed. A failure
    on the minus side always prevents the plus side from being
    attempted at all -- there is never a plus_confirmation on this
    exception for that reason (a completed plus write is only ever
    returned via RRPairQuantityResult, not raised)."""

    def __init__(self, message: str, *, side: str, minus_confirmation: QuantityEntryConfirmation | None = None):
        super().__init__(message)
        self.side = side
        self.minus_confirmation = minus_confirmation


@dataclass(slots=True)
class EstimateBaseline:
    """Phase 5.4: a full structural + financial snapshot of the
    estimate, for `verify_estimate_matches_baseline()` to compare
    against later. Covers exactly the fields Phase 5.3's cleanup gap
    missed: `group_subtotal_text` and `grand_total_text` are captured
    alongside row-level identity, so cleanup verification can never
    again pass just because a group's ACTIVE grid view was empty while
    the group's own subtotal (and Grand Total) still carried real
    value from a row that had gone missing from view without actually
    being removed."""

    group_names: list[str]
    group_rows: dict[str, list[GroupRowSnapshot]]
    group_subtotal_text: dict[str, str]
    grand_total_text: str
    saved: bool | None
    captured_at: str


@dataclass(slots=True)
class ReconciliationResult:
    """Phase 5.4: `verify_estimate_matches_baseline()`'s result --
    `ok` is True only when EVERY field (row identities, row count,
    quantities, group subtotals, Grand Total, saved state) matches the
    baseline; `mismatches` names each specific field that didn't, so a
    caller (and a human reading a failed cleanup report) knows exactly
    what's still wrong rather than a single opaque "cleanup failed"."""

    ok: bool
    mismatches: list[str] = field(default_factory=list)


def _normalize_unit_text(raw: str | None) -> str | None:
    """Strips OCR noise (gridline bleed, parens, pipes, whitespace) and
    uppercases -- shared by the OCR-confusion and synonym lookups so
    both operate on the same cleaned form. Returns None for empty
    input; never guesses content that isn't there."""
    if not raw:
        return None
    import re

    cleaned = re.sub(r"[^A-Za-z]", "", raw).upper()
    return cleaned or None


def _resolve_observed_unit_vocab(observed_xactimate_unit: str | None) -> str | None:
    """Cleans raw OCR text, applies the narrow `_UNIT_OCR_CONFUSIONS`
    correction, and returns the result ONLY if it lands on a real,
    evidence-backed unit in `_KNOWN_XACTIMATE_UNITS` -- otherwise None
    (Phase 4.7 Stage 6: "otherwise return unit_unreadable", never a
    guessed value)."""
    cleaned = _normalize_unit_text(observed_xactimate_unit)
    if cleaned is None:
        return None
    corrected = _UNIT_OCR_CONFUSIONS.get(cleaned, cleaned)
    return corrected if corrected in _KNOWN_XACTIMATE_UNITS else None


def check_unit_compatibility(
    source_unit: str | None, expected_xactimate_unit: str | None, observed_xactimate_unit: str | None,
) -> UnitVerificationResult:
    """Phase 4.7 Stage 8: pure compatibility logic over three
    independently-tracked unit concepts (module-level and side-effect
    free so it's directly unit-testable without a live session). A
    quantity match must never override a unit conflict -- callers
    evaluate this result independently of quantity verification.

    Priority order (most-blocking first, matching the build spec's own
    "Review required" / "Hard stop" bullet lists in Stage 8):
    1. observed unit missing (no OCR text at all) -> review_required
    2. observed unit unreadable (OCR text present but doesn't
       confidently resolve to a real unit) -> review_required
    3. expected unit missing -> review_required
    4. source unit missing -> review_required (checked even when
       observed/expected already agree -- Stage 8 lists this as an
       unconditional trigger, not one only reached on disagreement)
    5. exact match / configured synonym -> compatible
    6. explicit verified conversion exists
       (`_VERIFIED_UNIT_CONVERSIONS`, empty by default) -> compatible
    7. otherwise -> hard_stop (incompatible)

    See docs/xactimate-lookup.md Phase 4.7 Stage 8."""

    def result(state: str, reason: str, normalized: str | None = None) -> UnitVerificationResult:
        return UnitVerificationResult(
            source_unit=source_unit, expected_xactimate_unit=expected_xactimate_unit,
            observed_xactimate_unit=observed_xactimate_unit, unit_normalized=normalized,
            unit_match_state=state, unit_match_reason=reason,
        )

    observed_vocab = _resolve_observed_unit_vocab(observed_xactimate_unit)
    if observed_xactimate_unit is None:
        return result("observed_unit_missing", "no unit text was read from the committed row")
    if observed_vocab is None:
        return result(
            "unreadable",
            f"raw OCR {observed_xactimate_unit!r} did not confidently resolve to a known Xactimate unit "
            f"({sorted(_KNOWN_XACTIMATE_UNITS)}) -- refusing to guess",
        )
    if not expected_xactimate_unit:
        return result("expected_unit_missing", "no expected Xactimate unit was supplied for comparison", normalized=observed_vocab)
    if not source_unit:
        return result("source_unit_missing", "no source unit was supplied for comparison", normalized=observed_vocab)

    expected_clean = _normalize_unit_text(expected_xactimate_unit)
    expected_norm = _UNIT_SYNONYMS.get(expected_clean, expected_clean) if expected_clean else None

    if observed_vocab == expected_norm:
        state = "exact_match" if observed_vocab == expected_clean else "normalized_synonym"
        return result(state, f"observed unit {observed_vocab!r} matches expected unit {expected_norm!r}", normalized=observed_vocab)

    conversion_factor = _VERIFIED_UNIT_CONVERSIONS.get((observed_vocab, expected_norm))
    if conversion_factor is not None:
        return result(
            "verified_conversion",
            f"explicit verified conversion {observed_vocab}->{expected_norm} (factor {conversion_factor}) applies",
            normalized=observed_vocab,
        )

    return result(
        "incompatible",
        f"observed unit {observed_vocab!r} is not the same as, a configured synonym of, or an explicitly "
        f"verified conversion of expected unit {expected_norm!r}",
        normalized=observed_vocab,
    )


@dataclass(slots=True)
class CategorySelectorMatchResult:
    """Phase 5.12: same shape/philosophy as UnitVerificationResult above
    -- pure, side-effect-free, directly unit-testable. `match_state` is
    one of: "exact_match", "normalized_match", "unreadable", "mismatch".
    See check_category_selector_match()'s own docstring."""

    expected_category: str | None
    expected_selector: str | None
    observed_category: str | None
    observed_selector: str | None
    match_state: str
    reason: str


#: Phase 5.12 (live-caught): characters confirmed, live, to be OCR
#: NOISE on this specific crop (a stray leading "." misread as the
#: entire selector, a trailing "|" gridline-bleed artifact) -- never
#: characters that distinguish one real catalog code from another.
#: Deliberately EXCLUDES '<', '>', '+', '-': those are load-bearing
#: parts of real, DIFFERENT selectors in this exact catalog (e.g.
#: DOR/OH16 vs DOR/OH16- "Standard grade" vs DOR/OH16+ "High grade" vs
#: DOR/OH16> "16'x8'" are four genuinely different items) -- stripping
#: them would make check_category_selector_match() below silently
#: treat different real items as the same one.
_CODE_OCR_NOISE_CHARS = re.compile(r"[.|_\s]")


def _clean_code_for_comparison(text: str | None) -> str:
    return _CODE_OCR_NOISE_CHARS.sub("", text.strip().upper()) if text else ""


def check_category_selector_match(
    expected_category: str | None, expected_selector: str | None,
    observed_category: str | None, observed_selector: str | None,
) -> CategorySelectorMatchResult:
    """Phase 5.12 (live-caught): `select_candidate()` already
    independently proves the correct candidate was clicked, via an
    exact live UI-Automation TEXT match against candidate.category/
    selector (see that method's docstring) -- never OCR. This function
    is what runs AFTER that, on the OCR read of the row's own cells
    (`read_populated_fields()`), which exists as defense-in-depth
    against Xactimate itself doing something unexpected post-click, not
    to re-litigate a click already proven correct by stronger evidence.
    A strict raw-equality comparison there was cancelling correct,
    verified selections purely on OCR noise -- live-reproduced:
    expected WDR/SCRN< read back as WD/. (a stray leading OCR artifact
    picked as the "selector", and the category crop truncating a real
    3-letter code by one character), which used to hard-cancel an
    objectively correct match.

    Tolerates, in EITHER category or selector independently:
    - whitespace and the specific noise characters confirmed live
      (".", "|", "_") -- never '<>+-', which are real, meaningful parts
      of distinct catalog codes here (see _CODE_OCR_NOISE_CHARS).
    - a ONE-DIRECTIONAL truncation: the observed text is a genuine
      PREFIX of the expected text (e.g. "WD" prefix of "WDR", "SCRN"
      prefix of "SCRN<") of at least 2 characters. Deliberately never
      the reverse (observed longer than / not a prefix of expected) --
      every OCR failure mode observed live in this codebase has been
      characters DROPPED, never invented, so this can never mistake a
      genuinely different, longer/unrelated code for a match.

    Returns "unreadable" (not "mismatch") when the observed text is
    empty/None -- OCR producing nothing is inconclusive, not evidence
    of a wrong selection, matching check_unit_compatibility()'s own
    "unreadable observed is not evidence of a mismatch" principle.
    Still a stop condition downstream (an unreadable field can't
    positively confirm anything either) -- just reported with an
    honest, distinct reason."""

    def result(state: str, reason: str) -> CategorySelectorMatchResult:
        return CategorySelectorMatchResult(
            expected_category=expected_category, expected_selector=expected_selector,
            observed_category=observed_category, observed_selector=observed_selector,
            match_state=state, reason=reason,
        )

    exp_cat, exp_sel = _clean_code_for_comparison(expected_category), _clean_code_for_comparison(expected_selector)
    obs_cat, obs_sel = _clean_code_for_comparison(observed_category), _clean_code_for_comparison(observed_selector)

    if not obs_cat or not obs_sel:
        return result("unreadable", f"observed category/selector ({observed_category!r}/{observed_selector!r}) could not be read at all")

    if exp_cat == obs_cat and exp_sel == obs_sel:
        exact = (expected_category, expected_selector) == (observed_category, observed_selector)
        return result(
            "exact_match" if exact else "normalized_match",
            "observed matches expected" + ("" if exact else " after normalizing whitespace/OCR noise characters"),
        )

    def is_tolerable(expected: str, observed: str) -> bool:
        return expected == observed or (len(observed) >= 2 and expected.startswith(observed))

    if is_tolerable(exp_cat, obs_cat) and is_tolerable(exp_sel, obs_sel):
        return result(
            "normalized_match",
            f"observed {observed_category!r}/{observed_selector!r} is a truncated/noisy OCR read of "
            f"expected {expected_category!r}/{expected_selector!r}",
        )

    # Phase 5.16 (live-caught): the tolerance above only ever handles a
    # DROPPED character (a clean truncation) -- it has no answer for a
    # SUBSTITUTED one. Live-reproduced twice independently this
    # engagement, both times on the exact same selector: "RFG/300S"
    # read back as "RFG/3008" (S misread as the visually similar digit
    # 8) immediately after select_candidate() had ALREADY independently
    # proven the correct row was clicked via live UI-Automation TEXT
    # (never OCR) -- this check runs strictly as defense-in-depth on
    # top of that stronger evidence, exactly the situation this
    # function's own docstring describes. Scoped narrowly and
    # symmetrically with that upstream guarantee: the CATEGORY must
    # still match EXACTLY. A same-length, single-character category
    # substitution was DELIBERATELY tried and reverted here (live-
    # caught: "WDR" read back as "WDI") -- "SFG" vs "RFG" is a real,
    # genuinely-different, easily-confusable category pair already
    # guarded by a dedicated regression test below, and a 3-letter code
    # has too little room for a substituted character to reliably stay
    # inside the same family. Only the SELECTOR tolerates exactly one
    # substituted character in an otherwise equal-length string --
    # never a length difference, never more than one differing
    # character, never the category.
    if exp_cat == obs_cat and len(exp_sel) == len(obs_sel):
        differing = sum(1 for a, b in zip(exp_sel, obs_sel) if a != b)
        if differing == 1:
            return result(
                "normalized_match",
                f"observed {observed_category!r}/{observed_selector!r} is a single-character OCR "
                f"substitution of expected {expected_category!r}/{expected_selector!r}",
            )

    return result(
        "mismatch",
        f"observed {observed_category!r}/{observed_selector!r} does not match, normalize to, or truncate from "
        f"expected {expected_category!r}/{expected_selector!r}",
    )


def _split_category_selector(code: str) -> tuple[str, str]:
    """Xactimate catalog codes are always a fixed 3-letter category
    prefix followed by a variable-length selector, e.g. "SFGGUTA" ->
    ("SFG", "GUTA") -- confirmed against every row observed live
    (SFG/GUTA, SFG/GUTA>, SFG/GUTC, SFG/GUTG, SFG/GUTHRA<, ...)."""
    code = code.strip()
    return code[:3], code[3:]


class WindowsXactimateAdapter(XactimateAdapter):
    """Real Windows desktop adapter. See module docstring for the
    validated mechanism.

    ``supports_live_execution = True`` as of Phase 5.4's pilot-gate
    sign-off -- see docs/build-estimate.md Phase 5.4 for the exact
    evidence: a fresh structural+financial baseline established live,
    3/3 residue-free cancellation trials (after fixing a real retry gap
    that had left a $330.31 row behind), 2 real AUTO_SELECT commits
    landing correctly in 2 distinct groups with zero wrong-group
    writes, and a full cleanup reconciliation back to the original
    baseline. `production_project_allowed` and `unattended_mode_
    allowed` are separate, still-False gates -- this flag alone does
    NOT authorize either. Any future regression in the underlying
    mechanism should flip this back to False with the same rigor it
    took to earn -- never silently."""

    supports_live_execution = True

    #: Phase 5.5C: stays False. Live investigation established that
    #: `ensure_group()`'s state-reset fix reliably creates exactly the
    #: FIRST TWO groups of a session as true root siblings (5/5 clean
    #: lifecycle trials -- see docs/build-estimate.md Phase 5.5C), but a
    #: 3rd+ group reproducibly nests under the 2nd regardless of which
    #: of three different reset strategies precedes it (Phase 5.7
    #: re-confirmed this once more, plus two new hypotheses -- double
    #: stickiness-reset, an explicit save between creations -- neither
    #: helped). This flag is specifically about PERFECT top-level
    #: sibling placement; see `group_creation_available` for the
    #: capability that actually matters for execution throughput. Flip
    #: this only alongside the same rigor: a real mechanism for N>2,
    #: independently verified ancestry, and repeated clean lifecycle
    #: trials.
    multi_group_creation_available = False

    #: Phase 5.7: True as of the product-requirement change in this
    #: phase -- group ancestry/nesting depth is no longer a blocking
    #: safety condition for TEST-project execution. `ensure_group()`
    #: succeeds whenever the requested group can be created and then
    #: uniquely, independently located/selected/verified BY NAME,
    #: regardless of where in the tree Xactimate actually placed it.
    #: This is deliberately a DIFFERENT claim than
    #: `multi_group_creation_available` (which is about perfect
    #: top-level sibling placement and remains False): "the named group
    #: exists and is usable" is proven live for creation #1, #2, AND a
    #: #3/#4 that lands nested -- see ensure_group()'s own docstring for
    #: the exact mechanism and docs/build-estimate.md Phase 5.7 for the
    #: live evidence.
    group_creation_available = True

    def __init__(
        self,
        expected_project_name: str,
        *,
        evidence_dir: Path | None = None,
        dropdown_timeout_s: float = 5.0,
        window_finder=None,
    ) -> None:
        self.expected_project_name = expected_project_name
        self.evidence_dir = evidence_dir or Path.cwd() / "automation_evidence"
        self.dropdown_timeout_s = dropdown_timeout_s
        #: injectable for tests that want to fake window discovery
        #: without a real Xactimate process; production code leaves
        #: this None and uses _default_window_finder.
        self._window_finder = window_finder or self._default_window_finder

        self._main_hwnd: int | None = None
        self._last_dropdown_hwnd: int | None = None
        self._last_dropdown_rows: list[_RawDropdownRow] = []
        self._last_selected: DropdownResult | None = None
        self._candidate_selection_count = 0
        # Rich counterpart to snapshot_grid_identities_for_activation()'s
        # public tuple baseline. Xactimate can regroup duplicate R&R rows
        # by activity (-,-,+,+), so pending detection must compare a
        # logical multiset rather than assume the old rows stay a prefix.
        self._last_activation_baseline_rows: list[ActivationRowSnapshot] | None = None
        self._last_commit_baseline_rows: list[CommitRowSnapshot] | None = None
        # Exact immutable frame behind the most recent activation snapshot.
        # Retained only long enough to persist diagnostic activity-cell crops;
        # never read by binding or any other execution decision.
        self._last_activation_snapshot_image = None
        self._last_activation_snapshot_offset: tuple[int, int] | None = None
        self._last_activation_snapshot_row_1_top: int | None = None
        # Diagnostic-only raw OCR outputs collected while the authoritative
        # activation frame is read. The active accumulator is never consumed
        # by binding; the completed copy is joined to persisted evidence by
        # the same row geometry as the existing activity-cell crops.
        self._active_activation_activity_ocr: dict[int, dict[str, str | None]] | None = None
        self._last_activation_snapshot_activity_ocr: dict[int, dict[str, str | None]] = {}
        self._pending_quantity_target: PendingQuantityTarget | None = None
        self.last_quantity_confirmation: QuantityEntryConfirmation | None = None
        self._current_query: str | None = None
        #: Phase 5.8: monotonic timestamp of the most recent
        #: search_by_description()/search_by_category_selector() call
        #: -- read by capture_dropdown() to fill in
        #: DropdownCaptureDiagnostics.search_submitted_at.
        self._current_query_submitted_at: float | None = None
        #: Phase 5.8: the most recent capture_dropdown() call's popup-
        #: lifecycle timeline -- read-only diagnostics, see
        #: DropdownCaptureDiagnostics. None until the first search.
        self.last_dropdown_diagnostics: DropdownCaptureDiagnostics | None = None

        # Phase 5.5D: committed-row protection + destructive-call audit
        # trail -- see destructive_audit.py. One ledger/auditor per
        # adapter instance, matching one live session; a fresh adapter
        # starts with an empty ledger.
        self._execution_context = ExecutionContext()
        self._protected_row_ledger = ProtectedRowLedger()
        self._destructive_auditor = DestructiveActionAuditor(self.evidence_dir / "destructive_action_audit.jsonl")
        #: Phase 5.9: append-only per-task row lifecycle ledger -- see
        #: execution_diagnostics.py's module docstring.
        self._row_lifecycle_ledger = RowLifecycleLedger(self.evidence_dir / "row_lifecycle_ledger.jsonl")
        self._rr_binding_diagnostic_ledger = RRBindingDiagnosticLedger(
            self.evidence_dir / "rr_binding_diagnostics.jsonl",
        )
        self._zero_delta_commit_diagnostic_ledger = ZeroDeltaCommitDiagnosticLedger(
            self.evidence_dir / "zero_delta_commit_diagnostics.jsonl",
        )

        #: Phase 5.7B: names (normalized lowercase) of groups this
        #: adapter INSTANCE has already positively verified via a real
        #: disposable-probe commit -- see verify_group()'s use_cache
        #: parameter. Only ever accumulates confirmed True results,
        #: never a prior False (a stale negative would incorrectly keep
        #: blocking a group that's actually fine now; a stale positive
        #: would be unsafe, which is why only True is ever cached).
        #: Scoped to this adapter instance ("the current live run"), not
        #: persisted -- a fresh adapter (a genuinely new live session)
        #: always starts with an empty cache and re-earns every group.
        self._verified_groups_this_session: set[str] = set()
        #: Phase 5.8 Stage 8: total real disposable-probe runs (never
        #: incremented on a cache hit) and a per-group breakdown -- read-
        #: only diagnostics proving the cache is actually eliminating
        #: redundant probes, not just assumed to.
        self.probes_run_total: int = 0
        self.probes_by_group: dict[str, int] = {}
        #: Phase 5.9A: "MATCH"/"MISMATCH"/"UNAVAILABLE"/None (never run
        #: yet) -- the Grouping panel Subtotal pixel-delta check's
        #: result from the MOST RECENT _verify_group_once() call,
        #: recorded purely as optional corroborating diagnostic
        #: evidence. Never consulted by verify_group()/_verify_group_
        #: once() to decide pass/fail -- see that method's docstring.
        self.last_verify_group_subtotal_evidence: str | None = None
        #: Phase 5.10C: per-poll trace from the MOST RECENT
        #: _wait_for_probe_visible() call -- see that method's own
        #: docstring. Live diagnostics only, never consulted for
        #: pass/fail decisions.
        self.last_probe_visibility_polls: list[dict] = []
        #: Phase 5.12: click-target diagnostics from the MOST RECENT
        #: select_candidate() call -- row index/bounds/click x,y and the
        #: row's own UIA text immediately before the click, proving (or
        #: disproving) that the automation clicked the intended
        #: candidate row. Never consulted for pass/fail decisions --
        #: select_candidate()'s own live text-match against
        #: candidate.category/selector (see its docstring) is what
        #: already guarantees correctness; this is diagnostic-only.
        self.last_candidate_click: dict | None = None
        #: Phase 5.12: the 3 individual OCR reads read_populated_fields()
        #: majority-votes over (see that method's docstring), kept
        #: separately so a caller diagnosing a populated-fields mismatch
        #: can see whether the 3 reads agreed (a stable, real read) or
        #: disagreed (transient OCR noise) instead of only the final
        #: voted value.
        self.last_populated_fields_reads: list[dict] = []

    # ------------------------------------------------------------------
    # Phase 5.5D: committed-row protection + destructive-call audit
    # ------------------------------------------------------------------

    def set_execution_context(
        self, *, run_id: str | None = None, task_id: str | None = None,
        source_row: str | None = None, group: str | None = None,
    ) -> None:
        """Not part of the abstract contract. Purely descriptive --
        execution_runner.py calls this before each group/task so
        destructive-call audit entries and the protected-row ledger
        know "what's happening right now" without threading that
        context through every method's own parameters. Never affects
        ranking, routing, or commit behavior. Any argument left as
        None keeps that field's PREVIOUS value (a caller updating only
        `task_id` between tasks in the same group need not re-pass
        `run_id`/`group` every time)."""
        if run_id is not None:
            self._execution_context.run_id = run_id
        if task_id is not None:
            self._execution_context.task_id = task_id
        if source_row is not None:
            self._execution_context.source_row = source_row
        if group is not None:
            self._execution_context.group = group

    def record_lifecycle_event(self, event: str, **detail) -> None:
        """Not part of the abstract contract (Phase 5.9). Appends one
        entry to `self._row_lifecycle_ledger`, tagged with the CURRENT
        execution context (run_id/task_id/source_row/group -- see
        set_execution_context()). Purely observational, like
        record_protected_commit()'s own audit trail -- never raises,
        never affects control flow. Callers (orchestrator.py,
        execution_runner.py) call this duck-typed (`hasattr(adapter,
        "record_lifecycle_event")`) so a non-Windows/fake adapter is
        completely unaffected."""
        try:
            ctx = self._execution_context
            self._row_lifecycle_ledger.record(
                run_id=ctx.run_id, task_id=ctx.task_id, source_row=ctx.source_row,
                group=ctx.group, event=event, **detail,
            )
        except Exception:
            pass

    #: Phase 5.9 Stage 4: bounded settle-wait before ANY entry point
    #: that changes/creates group context (~3s total, matching this
    #: file's other bounded-retry constants) -- long enough to absorb
    #: the OS's own popup-close animation/repaint lag after recover()'s
    #: Escape, never unbounded.
    _GROUP_TRANSITION_SETTLE_ATTEMPTS = 6
    _GROUP_TRANSITION_SETTLE_POLL_S = 0.5

    def _assert_group_transition_settled(self, *, next_group: str) -> None:
        """Phase 5.9 Stage 4: the hard invariant this phase exists to
        prove -- "a new group may not begin while the previous group's
        final task still has an active dropdown, pending selection, or
        unfinished commit" -- checked live immediately before this
        adapter does ANYTHING that changes/creates group context
        (ensure_group(), select_group(), verify_group()'s probe, and
        `_reset_group_creation_stickiness()`'s own Components-tab
        click, which is a group-context-changing action in its own
        right and was live-caught to run several steps AFTER
        ensure_group()'s own opening self-heal check, with no fresh
        check immediately before ITS click -- exactly the gap a
        reappearing/slow-to-close popup could fall through).

        Polls (bounded, never unbounded) for the results popup and any
        unexpected dialog to clear on their own. Raises
        GroupTransitionUnsafeError -- NEVER dismisses anything itself,
        unlike `_dismiss_stray_results_popup()` -- if either is still
        present once the wait is exhausted, so the caller's group
        transition genuinely does not happen (see docs/build-estimate.md
        Phase 5.9: "Do not dismiss an open results dropdown just to
        proceed to the next group.")."""
        popup_hwnd = None
        dialog_present = False
        for _attempt in range(self._GROUP_TRANSITION_SETTLE_ATTEMPTS):
            popup_hwnd = self._find_dropdown_window()
            dialog_present = self._unexpected_dialog_present()
            if popup_hwnd is None and not dialog_present:
                return
            time.sleep(self._GROUP_TRANSITION_SETTLE_POLL_S)
        raise GroupTransitionUnsafeError(
            f"Refusing to enter group {next_group!r}: after a "
            f"{self._GROUP_TRANSITION_SETTLE_ATTEMPTS * self._GROUP_TRANSITION_SETTLE_POLL_S:.1f}s "
            f"settle-wait the live UI still shows a results popup (hwnd={popup_hwnd!r}) and/or an "
            f"unexpected dialog (present={dialog_present!r}) -- the previous group's final task has "
            f"not actually finished at the UI level."
        )

    def record_protected_commit(
        self, *, category: str | None, selector: str | None, description: str | None = None,
        quantity: float | None = None, unit: str | None = None,
        xactimate_item_number: str | None = None, verification_state: str | None = None,
    ) -> None:
        """Not part of the abstract contract (Phase 5.5D). Called by
        orchestrator.execute_plan() as soon as commit_item() completes
        successfully (never waiting on verify_commit(), which can
        itself legitimately fail/mismatch AFTER a real row already
        landed) -- the row is protected under the CURRENT execution
        context's group (see set_execution_context()). Best-effort:
        never raises, since a failure to protect a row must not mask
        the commit's own already-decided success."""
        try:
            hwnd = self._ensure_main_window()
            image, offset = self._capture_and_locate(hwnd)
            row_count_after = None
            if offset is not None:
                geom = self._last_row_geometry(image, offset)
                if geom is not None:
                    row_count_after, _ = geom
            ctx = self._execution_context
            record = ProtectedRowRecord(
                task_id=ctx.task_id, source_row=ctx.source_row, group=ctx.group,
                category=category, selector=selector, description=description,
                quantity=quantity, unit=unit, xactimate_item_number=xactimate_item_number,
                committed_row_identity=(category, selector),
                row_count_after_commit=row_count_after,
                timestamp=time.strftime("%Y-%m-%dT%H:%M:%S"),
                verification_state=verification_state,
            )
            self._protected_row_ledger.record(ctx.group, record)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Lazy Windows-only imports
    # ------------------------------------------------------------------

    @staticmethod
    def _win32():
        import ctypes
        import ctypes.wintypes as wintypes

        return ctypes, wintypes

    @staticmethod
    def _win32gui():
        import win32gui

        return win32gui

    @staticmethod
    def _win32ui():
        import win32ui

        return win32ui

    @staticmethod
    def _uia():
        import comtypes.client

        comtypes.client.GetModule("UIAutomationCore.dll")
        from comtypes.gen import UIAutomationClient as UIA

        uia = comtypes.client.CreateObject(UIA.CUIAutomation, interface=UIA.IUIAutomation)
        return uia, UIA

    @staticmethod
    def _pytesseract():
        import pytesseract

        # A future config-driven install path belongs in
        # config/xactimate_windows_profile.yaml once this adapter is
        # promoted past the pilot gate; hardcoded here to match the
        # environment this was validated against (see docs).
        default_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
        if Path(default_cmd).exists():
            pytesseract.pytesseract.tesseract_cmd = default_cmd
        return pytesseract

    # ------------------------------------------------------------------
    # Window discovery
    # ------------------------------------------------------------------

    def _default_window_finder(self):
        """Enumerates top-level windows and classifies them. Returns
        (main_windows: list[(hwnd, title, rect)], popup_windows: list[(hwnd, title, rect)])
        where popup_windows are unnamed HwndWrapper windows (the results
        dropdown or, transiently, the "Loading" overlay) owned by the
        same class family as the main window."""
        win32gui = self._win32gui()
        ctypes, wintypes = self._win32()
        user32 = ctypes.windll.user32

        mains: list[tuple[int, str, tuple[int, int, int, int]]] = []
        popups: list[tuple[int, str, tuple[int, int, int, int]]] = []

        def cb(hwnd, _):
            try:
                cls = win32gui.GetClassName(hwnd)
            except Exception:
                return True
            if _APP_CLASS_MARKER not in cls:
                return True
            try:
                title = win32gui.GetWindowText(hwnd)
                visible = win32gui.IsWindowVisible(hwnd)
                rect = win32gui.GetWindowRect(hwnd)
            except Exception:
                return True
            if not visible or rect == (0, 0, 0, 0):
                return True
            if title and title != "Loading":
                mains.append((hwnd, title, rect))
            elif title != "Loading" and (rect[2] - rect[0]) > 50:
                popups.append((hwnd, title, rect))
            return True

        win32gui.EnumWindows(cb, None)
        return mains, popups

    def _find_main_window(self) -> tuple[int, str] | None:
        mains, _ = self._window_finder()
        if not mains:
            return None
        for hwnd, title, _rect in mains:
            if title.strip().lower() == self.expected_project_name.strip().lower():
                return hwnd, title
        # application is running but the active project doesn't match --
        # verify_application() and verify_project() must be able to
        # distinguish these two cases, so return the first candidate
        # anyway and let verify_project() do the name comparison.
        hwnd, title, _rect = mains[0]
        return hwnd, title

    def _find_dropdown_window(self) -> int | None:
        _, popups = self._window_finder()
        if not popups:
            return None
        return popups[0][0]

    # ------------------------------------------------------------------
    # Low-level input primitives -- the validated mechanism
    # ------------------------------------------------------------------

    def _get_client_origin(self, hwnd: int) -> tuple[int, int]:
        ctypes, wintypes = self._win32()
        user32 = ctypes.windll.user32
        pt = wintypes.POINT(0, 0)
        user32.ClientToScreen(hwnd, ctypes.byref(pt))
        return pt.x, pt.y

    def _force_foreground(self, hwnd: int) -> bool:
        """SetForegroundWindow alone is frequently denied by Windows'
        foreground-lock protection when called from a background
        automation process. Uses the documented AttachThreadInput
        workaround, falling back to a minimize/restore cycle -- both
        confirmed live (see docs/xactimate-lookup.md Phase 4.1/4.2)."""
        ctypes, wintypes = self._win32()
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        if user32.GetForegroundWindow() == hwnd:
            return True

        target_pid = wintypes.DWORD()
        target_tid = user32.GetWindowThreadProcessId(hwnd, ctypes.byref(target_pid))
        current_tid = kernel32.GetCurrentThreadId()

        attached = False
        if target_tid and target_tid != current_tid:
            attached = bool(user32.AttachThreadInput(current_tid, target_tid, True))

        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)

        if attached:
            user32.AttachThreadInput(current_tid, target_tid, False)

        time.sleep(0.3)
        if user32.GetForegroundWindow() != hwnd:
            user32.ShowWindow(hwnd, 6)  # SW_MINIMIZE
            time.sleep(0.15)
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            user32.SetForegroundWindow(hwnd)
            time.sleep(0.3)

        return user32.GetForegroundWindow() == hwnd

    def _click_client(self, hwnd: int, x: int, y: int) -> None:
        ctypes, _ = self._win32()
        user32 = ctypes.windll.user32
        ox, oy = self._get_client_origin(hwnd)
        user32.SetCursorPos(ox + x, oy + y)
        time.sleep(0.05)
        user32.mouse_event(0x0002, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTDOWN
        time.sleep(0.05)
        user32.mouse_event(0x0004, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTUP

    def _click_screen(self, x: int, y: int) -> None:
        """Click at absolute screen coordinates -- used only for a
        freshly-read UI Automation bounding rectangle, which is already
        in screen coordinates. Never used with a cached/guessed point."""
        ctypes, _ = self._win32()
        user32 = ctypes.windll.user32
        user32.SetCursorPos(x, y)
        time.sleep(0.05)
        user32.mouse_event(0x0002, 0, 0, 0, 0)
        time.sleep(0.05)
        user32.mouse_event(0x0004, 0, 0, 0, 0)

    #: Phase 5.30: per-character pacing for the validated keybd_event
    #: typing mechanism below -- the ONLY free parameter in that
    #: mechanism. How fast a character can follow the previous one
    #: without Xactimate's live-search field dropping/corrupting a
    #: keystroke is an empirical property of Xactimate's own event
    #: handling, not of this codebase. Originally 0.1s (see docs/
    #: xactimate-lookup.md Phase 4.2B for the 5/5-trial validation of
    #: the MECHANISM itself, at that value). Live-validated down to
    #: 0.02s (TEST project, two representative searches -- a 28-char
    #: ordinary description and a 64-char R&R description -- 3/3
    #: intervals {0.05, 0.03, 0.02} each: identical 10-row popup,
    #: identical top-scoring (1.0) expected candidate, no truncation/
    #: corruption at any tested speed). Set to 0.03s, a margin above
    #: that 0.02s floor: this pass tested one trial per interval/
    #: search, short of Phase 4.2B's own 5/5-trial bar for the
    #: mechanism itself, and 0.03s already captures the large majority
    #: of the available saving (~2s/ordinary, ~4.7s/long-R&R search
    #: vs the original 0.1s). Kept as a class constant -- never a bare
    #: literal inside the loop -- so a future controlled live timing
    #: validation pass can override it per-call via `_type_keybdevent
    #: (text, char_interval_s=...)` without touching the mechanism (VK
    #: scan, shift handling, keybd_event sequence) at all. Production
    #: code never passes an override.
    _SEARCH_TYPE_CHAR_INTERVAL_S = 0.03

    def _type_keybdevent(self, text: str, *, char_interval_s: float | None = None) -> None:
        """The validated trigger mechanism: SendInput-based typing
        (pywinauto's default) never triggers Xactimate's live-search
        binding; the legacy keybd_event API does, reliably (5/5 trials,
        see docs/xactimate-lookup.md Phase 4.2B).

        `char_interval_s` (Phase 5.30, default None -- every existing
        caller unaffected, uses `_SEARCH_TYPE_CHAR_INTERVAL_S`):
        overrides the per-character trailing pacing for this call
        only. Exists solely for a controlled live timing validation
        pass (finding the fastest interval Xactimate's live-search
        field tolerates without dropping/corrupting keystrokes) --
        never changes the VK scan, shift handling, or keybd_event
        sequence itself, only how long this method waits between
        characters. See `_SEARCH_TYPE_CHAR_INTERVAL_S`'s own
        docstring."""
        interval = self._SEARCH_TYPE_CHAR_INTERVAL_S if char_interval_s is None else char_interval_s
        ctypes, _ = self._win32()
        user32 = ctypes.windll.user32
        KEYEVENTF_KEYUP = 0x0002
        for ch in text:
            vk_scan = user32.VkKeyScanW(ord(ch))
            vk = vk_scan & 0xFF
            need_shift = bool((vk_scan >> 8) & 1)
            if need_shift:
                user32.keybd_event(0x10, 0, 0, 0)
                time.sleep(0.01)
            user32.keybd_event(vk, 0, 0, 0)
            time.sleep(0.02)
            user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
            if need_shift:
                time.sleep(0.01)
                user32.keybd_event(0x10, 0, KEYEVENTF_KEYUP, 0)
            time.sleep(interval)

    def _press_key(self, vk: int) -> None:
        ctypes, _ = self._win32()
        user32 = ctypes.windll.user32
        KEYEVENTF_KEYUP = 0x0002
        user32.keybd_event(vk, 0, 0, 0)
        time.sleep(0.05)
        user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)

    def _press_ctrl(self, vk: int) -> None:
        ctypes, _ = self._win32()
        user32 = ctypes.windll.user32
        KEYEVENTF_KEYUP = 0x0002
        VK_CONTROL = 0x11
        user32.keybd_event(VK_CONTROL, 0, 0, 0)
        time.sleep(0.02)
        user32.keybd_event(vk, 0, 0, 0)
        time.sleep(0.05)
        user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
        time.sleep(0.02)
        user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)

    def _select_all_and_delete(self) -> None:
        VK_A = 0x41
        VK_DELETE = 0x2E
        self._press_ctrl(VK_A)
        time.sleep(0.1)
        self._press_key(VK_DELETE)
        time.sleep(0.2)

    # ------------------------------------------------------------------
    # Screen capture
    # ------------------------------------------------------------------

    def _capture_client_image(self, hwnd: int):
        """PrintWindow against the MAIN window -- works fine for the
        static chrome (search box text, Quick Entry panel, grid), which
        is part of the main window's own rendered surface. Does NOT and
        cannot capture the results popup -- that is a different
        top-level window (see module docstring)."""
        from PIL import Image

        win32gui = self._win32gui()
        win32ui = self._win32ui()
        ctypes, wintypes = self._win32()
        user32 = ctypes.windll.user32

        wrect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(wrect))
        crect = wintypes.RECT()
        user32.GetClientRect(hwnd, ctypes.byref(crect))
        ox, oy = self._get_client_origin(hwnd)
        cl_rel = ox - wrect.left
        ct_rel = oy - wrect.top
        cw = crect.right - crect.left
        ch = crect.bottom - crect.top

        hwndDC = win32gui.GetWindowDC(hwnd)
        mfcDC = win32ui.CreateDCFromHandle(hwndDC)
        saveDC = mfcDC.CreateCompatibleDC()
        saveBitMap = win32ui.CreateBitmap()
        full_w = wrect.right - wrect.left
        full_h = wrect.bottom - wrect.top
        saveBitMap.CreateCompatibleBitmap(mfcDC, full_w, full_h)
        saveDC.SelectObject(saveBitMap)
        user32.PrintWindow(hwnd, saveDC.GetSafeHdc(), 2)  # PW_RENDERFULLCONTENT
        bmpinfo = saveBitMap.GetInfo()
        bmpstr = saveBitMap.GetBitmapBits(True)
        img = Image.frombuffer("RGB", (bmpinfo["bmWidth"], bmpinfo["bmHeight"]), bmpstr, "raw", "BGRX", 0, 1)
        win32gui.DeleteObject(saveBitMap.GetHandle())
        saveDC.DeleteDC()
        mfcDC.DeleteDC()
        win32gui.ReleaseDC(hwnd, hwndDC)

        return img.crop((cl_rel, ct_rel, cl_rel + cw, ct_rel + ch))

    # ------------------------------------------------------------------
    # OCR-based, self-locating field reading (the static chrome has no
    # UI Automation peers at all -- confirmed in Phase 4.1/4.2 -- so
    # this is the only viable strategy for read_populated_fields()).
    # ------------------------------------------------------------------

    #: Phase 5.26: live-caught -- a real quantity-confirmation OCR read
    #: hung for 13+ minutes (manual process kill required) with no
    #: tesseract.exe process visible by the time it was investigated,
    #: and none of the surrounding control flow (enter_quantity()'s own
    #: post-write confirmation is a single straight-line sequence, no
    #: loop) can explain a multi-minute stall on its own. Root cause:
    #: pytesseract.image_to_string()/image_to_data() default to
    #: timeout=0, which pytesseract's own timeout_manager() treats as
    #: "no timeout at all" -- an unbounded subprocess.communicate()
    #: that blocks forever if the tesseract subprocess itself ever
    #: hangs or deadlocks. Every raw pytesseract call in this file goes
    #: through _ocr_text()/_ocr_data() below so this bound applies
    #: uniformly, not just to the R&R quantity path that happened to
    #: expose it. 10s is generous for a legitimate slow read under real
    #: system load while remaining far short of "minutes".
    _OCR_CALL_TIMEOUT_S = 10

    def _ocr_text(self, image, psm: int = 7) -> str:
        pytesseract = self._pytesseract()
        try:
            return pytesseract.image_to_string(
                image, config=f"--psm {psm}", timeout=self._OCR_CALL_TIMEOUT_S,
            ).strip()
        except RuntimeError as exc:
            if "timeout" not in str(exc).lower():
                raise
            # Treated exactly like any other unreadable/empty OCR result
            # -- every existing caller already handles "" as "nothing
            # useful was read here", never as a structural signal.
            return ""

    #: Fallback shape callers already only ever index by these specific
    #: keys (see every _ocr_data() call site) -- matches pytesseract's
    #: own Output.DICT column set for the columns actually consumed.
    _EMPTY_OCR_DATA = {"text": [], "left": [], "top": [], "width": [], "height": []}

    def _ocr_data(self, image, config: str = "--psm 11") -> dict:
        """Shared, bounded counterpart to _ocr_text() for every word-
        level pytesseract.image_to_data() call in this file (label/tab/
        anchor location) -- same timeout guard, same "a timeout is just
        another unreadable/empty result" contract: every caller already
        iterates `data["text"]` and treats no matches as "not found",
        so an empty result set here is never a fabricated finding, just
        an honest "found nothing this attempt."""
        pytesseract = self._pytesseract()
        from pytesseract import Output

        try:
            return pytesseract.image_to_data(
                image, output_type=Output.DICT, config=config, timeout=self._OCR_CALL_TIMEOUT_S,
            )
        except RuntimeError as exc:
            if "timeout" not in str(exc).lower():
                raise
            return dict(self._EMPTY_OCR_DATA)

    @staticmethod
    def _normalize_inch_mark(text: str) -> str:
        """Tesseract live-observed to consistently misread a trailing
        double-quote (inch mark) as a degree sign at this crop size --
        e.g. 'up to 5\xb0' for the real 'up to 5"' (see
        docs/xactimate-lookup.md Phase 4.3). Xactimate descriptions in
        this catalog use the inch mark, never degrees, so a degree sign
        directly after a digit is corrected; any other occurrence is
        left alone rather than guessed at."""
        import re

        return re.sub(r"(?<=\d)\xb0", '"', text)

    def _locate_label(self, image, needle: str, prefer: str = "topmost") -> tuple[int, int, int, int] | None:
        """Finds the bounding box of a specific label text anywhere in
        the image, via word-level OCR data -- used to self-correct for
        scroll drift instead of trusting a cached absolute position.

        Two live-caught bugs fixed here (see docs/xactimate-lookup.md
        Phase 4.3): (1) the grid has its own "Cat" column header, a
        second real match for the "Cat:" needle -- Tesseract's default
        page-segmentation mode (PSM 3) intermittently failed to detect
        the Quick Entry panel's "Cat:" label at all in some captured
        frames (confirmed by testing PSM 3/4/6/11/12 against the same
        saved screenshot: only 11 and 12 found both occurrences), so a
        single-match implementation would sometimes silently anchor on
        the wrong one. Fixed by using PSM 11 (sparse text) AND an
        explicit `prefer` direction instead of trusting dict/OCR
        iteration order. (2) Even PSM 11 was later found to
        intermittently miss the Quick Entry "Cat:" label entirely (not
        a wrong match -- no match at all) when its value box is empty,
        which is why `_anchor_offset()` no longer anchors on it at all
        -- see that method's docstring."""
        data = self._ocr_data(image)
        # Live-caught (Phase 5.3): PSM 11 reads the main grid's narrow
        # "Cat" column header with a bleeding column-divider artifact --
        # observed live as "Cat|" on one capture and "Cat," on the very
        # next capture of the SAME unchanged screen -- often enough to
        # make an exact-after-colon-strip match silently fail and leave
        # ONLY Quick Entry's "Cat:" label as a candidate, even with
        # prefer="bottommost" (there is nothing else to prefer over).
        # Since the artifact character itself is not stable, stripping
        # ALL trailing non-alphanumeric characters (not a fixed set) is
        # what actually closes this -- still exact-equality on the core
        # word, so this cannot match a genuinely different word.
        import re

        def _clean(word: str) -> str:
            return re.sub(r"[^a-z0-9]+$", "", word.strip().lower())

        needle_clean = _clean(needle)
        matches = []
        for i, word in enumerate(data["text"]):
            if _clean(word) == needle_clean:
                matches.append((data["top"][i], data["left"][i], data["width"][i], data["height"][i]))
        if not matches:
            return None
        matches.sort(key=lambda m: (m[0], m[1]), reverse=(prefer == "bottommost"))
        top, left, width, height = matches[0]
        return (left, top, left + width, top + height)

    def _locate_search_field(self, image) -> tuple[int, int, int, int] | None:
        """Locate the current Search input from live OCR geometry.

        Static Xactimate chrome has no UIA/MSAA descendants, so there is
        no accessible Edit element to query.  Live structural evidence
        provides the locator instead: normally the section heading and
        Search button; in focused rendering where that heading drops out
        of OCR, the Search button plus either the ``Home > Price List``
        breadcrumb or the lower ``Price List Searches`` heading. The input
        must be the long bordered rectangle corroborated by one of those
        relationships. Both horizontal borders are detected from current
        pixels; calibrated search-box coordinates are never consulted.
        """
        data = self._ocr_data(image)
        words = []
        word_boxes: dict[str, list[tuple[int, int, int, int]]] = {}
        for i, text in enumerate(data["text"]):
            clean = re.sub(r"[^a-z]+", "", str(text).lower())
            box = (
                data["left"][i], data["top"][i],
                data["left"][i] + data["width"][i],
                data["top"][i] + data["height"][i],
            )
            if clean:
                word_boxes.setdefault(clean, []).append(box)
            if clean == "search":
                words.append((data["left"][i], data["top"][i], data["width"][i], data["height"][i]))

        gray = image.convert("L")

        def dark_runs(y: int, x0: int, x1: int) -> list[tuple[int, int]]:
            runs = []
            start = None
            for x in range(x0, x1):
                dark = gray.getpixel((x, y)) < 245
                if dark and start is None:
                    start = x
                elif not dark and start is not None:
                    if x - start >= 120:
                        runs.append((start, x))
                    start = None
            if start is not None and x1 - start >= 120:
                runs.append((start, x1))
            return runs

        relationships = []
        for hx, hy, hw, hh in sorted(words, key=lambda box: box[1]):
            for bx, by, bw, bh in words:
                if not (20 <= by - hy <= 90 and bx >= hx + 140):
                    continue
                relationships.append((hx - 16, (bx, by, bx + bw, by + bh)))

        # Focused-state live evidence: clicking this WPF Search field can
        # make OCR omit the section-heading "Search" while the Search
        # button, Home breadcrumb, Price List Searches heading below the
        # input, and input borders remain unchanged and readable. Require
        # that complete vertical relationship rather than treating Price
        # as if it shared Home's breadcrumb row (it does not).
        for bx, by, bw, bh in words:
            button = (bx, by, bx + bw, by + bh)
            # Live-caught focused state: the section-heading "Search"
            # and the lower "Price List Searches" heading can both be
            # absent from OCR while the actual breadcrumb remains
            # readable as Home > Price List. Corroborate that same-row
            # breadcrumb with the Search button below/right; the active
            # Items underline is independently required by
            # _items_search_pane_field().
            for home in word_boxes.get("home", []):
                for price in word_boxes.get("price", []):
                    for list_box in word_boxes.get("list", []):
                        breadcrumb_mid_y = (home[1] + home[3]) // 2
                        if not (10 <= by - home[1] <= 50):
                            continue
                        if not (
                            abs(breadcrumb_mid_y - (price[1] + price[3]) // 2) <= 8
                            and abs(breadcrumb_mid_y - (list_box[1] + list_box[3]) // 2) <= 8
                        ):
                            continue
                        if not (home[0] < price[0] < list_box[0] < bx):
                            continue
                        if bx - home[0] < 140:
                            continue
                        relationships.append((home[0] - 16, button))

            for home in word_boxes.get("home", []):
                for price in word_boxes.get("price", []):
                    for list_box in word_boxes.get("list", []):
                        for searches in word_boxes.get("searches", []):
                            heading_mid_y = (price[1] + price[3]) // 2
                            if not (10 <= by - home[1] <= 50):
                                continue
                            if not (20 <= price[1] - by <= 60):
                                continue
                            if not (
                                abs(heading_mid_y - (list_box[1] + list_box[3]) // 2) <= 8
                                and abs(heading_mid_y - (searches[1] + searches[3]) // 2) <= 8
                            ):
                                continue
                            if not (price[0] < list_box[0] < searches[0] < bx):
                                continue
                            if abs(price[0] - home[0]) > 30 or bx - home[0] < 140:
                                continue
                            relationships.append((min(home[0], price[0]) - 16, button))

        seen_relationships = set()
        for left_hint, button in relationships:
            bx, by, br, bb = button
            relationship_key = (left_hint, button)
            if relationship_key in seen_relationships:
                continue
            seen_relationships.add(relationship_key)
            x0 = max(0, left_hint)
            x1 = min(image.width, bx - 8)
            y0 = max(0, by - 12)
            y1 = min(image.height, bb + 12)
            horizontal = []
            for y in range(y0, y1):
                horizontal.extend((y, left, right) for left, right in dark_runs(y, x0, x1))
            for top, left, right in horizontal:
                for bottom, left2, right2 in horizontal:
                    height = bottom - top
                    overlap = min(right, right2) - max(left, left2)
                    if 14 <= height <= 32 and overlap >= 120:
                        field_left = max(left, left2)
                        field_right = min(right, right2)
                        if field_right - field_left >= 120:
                            return (field_left, top, field_right, bottom + 1)
        return None

    def _locate_search_subtab(self, image) -> tuple[int, int, int, int] | None:
        """Locate the small 'Search' sub-tab inside the Quick Entry panel
        -- distinct from the outer Items tab and from
        _locate_search_field()'s own broader Search heading/button
        matches. Quick Entry shows this sub-tab alongside a sibling
        'Macros' sub-tab; a match is accepted only when 'Macros' sits
        immediately to its right on the same row, the same neighbor-
        corroboration pattern _locate_items_tab() uses for its own tab
        strip. Live-caught (this diagnosis): after a candidate is
        selected/activated, this inner sub-tab can end up off 'Search'
        while the OUTER Items tab remains active throughout -- clicking
        the outer tab again is then a no-op, since it was never the
        thing that changed.

        Returns None -- ambiguous, never guessed at -- unless EXACTLY
        one such Search/Macros pair is found, deliberately stricter than
        _locate_items_tab()'s first-match behavior: a second bounded
        recovery click is only ever safe to make from a uniquely
        identified target."""
        data = self._ocr_data(image)
        search_boxes = []
        macros_boxes = []
        for i, text in enumerate(data["text"]):
            clean = re.sub(r"[^a-z]+", "", str(text).lower())
            box = (
                data["left"][i], data["top"][i],
                data["left"][i] + data["width"][i],
                data["top"][i] + data["height"][i],
            )
            if clean == "search":
                search_boxes.append(box)
            elif clean == "macros":
                macros_boxes.append(box)

        matches = []
        for search in search_boxes:
            search_mid_y = (search[1] + search[3]) // 2
            for macros in macros_boxes:
                macros_mid_y = (macros[1] + macros[3]) // 2
                if search[0] < macros[0] and abs(search_mid_y - macros_mid_y) <= 12:
                    matches.append(search)
                    break
        if len(matches) != 1:
            return None
        return matches[0]

    def _locate_items_tab(self, image) -> tuple[int, int, int, int] | None:
        """Locate Items only when a neighboring tab proves tab context.

        ``Items`` also appears in the page title (``Estimate Items``),
        and OCR can miss any one narrow tab label.  Evaluate every Items
        match and accept one only when Components, Supporting, or Summary
        is horizontally aligned to its right on the same tab strip.
        """
        data = self._ocr_data(image)
        items_boxes = []
        neighbor_boxes = []
        for i, text in enumerate(data["text"]):
            clean = re.sub(r"[^a-z]+", "", str(text).lower())
            box = (
                data["left"][i], data["top"][i],
                data["left"][i] + data["width"][i],
                data["top"][i] + data["height"][i],
            )
            if clean == "items":
                items_boxes.append(box)
            elif clean in {"components", "supporting", "summary"}:
                neighbor_boxes.append(box)

        for items in items_boxes:
            items_mid_y = (items[1] + items[3]) // 2
            if items_mid_y > image.height // 3:
                continue
            for neighbor in neighbor_boxes:
                neighbor_mid_y = (neighbor[1] + neighbor[3]) // 2
                if items[0] < neighbor[0] and abs(items_mid_y - neighbor_mid_y) <= 12:
                    return items
        return None

    def _locate_components_tab(self, image) -> tuple[int, int, int, int] | None:
        """Locate Components only within the verified Estimate Items tab strip."""
        data = self._ocr_data(image)
        components_boxes = []
        neighbor_boxes = []
        for i, text in enumerate(data["text"]):
            clean = re.sub(r"[^a-z]+", "", str(text).lower())
            box = (
                data["left"][i], data["top"][i],
                data["left"][i] + data["width"][i],
                data["top"][i] + data["height"][i],
            )
            if clean == "components":
                components_boxes.append(box)
            elif clean in {"items", "supporting", "summary"}:
                neighbor_boxes.append(box)
        for components in components_boxes:
            mid_y = (components[1] + components[3]) // 2
            if mid_y > image.height // 3:
                continue
            if any(abs(mid_y - (n[1] + n[3]) // 2) <= 12 for n in neighbor_boxes):
                return components
        return None

    @staticmethod
    def _tab_is_active(image, tab: tuple[int, int, int, int]) -> bool:
        """Confirm a freshly located tab from its live cyan underline.

        The tab labels themselves are always visible, active or not. Xactimate's
        selected tab has a two-pixel cyan underline immediately below the label;
        requiring a substantial run of those pixels proves pane state without a
        fixed coordinate or a pane label that can also occur on another tab.
        """
        l, _t, r, b = tab
        rgb = image.convert("RGB")
        cyan_pixels = 0
        for y in range(b, min(image.height, b + 12)):
            for x in range(max(0, l - 8), min(image.width, r + 8)):
                red, green, blue = rgb.getpixel((x, y))
                if red < 80 and green > 110 and blue > 140 and green - red > 60 and blue - red > 80:
                    cyan_pixels += 1
        return cyan_pixels >= max(12, int((r - l) * 0.6))

    def _items_search_pane_field(self, image) -> tuple[int, int, int, int] | None:
        """Return Search only when the freshly located Items tab is active."""
        items = self._locate_items_tab(image)
        if items is None or not self._tab_is_active(image, items):
            return None
        return self._locate_search_field(image)

    def _items_grid_context_is_verified(
        self, image, *, allow_search_controls_offscreen: bool = False,
    ) -> bool:
        """Verify the active Items grid, including its scrolled valid shape.

        Normally the Search controls are part of the pane proof.  A bounded
        fresh-target viewport canonicalization deliberately scrolls the whole
        right content pane, however, so those controls can be off-screen while
        the Items tab and grid remain valid.  Only that caller may use the
        narrower proof: the freshly located Items tab must still be active.
        The caller independently requires a live grid anchor, unchanged anchor
        geometry, and an exact post-write row recount.
        """
        if self._items_search_pane_field(image) is not None:
            return True
        if not allow_search_controls_offscreen:
            return False
        items = self._locate_items_tab(image)
        return items is not None and self._tab_is_active(image, items)

    def _components_pane_is_verified(self, image) -> bool:
        """Require a freshly located Components tab with its active underline."""
        components = self._locate_components_tab(image)
        return components is not None and self._tab_is_active(image, components)

    def _search_focus_is_verified(self, hwnd: int, field: tuple[int, int, int, int]) -> bool:
        """Require the OS caret to be inside the live-located Search field.

        The WPF surface has one native HWND, so ``hwndFocus`` is expected
        to be the main window.  ``rcCaret`` is converted from the caret
        window's client coordinates to the main client before testing it.
        Absence of caret evidence is uncertainty and therefore failure.
        """
        ctypes, wintypes = self._win32()
        user32 = ctypes.windll.user32

        class GUITHREADINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("flags", wintypes.DWORD),
                ("hwndActive", wintypes.HWND),
                ("hwndFocus", wintypes.HWND),
                ("hwndCapture", wintypes.HWND),
                ("hwndMenuOwner", wintypes.HWND),
                ("hwndMoveSize", wintypes.HWND),
                ("hwndCaret", wintypes.HWND),
                ("rcCaret", wintypes.RECT),
            ]

        if user32.GetForegroundWindow() != hwnd:
            return False
        thread_id = user32.GetWindowThreadProcessId(hwnd, None)
        info = GUITHREADINFO(cbSize=ctypes.sizeof(GUITHREADINFO))
        if not thread_id or not user32.GetGUIThreadInfo(thread_id, ctypes.byref(info)):
            return False
        if info.hwndFocus != hwnd or not info.hwndCaret:
            return False
        caret_window = info.hwndCaret
        point = wintypes.POINT(info.rcCaret.left, info.rcCaret.top)
        if not user32.ClientToScreen(caret_window, ctypes.byref(point)):
            return False
        if not user32.ScreenToClient(hwnd, ctypes.byref(point)):
            return False
        left, top, right, bottom = field
        return left <= point.x <= right and top <= point.y <= bottom

    def _wait_for_search_field(
        self, hwnd: int, *, attempts: int = 4,
    ) -> tuple[int, int, int, int] | None:
        """Boundedly recapture until the live Search field is rendered.

        This is feedback-driven readiness polling, not a blind delay: each
        attempt captures a new frame and immediately stops on positive
        OCR/geometry evidence.  OCR itself supplies the render interval,
        so no unconditional sleep or fixed-coordinate fallback is used.
        """
        for _attempt in range(attempts):
            image = self._capture_client_image(hwnd)
            field = self._items_search_pane_field(image)
            if field is not None:
                return field
        return None

    def _anchor_offset(self, image) -> tuple[int, int] | None:
        """Locates the grid header's own "Cat" column label live and
        returns the (dx, dy) shift from its calibrated position in
        _ANCHORS -- applied to every other fixed anchor to correct for
        scroll drift. Returns None if the label can't be found (caller
        should fall back / stop).

        Originally anchored on the Quick Entry panel's "Cat:" label
        instead. Live testing found that label is intermittently
        undetectable by OCR *at all* (not a wrong match -- genuinely
        absent from the word list) when its value box is empty, which
        happens whenever nothing has been explicitly clicked into Quick
        Entry sync yet -- exactly the state `read_populated_fields()`
        is usually called in. The grid header's "Cat" column label, by
        contrast, was present in every sample checked (it's part of the
        grid itself, which must exist for there to be anything to
        anchor for). Preferring the *bottommost* "Cat" match is what
        selects the grid header over Quick Entry's when both are
        present. See docs/xactimate-lookup.md Phase 4.4."""
        found = self._locate_label(image, "Cat", prefer="bottommost")
        if found is None:
            return None
        calibrated = _ANCHORS["grid_header_cat_label"]
        dx = found[0] - calibrated[0]
        dy = found[1] - calibrated[1]
        return dx, dy

    def _capture_and_locate(self, hwnd: int, attempts: int = 6, delay_s: float = 0.6):
        """Captures a fresh screenshot and computes the anchor offset,
        retrying with a short delay if the anchor can't be found. Live
        testing found the anchor OCR occasionally misses on a single
        attempt even though a screenshot taken moments later (same
        underlying state, re-captured) succeeds -- a transient
        rendering/capture-timing issue, not a fundamentally undetectable
        state. Retrying is a standard, justified mitigation for exactly
        this class of flakiness rather than failing on the first miss.
        Returns (image, offset) -- offset is None only if every attempt
        failed. See docs/xactimate-lookup.md Phase 4.4."""
        image = None
        offset = None
        for attempt in range(attempts):
            image = self._capture_client_image(hwnd)
            offset = self._anchor_offset(image)
            if offset is not None:
                return image, offset
            if attempt < attempts - 1:
                time.sleep(delay_s)
        return image, None

    def _shifted_anchor(self, name: str, offset: tuple[int, int]) -> tuple[int, int, int, int]:
        l, t, r, b = _ANCHORS[name]
        dx, dy = offset
        return (l + dx, t + dy, r + dx, b + dy)

    #: Minimum alphanumeric-character count (after stripping gridline/OCR
    #: noise punctuation) for an OCR'd token to count as real content
    #: rather than a stray artifact -- see `_is_meaningful_ocr_text()`.
    _MEANINGFUL_TEXT_MIN_LEN = {
        "category": 2, "selector": 2, "description": 3, "unit": 1,
    }

    @staticmethod
    def _is_meaningful_ocr_text(value: str | None, min_len: int) -> bool:
        """True when `value`, after stripping everything but letters/
        digits, still has at least `min_len` characters left. Filters
        out exactly the kind of single-character gridline-bleed noise
        (a stray "<", "|", ".") that a real row never produces but a
        genuinely blank cell does -- the same class of artifact
        `_count_grid_rows()`'s old '#'-column reader had to work around
        (see its historical docstring, kept below), applied here as a
        generic per-field content check instead of column-specific
        digit parsing."""
        if not value:
            return False
        cleaned = re.sub(r"[^A-Za-z0-9]", "", value)
        return len(cleaned) >= min_len

    def _row_has_content(self, image, offset: tuple[int, int], row_top: int) -> bool:
        """True when the row at `row_top` shows plausible physical-item
        content by ANY one of three independent signals -- category+
        selector, description, or quantity+unit together -- so a single
        noisy/misread field (e.g. a garbled selector) never hides a row
        whose description or quantity still reads fine. Category+
        selector is checked first since it is the cheapest (no extra
        crop needed) and correct for the overwhelming majority of real
        rows, so most rows never pay for the extra description/quantity
        reads at all."""
        category, selector = self._read_category_selector_at(image, offset, row_top)
        if self._is_meaningful_ocr_text(category, self._MEANINGFUL_TEXT_MIN_LEN["category"]) and \
                self._is_meaningful_ocr_text(selector, self._MEANINGFUL_TEXT_MIN_LEN["selector"]):
            return True
        description = self._read_description_at(image, offset, row_top)
        if self._is_meaningful_ocr_text(description, self._MEANINGFUL_TEXT_MIN_LEN["description"]):
            return True
        quantity = self._read_quantity_at(image, offset, row_top)
        if quantity is not None:
            raw_unit, _normalized_unit = self._read_unit_at(image, offset, row_top)
            if self._is_meaningful_ocr_text(raw_unit, self._MEANINGFUL_TEXT_MIN_LEN["unit"]):
                return True
        return False

    #: Generous ceiling on how many row positions `_count_grid_rows()`
    #: will ever probe -- large enough for every group size observed to
    #: date (the historical '#'-column reader's own largest calibrated
    #: crop covered 24 rows; see the combos this replaced), while still
    #: bounding worst-case OCR cost to a fixed, small number of reads
    #: rather than scanning indefinitely.
    _MAX_ROW_SCAN = 40

    def _count_grid_rows(self, image, offset: tuple[int, int]) -> int:
        """Counts populated grid rows from the rows' OWN content --
        category/selector, description, and quantity/unit, all already
        independently read (and relied upon) elsewhere in this file --
        rather than OCR'ing the '#' (row-number) column.

        Replaced (see docs/xactimate-lookup.md "row-count primitive
        replaced"): a live audit found the '#' column crop -- computed
        the same way every other grid column is, by shifting its
        calibrated `_GRID_COLUMNS["number"]` x-range by the anchor
        offset derived from the grid header's "Cat" label -- landing on
        blank space/gridline in a live session where every OTHER column
        (Cat, Sel, Description, Quantity), shifted by that exact same
        offset, still read correctly. The '#' column's real on-screen
        position relative to the "Cat" anchor had drifted independently
        of the rest of the grid; the old implementation's only failure
        path for "can't read the row-number column" was to return 0,
        which cascaded into `_last_row_geometry()` returning None and
        every caller (enter_quantity's row targeting, commit
        verification, the pre/post-activation row-count baseline the
        R&R multiset-delta safety logic depends on, group-verification
        probe cleanup, ...) silently treating a populated grid as
        empty.

        Walks row positions from `grid_row_1` downward in fixed
        `_GRID_ROW_HEIGHT` steps -- geometry already proven correct
        independently of the '#' column, since every other column reads
        correctly at these exact same positions -- and asks
        `_row_has_content()` whether each one looks populated. Stops at
        the first STABLE run of two consecutive empty positions after
        at least one populated row (one blank position alone is treated
        as a plausible single-field misread on an otherwise-real row,
        not the end of the grid -- the scan continues, and a later
        populated position still extends the count through it). Two
        empty positions in a row -- or two at the very top -- is treated
        as the genuine end of content: this both matches the one always-
        present blank "new entry" row Xactimate leaves under the last
        real item, and fails closed (returns 0, exactly like "grid not
        located" did before) rather than guessing when nothing readable
        exists. A row position that doesn't fully fit within the
        captured image (`_row_is_visible()`) is never read or counted,
        so a clipped partial row at the bottom of the viewport is
        correctly excluded rather than counted from a partial read."""
        row_1_top = self._shifted_anchor("grid_row_1", offset)[1]
        last_populated_index = -1
        consecutive_empty = 0
        for index in range(self._MAX_ROW_SCAN):
            row_top = row_1_top + index * _GRID_ROW_HEIGHT
            if not self._row_is_visible(image, row_top):
                break
            if self._row_has_content(image, offset, row_top):
                last_populated_index = index
                consecutive_empty = 0
            else:
                consecutive_empty += 1
                if consecutive_empty >= 2:
                    break
        return last_populated_index + 1

    def _activation_rows_from_same_image(
        self, image, offset: tuple[int, int],
    ) -> tuple[list[ActivationRowSnapshot], int]:
        """Read activation rows while determining their extent once.

        This is the same bounded row walk and the same three content
        signals as :meth:`_count_grid_rows`, but retains the activation
        fields already read from this image instead of OCR-reading every
        populated row a second time. The returned integer is the first-row
        top used by callers' existing trailing-row probes.
        """
        row_1_top = self._shifted_anchor("grid_row_1", offset)[1]
        scanned: list[ActivationRowSnapshot] = []
        last_populated_index = -1
        consecutive_empty = 0
        for index in range(self._MAX_ROW_SCAN):
            row_top = row_1_top + index * _GRID_ROW_HEIGHT
            if not self._row_is_visible(image, row_top):
                break
            row = self._read_activation_row_at(image, offset, row_top)
            scanned.append(row)
            has_content = (
                self._is_meaningful_ocr_text(row.category, self._MEANINGFUL_TEXT_MIN_LEN["category"])
                and self._is_meaningful_ocr_text(row.selector, self._MEANINGFUL_TEXT_MIN_LEN["selector"])
            ) or self._is_meaningful_ocr_text(
                row.description, self._MEANINGFUL_TEXT_MIN_LEN["description"],
            )
            if not has_content:
                quantity = self._read_quantity_at(image, offset, row_top)
                if quantity is not None:
                    raw_unit, _normalized_unit = self._read_unit_at(image, offset, row_top)
                    has_content = self._is_meaningful_ocr_text(
                        raw_unit, self._MEANINGFUL_TEXT_MIN_LEN["unit"],
                    )
            if has_content:
                last_populated_index = index
                consecutive_empty = 0
            else:
                consecutive_empty += 1
                if consecutive_empty >= 2:
                    break
        return scanned[:last_populated_index + 1], row_1_top

    def _commit_rows_from_same_image(
        self, image, offset: tuple[int, int],
    ) -> list[CommitRowSnapshot]:
        """Read rich commit rows while determining their extent once."""
        row_1_top = self._shifted_anchor("grid_row_1", offset)[1]
        scanned: list[CommitRowSnapshot] = []
        last_populated_index = -1
        consecutive_empty = 0
        for index in range(self._MAX_ROW_SCAN):
            row_top = row_1_top + index * _GRID_ROW_HEIGHT
            if not self._row_is_visible(image, row_top):
                break
            activation = self._read_activation_row_at(image, offset, row_top)
            raw_unit, unit = self._read_unit_at(image, offset, row_top)
            quantity = self._read_quantity_at(image, offset, row_top)
            scanned.append(CommitRowSnapshot(
                category=activation.category,
                selector=activation.selector,
                description=activation.description,
                activity=activation.activity,
                quantity=quantity,
                unit=unit,
            ))
            has_content = (
                self._is_meaningful_ocr_text(
                    activation.category, self._MEANINGFUL_TEXT_MIN_LEN["category"],
                )
                and self._is_meaningful_ocr_text(
                    activation.selector, self._MEANINGFUL_TEXT_MIN_LEN["selector"],
                )
            ) or self._is_meaningful_ocr_text(
                activation.description, self._MEANINGFUL_TEXT_MIN_LEN["description"],
            ) or (
                quantity is not None
                and self._is_meaningful_ocr_text(raw_unit, self._MEANINGFUL_TEXT_MIN_LEN["unit"])
            )
            if has_content:
                last_populated_index = index
                consecutive_empty = 0
            else:
                consecutive_empty += 1
                if consecutive_empty >= 2:
                    break
        return scanned[:last_populated_index + 1]

    # ------------------------------------------------------------------
    # XactimateAdapter contract
    # ------------------------------------------------------------------

    def verify_application(self) -> bool:
        try:
            found = self._find_main_window()
        except Exception:
            return False
        if found is None:
            return False
        self._main_hwnd = found[0]
        return True

    def verify_project(self) -> bool:
        try:
            found = self._find_main_window()
        except Exception:
            return False
        if found is None:
            return False
        hwnd, title = found
        self._main_hwnd = hwnd
        return title.strip().lower() == self.expected_project_name.strip().lower()

    def _ensure_main_window(self) -> int:
        if self._main_hwnd is None:
            found = self._find_main_window()
            if found is None:
                raise AdapterError("Xactimate main window not found.")
            self._main_hwnd = found[0]
        return self._main_hwnd

    def _reset_scroll_state(self) -> None:
        """Click the live-located Items tab and verify its Search pane.

        Clicking the always-visible 'Items' tab (outside the
        scrollable content pane) re-selects the current tab and, per
        live testing, returns the content pane to its top scroll
        position -- the state _ANCHORS was calibrated against.

        Live-caught (Phase 4.7): the tab bar (Items/Components/
        Supporting Events/Labor Minimums/Labor Summary) is FIXED
        window chrome, not part of the scrollable grid content pane --
        confirmed live by comparing a screenshot's actual tab
        position (consistently at the raw, uncorrected `_ANCHORS[
        "items_tab"]` coordinates) against `_anchor_offset()`'s
        measurement (a real dy=-62px this session, but that offset
        describes the GRID's drift, not the tab bar's). Applying the
        grid's offset to this click overshoots and can land on a
        different tab entirely (reproduced live: landed on "Labor
        Minimums" instead of "Items", silently breaking every
        subsequent search until manually corrected). Left
        uncorrected, matching this method's original, live-verified
        behavior across every prior phase. See
        docs/xactimate-lookup.md Phase 4.7."""
        hwnd = self._ensure_main_window()
        before = self._capture_client_image(hwnd)
        # If the active Items pane already exposes a positively located
        # Search field, it is already in the only state this reset exists
        # to establish.  Do not churn the layout with a redundant tab
        # click.
        if self._items_search_pane_field(before) is not None:
            return
        # The active-pane decision must not hinge on one OCR frame. A
        # transient miss here previously converted an already-healthy
        # Items/Search pane into a needless Items click; that click can
        # itself repaint/destabilize the pane. Reuse the same bounded,
        # evidence-driven readiness poll used after a necessary click.
        # There is no sleep, cached rectangle, or coordinate fallback.
        if self._wait_for_search_field(hwnd) is not None:
            return
        # The bounded poll captured newer frames. Locate the click target
        # again from a fresh image rather than using the stale first one.
        before = self._capture_client_image(hwnd)
        items = self._locate_items_tab(before)
        if items is None:
            raise ItemsTabVerificationError(
                "Could not positively locate the Items tab with its neighboring tab context; refusing to click."
            )
        l, t, r, b = items
        self._click_client(hwnd, (l + r) // 2, (t + b) // 2)
        if self._wait_for_search_field(hwnd) is not None:
            return
        # Live-caught (this diagnosis): the OUTER Items tab can already
        # be active the whole time -- the click above is then a no-op --
        # while an INNER Quick Entry sub-tab (Search/Macros) has drifted
        # off 'Search', which is what actually hides the Search field.
        # Exactly ONE additional bounded, evidence-verified recovery: a
        # fresh capture, a locate that fails closed on anything but a
        # single unambiguous match, one click, one re-poll. No blind
        # coordinate click, no loop, no Escape, no scroll, no dialog
        # dismissal -- if this doesn't positively confirm Search, fail
        # closed exactly as before (same exception, same message, same
        # task-local handling downstream).
        recovery_image = self._capture_client_image(hwnd)
        search_subtab = self._locate_search_subtab(recovery_image)
        if search_subtab is not None:
            sl, st, sr, sb = search_subtab
            self._click_client(hwnd, (sl + sr) // 2, (st + sb) // 2)
            if self._wait_for_search_field(hwnd) is not None:
                return
        raise ItemsTabVerificationError(
            "Items tab click did not produce a positively located Search field within the bounded readiness poll; "
            "refusing to continue."
        )

    def _restore_canonicalized_items_pane_top(self) -> None:
        """Restore the top of a deliberately canonicalized Items pane.

        A fresh quantity write may scroll the whole right-side content pane
        downward so every established physical row is strictly countable.
        At the successful post-write boundary, the active Items tab, live grid
        anchor, dialog/dropdown absence, and exact row-count equality have
        already been independently proven.  Re-clicking that already-active
        outer tab is not a deterministic inverse of the wheel scroll: live it
        can be a no-op, leaving the top-only Search controls off-screen.

        Use the same live-anchored wheel mechanism that performed the bounded
        downward canonicalization, in the opposite direction.  Active Items
        and a live grid anchor authorize each upward step, but are never the
        success condition: this returns only after the normal, strict
        Items+Search readiness predicate is positive.  Any navigation change,
        dialog/dropdown, lost anchor, or exhausted bounded budget fails closed.
        This method is intentionally called only for a freshly bound target
        whose viewport was canonicalized; legacy/resume paths retain
        ``_reset_scroll_state()`` unchanged.
        """
        hwnd = self._ensure_main_window()
        for _attempt in range(self._SCROLL_INTO_VIEW_MAX_ATTEMPTS):
            image, offset = self._capture_and_locate(hwnd, attempts=1, delay_s=0)
            if (
                offset is None
                or not self._items_grid_context_is_verified(
                    image, allow_search_controls_offscreen=True,
                )
            ):
                raise ItemsTabVerificationError(
                    "Canonicalized Items-pane reset lost the active Items grid; refusing to scroll or continue."
                )
            if self._find_dropdown_window() is not None or self._unexpected_dialog_present():
                raise ItemsTabVerificationError(
                    "Canonicalized Items-pane reset found a dialog or dropdown; refusing to scroll or continue."
                )
            if self._items_search_pane_field(image) is not None:
                return
            self._scroll_grid_body(hwnd, notches=-2)
            time.sleep(0.5)

        final_image, final_offset = self._capture_and_locate(hwnd, attempts=1, delay_s=0)
        if (
            final_offset is not None
            and self._items_grid_context_is_verified(
                final_image, allow_search_controls_offscreen=True,
            )
            and self._find_dropdown_window() is None
            and not self._unexpected_dialog_present()
            and self._items_search_pane_field(final_image) is not None
        ):
            return
        raise ItemsTabVerificationError(
            "Canonicalized Items pane did not return to a positively verified Items/Search top state "
            "within the bounded upward-scroll budget; refusing to continue."
        )

    def focus_search(self) -> None:
        """Live-caught (Phase 4.7): unlike the tab bar, the search box
        IS part of the scrollable grid content pane and DOES share its
        drift -- confirmed live (a real dy=-62px session where the
        uncorrected click missed the search box entirely, breaking
        every search with a misleading "no results popup" error; the
        drift-corrected click landed on it correctly). Captured fresh
        AFTER `_reset_scroll_state()`'s click, since that click can
        itself change the drift state. See docs/xactimate-lookup.md
        Phase 4.7."""
        hwnd = self._ensure_main_window()
        if not self._force_foreground(hwnd):
            raise AdapterError("Could not bring Xactimate window to the foreground.")
        self._reset_scroll_state()
        image = self._capture_client_image(hwnd)
        field = self._items_search_pane_field(image)
        if field is None:
            raise SearchFocusError(
                "Could not positively locate the current Search field; refusing to click or type."
            )
        l, t, r, b = field
        self._click_client(hwnd, (l + r) // 2, (t + b) // 2)
        time.sleep(0.2)
        current = self._capture_client_image(hwnd)
        current_field = self._items_search_pane_field(current)
        if current_field is None or not self._search_focus_is_verified(hwnd, current_field):
            raise SearchFocusError(
                "Search field keyboard focus could not be positively verified after the click; refusing to clear or type."
            )

    def clear_search(self) -> None:
        self._select_all_and_delete()
        self._current_query = None
        self._last_dropdown_hwnd = None
        self._last_dropdown_rows = []

    def search_by_description(self, phrase: str) -> None:
        self._current_query = phrase
        self._current_query_submitted_at = time.monotonic()
        self._type_keybdevent(phrase)

    def search_by_category_selector(self, category: str, selector: str) -> None:
        query = f"{category}{selector}"
        self._current_query = query
        self._current_query_submitted_at = time.monotonic()
        self._type_keybdevent(query)

    #: Phase 5.8: bounded stabilization -- after the popup window handle
    #: first appears, poll its row content at this interval, requiring
    #: two consecutive reads with the SAME non-zero row count before
    #: trusting it, rather than clicking/reading on the very first
    #: sighting (live-caught: a popup that visibly displayed real
    #: candidates could still be mid-render or about to close at the
    #: instant its window handle first becomes enumerable). Bounded by
    #: _DROPDOWN_STABILIZE_TIMEOUT_S, never an unbounded wait -- live-
    #: measured popup content settles within ~0.3-0.7s of search
    #: submission and then stays stable (no flicker observed across 8
    #: rapid back-to-back live searches), so this adds one extra ~0.08s
    #: poll in the common case.
    _DROPDOWN_STABILIZE_POLL_S = 0.08
    _DROPDOWN_STABILIZE_TIMEOUT_S = 1.5
    #: How many times to re-submit the SAME search text (never advance
    #: to a different description-first attempt -- that decision stays
    #: entirely in execution_runner.py, untouched) if the popup never
    #: yields any candidate rows at all, or disappears before any were
    #: read.
    _DROPDOWN_SEARCH_RETRY_ATTEMPTS = 2

    def capture_dropdown(self):
        """Waits for the separate top-level popup window to appear,
        then stabilizes and reads its rows. Never trusts a cached popup
        handle from a previous search.

        Phase 5.8 (live-caught): a single immediate read on first
        sighting the popup's window handle risked reading it mid-
        render or right as it closed, and a resulting empty read was
        indistinguishable downstream from a genuine zero-result search
        -- "VISIBLE CANDIDATES != NO_MATCH" was being violated. Fixed
        with a bounded stabilization poll (_capture_dropdown_once()):
        requires two consecutive non-zero reads with the same row
        count before trusting the result, but retains the FIRST non-
        empty read even if the popup closes before a second confirming
        read -- once candidate text is successfully read, it is never
        discarded merely because the popup later disappeared. If a
        whole stabilization cycle produces zero rows without the popup
        ever closing, that IS positive evidence of a genuine zero-
        result search (STOP_REASON_NO_RESULTS downstream). If the
        popup never appears at all, or disappears before any rows were
        ever read, the SAME search text (not a different phrase -- see
        _description_first_search_attempts()'s own, unchanged, attempt
        sequence) is re-submitted up to _DROPDOWN_SEARCH_RETRY_ATTEMPTS
        times before giving up with PopupCaptureFailedError. Populates
        self.last_dropdown_diagnostics with the full timing trace
        regardless of outcome."""
        diag = DropdownCaptureDiagnostics(search_submitted_at=self._current_query_submitted_at)
        last_outcome = None
        for attempt in range(1 + self._DROPDOWN_SEARCH_RETRY_ATTEMPTS):
            rows, outcome, first_seen_at, closed_at, stable_at = self._capture_dropdown_once()
            last_outcome = outcome
            if diag.popup_first_seen_at is None and first_seen_at is not None:
                diag.popup_first_seen_at = first_seen_at

            if outcome == "CANDIDATES_PARSED":
                diag.popup_stable_at = stable_at
                diag.popup_row_count = len(rows)
                diag.popup_closed_at = closed_at  # set only when a good read was retained despite the popup closing right after
                diag.candidate_parse_completed_at = time.monotonic()
                diag.search_retries = attempt
                diag.outcome = outcome
                self.last_dropdown_diagnostics = diag
                # Phase 5.8 (self-caught in review): use the hwnd the
                # successful read actually came from (carried on each
                # row already), never a fresh _find_dropdown_window()
                # call here -- re-querying at this point could itself
                # return None if the popup closed in the instant after
                # a good read, which would silently defeat the whole
                # "retain the snapshot even if the popup later closes"
                # guarantee by making select_candidate() refuse anyway.
                self._last_dropdown_hwnd = rows[0].popup_hwnd
                self._last_dropdown_rows = rows
                return rows

            if outcome == "NO_RESULTS":
                diag.popup_row_count = 0
                diag.candidate_parse_completed_at = time.monotonic()
                diag.search_retries = attempt
                diag.outcome = outcome
                self.last_dropdown_diagnostics = diag
                self._last_dropdown_hwnd = None
                self._last_dropdown_rows = []
                return []

            # POPUP_CAPTURE_FAILED (never found) or POPUP_DISAPPEARED
            # (found, but closed before ANY row was ever read) -- retry
            # the SAME search text, bounded, before giving up.
            diag.popup_closed_at = closed_at
            if attempt < self._DROPDOWN_SEARCH_RETRY_ATTEMPTS and self._current_query:
                query = self._current_query
                self.focus_search()
                self.clear_search()
                self._type_keybdevent(query)
                self._current_query = query
                self._current_query_submitted_at = time.monotonic()

        diag.outcome = last_outcome
        diag.search_retries = self._DROPDOWN_SEARCH_RETRY_ATTEMPTS
        self.last_dropdown_diagnostics = diag
        self._last_dropdown_hwnd = None
        self._last_dropdown_rows = []
        raise PopupCaptureFailedError(
            f"Results popup {(last_outcome or 'capture_failed').lower()} for query {self._current_query!r} "
            f"after {1 + self._DROPDOWN_SEARCH_RETRY_ATTEMPTS} attempt(s) -- no candidate rows were ever read."
        )

    def _capture_dropdown_once(self):
        """One wait-for-popup + bounded-stabilize-and-read cycle.
        Returns (rows, outcome, popup_first_seen_at, popup_closed_at,
        popup_stable_at). outcome is one of "CANDIDATES_PARSED",
        "NO_RESULTS", "POPUP_CAPTURE_FAILED", "POPUP_DISAPPEARED" --
        see capture_dropdown()'s own docstring for what each means and
        how the caller uses it. Never raises."""
        deadline = time.monotonic() + self.dropdown_timeout_s
        dropdown_hwnd = None
        popup_first_seen_at = None
        while time.monotonic() < deadline:
            dropdown_hwnd = self._find_dropdown_window()
            if dropdown_hwnd is not None:
                popup_first_seen_at = time.monotonic()
                break
            time.sleep(0.1)

        if dropdown_hwnd is None:
            return [], "POPUP_CAPTURE_FAILED", None, None, None

        stabilize_deadline = time.monotonic() + self._DROPDOWN_STABILIZE_TIMEOUT_S
        last_rows: list[_RawDropdownRow] = []
        popup_closed_at = None
        while time.monotonic() < stabilize_deadline:
            hwnd_now = self._find_dropdown_window()
            if hwnd_now is None:
                popup_closed_at = time.monotonic()
                break
            rows = self._read_dropdown_rows(hwnd_now)
            if rows and last_rows and len(rows) == len(last_rows):
                return rows, "CANDIDATES_PARSED", popup_first_seen_at, None, time.monotonic()
            last_rows = rows
            time.sleep(self._DROPDOWN_STABILIZE_POLL_S)

        if last_rows:
            # Real candidate text WAS read at least once -- retained
            # even though a second confirming read never happened
            # (stabilization window ran out, or the popup closed right
            # after this read). See capture_dropdown()'s docstring:
            # "once candidate text is successfully read, retain that
            # candidate snapshot even if the popup later closes."
            return last_rows, "CANDIDATES_PARSED", popup_first_seen_at, popup_closed_at, time.monotonic()

        if popup_closed_at is not None:
            return [], "POPUP_DISAPPEARED", popup_first_seen_at, popup_closed_at, None

        # Popup stayed open and enumerable for the ENTIRE stabilization
        # window, every single read confirmed zero rows -- positive
        # evidence of a genuine zero-result search.
        return [], "NO_RESULTS", popup_first_seen_at, None, None

    def _read_dropdown_rows(self, dropdown_hwnd: int) -> list[_RawDropdownRow]:
        uia, UIA = self._uia()
        element = uia.ElementFromHandle(dropdown_hwnd)
        walker = uia.RawViewWalker
        scrollviewer = walker.GetFirstChildElement(element)
        if scrollviewer is None:
            return []

        rows: list[_RawDropdownRow] = []
        item = walker.GetFirstChildElement(scrollviewer)
        idx = 0
        while item:
            try:
                if item.CurrentControlType == UIA.UIA_ListItemControlTypeId:
                    texts: list[str] = []
                    child = walker.GetFirstChildElement(item)
                    while child:
                        try:
                            texts.append(child.CurrentName)
                        except Exception:
                            pass
                        try:
                            child = walker.GetNextSiblingElement(child)
                        except Exception:
                            break
                    if len(texts) >= 3:
                        rect = item.CurrentBoundingRectangle
                        rows.append(
                            _RawDropdownRow(
                                code_text=texts[0],
                                description_text=texts[1],
                                price_text=texts[2],
                                row_position=idx,
                                popup_hwnd=dropdown_hwnd,
                                rect_at_capture=(rect.left, rect.top, rect.right, rect.bottom),
                            )
                        )
                        idx += 1
            except Exception:
                pass
            try:
                item = walker.GetNextSiblingElement(item)
            except Exception:
                break
        return rows

    def parse_dropdown(self, raw) -> list[DropdownResult]:
        results = []
        for row in raw:
            category, selector = _split_category_selector(row.code_text)
            results.append(
                DropdownResult(
                    raw_text=f"{row.code_text} {row.description_text}",
                    row_position=row.row_position,
                    category=category,
                    selector=selector,
                    description=row.description_text,
                    item_number=None,
                    extraction_confidence=1.0,  # exact UI Automation text, not OCR
                    price_text=row.price_text,  # Phase 5.6: previously discarded
                )
            )
        return results

    def select_candidate(self, candidate: DropdownResult) -> None:
        """Never reuses the rect captured at parse time. Re-locates the
        popup, re-reads its rows fresh, verifies the candidate's text
        still matches, re-reads THAT row's live bounding rectangle, and
        only then clicks its center."""
        if self._last_dropdown_hwnd is None:
            raise PopupNotFoundError("select_candidate() called with no prior capture_dropdown().")

        win32gui = self._win32gui()
        try:
            still_open = win32gui.IsWindow(self._last_dropdown_hwnd)
        except Exception:
            still_open = False
        if not still_open:
            raise PopupNotFoundError("Results popup closed before selection could occur.")

        fresh_rows = self._read_dropdown_rows(self._last_dropdown_hwnd)
        match = None
        for row in fresh_rows:
            category, selector = _split_category_selector(row.code_text)
            if category == candidate.category and selector == candidate.selector:
                match = row
                break

        if match is None:
            raise StaleCandidateError(
                f"Candidate {candidate.category}/{candidate.selector} is no longer present in the "
                f"live popup -- refusing to click a stale/guessed position."
            )

        # Preserve the existing fail-closed main-window availability check,
        # but do not OCR the Items grid here. The authoritative activation
        # baseline was already captured before this method, and the causal
        # post-click delta is established by pending_item_created() or
        # bind_rr_pair_after_activation(). The former row-count value stored
        # here had no production consumer.
        self._ensure_main_window()

        # re-read the row's rectangle live, immediately before clicking --
        # never the rect captured during parse_dropdown().
        uia, UIA = self._uia()
        element = uia.ElementFromHandle(match.popup_hwnd)
        walker = uia.RawViewWalker
        scrollviewer = walker.GetFirstChildElement(element)
        target_elem = None
        item = walker.GetFirstChildElement(scrollviewer) if scrollviewer is not None else None
        while item:
            child = walker.GetFirstChildElement(item)
            code_text = child.CurrentName if child else None
            if code_text == match.code_text:
                target_elem = item
                break
            try:
                item = walker.GetNextSiblingElement(item)
            except Exception:
                break

        if target_elem is None:
            raise StaleCandidateError("Candidate row vanished between text verification and rectangle read.")

        rect = target_elem.CurrentBoundingRectangle
        cx = (rect.left + rect.right) // 2
        cy = (rect.top + rect.bottom) // 2
        #: Phase 5.12 Stage 3: recorded BEFORE the click -- row_text is
        #: the row's own live UIA text (the same text select_candidate()
        #: just verified matches candidate.category/selector above), not
        #: OCR, so this is authoritative proof of which row was about to
        #: be clicked.
        self.last_candidate_click = {
            "expected_category": candidate.category, "expected_selector": candidate.selector,
            "row_position": match.row_position,
            "row_text_before_click": match.code_text,
            "row_bounds": (rect.left, rect.top, rect.right, rect.bottom),
            "click_xy": (cx, cy),
        }
        # Monotonic dirty-state evidence: recover() may clear
        # _last_selected, but it cannot erase the fact that a click
        # was attempted during this task's lookup attempt. Record it
        # before the OS interaction so even a click API failure is
        # conservatively treated as dirty.
        self._candidate_selection_count += 1
        self._click_screen(cx, cy)
        if self.last_dropdown_diagnostics is not None:
            self.last_dropdown_diagnostics.selection_clicked_at = time.monotonic()
        time.sleep(1.0)

        # Live-discovered: re-selecting a CAT/SEL that already exists in the
        # active group pops a real "Duplicate Item(s)" modal ("SFG GUTA
        # already exists in UTILITY_ROO2, Continue?", Yes/No) instead of
        # silently adding/updating anything. Per the adapter contract this
        # is a hard stop by default -- never guessed through -- UNLESS
        # _is_intentional_duplicate() (Phase 5.18) can positively prove,
        # from THIS session's own ProtectedRowLedger, that the existing
        # row was committed by a DIFFERENT task than the one running now
        # (a genuinely separate source line legitimately mapping to the
        # same CAT/SEL in the same group -- live-confirmed real for
        # Xactimate's own paired add/remove steep-roof-surcharge
        # convention, both entries sharing RFG/STEEP). Still fails closed
        # for every uncertain case: no execution context, nothing
        # protected yet with this CAT/SEL, or -- the actual dangerous
        # case this guards against -- the CURRENT task's own task_id
        # already appears among the protected rows (a retry of an
        # already-committed task, which must never silently double-add).
        # recover() is what presses Escape after a genuine hard stop.
        if self._unexpected_dialog_present():
            if self._is_intentional_duplicate(candidate):
                self._handle_duplicate_item_dialog()
            else:
                raise UnexpectedDialogError(
                    f"Unexpected dialog appeared after selecting {candidate.category}/{candidate.selector} "
                    f"(observed live: Xactimate's own 'Duplicate Item(s)' confirmation when the candidate "
                    f"already exists in the active group)."
                )

        self._last_selected = candidate

    def _unexpected_dialog_present(self) -> bool:
        win32gui = self._win32gui()

        def cb(hwnd, acc):
            try:
                cls = win32gui.GetClassName(hwnd)
                title = win32gui.GetWindowText(hwnd)
                visible = win32gui.IsWindowVisible(hwnd)
            except Exception:
                return True
            if visible and _APP_CLASS_MARKER in cls and title not in ("", "Loading", self.expected_project_name):
                acc.append(hwnd)
            return True

        found: list[int] = []
        win32gui.EnumWindows(cb, found)
        return bool(found)

    def _last_row_geometry(self, image, offset: tuple[int, int]) -> tuple[int, int] | None:
        """Returns (row_count, last_row_top_y) or None if the grid is
        empty or couldn't be located. Anchored on grid_row_1, NOT the
        header's bottom edge -- live testing found a real ~9px gap
        between the header and the first data row that a header-bottom-
        relative calculation silently ignored, misaligning every crop
        by that amount (a real bug caught by comparing OCR output
        against the actual screenshot, not assumed away)."""
        row_count = self._count_grid_rows(image, offset)
        if row_count == 0:
            return None
        row_1 = self._shifted_anchor("grid_row_1", offset)
        last_row_top = row_1[1] + (row_count - 1) * _GRID_ROW_HEIGHT
        return row_count, last_row_top

    def read_populated_fields(self) -> PopulatedFields:
        """Reads the most-recently-added grid row's own cells directly
        -- NOT the "Quick Entry" panel. Live testing found Quick Entry
        only re-syncs to a row when that row is explicitly clicked
        (e.g. by enter_quantity()'s own click on the Quantity cell); it
        stays blank immediately after select_candidate(). The grid row,
        by contrast, is already fully legible with no extra click and
        therefore no extra risk -- this is a deliberate deviation from
        the preference order in the original build spec (accessible
        controls > keyboard nav > OCR crops > committed-row as a
        *secondary* source), justified by what was actually observed
        live: the grid row is the lower-risk primary source here, not
        a fallback. Neither the grid nor Quick Entry has UI Automation
        peers (Phase 4.1 finding, unchanged), so this is still OCR,
        self-locating via the same 'Cat:'-anchored offset used
        everywhere else in this adapter to stay robust to scroll drift."""
        #: OCR on a small cell crop is not perfectly deterministic --
        #: live testing caught real per-read noise (a stray cursor/border
        #: artifact producing "SFG |" instead of "SFG", "G" misread as
        #: "S" producing "SUTA" instead of "GUTA", on an otherwise-correct
        #: selection). Reads three independent fresh captures and takes
        #: the per-field majority vote (see docs/xactimate-lookup.md
        #: Phase 4.3) rather than trusting a single OCR pass -- this is
        #: the "multiple-read agreement" strategy the original build spec
        #: names explicitly. Any field where all three reads disagree is
        #: returned as-is from the first read; the orchestrator's own
        #: populated_fields_mismatch check is what actually catches a
        #: genuinely wrong selection, so this is a reliability
        #: improvement, not a substitute for that safety check.
        raw = [self._read_populated_fields_once() for _ in range(3)]
        reads = [r for r, _scrolled in raw]
        # Phase 6.3: reset scroll state if ANY of the 3 reads needed to
        # scroll the pending row into view -- same "never leak a scroll
        # into a later, unrelated operation" discipline as
        # enter_quantity() (Phase 6.2). Only the first read ever
        # actually scrolls in practice (the row stays visible for the
        # other two once scrolled), but this checks all three rather
        # than assuming that.
        if any(scrolled for _r, scrolled in raw):
            self._reset_scroll_state()
        #: Phase 5.12 Stage 2: the 3 raw reads BEFORE majority voting --
        #: see self.last_populated_fields_reads' own docstring in
        #: __init__. Distinguishes "all 3 reads agree on a genuinely
        #: different value" (a real OCR/alignment problem) from "reads
        #: disagree with each other" (ordinary per-read noise the vote
        #: already absorbs).
        self.last_populated_fields_reads = [
            {"category": r.category, "selector": r.selector, "description": r.description,
             "unit": r.unit, "action": r.action}
            for r in reads
        ]

        def majority(values):
            counts: dict[str | None, int] = {}
            for v in values:
                counts[v] = counts.get(v, 0) + 1
            best = max(counts.items(), key=lambda kv: kv[1])
            return best[0]

        return PopulatedFields(
            category=majority([r.category for r in reads]),
            selector=majority([r.selector for r in reads]),
            description=majority([r.description for r in reads]),
            unit=majority([r.unit for r in reads]),
            action=majority([r.action for r in reads]),
            item_number=None,  # not visible anywhere in the observed UI -- honestly None, never guessed
        )

    def _read_populated_fields_once(self) -> tuple[PopulatedFields, bool]:
        hwnd = self._ensure_main_window()
        image, offset = self._capture_and_locate(hwnd)
        if offset is None:
            raise AdapterError("Could not locate the grid ('Cat:' anchor) to read populated fields.")

        geom = self._last_row_geometry(image, offset)
        if geom is None:
            raise AdapterError("No grid row found to read populated fields from -- was select_candidate() called?")

        # Phase 6.3 (live-caught): see _ensure_last_row_visible()'s own
        # docstring -- this read is exposed to the EXACT SAME viewport-
        # blindness that Phase 6.2 fixed for enter_quantity(), and
        # live-reproduced as the true root cause of task_line_0019's
        # "populated_fields_mismatch" (a stably garbled/empty read, not
        # per-attempt OCR noise, because the crop was landing entirely
        # outside the captured client area).
        image, offset, geom, scrolled = self._ensure_last_row_visible(hwnd, image, offset, geom)
        _row_count, row_top = geom
        dx = offset[0]

        def crop_col(col_name):
            col_l, col_r = _GRID_COLUMNS[col_name]
            return image.crop((col_l + dx, row_top, col_r + dx, row_top + _GRID_ROW_HEIGHT))

        # category/selector text in a STATIC (non-highlighted) grid row is
        # small enough (~9px tall) that native-resolution OCR corrupts it
        # ("RFG"->"RFC", "ARMVN"->"ARMVI") -- not caught by Stage 1's
        # single-highlighted-row testing, where Xactimate renders this
        # text larger. Same 4x-upscale fix already applied to
        # activity/unit below. See docs/xactimate-lookup.md Phase 4.4
        # Stage 3.
        # psm=7 (single line) misreads "RFG" as "RFC" on this crop size --
        # psm=6 (single uniform block) reads it correctly at every scale
        # tested (1x-8x); see docs/xactimate-lookup.md Phase 4.4 Stage 3.
        #
        # Phase 5.12 (live-caught): _GRID_COLUMNS["category"]'s right
        # edge (539, 559) is calibrated tight enough that a real
        # 3-letter code can lose its last glyph entirely ("WDR" -> "WD",
        # a clean drop, tolerated by check_category_selector_match()'s
        # truncation logic) OR have it PARTIALLY cropped into noise
        # ("DOR" -> "DO!", live-reproduced -- not a clean truncation,
        # which the same tolerance correctly refuses to accept, since a
        # partial glyph misread as a different real character isn't
        # distinguishable from a genuine wrong-selector case by that
        # logic alone). Widened LOCALLY here (not in the shared
        # _GRID_COLUMNS dict, which _read_category_selector_at() also
        # uses for probe/commit identity verification -- Phase 5.9/
        # 5.10C's extensively-validated mechanism -- and must not be
        # touched by this fix) up to column boundary's own left edge,
        # using all the real gap available before the selector column
        # legitimately starts.
        cat_l, _cat_r = _GRID_COLUMNS["category"]
        sel_l, _sel_r = _GRID_COLUMNS["selector"]
        cat_crop = image.crop((cat_l + dx, row_top, sel_l + dx, row_top + _GRID_ROW_HEIGHT))
        cat_crop = cat_crop.resize((cat_crop.width * 4, cat_crop.height * 4))
        cat_raw = self._ocr_text(cat_crop, psm=6).strip()
        # A real category code is always pure letters (RFG, WDR, DOR,
        # HVC, ...) -- the wider crop above can now legitimately bleed
        # into the selector column's leading character for a short
        # selector, so only the leading alphabetic run is kept,
        # discarding anything from the first non-letter character on.
        cat_letters_match = re.match(r"[A-Za-z]+", cat_raw)
        cat = cat_letters_match.group(0) if cat_letters_match else cat_raw
        sel_crop = crop_col("selector")
        sel_crop = sel_crop.resize((sel_crop.width * 4, sel_crop.height * 4))
        # Live-caught (Phase 4.7): the selector crop can visually
        # include the neighboring activity symbol for a short code
        # (e.g. real "GUTA" OCR'd as "GUTA &") -- see
        # `_read_category_selector_at()`'s docstring for the full
        # explanation. Phase 5.12 (live-caught): that fix assumed the
        # stray token always TRAILS the real selector -- reproduced
        # live, a freshly-selected (not yet quantity-entered) row's
        # selector crop OCR'd as ". SCRN<" (a stray LEADING artifact),
        # which the original `split()[0]` picked as "." instead of the
        # real "SCRN<". A real selector code is always the LONGEST
        # whitespace-separated token (stray artifacts observed live are
        # always 1-2 characters: ".", "|", "&"; every real selector
        # seen is 3+), so picking the longest token handles both the
        # leading- and trailing-artifact cases without assuming which
        # side the noise lands on.
        sel_raw = self._ocr_text(sel_crop, psm=6).strip()
        sel = max(sel_raw.split(), key=len) if sel_raw else ""
        act_crop = crop_col("activity")
        act_crop = act_crop.resize((act_crop.width * 4, act_crop.height * 4))
        act = self._ocr_text(act_crop, psm=6)
        desc = self._normalize_inch_mark(self._ocr_text(crop_col("description"), psm=6))
        # Unit renders as an active combobox (with a dropdown-arrow glyph
        # immediately to its right, text shifted ~13px left) if the row
        # happens to be highlighted -- but the real orchestrator call
        # order is select_candidate() -> read_populated_fields() ->
        # enter_quantity(), so this method normally runs against the
        # STATIC (unhighlighted) row, not the highlighted one. The
        # column boundary below targets the static-state position
        # (confirmed via OCR word-position measurement); 4x upscaling
        # is kept regardless of state since small grid text is
        # unreliable at native resolution either way. See
        # docs/xactimate-lookup.md Phase 4.4 Stage 3.
        unit_crop = crop_col("unit")
        # 4x misread "SQ" as "$Q" (the "S" gets confused for a dollar
        # sign at that scale); 6x reads it correctly across every PSM
        # tested. See docs/xactimate-lookup.md Phase 4.4 Stage 3.
        unit_crop = unit_crop.resize((unit_crop.width * 6, unit_crop.height * 6))
        # Phase 5.12 (live-caught): this call never got the psm=6 fix
        # applied to category/description/selector/activity above --
        # left at _ocr_text()'s default psm=7, which live-reproduced a
        # clean, correctly-cropped "EA" (confirmed by saving and
        # visually inspecting the actual crop) reading back as garbage
        # ("_|FA |") purely from the wrong page-segmentation mode, not a
        # crop/alignment problem. psm=6 reads the identical crop
        # correctly.
        unit = self._ocr_text(unit_crop, psm=6)

        return PopulatedFields(
            category=cat or None,
            selector=sel or None,
            description=desc or None,
            unit=unit or None,
            action=act or None,
            item_number=None,
        ), scrolled

    def _scroll_grid_body(self, hwnd: int, notches: int = 2) -> None:
        """Phase 6.2 (live-caught): scrolls the estimate items pane via
        a simulated mouse wheel. Positive ``notches`` scroll downward
        to a newly-added last row; the narrowly scoped canonicalized-pane
        restoration uses negative ``notches`` to return toward the top.
        Live-confirmed this scrolls the WHOLE right-side content pane
        (search box, thumbnail gallery, and Quick Entry all shrink away
        first, before the grid itself gains visible rows), not a grid-
        only scroll region -- fine for every caller here, since nothing
        downstream of a quantity-entry/row-read depends on the search
        box or thumbnails staying visible, and every row-position
        calculation in this file is anchor-relative (the 'Cat:' header
        is re-located fresh from each new capture), so it transparently
        benefits once the header itself has scrolled higher on screen.
        The scroll target point is computed from the CURRENT grid
        anchor (never a fixed/cached screen position), so this remains
        correct across window sizes and after earlier scrolling."""
        image, offset = self._capture_and_locate(hwnd, attempts=1, delay_s=0)
        if offset is None:
            return
        row_1 = self._shifted_anchor("grid_row_1", offset)
        gx, gy = row_1[0] + 300, row_1[1] + 100
        ctypes, _ = self._win32()
        user32 = ctypes.windll.user32
        ox, oy = self._get_client_origin(hwnd)
        user32.SetCursorPos(ox + gx, oy + gy)
        time.sleep(0.05)
        MOUSEEVENTF_WHEEL = 0x0800
        WHEEL_DELTA = 120
        user32.mouse_event(MOUSEEVENTF_WHEEL, 0, 0, -WHEEL_DELTA * notches, 0)

    #: Phase 6.2: bounded scroll-into-view budget -- live-measured 3
    #: notches (~0.6s settle each) reliably brought a row 1 position
    #: beyond the original 15-row-tall viewport into view; this is a
    #: multiple of that observed requirement, not a guess, and remains
    #: a bounded, fail-closed retry (never an unbounded wait/scroll).
    _SCROLL_INTO_VIEW_MAX_ATTEMPTS = 8

    #: Phase 6.2 (live-caught): a row sitting exactly at the ragged
    #: bottom edge of the captured client area -- technically within
    #: `image.height`, per pure arithmetic -- is where _count_grid_rows()
    #: itself is least reliable: its own OCR of that row's '#' column
    #: can fail right at the edge, silently undercounting the TRUE row
    #: count by one and making a caller compute "last row" one row too
    #: high. Live-reproduced: a targeted click landed on the confirmed-
    #: correct row directly ABOVE the true newest (still-pending, still
    #: at quantity 0) row. Requiring a full extra row of margin below
    #: the target -- not just "the target's own row fits" -- gives the
    #: count itself room to be trustworthy before this file ever acts
    #: on it.
    _ROW_VISIBILITY_MARGIN_ROWS = 1

    def _row_is_visible(self, image, row_top: int) -> bool:
        margin = self._ROW_VISIBILITY_MARGIN_ROWS * _GRID_ROW_HEIGHT
        return row_top >= 0 and row_top + _GRID_ROW_HEIGHT + margin <= image.height

    def _ensure_last_row_visible(self, hwnd: int, image, offset, geom):
        """Phase 6.3 (live-caught): shared scroll-into-view logic,
        extracted from enter_quantity() (Phase 6.2) so
        _read_populated_fields_once() can use the identical mechanism.
        Both read/click the grid's "last row" via the same viewport-
        blind _last_row_geometry() arithmetic, and both are equally
        exposed to a computed position landing beyond the captured
        client area -- live-reproduced as the actual root cause of
        task_line_0019's `populated_fields_mismatch` ("owe"/"eee",
        later "None"/"None"): the 17th row's computed y (1056) exceeded
        the 1023px-tall captured client area by 33px, so every one of
        read_populated_fields()'s 3 majority-vote reads cropped from
        entirely outside the real image -- stably garbled/empty on
        every attempt, not per-read OCR noise (which is exactly why
        Phase 5.17's category-only OCR-noise tolerance never rescued
        it: that mechanism requires the SELECTOR to independently
        agree, and an off-screen crop corrupts selector too).

        Returns the (possibly refreshed after scrolling) (image,
        offset, geom, scrolled) -- `scrolled` lets the caller decide
        whether it is responsible for eventually resetting scroll
        state. Raises RowOffscreenError if the row never becomes
        visible within the bounded budget."""
        row_count, last_row_top = geom
        if self._row_is_visible(image, last_row_top):
            return image, offset, geom, False
        for _attempt in range(self._SCROLL_INTO_VIEW_MAX_ATTEMPTS):
            self._scroll_grid_body(hwnd, notches=2)
            time.sleep(0.5)
            image, offset = self._capture_and_locate(hwnd, attempts=1, delay_s=0)
            if offset is None:
                continue
            geom2 = self._last_row_geometry(image, offset)
            if geom2 is None:
                continue
            if self._row_is_visible(image, geom2[1]):
                return image, offset, geom2, True

        # Phase 6.3 (live-caught): scrolling alone has a real floor --
        # the content pane above the grid (search box, thumbnails) only
        # shrinks so far, live-confirmed by 8 scroll attempts producing
        # NO position change at all once that floor is hit. This is not
        # a bug in the scroll loop above; it is a genuine "this window
        # is too short to ever reveal this row, no matter how much we
        # scroll" limit. The general, resolution-independent fix is NOT
        # to assume a large window up front (a small-monitor deployment
        # would break) -- it is to actively grow the window, as a
        # bounded LAST RESORT, only once scrolling alone has proven
        # insufficient. Never shrinks it back down mid-task (a later
        # step in the same task benefits from the same extra room); a
        # caller that cares about restoring the original size can do so
        # once the whole task is known to be finished.
        try:
            win32gui = self._win32gui()
            import win32con
            placement = win32gui.GetWindowPlacement(hwnd)
            already_maximized = placement[1] == win32con.SW_SHOWMAXIMIZED
        except Exception:
            already_maximized = True  # can't tell -- don't loop forever retrying a resize that won't help
        if not already_maximized:
            try:
                win32gui.ShowWindow(hwnd, win32con.SW_MAXIMIZE)
            except Exception:
                pass
            time.sleep(1.0)
            for _attempt in range(self._SCROLL_INTO_VIEW_MAX_ATTEMPTS):
                image, offset = self._capture_and_locate(hwnd, attempts=1, delay_s=0)
                if offset is not None:
                    geom2 = self._last_row_geometry(image, offset)
                    if geom2 is not None and self._row_is_visible(image, geom2[1]):
                        return image, offset, geom2, True
                self._scroll_grid_body(hwnd, notches=2)
                time.sleep(0.5)

        raise RowOffscreenError(
            f"the pending row (computed y={last_row_top}) never became visible within the client area "
            f"(height={image.height}) after {self._SCROLL_INTO_VIEW_MAX_ATTEMPTS} scroll-into-view "
            f"attempts and maximizing the window -- refusing to read/click a coordinate outside the "
            f"captured viewport."
        )

    def _quantity_target_candidates(
        self, image, offset, target: PendingQuantityTarget, *, include_row_count: bool = False,
    ):
        """Read all currently visible rows matching the retained target.

        ``include_row_count`` is used only by a freshly activation-bound
        quantity write.  The same row walk that proves the target identity
        has already established the current row extent, so return that extent
        with the matches instead of making ``enter_quantity()`` OCR-count the
        same captured image again.  Existing/resumed callers keep the original
        list-only return shape and initial-quantity inspection unchanged.

        A fresh target's initial quantity is deliberately not OCR-read: its
        causal activation binding authorizes one deterministic source-quantity
        write regardless of Xactimate's default value.  Identity, activity,
        uniqueness, and row extent remain mandatory before a match is returned.
        """
        def result(matches, row_count):
            return (matches, row_count) if include_row_count else matches

        rows, row_1_top = self._activation_rows_from_same_image(image, offset)
        if not rows:
            # _last_row_geometry() previously returned None here before
            # either caller-specific trailing-row guard could run.
            return result([], 0)
        if target.allow_initial_quantity_overwrite:
            for _probe in range(2):
                row_top = row_1_top + len(rows) * _GRID_ROW_HEIGHT
                if row_top < 0 or row_top + _GRID_ROW_HEIGHT > image.height:
                    break
                trailing = self._read_activation_row_at(image, offset, row_top)
                if not all(str(value or "").strip() for value in (trailing.category, trailing.selector)):
                    break
                rows.append(trailing)
            target_index = self._unique_clean_ordinary_delta_index(
                list(target.activation_baseline_rows), rows,
            )
            if target_index is None:
                return result([], len(rows))
            row_top = row_1_top + target_index * _GRID_ROW_HEIGHT
            observed = None
            if not target.write_source_quantity_once:
                observed = self._read_quantity_at(image, offset, row_top)
                if observed is None:
                    observed = self._read_quantity_at(
                        image, offset, row_top, min_votes=1, enhance_contrast=True,
                    )
            return result([(target_index, row_top, observed)], len(rows))

        matches = []
        # Retain the activation snapshot's two-row undercount guard.
        candidate_rows = list(rows)
        for _probe in range(2):
            row_top = row_1_top + len(candidate_rows) * _GRID_ROW_HEIGHT
            if row_top < 0 or row_top + _GRID_ROW_HEIGHT > image.height:
                break
            trailing = self._read_activation_row_at(image, offset, row_top)
            if not all(self._activation_identity(trailing)[:2]):
                break
            candidate_rows.append(trailing)
        for index, row in enumerate(candidate_rows):
            row_top = row_1_top + index * _GRID_ROW_HEIGHT
            identity = self._activation_identity(row)
            if not self._quantity_identity_matches(identity, target.identity):
                continue
            # Only real R&R activity glyphs are stable identity evidence.
            # Ordinary single rows can yield arbitrary OCR from this
            # visually empty column (live: "&" before entry, "o" after
            # Tab); never make that noise load-bearing.
            if target.activity in ("-", "+") and self._activity_token(row) != target.activity:
                continue
            observed = None
            if not target.write_source_quantity_once:
                observed = self._read_quantity_at(image, offset, row_top)
                if observed is None:
                    observed = self._read_quantity_at(
                        image, offset, row_top, min_votes=1, enhance_contrast=True,
                    )
            matches.append((index, row_top, observed))
        return result(matches, len(candidate_rows))

    @staticmethod
    def _quantity_identity_matches(
        observed: tuple[str, str, str], expected: tuple[str, str, str],
    ) -> bool:
        """Exact logical identity with spacing-insensitive description.

        CAT and SEL remain exact. Description OCR can split or join one
        word across highlight/Tab repaint (live: ``up to``/``upto``), so
        compare the already alphanumeric-normalized streams without
        spaces. This is not fuzzy matching and cannot turn a different
        character sequence into a match.
        """
        if observed[0] != expected[0] or observed[1] != expected[1]:
            return False
        observed_description = observed[2]
        expected_description = expected[2]
        if not observed_description or not expected_description:
            return False
        if observed_description.replace(" ", "") == expected_description.replace(" ", ""):
            return True

        # Live-caught on SFG/GUTA after quantity Tab: the description
        # crop is deliberately wide enough for long item descriptions,
        # and the selected-row repaint exposed the first two letters of
        # the adjacent Coverage cell ("Dwelling") as a separate final
        # OCR token: ``... up to 5`` became ``... up to 5 dy``. Initial
        # targeting had already positively matched the retained row and
        # written 200; confirmation then rejected that same CAT/SEL row
        # and misleadingly exhausted the scroll budget. Tolerate only
        # one trailing token of at most two characters, on either read,
        # after the remaining normalized description is exact. This is
        # not fuzzy matching and cannot erase a real mid-description or
        # longer suffix difference.
        observed_tokens = observed_description.split()
        expected_tokens = expected_description.split()
        if len(observed_tokens) == len(expected_tokens) + 1 and len(observed_tokens[-1]) <= 2:
            return "".join(observed_tokens[:-1]) == "".join(expected_tokens)
        if len(expected_tokens) == len(observed_tokens) + 1 and len(expected_tokens[-1]) <= 2:
            return "".join(expected_tokens[:-1]) == "".join(observed_tokens)
        return False

    def _canonicalize_fresh_quantity_viewport(
        self, hwnd: int, image, offset, target: "PendingQuantityTarget",
        row_top: int, observed, scanned_row_count: int, scrolled: bool,
    ):
        """Live-caught (task_line_0015): before writing to a freshly,
        positively-bound target, make sure the FULL row extent
        `_quantity_target_candidates()` just established is safely
        countable under the SAME strict `_row_is_visible()` margin
        rule the post-write structural comparison (`_last_row_
        geometry()`/`_count_grid_rows()`) uses. Without this,
        `_quantity_target_candidates()`'s own trailing-row probe
        (a looser `row_top + _GRID_ROW_HEIGHT > image.height` bound,
        no margin) can retain a final row that the stricter post-Tab
        recount would not -- a genuine 14-row grid read as 14 rows
        before Tab and 13 after, purely from this rule mismatch, even
        though the actual target (a different row entirely) was
        correctly identified and written both times. The omitted row
        was an unrelated trailing row, never the target.

        If the known last row of the established extent already
        satisfies `_row_is_visible()`, this is a no-op -- the existing
        fast path for an ordinary small grid is untouched, no extra
        scroll/capture. Otherwise, uses the EXISTING bounded scroll/
        maximize machinery (`_ensure_last_row_visible()`) to bring
        that last row into a safely countable position, then re-
        captures and re-proves the target from scratch: unique
        identity match, full row extent. Raises
        QuantityNotConfirmedError if repositioning leaves the target
        unresolvable to exactly one match (changed identity, vanished,
        or now ambiguous/duplicate) -- fails closed exactly like every
        other uniqueness check in this file, never guesses. Propagates
        RowOffscreenError unchanged if safe geometry can never be
        established within the existing bounded scroll/maximize
        budget -- the existing offscreen/physical-state behavior,
        never weakened."""
        row_1_top = self._shifted_anchor("grid_row_1", offset)[1]
        last_row_top = row_1_top + (scanned_row_count - 1) * _GRID_ROW_HEIGHT
        if self._row_is_visible(image, last_row_top):
            return image, offset, row_top, observed, scanned_row_count, scrolled
        new_image, new_offset, _geom, _newly_scrolled = self._ensure_last_row_visible(
            hwnd, image, offset, (scanned_row_count, last_row_top),
        )
        matches2, scanned_row_count2 = self._quantity_target_candidates(
            new_image, new_offset, target, include_row_count=True,
        )
        if len(matches2) != 1:
            raise QuantityNotConfirmedError(
                "enter_quantity(): the pending target could not be uniquely re-proven after canonicalizing "
                f"the viewport for safe row counting ({len(matches2)} match(es))."
            )
        _index2, new_row_top, new_observed = matches2[0]
        return new_image, new_offset, new_row_top, new_observed, scanned_row_count2, True

    def _locate_pending_quantity_row(
        self,
        hwnd: int,
        *,
        confirmation_ordinal: int | None = None,
        expected_quantity: float | None = None,
    ):
        """Locate the task's retained logical row, never the bottom row.

        Before entry, an identical R&R duplicate is normally
        disambiguated by requiring exactly one matching ``+`` row to
        remain blank/zero. If the retained activity occurrence already
        holds the expected quantity, that occurrence is accepted
        without a write. After entry, the same identity/activity
        occurrence is re-read at freshly captured geometry. Every
        coordinate is therefore current and identity-verified before
        use.
        """
        target = self._pending_quantity_target
        if target is None:
            raise QuantityNotConfirmedError(
                "enter_quantity(): no positively identified pending-item delta is retained; refusing to target a row."
            )

        scrolled = False
        last_height = None
        for attempt in range(self._SCROLL_INTO_VIEW_MAX_ATTEMPTS + 1):
            image, offset = self._capture_and_locate(hwnd, attempts=1, delay_s=0)
            last_height = getattr(image, "height", None)
            if offset is not None:
                scan = self._quantity_target_candidates(
                    image, offset, target,
                    include_row_count=target.write_source_quantity_once,
                )
                if target.write_source_quantity_once:
                    matches, scanned_row_count = scan
                else:
                    matches = scan
                    scanned_row_count = None

                def located(row_top, ordinal, observed):
                    base = (image, offset, row_top, ordinal, observed, scrolled)
                    return base + ((scanned_row_count,) if target.write_source_quantity_once else ())

                if confirmation_ordinal is not None:
                    if len(matches) == 1:
                        _index, row_top, observed = matches[0]
                        return located(row_top, 1, observed)
                    if target.activity in ("-", "+") and 1 <= confirmation_ordinal <= len(matches):
                        _index, row_top, observed = matches[confirmation_ordinal - 1]
                        return located(row_top, confirmation_ordinal, observed)
                    if len(matches) > 1:
                        raise QuantityNotConfirmedError(
                            "enter_quantity(): multiple ordinary rows match the retained CAT/SEL/description "
                            "during confirmation; refusing to resolve them by stale row index or quantity."
                        )
                elif len(matches) == 1:
                    _index, row_top, observed = matches[0]
                    if target.write_source_quantity_once:
                        image, offset, row_top, observed, scanned_row_count, scrolled = (
                            self._canonicalize_fresh_quantity_viewport(
                                hwnd, image, offset, target, row_top, observed, scanned_row_count, scrolled,
                            )
                        )
                        return located(row_top, 1, observed)
                    if observed is None or abs(observed) <= 0.01:
                        return located(row_top, 1, observed)
                    if expected_quantity is not None and abs(observed - expected_quantity) <= 0.01:
                        return located(row_top, 1, observed)
                    if target.activity is None and target.allow_initial_quantity_overwrite:
                        return located(row_top, 1, observed)
                    raise QuantityNotConfirmedError(
                        "enter_quantity(): the only identity-matched pending target already has a different non-zero "
                        f"quantity ({observed:g}, expected {expected_quantity!r}); refusing to overwrite it."
                    )
                elif len(matches) > 1:
                    if (
                        target.write_source_quantity_once
                        and target.activity in ("-", "+")
                        and 1 <= target.activity_ordinal <= len(matches)
                    ):
                        chosen = matches[target.activity_ordinal - 1]
                        return located(chosen[1], target.activity_ordinal, chosen[2])
                    blank = [entry for entry in matches if entry[2] is None or abs(entry[2]) <= 0.01]
                    if len(blank) == 1:
                        chosen = blank[0]
                        ordinal = matches.index(chosen) + 1
                        return located(chosen[1], ordinal, chosen[2])
                    if target.activity in ("-", "+") and 1 <= target.activity_ordinal <= len(matches):
                        chosen = matches[target.activity_ordinal - 1]
                        observed = chosen[2]
                        if (
                            expected_quantity is not None
                            and observed is not None
                            and abs(observed - expected_quantity) <= 0.01
                        ):
                            return located(chosen[1], target.activity_ordinal, observed)
                        if observed is not None and abs(observed) > 0.01:
                            raise QuantityNotConfirmedError(
                                "enter_quantity(): the retained R&R activity occurrence already has a different "
                                f"non-zero quantity ({observed:g}, expected {expected_quantity!r}); refusing to "
                                "overwrite it."
                            )
                    raise QuantityNotConfirmedError(
                        "enter_quantity(): identical target rows could not be uniquely resolved to one blank pending row."
                    )
            if self._unexpected_dialog_present():
                raise QuantityNotConfirmedError(
                    "enter_quantity(): an unexpected dialog appeared while locating the pending row."
                )
            if attempt < self._SCROLL_INTO_VIEW_MAX_ATTEMPTS:
                self._scroll_grid_body(hwnd, notches=2)
                scrolled = True
                time.sleep(0.5)

        raise RowOffscreenError(
            "enter_quantity(): the identity-verified pending row never became locatable within the current grid "
            f"after {self._SCROLL_INTO_VIEW_MAX_ATTEMPTS} bounded scroll attempts "
            f"(last client height={last_height!r}); refusing to click a guessed row."
        )

    def locate_existing_rr_pair_half(
        self, *, category: str, selector: str, description: str, activity: str,
    ) -> tuple[PendingQuantityTarget, float | None] | None:
        """Phase 6.3 (R&R Stage 3): READ-ONLY re-identification of an
        ALREADY-EXISTING physical R&R half from a persisted
        CoordinatedPair binding -- e.g. after a process restart, so a
        future Stage 4 resume path can re-find the "-" or "+" row of a
        pair that was activated in an earlier session, WITHOUT ever
        re-clicking a candidate (which would create a duplicate
        physical pair). Never activates anything, never selects a
        candidate, never writes a quantity.

        Reuses _quantity_target_candidates() -- the exact same
        identity/activity matching a live write already uses -- so
        "re-find" and "write" agree on what counts as a match by
        construction, not by a second, parallel implementation.

        The returned target carries write_source_quantity_once=False
        (unlike a freshly bound activation target): a resumed target
        must go through enter_quantity()'s cautious legacy-target
        protections (skip only if already exactly matching; refuse to
        overwrite a different non-zero value) rather than being treated
        as fresh and force-written -- resume must never blindly rewrite
        a cell that may already hold a correct value from a prior
        session.

        Returns None -- never a guessed/best-effort match -- whenever
        the half cannot be uniquely, safely re-identified: zero matches
        (row missing) or more than one match (duplicate/ambiguous).
        Callers must treat None as structural uncertainty, never as a
        signal to fall back to re-activation."""
        if activity not in ("-", "+"):
            raise ValueError("locate_existing_rr_pair_half(): activity must be '-' or '+'.")
        hwnd = self._ensure_main_window()
        image, offset = self._capture_and_locate(hwnd)
        if offset is None:
            return None
        # _quantity_target_candidates() always compares against a row's
        # NORMALIZED identity (_activation_identity()); normalize here too
        # so this matches regardless of whether the caller passes raw OCR
        # text or an already-normalized identity tuple (e.g. one persisted
        # straight from PendingRRPairTarget.identity) -- normalization is
        # idempotent, so an already-normalized caller is unaffected.
        identity = (
            self._normalized_pair_text(category),
            self._normalized_pair_text(selector),
            self._normalized_pair_text(description),
        )
        probe_target = PendingQuantityTarget(
            identity=identity, activity=activity, after_index=0, activity_ordinal=1,
            physical_row_delta=2, write_source_quantity_once=False,
        )
        matches = self._quantity_target_candidates(image, offset, probe_target)
        if len(matches) != 1:
            return None
        _index, _row_top, observed = matches[0]
        return probe_target, observed

    def verify_existing_rr_pair_half_quantity(
        self, *, category: str, selector: str, description: str, activity: str, expected_quantity: float,
    ) -> QuantityEntryConfirmation | None:
        """Phase 6.3 (R&R Stage 3): READ-ONLY convenience wrapper over
        locate_existing_rr_pair_half() that compares the observed
        quantity against an expected value and returns a
        QuantityEntryConfirmation-shaped result -- the same shape
        write_and_verify_rr_pair_quantities() produces on a fresh
        write -- so a resuming caller can treat "already written in a
        prior session, re-verified now" and "just written this
        session" uniformly. Returns None (never a fabricated
        confirmation) when the half cannot be uniquely re-identified at
        all -- see locate_existing_rr_pair_half()'s own docstring."""
        located = self.locate_existing_rr_pair_half(
            category=category, selector=selector, description=description, activity=activity,
        )
        if located is None:
            return None
        _target, observed = located
        if observed is not None and abs(observed - expected_quantity) <= 0.01:
            return QuantityEntryConfirmation(
                expected=expected_quantity, observed=observed, confidence="CONFIRMED",
                review_required=False,
                reason="Read-only resume re-identification matched the expected quantity without any write.",
            )
        return QuantityEntryConfirmation(
            expected=expected_quantity, observed=observed, confidence="LOW_CONFIDENCE", review_required=True,
            reason=(
                f"Read-only resume re-identification observed {observed!r}, expected {expected_quantity!r}; "
                "no write was performed."
            ),
        )

    def locate_existing_rr_pair(
        self, *, category: str, selector: str, description: str,
    ) -> "PendingRRPairTarget | None":
        """Phase 5.23 (R&R Stage 4 integration): the one Stage 3
        primitive genuinely missing for a full-pair resume -- re-
        identifies BOTH halves of an already-existing, persisted R&R
        pair binding in one call, entirely read-only (never re-
        activates a candidate), and returns them as the SAME
        PendingRRPairTarget shape bind_rr_pair_after_activation()
        returns on a fresh activation -- so a resuming caller can feed
        the result straight into write_and_verify_rr_pair_quantities()
        without a caller outside this module ever needing to import or
        construct PendingRRPairTarget itself (this module stays the
        only place that type is named, matching this whole package's
        adapter-agnostic boundary -- see adapter.py's own module
        docstring).

        Built entirely from locate_existing_rr_pair_half(), called
        twice (once per activity) -- no new matching/OCR logic.
        Returns None -- never a partially-reconstructed pair -- if
        EITHER half cannot be uniquely re-identified."""
        minus = self.locate_existing_rr_pair_half(
            category=category, selector=selector, description=description, activity="-",
        )
        if minus is None:
            return None
        plus = self.locate_existing_rr_pair_half(
            category=category, selector=selector, description=description, activity="+",
        )
        if plus is None:
            return None
        minus_target, _minus_observed = minus
        plus_target, _plus_observed = plus
        return PendingRRPairTarget(minus_target=minus_target, plus_target=plus_target, identity=minus_target.identity)

    def write_and_verify_existing_rr_pair_half_quantity(
        self, *, category: str, selector: str, description: str, activity: str, quantity: float,
        on_physical_write: Callable[[], None] | None = None,
    ) -> QuantityEntryConfirmation:
        """Phase 5.23 (R&R Stage 4 integration): the WRITE counterpart
        to verify_existing_rr_pair_half_quantity() -- re-identifies an
        ALREADY-EXISTING physical R&R half from a persisted binding
        (read-only, via locate_existing_rr_pair_half(), never re-
        activating a candidate) and then writes+confirms `quantity` on
        it via the exact same enter_quantity() every other quantity
        write in this file uses. Needed for resuming a coordinated
        pair whose minus side already verified in a PRIOR session --
        only the plus side (or, symmetrically, only the minus side)
        needs a real write now, and the other, already-verified side
        must never be touched again; write_and_verify_rr_pair_
        quantities() itself always writes BOTH, so it cannot be reused
        for this one-sided resume shape.

        `on_physical_write` (Phase 5.26): forwarded verbatim to enter_
        quantity() -- see its own docstring. Lets a resuming caller
        checkpoint "this side's physical write is a settled fact"
        before the same slow/OCR-bound confirmation read that
        motivated this parameter there.

        Raises RRPairQuantityError(side=activity's own "minus"/"plus"
        label) -- reusing the SAME failure type write_and_verify_rr_
        pair_quantities() uses, so a caller that already knows how to
        handle that shape needs no second failure type -- if the half
        cannot be uniquely re-identified at all, or if the write/
        confirm itself fails."""
        side = "minus" if activity == "-" else "plus"
        located = self.locate_existing_rr_pair_half(
            category=category, selector=selector, description=description, activity=activity,
        )
        if located is None:
            raise RRPairQuantityError(
                f"R&R pair {activity!r}-side half could not be uniquely re-identified for a resumed write "
                f"(category={category!r}, selector={selector!r}, description={description!r}).",
                side=side,
            )
        target, _observed = located
        self._pending_quantity_target = target
        try:
            self.enter_quantity(quantity, on_physical_write=on_physical_write)
        except QuantityConfirmationError as exc:
            raise RRPairQuantityError(
                f"R&R pair {activity!r}-side resumed quantity entry failed: {exc}", side=side,
            ) from exc
        confirmation = self.last_quantity_confirmation
        if confirmation is None:
            raise RRPairQuantityError(
                f"R&R pair {activity!r}-side resumed quantity entry produced no confirmation record.", side=side,
            )
        return confirmation

    def enter_quantity(
        self, quantity: float, *, on_physical_write: Callable[[], None] | None = None,
    ) -> None:
        """`on_physical_write` (Phase 5.26): an optional callback invoked
        the moment this call has POSITIVE evidence the physical cell
        holds `quantity` -- either because the click+type+Tab sequence
        below just completed, or because the cell already held the
        exact value and no write was needed -- and BEFORE the
        (potentially slow, OCR-based) same-cell confirmation read that
        follows, when that read still happens at all (see Phase 5.29
        below). Live-caught: a real confirmation read hung for many
        minutes with no bound at the time (see _ocr_text()'s own fix);
        a caller that only checkpoints AFTER this whole call returns
        has no way to durably record "the value was physically entered"
        if that confirmation phase is what stalls or fails. Never
        called if the target row can't even be located/edited (a
        genuine "nothing happened yet" case) -- only once a write (or
        an already-correct value) is a settled fact. Never raises on
        the caller's behalf; any exception from the callback itself
        propagates uncaught, exactly like record_physical_item_
        created()'s own callback contract elsewhere in this file.

        Performance policy for a freshly, positively-bound target
        (`write_source_quantity_once=True` -- a candidate just activated
        this call, never a resumed/legacy/pre-existing row): numeric OCR is
        skipped both for the irrelevant initial/default quantity and for the
        final advisory read. Every structural drift check
        between the physical-write checkpoint and that read (dialog/
        dropdown appeared, grid anchor moved, row count changed, row
        geometry drifted) still runs unchanged; those are safety
        checks on the write action itself, not the numeric value, and
        stay in place regardless of this quantity-OCR policy. Nothing in
        production consumes either numeric OCR result for a fresh write
        (Phase 5.27 made confirmation advisory-only; Phase 5.28 proved
        binding itself needs no quantity evidence), so reading either value
        costs wall-clock time for a result nobody acts on.
        `last_quantity_confirmation` is still set, with
        `confidence="SKIPPED"` and `observed=None`, so callers/evidence
        never see a fabricated numeric read. Every OTHER path (a
        resumed/legacy target reaching the real write below, or the
        already-matching early-return above) is completely unaffected
        and still gets its full OCR confirmation, exactly as before --
        those are precisely the "trustworthy write provenance absent"
        cases this skip must never touch."""
        # Per-task state: never let an advisory from an earlier item leak
        # into the current item's post-commit outcome.
        self.last_quantity_confirmation = None
        hwnd = self._ensure_main_window()
        target = self._pending_quantity_target
        fresh_target = bool(target and target.write_source_quantity_once)
        located = self._locate_pending_quantity_row(hwnd, expected_quantity=quantity)
        if fresh_target:
            image, offset, row_top, ordinal, before_quantity, scrolled, row_count_before = located
        else:
            image, offset, row_top, ordinal, before_quantity, scrolled = located

        # Pre-existing/legacy retained targets may already contain the exact
        # requested quantity and can remain untouched. A freshly activated
        # logical item is different: its identity and quantity-bearing row
        # were already bound through the causal click delta, so write the
        # source quantity exactly once without making initial quantity OCR
        # load-bearing.
        #
        # Some Xactimate items instantiate with a real default quantity
        # (live: SFG/GSG appears as 1 EA). A fresh target's identity has
        # already been established by retained CAT/SEL + normalized description
        # (and activity occurrence for R&R), so that default is neither read nor
        # made load-bearing: the source value is written once. Every legacy/
        # pre-existing or ambiguous target retains the exact-match/no-write and
        # different-nonzero/fail-closed rules above.
        if (
            not (target and target.write_source_quantity_once)
            and before_quantity is not None
            and abs(before_quantity - quantity) <= 0.01
        ):
            self.last_quantity_confirmation = QuantityEntryConfirmation(
                expected=quantity, observed=before_quantity, confidence="CONFIRMED",
                review_required=False, reason="Exact expected quantity was already populated; no write performed.",
            )
            if on_physical_write is not None:
                on_physical_write()
            if scrolled:
                self._reset_scroll_state()
            return

        # Identity is load-bearing only BEFORE the write. Bind the exact
        # identity-verified row to its current grid index and quantity-cell
        # geometry now; confirmation below must read this same structural
        # row, never reconstruct identity from repaint-sensitive CAT/SEL/
        # description/activity OCR and never substitute the last row.
        if not fresh_target:
            geometry_before = self._last_row_geometry(image, offset)
            if geometry_before is None:
                raise QuantityNotConfirmedError(
                    "enter_quantity(): the identity-verified target disappeared before its quantity cell could be edited."
                )
            row_count_before, _last_row_top = geometry_before
        row_1_top_before = self._shifted_anchor("grid_row_1", offset)[1]
        row_delta = row_top - row_1_top_before
        if row_delta < 0 or row_delta % _GRID_ROW_HEIGHT != 0:
            raise QuantityNotConfirmedError(
                "enter_quantity(): the identity-verified target does not align to a stable grid row; refusing to edit."
            )
        row_index = row_delta // _GRID_ROW_HEIGHT
        if row_index >= row_count_before:
            raise QuantityNotConfirmedError(
                "enter_quantity(): the identity-verified target is outside the current grid row count; refusing to edit."
            )

        col_l, col_r = _GRID_COLUMNS["quantity"]
        qx = (col_l + col_r) // 2 + offset[0]
        qy = row_top + _GRID_ROW_HEIGHT // 2
        image_width = getattr(image, "width", None)
        image_height = getattr(image, "height", None)
        if (
            qx < 0 or qy < 0
            or (image_width is not None and qx >= image_width)
            or (image_height is not None and qy >= image_height)
        ):
            raise QuantityNotConfirmedError(
                "enter_quantity(): the identity-verified quantity cell is outside the captured grid viewport."
            )
        self._click_client(hwnd, qx, qy)
        time.sleep(0.2)
        self._select_all_and_delete()
        self._type_keybdevent(f"{quantity:g}")
        time.sleep(0.2)
        self._press_key(0x09)  # Tab commits the cell edit; never Enter.
        time.sleep(1.0)

        # Phase 5.26: the click+type+Tab sequence above has POSITIVELY
        # happened -- checkpoint that fact now, before the confirmation
        # read below (which can be slow/OCR-bound) even begins. See
        # this method's own `on_physical_write` docstring.
        if on_physical_write is not None:
            on_physical_write()

        # Confirm from the SAME row/cell context. CAT/SEL/description/
        # activity OCR is deliberately not repeated here: it already
        # established the binding above, while Tab repaint is proven noisy.
        # Structural drift is checked once before one ordinary same-cell
        # OCR read.  OCR disagreement alone cannot choose another row,
        # cause another write, or turn a physically created item into an
        # unconfirmed failure. This structural drift check (dialog/
        # anchor/row-count/geometry) runs for EVERY write, fresh or
        # resumed -- it is safety-relevant regardless of the quantity
        # policy. Only the NUMERIC OCR read a few lines below is
        # skipped for a fresh write; see Phase 5.29 there.
        after, after_offset = self._capture_and_locate(hwnd, attempts=1, delay_s=0)
        allow_search_controls_offscreen = bool(
            target is not None and target.write_source_quantity_once and scrolled
        )
        if after_offset is None or not self._items_grid_context_is_verified(
            after, allow_search_controls_offscreen=allow_search_controls_offscreen,
        ):
            raise QuantityNotConfirmedError(
                "enter_quantity(): focus/navigation left the expected Items/Search grid during quantity editing."
            )
        if self._find_dropdown_window() is not None or self._unexpected_dialog_present():
            raise QuantityNotConfirmedError(
                "enter_quantity(): a dialog or dropdown appeared during quantity editing."
            )
        if after_offset != offset:
            raise QuantityNotConfirmedError(
                "enter_quantity(): the grid anchor materially changed during quantity editing."
            )

        geometry_after = self._last_row_geometry(after, after_offset)
        if geometry_after is None:
            raise QuantityNotConfirmedError(
                "enter_quantity(): the edited target row disappeared during quantity editing."
            )
        row_count_after, _last_after_top = geometry_after
        if row_count_after != row_count_before:
            raise QuantityNotConfirmedError(
                "enter_quantity(): the grid row count changed unexpectedly during quantity editing "
                f"({row_count_before} -> {row_count_after})."
            )

        row_1_top_after = self._shifted_anchor("grid_row_1", after_offset)[1]
        edited_row_top = row_1_top_after + row_index * _GRID_ROW_HEIGHT
        if (
            row_index >= row_count_after
            or edited_row_top < 0
            or edited_row_top + _GRID_ROW_HEIGHT > after.height
        ):
            raise QuantityNotConfirmedError(
                "enter_quantity(): the edited target row disappeared from the expected grid geometry."
            )

        # Phase 5.29 (performance): a freshly, positively-bound target's
        # numeric value is already authoritative (the physical-write
        # checkpoint above fired on exactly that evidence) -- nothing
        # in production consumes this OCR result for such a target
        # (Phase 5.27 made it advisory-only; Phase 5.28 proved binding
        # itself needs no quantity evidence), so skip the read (a
        # 3-scale OCR vote in _read_quantity_at()) rather than pay its
        # wall-clock cost for a result nobody acts on. A resumed/
        # legacy target (write_source_quantity_once=False) -- exactly
        # the "trustworthy write provenance absent" case that must
        # keep this check -- still gets the full OCR confirmation below.
        if target is not None and target.write_source_quantity_once:
            self.last_quantity_confirmation = QuantityEntryConfirmation(
                expected=quantity, observed=None, confidence="SKIPPED",
                review_required=False,
                reason=(
                    "Positively-bound fresh write -- the deterministic typed value is authoritative; "
                    "post-write numeric OCR confirmation was intentionally skipped (advisory-only, not "
                    "required for success)."
                ),
            )
            if scrolled:
                self._restore_canonicalized_items_pane_top()
            return

        observed = self._read_quantity_at(after, after_offset, edited_row_top)
        if observed is not None and abs(observed - quantity) <= 0.01:
            self.last_quantity_confirmation = QuantityEntryConfirmation(
                expected=quantity, observed=observed, confidence="CONFIRMED",
                review_required=False, reason="Same-cell quantity read-back matched the expected value.",
            )
        else:
            self.last_quantity_confirmation = QuantityEntryConfirmation(
                expected=quantity, observed=observed, confidence="LOW_CONFIDENCE",
                review_required=True,
                reason=(
                    f"Stable same-cell post-write OCR read {observed!r} instead of {quantity:g}; "
                    "the entered expected quantity was preserved without rewriting."
                ),
            )

        if scrolled:
            self._reset_scroll_state()

    def write_and_verify_rr_pair_quantities(
        self, pair_target: "PendingRRPairTarget", minus_quantity: float, plus_quantity: float,
        *,
        on_minus_write: Callable[[], None] | None = None,
        on_minus_verified: Callable[["QuantityEntryConfirmation"], None] | None = None,
        on_plus_write: Callable[[], None] | None = None,
    ) -> "RRPairQuantityResult":
        """Phase 6.3 (R&R Stage 3): write and independently verify BOTH
        halves of one bound R&R pair -- the remove ("-") quantity and
        the replace ("+") quantity, which may differ.

        Deliberately reuses enter_quantity() unmodified, called twice,
        rather than any copy-pasted quantity-entry logic: binding
        `self._pending_quantity_target` to pair_target.minus_target
        (then .plus_target) and calling enter_quantity() is the exact
        same mechanism every ordinary and single-target R&R task
        already uses -- _locate_pending_quantity_row()/_quantity_
        target_candidates() already disambiguate a "-" occurrence from
        a "+" occurrence of the SAME identity generically, via
        `target.activity`, with no changes needed here.

        Both targets were built with write_source_quantity_once=True
        (see _pending_rr_pair_targets_from_delta()), so enter_quantity()
        never treats either side's initial/default on-screen quantity
        as a conflict gate -- each freshly created physical half
        receives its own source quantity authoritatively, exactly once,
        regardless of whether minus_quantity == plus_quantity: equal
        values still produce two independent writes and two independent
        same-cell read-back confirmations, never inferred from one.

        A minus-side failure (enter_quantity() raising) is re-raised as
        RRPairQuantityError(side="minus") -- the plus side is NEVER
        attempted, so no result can ever claim it succeeded. A plus-
        side failure is re-raised as RRPairQuantityError(side="plus",
        minus_confirmation=...), preserving the ALREADY-obtained minus
        confirmation so a caller can still persist that partial
        progress. A LOW_CONFIDENCE (OCR-uncertain but structurally
        stable) confirmation on either side is never an exception --
        exactly like the existing single-target enter_quantity()
        contract, it is returned as part of a normal
        RRPairQuantityResult, never silently upgraded to a guessed
        CONFIRMED. Phase 5.27: the caller no longer routes an
        otherwise-successful pair to review solely for this -- a
        positively-bound, deterministic write is its own authoritative
        evidence; LOW_CONFIDENCE remains available as advisory
        diagnostics (see QuantityEntryConfirmation's own docstring).

        `on_minus_verified` (Phase 5.23, R&R Stage 4 addition -- the
        one Stage 3 primitive integration revealed as genuinely
        missing): an optional callback invoked with the minus-side
        `QuantityEntryConfirmation` immediately after it is obtained,
        BEFORE the plus-side write is even attempted. write_and_verify_
        rr_pair_quantities() is one atomic call from Python's own point
        of view, so a caller that needs a real crash-safe checkpoint
        BETWEEN the two writes (e.g. a runner persisting "minus
        verified" before risking the plus write) has no other point to
        hook -- mirrors the existing adapter-agnostic set_physical_
        item_created_callback()/record_physical_item_created() pattern
        in spirit, but scoped to this one call instead of adapter-
        lifetime state. Never called again for the plus side (that
        evidence is simply the returned RRPairQuantityResult, or the
        RRPairQuantityError.minus_confirmation on a plus-side failure).
        A callback exception propagates uncaught -- exactly like
        record_physical_item_created()'s own callback contract, this
        is never swallowed, since a checkpoint that silently failed to
        persist would be worse than a loud failure.

        `on_minus_write`/`on_plus_write` (Phase 5.26): forwarded
        verbatim as `on_physical_write` to the respective enter_
        quantity() call -- see that method's own docstring for exactly
        when each fires (before that side's own confirmation read,
        never after). Live-caught: the plus-side physical write can
        complete and remain durably unrecorded if ONLY on_minus_
        verified/the eventual return value are used to checkpoint,
        since those both wait for confirmation to finish -- these two
        callbacks close that gap for BOTH sides symmetrically."""
        self._pending_quantity_target = pair_target.minus_target
        try:
            self.enter_quantity(minus_quantity, on_physical_write=on_minus_write)
        except QuantityConfirmationError as exc:
            raise RRPairQuantityError(
                f"R&R pair minus-side quantity entry failed: {exc}", side="minus",
            ) from exc
        minus_confirmation = self.last_quantity_confirmation
        if minus_confirmation is None:
            raise RRPairQuantityError(
                "R&R pair minus-side quantity entry produced no confirmation record.", side="minus",
            )
        if on_minus_verified is not None:
            on_minus_verified(minus_confirmation)

        self._pending_quantity_target = pair_target.plus_target
        try:
            self.enter_quantity(plus_quantity, on_physical_write=on_plus_write)
        except QuantityConfirmationError as exc:
            raise RRPairQuantityError(
                f"R&R pair plus-side quantity entry failed: {exc}", side="plus",
                minus_confirmation=minus_confirmation,
            ) from exc
        plus_confirmation = self.last_quantity_confirmation
        if plus_confirmation is None:
            raise RRPairQuantityError(
                "R&R pair plus-side quantity entry produced no confirmation record.", side="plus",
                minus_confirmation=minus_confirmation,
            )

        return RRPairQuantityResult(minus_confirmation=minus_confirmation, plus_confirmation=plus_confirmation)

    def read_quantity(self) -> float | None:
        """Not part of the abstract XactimateAdapter contract -- an
        adapter-specific helper used by the quantity validation trials
        and available to callers that want an independent read-back
        without going through read_populated_fields(). Reads the
        LAST row; see `_read_quantity_at()` for reading a specific,
        identity-verified row (Phase 4.7)."""
        hwnd = self._ensure_main_window()
        image, offset = self._capture_and_locate(hwnd)
        if offset is None:
            return None
        geom = self._last_row_geometry(image, offset)
        if geom is None:
            return None
        _row_count, last_row_top = geom
        return self._read_quantity_at(image, offset, last_row_top)

    def _read_quantity_at(
        self, image, offset: tuple[int, int], row_top: int, *, min_votes: int = 2, enhance_contrast: bool = False
    ) -> float | None:
        """Crops and reads the Quantity cell at an ARBITRARY row_top --
        extracted from `read_quantity()` (Phase 4.7) so the same
        live-verified reading strategy can be applied to a specific,
        identity-verified row (`verify_commit()`), not just "whatever
        the last row is".

        `min_votes`/`enhance_contrast` (Phase 6.2, defaults identical to
        every existing caller's behavior): every PRIOR caller reads an
        already-committed, no-longer-selected row (plain white
        background) -- exactly what the careful multi-scale calibration
        below was tuned against. enter_quantity()'s new pre-commit
        read-back (Stage 11) reads the row WHILE still selected/
        highlighted (light blue background, live-confirmed via a saved
        crop), which broke that calibration outright: a real, clearly-
        legible "5" got zero of three scales agreeing (native read "5"
        correctly but alone; 4x/6x both misread the identical crop),
        and a real "1" got EVERY scale misreading it as a bare "|" with
        no digit for the numeric regex to find at all -- not a close
        call, a totally different failure mode from the tuned-for
        white-background case. `enhance_contrast=True` grayscales then
        binarizes (threshold 160) the crop before OCR, which fixed the
        "1" case at every scale live -- but live-verified to actively
        REGRESS the untouched white-background case (a known-good
        "33.66" read back as pure noise at every scale once binarized),
        so it is opt-in, never the default. `min_votes=1` accepts a
        single confident read once contrast is enhanced -- proportionate
        given this check only needs to catch GROSS failures (a click
        that missed entirely, reading back None/0), not replace the
        stricter, independent, unweakened post-commit
        verify_quantity_committed() that still runs afterward."""
        col_l, col_r = _GRID_COLUMNS["quantity"]
        dx = offset[0]
        crop = image.crop((col_l + dx, row_top, col_r + dx, row_top + _GRID_ROW_HEIGHT))
        if enhance_contrast:
            crop = crop.convert("L").point(lambda p: 0 if p < 160 else 255)
        # Live-caught (Phase 4.3): at native resolution Tesseract can drop
        # a decimal point entirely (visually present but sub-pixel at this
        # crop size) -- "2.5" read back as "25" with no punctuation at
        # all. Upscaling is a standard mitigation for this failure mode.
        #
        # Live-caught (Phase 4.5): 4x upscale can blur a SHORT value (a
        # single digit, e.g. "7") into an empty OCR result, while native
        # resolution reads it correctly.
        #
        # Live-caught (Phase 4.6): NEITHER a single fixed scale nor a
        # native-vs-upscaled preference is safe -- every scale tried
        # confidently misreads SOME real value to a different, wrong,
        # non-empty digit (not just "empty" -- there is no reliable
        # signal to detect the failure from the output alone): native
        # misread a real "5" as "3"; 2x and 3x misread a real "1" as
        # "oo"/"ji". No single scale among {1x, 2x, 3x, 4x, 6x} was
        # correct on every one of "1", "5", "2.5", "7" tested live.
        # {1x, 4x, 6x} each independently got 3 of those 4 right (never
        # the same 3), so a majority vote across those three scales gets
        # all 4 right -- the same "multiple-read agreement" strategy
        # `read_populated_fields()` already uses across independent
        # reads, applied here across independent scales instead. A tie
        # (no value read by 2+ scales) returns None rather than
        # guessing. See docs/xactimate-lookup.md Phase 4.6.
        import re

        def numeric_at(scale):
            im = crop.resize((crop.width * scale, crop.height * scale))
            text = self._ocr_text(im).replace(",", "").strip()
            match = re.search(r"-?\d+(?:\.\d+)?", text)
            if match is None:
                return None
            try:
                return float(match.group())
            except ValueError:
                return None

        reads = [numeric_at(s) for s in (1, 4, 6)]
        votes: dict[float, int] = {}
        for v in reads:
            if v is not None:
                votes[v] = votes.get(v, 0) + 1
        if not votes:
            return None
        best_value, best_count = max(votes.items(), key=lambda kv: kv[1])
        return best_value if best_count >= min_votes else None

    def _read_unit_at(self, image, offset: tuple[int, int], row_top: int) -> tuple[str | None, str | None]:
        """Crops and reads the Unit cell at an ARBITRARY row_top,
        returning (raw_ocr_text, normalized_unit_or_None) -- Phase 4.7
        Stage 6. Tries multiple (scale, PSM) combinations (live
        investigation found the real unit position/rendering varies
        by row highlight state -- see the `unit` entry in
        `_GRID_COLUMNS` -- and that "LF" specifically misreads as "uF"
        at several scales), applies the narrow `_UNIT_OCR_CONFUSIONS`
        correction, and votes among results that resolve to a real,
        evidence-backed unit in `_KNOWN_XACTIMATE_UNITS`. Requires at
        least 2 agreeing votes before returning a normalized value --
        below that, returns (first_raw_read, None) rather than
        guessing (the caller/`check_unit_compatibility()` surfaces
        this as `unreadable`). The raw text is ALWAYS the literal
        first OCR attempt, never overwritten by normalization (Stage
        1's 'preserve raw OCR' requirement). See
        docs/xactimate-lookup.md Phase 4.7 Stage 6."""
        col_l, col_r = _GRID_COLUMNS["unit"]
        dx = offset[0]
        crop = image.crop((col_l + dx, row_top, col_r + dx, row_top + _GRID_ROW_HEIGHT))

        combos = [(1, 6), (2, 6), (2, 7), (4, 6), (6, 6)]
        raw_reads: list[str] = []
        votes: dict[str, int] = {}
        for scale, psm in combos:
            im = crop.resize((crop.width * scale, crop.height * scale))
            text = self._ocr_text(im, psm=psm).strip()
            raw_reads.append(text)
            vocab = _resolve_observed_unit_vocab(text)
            if vocab is not None:
                votes[vocab] = votes.get(vocab, 0) + 1

        raw = next((r for r in raw_reads if r), None)
        if not votes:
            return raw, None
        best_unit, best_count = max(votes.items(), key=lambda kv: kv[1])
        return raw, (best_unit if best_count >= 2 else None)

    #: Live-caught (Phase 5.7B): Xactimate's own cross-group duplicate-
    #: item reminder ("<CAT> <SEL> already exists in <GROUP>, Continue?")
    #: -- fires on Ctrl+S whenever the item being committed shares a
    #: CAT/SEL with one already present in ANY OTHER group of the same
    #: estimate. Reproduced live running the full aranda-insurance-v3
    #: plan: it fired after the first group's disposable SFG/GUTA
    #: verification probe, and every SUBSEQUENT group's commit (probe
    #: AND real PDF items) silently failed for the rest of the run --
    #: `_unexpected_dialog_present()` correctly detected the blocked
    #: state, but nothing ever recognized or dismissed THIS specific
    #: dialog, so every later verify_group()/ensure_group() call safely
    #: refused against a window that could no longer be interacted with
    #: at all.
    _DUPLICATE_ITEM_DIALOG_TITLE = "Duplicate Item(s)"

    def _handle_duplicate_item_dialog(self) -> bool:
        """Not part of the abstract contract (Phase 5.7B). If
        Xactimate's "Duplicate Item(s)" confirmation is currently open,
        clicks "Yes" and returns True; returns False (no-op) if the
        dialog isn't present. "Yes" -- not "No"/Escape -- is the
        correct answer here: the SAME catalog item genuinely and
        correctly appearing in more than one group is normal in a real
        estimate (confirmed live: e.g. SFG/GUTA legitimately selected
        for both a gutter row in one group and a downspout row in
        another), and the disposable group-verification probe
        intentionally reuses one fixed item across every group by
        design. This dialog is Xactimate's own generic cross-group
        hygiene reminder, not a signal that the current selection is
        wrong -- answering "No" would silently discard an otherwise
        correct, already-decided commit. The dialog exposes no UI
        Automation button elements (confirmed live, same as the rest of
        this app's custom-drawn chrome), so "Yes" is located the same
        way every other custom-drawn control in this file is: a fresh
        OCR read of its own client area, never a hardcoded screen
        position."""
        hwnd = self._find_window_by_title(self._DUPLICATE_ITEM_DIALOG_TITLE)
        if hwnd is None:
            return False
        image = self._capture_client_image(hwnd)
        yes_pos = self._locate_label(image, "Yes", prefer="topmost")
        if yes_pos is None:
            return False
        cx = (yes_pos[0] + yes_pos[2]) // 2
        cy = (yes_pos[1] + yes_pos[3]) // 2
        self._click_client(hwnd, cx, cy)
        time.sleep(0.5)
        return True

    def _is_intentional_duplicate(self, candidate: DropdownResult) -> bool:
        """Phase 5.18 (live-caught): a REAL task's own Duplicate Item(s)
        dialog (select_candidate()'s hard-stop above) is legitimate to
        confirm -- not merely tolerable, like the disposable probe's --
        ONLY when this session's own ProtectedRowLedger proves the
        existing row was committed by a DIFFERENT task than the one
        running now (e.g. Xactimate's own paired add/remove steep-roof-
        surcharge convention, where two separate source lines both
        legitimately map to RFG/STEEP in the same group). Fails closed
        -- returns False -- for every case that isn't positively proven:
        no execution context set, nothing protected yet in this group
        with this exact CAT/SEL (a genuinely unexpected duplicate has no
        such record to point to), or -- the actual danger this guards
        against -- the CURRENT task's own task_id already appears among
        the protected rows for this CAT/SEL (a retry of an already-
        committed task, which must never silently double-add a row)."""
        ctx = self._execution_context
        if ctx is None or not ctx.task_id:
            return False
        existing = self._protected_row_ledger.records_for_group(ctx.group)
        matching = [r for r in existing if r.category == candidate.category and r.selector == candidate.selector]
        if not matching:
            return False
        if any(r.task_id == ctx.task_id for r in matching):
            return False
        return True

    def allows_intentional_duplicate(self, candidate: DropdownResult) -> bool:
        """Public duck-typed gate used by the orchestrator's narrow
        description-to-CAT/SEL instantiation fallback.  It deliberately
        reuses the exact same distinct-task/same-task protections as the
        Duplicate Item(s) dialog handler."""
        return self._is_intentional_duplicate(candidate)

    def pending_item_created(self, before_snapshot, timeout_s: float = 8.0) -> bool:
        """Positively detect one newly-created logical item.

        Xactimate may instantiate one logical R&R item as either one
        physical row or a remove/add pair, and may regroup multiple
        identical pairs by activity (``-,-,+,+``). The rich baseline is
        therefore reconciled as a logical multiset, not a positional
        prefix. Arbitrary two-row growth still fails closed.
        """
        expected = len(before_snapshot)
        self._pending_quantity_target = None
        baseline_rows = self._last_activation_baseline_rows
        if baseline_rows is None:
            baseline_rows = [] if expected == 0 else None
        if baseline_rows is None or len(baseline_rows) != expected:
            raise PhysicalStateUncertainError(
                "Pending-item detection has no matching rich activation baseline; refusing to infer a row delta."
            )
        baseline_identities = [(row.category, row.selector) for row in baseline_rows]
        if baseline_identities != list(before_snapshot):
            raise PhysicalStateUncertainError(
                "Pending-item detection's rich baseline does not match the supplied physical-row baseline."
            )
        start = time.time()
        while True:
            rows = self._snapshot_activation_rows()
            physical_delta = len(rows) - expected
            if physical_delta == 1:
                legacy_target = self._pending_quantity_target_from_delta(baseline_rows, rows)
                if (
                    legacy_target is not None
                    and self._is_one_safe_single_row_delta(baseline_rows, rows)
                    and (self._last_selected is None or legacy_target.activity == "+")
                ):
                    # Preserve the separately-proven one-row R&R form and
                    # compatibility for non-production fakes.
                    self._pending_quantity_target = legacy_target
                    return True
                target = self._ordinary_single_row_target_from_selected_candidate(baseline_rows, rows)
                if target is not None and target.allow_initial_quantity_overwrite:
                    self._pending_quantity_target = target
                    return True
                # Task-local, not project-level: exactly one new row landed
                # from this task's own activation, but it doesn't match a
                # recognized single-row/R&R-+ shape -- reconciliation
                # ambiguity confined to this one commit, not evidence the
                # grid/group/app is unsafe for later tasks. See
                # TaskLocalRowReconciliationError's docstring.
                raise TaskLocalRowReconciliationError(
                    "Candidate activation added one physical row, but the logical multiset delta was not "
                    "one safe ordinary row or quantity-bearing R&R + row."
                )
            if physical_delta == 2:
                target = self._pending_quantity_target_from_delta(baseline_rows, rows)
                if target is not None and self._is_one_logical_rr_multiset_delta(baseline_rows, rows):
                    self._pending_quantity_target = target
                    return True
                # Task-local, not project-level: this task's own activation
                # added two rows but they don't reconcile as one corroborated
                # R&R -/+ pair -- ambiguity about which new row is THIS
                # task's item, not evidence anything outside this task is
                # unsafe. See TaskLocalRowReconciliationError's docstring.
                raise TaskLocalRowReconciliationError(
                    "Candidate activation added two physical rows, but the logical multiset delta was not "
                    f"exactly one R&R -/+ item (before={expected}, after={len(rows)})."
                )
            if physical_delta > 2:
                raise PhysicalStateUncertainError(
                    "Candidate activation changed the grid by more than one logical item; refusing to guess "
                    f"which item was created (before={expected}, after={len(rows)})."
                )
            if physical_delta < 0:
                raise PhysicalStateUncertainError(
                    "Candidate activation removed physical rows; refusing to infer a pending item."
                )
            if physical_delta == 0 and not self._baseline_rows_accounted(baseline_rows, rows):
                raise PhysicalStateUncertainError(
                    "Candidate activation left the row count unchanged but a prior physical row disappeared or "
                    "mutated beyond structural reconciliation."
                )
            if self._unexpected_dialog_present():
                raise PhysicalStateUncertainError(
                    "An unexpected dialog appeared while confirming pending-item creation."
                )
            if time.time() - start >= timeout_s:
                raise PhysicalStateUncertainError(
                    "Candidate selection created zero new physical rows within the activation window."
                )
            time.sleep(0.25)

    def bind_rr_pair_after_activation(
        self, before_snapshot: list[tuple[str | None, str | None]], timeout_s: float = 8.0,
    ) -> "PendingRRPairTarget":
        """Phase 6.3 (R&R Stage 3): the coordinated-pair counterpart to
        pending_item_created() above -- positively detects and binds
        BOTH physical halves of one freshly activated R&R item, instead
        of the single "+"-only target pending_item_created() retains.

        Deliberately self-contained rather than refactored out of
        pending_item_created() (which stays completely unmodified by
        this phase): this duplicates a small amount of baseline-
        snapshot/polling glue, never the quantity-entry logic itself,
        which is fully reused by write_and_verify_rr_pair_quantities()
        below via the exact same enter_quantity() every ordinary and
        single-target R&R task already uses. Same immediate-decision-
        per-poll timing model as pending_item_created() -- no implicit
        "wait, maybe the second row hasn't rendered yet" grace period,
        matching that function's own already-proven characteristic
        rather than introducing new, live-unvalidated timing behavior.

        Callers decide WHETHER to call this instead of pending_item_
        created() -- e.g. a future Stage 4 runner, for a task known
        (via CoordinatedPair) to be part of a coordinated pair. Never
        called by any existing production path today; ordinary and
        legacy R&R-"+"-only activation go through pending_item_
        created() completely unaffected.

        Raises PhysicalStateUncertainError for every shape this shares
        with pending_item_created() (dialog appeared, timeout, more/
        fewer rows than one logical pair, baseline mismatch) --
        genuine structural/physical-state uncertainty, never a retry
        trigger. Raises TaskLocalRowReconciliationError specifically
        when exactly two rows landed but they don't reconcile as one
        cleanly, uniquely bound -/+ pair (missing half, duplicate half,
        wrong identity) -- confined to this one task, never evidence
        the wider grid/group is unsafe."""
        expected = len(before_snapshot)
        self._pending_quantity_target = None
        baseline_rows = self._last_activation_baseline_rows
        if baseline_rows is None:
            baseline_rows = [] if expected == 0 else None
        if baseline_rows is None or len(baseline_rows) != expected:
            self._persist_rr_binding_diagnostic(
                list(baseline_rows or []), [], target=None,
                final_outcome="physical_state_uncertain",
                final_reason="missing_or_wrong_length_rich_activation_baseline",
                include_branch_results=False,
            )
            raise PhysicalStateUncertainError(
                "R&R pair binding has no matching rich activation baseline; refusing to infer a row delta."
            )
        baseline_identities = [(row.category, row.selector) for row in baseline_rows]
        if baseline_identities != list(before_snapshot):
            self._persist_rr_binding_diagnostic(
                baseline_rows, [], target=None,
                final_outcome="physical_state_uncertain",
                final_reason="rich_baseline_does_not_match_supplied_baseline",
                include_branch_results=False,
            )
            raise PhysicalStateUncertainError(
                "R&R pair binding's rich baseline does not match the supplied physical-row baseline."
            )
        start = time.time()
        while True:
            rows = self._snapshot_activation_rows()
            physical_delta = len(rows) - expected
            if physical_delta == 2:
                pair_target = self._pending_rr_pair_targets_from_delta(baseline_rows, rows)
                self._persist_rr_binding_diagnostic(
                    baseline_rows, rows, target=pair_target,
                    final_outcome="bound" if pair_target is not None else "task_local_reconciliation",
                    final_reason=(
                        "authoritative_dispatch_returned_pair"
                        if pair_target is not None
                        else "authoritative_dispatch_returned_none"
                    ),
                )
                if pair_target is not None:
                    return pair_target
                # Task-local, not project-level: this task's own activation
                # added two rows but they don't reconcile as one uniquely
                # bound R&R -/+ pair -- ambiguity about which new rows are
                # THIS task's item, not evidence anything outside this task
                # is unsafe. See TaskLocalRowReconciliationError's docstring.
                raise TaskLocalRowReconciliationError(
                    "Candidate activation added two physical rows, but they did not reconcile as one uniquely "
                    f"bound R&R -/+ pair (before={expected}, after={len(rows)})."
                )
            if physical_delta == 1 or physical_delta > 2:
                self._persist_rr_binding_diagnostic(
                    baseline_rows, rows, target=None,
                    final_outcome="physical_state_uncertain",
                    final_reason="physical_row_delta_is_not_two",
                    include_branch_results=False,
                )
                raise PhysicalStateUncertainError(
                    "R&R pair activation did not produce exactly two new physical rows; refusing to guess "
                    f"which rows belong to this pair (before={expected}, after={len(rows)})."
                )
            if physical_delta < 0:
                self._persist_rr_binding_diagnostic(
                    baseline_rows, rows, target=None,
                    final_outcome="physical_state_uncertain",
                    final_reason="activation_removed_physical_rows",
                    include_branch_results=False,
                )
                raise PhysicalStateUncertainError(
                    "Candidate activation removed physical rows; refusing to infer an R&R pair."
                )
            if physical_delta == 0 and not self._baseline_rows_accounted(baseline_rows, rows):
                self._persist_rr_binding_diagnostic(
                    baseline_rows, rows, target=None,
                    final_outcome="physical_state_uncertain",
                    final_reason="unchanged_row_count_with_unreconciled_baseline",
                    include_branch_results=False,
                )
                raise PhysicalStateUncertainError(
                    "Candidate activation left the row count unchanged but a prior physical row disappeared or "
                    "mutated beyond structural reconciliation."
                )
            if self._unexpected_dialog_present():
                self._persist_rr_binding_diagnostic(
                    baseline_rows, rows, target=None,
                    final_outcome="physical_state_uncertain",
                    final_reason="unexpected_dialog_during_binding",
                    include_branch_results=False,
                )
                raise PhysicalStateUncertainError(
                    "An unexpected dialog appeared while confirming R&R pair activation."
                )
            if time.time() - start >= timeout_s:
                self._persist_rr_binding_diagnostic(
                    baseline_rows, rows, target=None,
                    final_outcome="physical_state_uncertain",
                    final_reason="activation_timeout_with_zero_new_rows",
                    include_branch_results=False,
                )
                raise PhysicalStateUncertainError(
                    "Candidate selection created zero new physical rows within the activation window."
                )
            time.sleep(0.25)

    @classmethod
    def _activation_identity(cls, row: ActivationRowSnapshot) -> tuple[str, str, str]:
        return (
            cls._normalized_pair_text(row.category),
            cls._normalized_pair_text(row.selector),
            cls._normalized_pair_text(row.description),
        )

    @staticmethod
    def _activity_token(row: ActivationRowSnapshot) -> str | None:
        value = str(row.activity or "").strip()
        return value or None

    @classmethod
    def _rr_diagnostic_row(cls, row: ActivationRowSnapshot, index: int,
                           rows: list[ActivationRowSnapshot]) -> dict:
        identity = cls._activation_identity(row)
        activity = cls._activity_token(row)
        ordinal = sum(
            1 for prior in rows[:index + 1]
            if cls._activation_identity(prior) == identity and cls._activity_token(prior) == activity
        )
        if activity == "-":
            activity_class = "literal_minus"
        elif activity == "+":
            activity_class = "literal_plus"
        elif activity in _ACTIVITY_MINUS_OCR_CONFUSIONS:
            activity_class = "accepted_noisy_minus"
        elif activity in _ACTIVITY_PLUS_OCR_CONFUSIONS:
            activity_class = "accepted_noisy_plus"
        else:
            activity_class = "unrecognized"
        return {
            "row_index": index,
            "activity_ordinal": ordinal,
            "binder_input": {
                "category": row.category,
                "selector": row.selector,
                "description": row.description,
                "activity": row.activity,
            },
            "normalized": {
                "category": identity[0],
                "selector": identity[1],
                "description": identity[2],
                "activity": activity,
                "activity_class": activity_class,
            },
        }

    @staticmethod
    def _rr_activation_delta_indices(
        before_rows: list[ActivationRowSnapshot], after_rows: list[ActivationRowSnapshot],
    ) -> list[int]:
        """Indices not consumed by an exact multiset copy of the baseline."""
        remaining = Counter(before_rows)
        delta: list[int] = []
        for index, row in enumerate(after_rows):
            if remaining[row] > 0:
                remaining[row] -= 1
            else:
                delta.append(index)
        return delta

    @classmethod
    def _rr_strict_branch_reason(
        cls, before_rows: list[ActivationRowSnapshot], after_rows: list[ActivationRowSnapshot],
    ) -> str:
        if len(after_rows) != len(before_rows) + 2:
            return "row_count_delta_is_not_two"
        before_groups = cls._rows_by_activation_identity(before_rows)
        after_groups = cls._rows_by_activation_identity(after_rows)
        if before_rows and not before_groups:
            return "baseline_contains_incomplete_identity"
        if not after_groups:
            return "after_snapshot_contains_incomplete_identity"
        changed_identity = None
        for identity in set(before_groups) | set(after_groups):
            before_group = before_groups.get(identity, [])
            after_group = after_groups.get(identity, [])
            count_delta = len(after_group) - len(before_group)
            if count_delta not in (0, 2):
                return "identity_count_delta_is_not_zero_or_two"
            if count_delta == 2:
                if changed_identity is not None:
                    return "more_than_one_identity_gained_two_rows"
                changed_identity = identity
            remainder = cls._activity_remainder(before_group, after_group)
            if remainder is None:
                return "baseline_activity_cannot_be_reconciled"
            remaining, before_unknown = remainder
            remaining_total = sum(remaining.values())
            if count_delta == 0 and remaining_total != before_unknown:
                return "unchanged_identity_has_unexplained_activity_rows"
            if count_delta == 2:
                if remaining_total != before_unknown + 2:
                    return "changed_identity_activity_cardinality_is_not_two"
                if remaining.get("-", 0) < 1 or remaining.get("+", 0) < 1:
                    return "changed_identity_lacks_literal_minus_plus_roles"
        if changed_identity is None:
            return "no_identity_gained_two_rows"
        minus_indices = cls._activity_matching_indices(after_rows, changed_identity, "-")
        plus_indices = cls._activity_matching_indices(after_rows, changed_identity, "+")
        if len(minus_indices) != 1 or len(plus_indices) != 1:
            return "literal_role_ownership_is_not_unique"
        return "passed"

    @classmethod
    def _rr_tolerant_branch_reason(
        cls,
        before_rows: list[ActivationRowSnapshot],
        after_rows: list[ActivationRowSnapshot],
        *,
        clean_activity: str,
        noisy_activity: str,
        accepted_confusions: frozenset[str],
    ) -> str:
        if len(after_rows) != len(before_rows) + 2:
            return "row_count_delta_is_not_two"
        before_counter = Counter(before_rows)
        after_counter = Counter(after_rows)
        if before_counter - after_counter:
            return "baseline_is_not_an_exact_preserved_multiset"
        added = after_counter - before_counter
        if sum(added.values()) != 2:
            return "exact_additive_delta_does_not_contain_two_rows"
        added_rows = list(added.elements())
        identities = {cls._activation_identity(row) for row in added_rows}
        if len(identities) != 1:
            return "delta_rows_do_not_share_one_identity"
        identity = next(iter(identities))
        if not all(identity):
            return "delta_identity_is_incomplete"
        if any(cls._activation_identity(row) == identity for row in before_counter):
            return "delta_identity_already_exists_in_baseline"
        clean_indices = cls._activity_matching_indices(after_rows, identity, clean_activity)
        if len(clean_indices) != 1:
            return f"literal_{'minus' if clean_activity == '-' else 'plus'}_role_is_not_unique"
        all_identity_indices = [
            index for index, row in enumerate(after_rows) if cls._activation_identity(row) == identity
        ]
        if len(all_identity_indices) != 2:
            return "identity_does_not_have_exactly_two_after_rows"
        remaining = [index for index in all_identity_indices if index not in clean_indices]
        if len(remaining) != 1:
            return "complementary_role_row_is_not_unique"
        token = cls._activity_token(after_rows[remaining[0]])
        if token not in accepted_confusions:
            role = "plus" if noisy_activity == "+" else "minus"
            return f"complementary_activity_is_not_an_accepted_noisy_{role}_token"
        return "passed"

    @classmethod
    def _rr_branch_diagnostics(
        cls, before_rows: list[ActivationRowSnapshot], after_rows: list[ActivationRowSnapshot],
    ) -> list[dict]:
        strict_reason = cls._rr_strict_branch_reason(before_rows, after_rows)
        plus_reason = cls._rr_tolerant_branch_reason(
            before_rows, after_rows, clean_activity="-", noisy_activity="+",
            accepted_confusions=_ACTIVITY_PLUS_OCR_CONFUSIONS,
        )
        minus_reason = cls._rr_tolerant_branch_reason(
            before_rows, after_rows, clean_activity="+", noisy_activity="-",
            accepted_confusions=_ACTIVITY_MINUS_OCR_CONFUSIONS,
        )
        strict_passed = strict_reason == "passed"
        plus_reached = not strict_passed
        plus_passed = plus_reason == "passed"
        minus_reached = plus_reached and not plus_passed
        return [
            {
                "branch": "strict_literal_minus_plus",
                "authoritative_dispatch_reached": True,
                "passed": strict_passed,
                "reason": strict_reason,
            },
            {
                "branch": "noisy_plus_fallback",
                "authoritative_dispatch_reached": plus_reached,
                "passed": plus_passed,
                "reason": plus_reason,
            },
            {
                "branch": "noisy_minus_fallback",
                "authoritative_dispatch_reached": minus_reached,
                "passed": minus_reason == "passed",
                "reason": minus_reason,
            },
        ]

    @staticmethod
    def _rr_diagnostic_target(target: PendingRRPairTarget | None) -> dict | None:
        if target is None:
            return None
        return {
            "identity": list(target.identity),
            "minus": {
                "after_index": target.minus_target.after_index,
                "activity": target.minus_target.activity,
                "activity_ordinal": target.minus_target.activity_ordinal,
            },
            "plus": {
                "after_index": target.plus_target.after_index,
                "activity": target.plus_target.activity,
                "activity_ordinal": target.plus_target.activity_ordinal,
            },
        }

    def _persist_rr_binding_diagnostic(
        self,
        before_rows: list[ActivationRowSnapshot],
        after_rows: list[ActivationRowSnapshot],
        *,
        target: PendingRRPairTarget | None,
        final_outcome: str,
        final_reason: str,
        include_branch_results: bool = True,
    ) -> None:
        """Best-effort persistence that is never allowed to affect binding."""
        try:
            delta_indices = self._rr_activation_delta_indices(before_rows, after_rows)
            crop_paths: dict[int, str] = {}
            image = self._last_activation_snapshot_image
            offset = self._last_activation_snapshot_offset
            row_1_top = self._last_activation_snapshot_row_1_top
            if image is not None and offset is not None and row_1_top is not None:
                try:
                    crop_dir = self.evidence_dir / "rr_binding_activity_crops"
                    crop_dir.mkdir(parents=True, exist_ok=True)
                    stamp = time.time_ns()
                    for index in delta_indices:
                        row_top = row_1_top + index * _GRID_ROW_HEIGHT
                        crop = self._activation_activity_crop(image, offset, row_top)
                        path = crop_dir / f"rr_binding_{stamp}_row_{index}.png"
                        crop.save(path)
                        crop_paths[index] = str(path)
                except Exception:
                    # JSON row/branch evidence remains useful even when an
                    # optional crop cannot be written.
                    crop_paths = {}

            context = self._execution_context
            branch_results = (
                self._rr_branch_diagnostics(before_rows, after_rows)
                if include_branch_results
                else [
                    {
                        "branch": name,
                        "authoritative_dispatch_reached": False,
                        "passed": False,
                        "reason": "not_evaluated_for_this_physical_shape",
                    }
                    for name in (
                        "strict_literal_minus_plus", "noisy_plus_fallback", "noisy_minus_fallback",
                    )
                ]
            )
            before = [self._rr_diagnostic_row(row, index, before_rows) for index, row in enumerate(before_rows)]
            after = [self._rr_diagnostic_row(row, index, after_rows) for index, row in enumerate(after_rows)]
            for row in after:
                index = row["row_index"]
                row["is_activation_delta"] = index in delta_indices
                row["activity_crop_path"] = crop_paths.get(index)
                row_top = row_1_top + index * _GRID_ROW_HEIGHT if row_1_top is not None else None
                observation = (
                    self._last_activation_snapshot_activity_ocr.get(row_top)
                    if row_top is not None
                    else None
                )
                row["activity_ocr"] = dict(observation) if observation is not None else None
            self._rr_binding_diagnostic_ledger.record({
                "run_id": context.run_id,
                "task_id": context.task_id,
                "source_row": context.source_row,
                "group": context.group,
                "before_row_count": len(before_rows),
                "after_row_count": len(after_rows),
                "physical_row_delta": len(after_rows) - len(before_rows),
                "before_rows": before,
                "after_rows": after,
                "activation_delta_indices": delta_indices,
                "activation_delta_method": "exact_snapshot_multiset_subtraction",
                "branches": branch_results,
                "final_result": self._rr_diagnostic_target(target),
                "final_outcome": final_outcome,
                "final_reason": final_reason,
            })
        except Exception:
            # Diagnostic construction/persistence is observational only.
            pass

    @classmethod
    def _pending_quantity_target_from_delta(
        cls,
        before_rows: list[ActivationRowSnapshot],
        after_rows: list[ActivationRowSnapshot],
    ) -> PendingQuantityTarget | None:
        """Return the quantity-bearing row descriptor for one safe delta.

        Validation remains delegated to the existing single-row/R&R
        multiset rules.  This helper only retains which validated
        identity was added and, for R&R, the ``+`` occurrence that owns
        quantity.  It never converts an invalid delta into a target.
        """
        is_single = cls._is_one_safe_single_row_delta(before_rows, after_rows)
        is_rr = cls._is_one_logical_rr_multiset_delta(before_rows, after_rows)
        if not (is_single or is_rr):
            return None

        before_groups = cls._rows_by_activation_identity(before_rows)
        after_groups = cls._rows_by_activation_identity(after_rows)
        required_delta = 1 if is_single else 2
        changed = [
            identity for identity, group in after_groups.items()
            if len(group) - len(before_groups.get(identity, [])) == required_delta
        ]
        if len(changed) != 1:
            return None
        identity = changed[0]

        if is_rr:
            activity = "+"
        else:
            before_tokens = [cls._activity_token(row) for row in before_groups.get(identity, [])]
            after_tokens = [cls._activity_token(row) for row in after_groups[identity]]
            remaining = list(after_tokens)
            for token in before_tokens:
                if token in remaining:
                    remaining.remove(token)
                elif None in remaining:
                    remaining.remove(None)
                else:
                    return None
            if len(remaining) != 1:
                return None
            # A non-R&R row's visually empty activity cell commonly
            # produces changing OCR garbage. Retain activity only when
            # it is a real +/- glyph; ordinary rows are identified by
            # their validated CAT/SEL/description delta instead.
            activity = remaining[0] if remaining[0] in ("-", "+") else None

        matching_indices = [
            index for index, row in enumerate(after_rows)
            if cls._activation_identity(row) == identity
            and (activity is None or cls._activity_token(row) == activity)
        ]
        if not matching_indices:
            return None
        return PendingQuantityTarget(
            identity=identity,
            activity=activity,
            after_index=matching_indices[-1],
            activity_ordinal=len(matching_indices),
            physical_row_delta=2 if is_rr else 1,
            write_source_quantity_once=True,
        )

    @classmethod
    def _activity_matching_indices(
        cls, rows: list[ActivationRowSnapshot], identity: tuple[str, str, str], activity: str,
    ) -> list[int]:
        return [
            index for index, row in enumerate(rows)
            if cls._activation_identity(row) == identity and cls._activity_token(row) == activity
        ]

    @classmethod
    def _pending_rr_pair_targets_from_delta(
        cls,
        before_rows: list[ActivationRowSnapshot],
        after_rows: list[ActivationRowSnapshot],
    ) -> "PendingRRPairTarget | None":
        """Phase 6.3 (R&R Stage 3) / Phase 5.25 (live OCR-glyph
        tolerance, "+" side) / Phase 5.28 (live OCR-glyph tolerance,
        "-" side): tries the strict proof first
        (_rr_pair_from_delta_strict()); only if that finds nothing does
        it try the narrowly-scoped OCR-tolerant fallbacks, in order --
        _rr_pair_from_delta_with_plus_glyph_tolerance(), then
        _rr_pair_from_delta_with_minus_glyph_tolerance() -- see each
        function's own docstring. The strict path is completely
        unmodified by either addition; every delta it already
        recognized it still recognizes identically, and each fallback
        is only ever reached once every function tried before it has
        already returned None."""
        pair = cls._rr_pair_from_delta_strict(before_rows, after_rows)
        if pair is not None:
            return pair
        pair = cls._rr_pair_from_delta_with_plus_glyph_tolerance(before_rows, after_rows)
        if pair is not None:
            return pair
        return cls._rr_pair_from_delta_with_minus_glyph_tolerance(before_rows, after_rows)

    @classmethod
    def _rr_pair_from_delta_strict(
        cls,
        before_rows: list[ActivationRowSnapshot],
        after_rows: list[ActivationRowSnapshot],
    ) -> "PendingRRPairTarget | None":
        """Phase 6.3 (R&R Stage 3): the dual-target counterpart to
        _pending_quantity_target_from_delta()'s single "+"-only R&R
        target above -- reuses the SAME order-independent multiset
        proof (_is_one_logical_rr_multiset_delta()) that one logical
        R&R pair was created, then binds BOTH physical halves instead
        of discarding the "-" side. Never touches, weakens, or is
        called by the existing single-target function; ordinary and
        legacy R&R-"+"-only activation are completely unaffected.

        Deliberately STRICTER than the legacy single-target path's
        ordinal-based multi-match tolerance (see
        _locate_pending_quantity_row()'s own handling of `activity_
        ordinal` for a pre-existing duplicate-identity row): requires
        EXACTLY one "-" row and EXACTLY one "+" row bearing the one
        changed identity, full stop. Returns None -- never a partially
        built pair -- for every shape that isn't unambiguously one
        clean R&R pair: a missing half, a duplicate minus or plus, both
        halves sharing one activity, an unrelated third row changing
        the physical delta, or any identity that can't be corroborated
        (_rows_by_activation_identity() itself already refuses to
        group anything when even one row's identity fields are
        incomplete)."""
        if not cls._is_one_logical_rr_multiset_delta(before_rows, after_rows):
            return None

        before_groups = cls._rows_by_activation_identity(before_rows)
        after_groups = cls._rows_by_activation_identity(after_rows)
        changed = [
            identity for identity, group in after_groups.items()
            if len(group) - len(before_groups.get(identity, [])) == 2
        ]
        if len(changed) != 1:
            return None
        identity = changed[0]

        minus_indices = cls._activity_matching_indices(after_rows, identity, "-")
        plus_indices = cls._activity_matching_indices(after_rows, identity, "+")
        if len(minus_indices) != 1 or len(plus_indices) != 1:
            return None

        minus_target = PendingQuantityTarget(
            identity=identity, activity="-", after_index=minus_indices[0], activity_ordinal=1,
            physical_row_delta=2, write_source_quantity_once=True,
        )
        plus_target = PendingQuantityTarget(
            identity=identity, activity="+", after_index=plus_indices[0], activity_ordinal=1,
            physical_row_delta=2, write_source_quantity_once=True,
        )
        return PendingRRPairTarget(minus_target=minus_target, plus_target=plus_target, identity=identity)

    @classmethod
    def _rr_pair_from_delta_with_plus_glyph_tolerance(
        cls,
        before_rows: list[ActivationRowSnapshot],
        after_rows: list[ActivationRowSnapshot],
    ) -> "PendingRRPairTarget | None":
        """Phase 5.25: live-caught -- a real R&R candidate activation
        (RFG/240, 9 -> 11 rows) produced one row whose activity read
        cleanly as "-" and a second, structurally complementary row
        whose activity OCR'd as the digit "4" instead of "+". The
        strict path above correctly refused to guess (its own
        _is_one_logical_rr_multiset_delta() gate requires a LITERAL
        "+" token and is never touched by this fallback), leaving the
        pair PAIR_REVIEW_REQUIRED. This function is the smallest
        scoped mechanism to recognize that SPECIFIC, evidenced glyph
        confusion -- reached ONLY after the strict path has already
        returned None -- while proving, independently and without
        reusing or relaxing _is_one_logical_rr_multiset_delta()/
        _activity_token() (both stay completely unmodified; "4" is
        NEVER globally treated as "+" anywhere else in this file), the
        exact same structural shape the strict path itself requires:

        1. Row count grew by EXACTLY 2 (cardinality unchanged).
        2. Every pre-existing row is byte-identical in the after
           snapshot (a Counter multiset diff, not a positional
           comparison) -- if so much as one existing row's content
           changed or disappeared, this returns None; only a truly
           clean, additive delta is ever in scope.
        3. The two NEW rows share exactly one CAT/SEL/description
           identity (mismatched candidate identities fail closed).
        4. That identity did not already exist anywhere in the before
           snapshot -- this fallback recognizes only a clean, from-
           nothing activation, never a pre-existing-duplicate shape
           (the strict path's own activity_ordinal tolerance already
           owns that case).
        5. Exactly one of the two new rows reads a LITERAL "-" (never
           OCR-tolerant on this side -- one positively recognized "-"
           half is a precondition, not something this fallback ever
           guesses at).
        6. The other (and, by point 1-4, ONLY other) new row for that
           identity reads a value in the tightly scoped
           _ACTIVITY_PLUS_OCR_CONFUSIONS set -- today just {"4"} --
           never an arbitrary/unconstrained value.

        Any failure of 1-6 returns None -- fails closed exactly like
        the strict path, never a partially built pair, never a guess
        from OCR text alone without this full structural corroboration."""
        if len(after_rows) != len(before_rows) + 2:
            return None
        before_counter = Counter(before_rows)
        after_counter = Counter(after_rows)
        if before_counter - after_counter:
            # A pre-existing row disappeared or its content changed --
            # not a clean additive delta; refuse to guess.
            return None
        added = after_counter - before_counter
        if sum(added.values()) != 2:
            return None
        added_rows = list(added.elements())
        identities = {cls._activation_identity(row) for row in added_rows}
        if len(identities) != 1:
            return None
        identity = next(iter(identities))
        if not all(identity):
            return None
        if any(cls._activation_identity(row) == identity for row in before_counter):
            return None

        minus_indices = cls._activity_matching_indices(after_rows, identity, "-")
        if len(minus_indices) != 1:
            return None
        all_identity_indices = [
            index for index, row in enumerate(after_rows) if cls._activation_identity(row) == identity
        ]
        if len(all_identity_indices) != 2:
            return None
        remaining = [index for index in all_identity_indices if index not in minus_indices]
        if len(remaining) != 1:
            return None
        plus_index = remaining[0]
        if cls._activity_token(after_rows[plus_index]) not in _ACTIVITY_PLUS_OCR_CONFUSIONS:
            return None

        minus_target = PendingQuantityTarget(
            identity=identity, activity="-", after_index=minus_indices[0], activity_ordinal=1,
            physical_row_delta=2, write_source_quantity_once=True,
        )
        plus_target = PendingQuantityTarget(
            identity=identity, activity="+", after_index=plus_index, activity_ordinal=1,
            physical_row_delta=2, write_source_quantity_once=True,
        )
        return PendingRRPairTarget(minus_target=minus_target, plus_target=plus_target, identity=identity)

    @classmethod
    def _rr_pair_from_delta_with_minus_glyph_tolerance(
        cls,
        before_rows: list[ActivationRowSnapshot],
        after_rows: list[ActivationRowSnapshot],
    ) -> "PendingRRPairTarget | None":
        """Phase 5.28: live-caught -- three independent R&R candidate
        activations (RFG/240 Roof, RFG/HIGH Roof, RFG/240 Shed), across
        two different groups and three different CAT/SEL pairs in one
        session, all produced a physical "-" row whose activity read
        cleanly as OCR garbage "to" instead of "-", while the
        structurally complementary new row read a clean literal "+".
        The strict path and the Phase 5.25 plus-glyph-tolerance
        fallback above both correctly refused to guess (neither is
        ever touched by this function; reached ONLY after both have
        already returned None), leaving these pairs PAIR_REVIEW_
        REQUIRED even though their own activation delta already proves
        one clean R&R pair landed. This is the exact minus-side mirror
        of _rr_pair_from_delta_with_plus_glyph_tolerance() above --
        same structural proof, roles swapped -- proving, independently
        and without reusing or relaxing _is_one_logical_rr_multiset_
        delta()/_activity_token() (both stay completely unmodified;
        "to" is NEVER globally treated as "-" anywhere else in this
        file), the exact same structural shape the strict path itself
        requires:

        1. Row count grew by EXACTLY 2 (cardinality unchanged).
        2. Every pre-existing row is byte-identical in the after
           snapshot (a Counter multiset diff, not a positional
           comparison) -- if so much as one existing row's content
           changed or disappeared, this returns None; only a truly
           clean, additive delta is ever in scope.
        3. The two NEW rows share exactly one CAT/SEL/description
           identity (mismatched candidate identities fail closed).
        4. That identity did not already exist anywhere in the before
           snapshot -- this fallback recognizes only a clean, from-
           nothing activation, never a pre-existing-duplicate shape.
        5. Exactly one of the two new rows reads a LITERAL "+" (never
           OCR-tolerant on this side -- one positively recognized "+"
           half is a precondition, not something this fallback ever
           guesses at).
        6. The other (and, by point 1-4, ONLY other) new row for that
           identity reads a value in the tightly scoped
           _ACTIVITY_MINUS_OCR_CONFUSIONS set -- today just {"to"} --
           never an arbitrary/unconstrained value.

        Any failure of 1-6 returns None -- fails closed exactly like
        the strict path, never a partially built pair, never a guess
        from OCR text alone without this full structural corroboration."""
        if len(after_rows) != len(before_rows) + 2:
            return None
        before_counter = Counter(before_rows)
        after_counter = Counter(after_rows)
        if before_counter - after_counter:
            # A pre-existing row disappeared or its content changed --
            # not a clean additive delta; refuse to guess.
            return None
        added = after_counter - before_counter
        if sum(added.values()) != 2:
            return None
        added_rows = list(added.elements())
        identities = {cls._activation_identity(row) for row in added_rows}
        if len(identities) != 1:
            return None
        identity = next(iter(identities))
        if not all(identity):
            return None
        if any(cls._activation_identity(row) == identity for row in before_counter):
            return None

        plus_indices = cls._activity_matching_indices(after_rows, identity, "+")
        if len(plus_indices) != 1:
            return None
        all_identity_indices = [
            index for index, row in enumerate(after_rows) if cls._activation_identity(row) == identity
        ]
        if len(all_identity_indices) != 2:
            return None
        remaining = [index for index in all_identity_indices if index not in plus_indices]
        if len(remaining) != 1:
            return None
        minus_index = remaining[0]
        if cls._activity_token(after_rows[minus_index]) not in _ACTIVITY_MINUS_OCR_CONFUSIONS:
            return None

        minus_target = PendingQuantityTarget(
            identity=identity, activity="-", after_index=minus_index, activity_ordinal=1,
            physical_row_delta=2, write_source_quantity_once=True,
        )
        plus_target = PendingQuantityTarget(
            identity=identity, activity="+", after_index=plus_indices[0], activity_ordinal=1,
            physical_row_delta=2, write_source_quantity_once=True,
        )
        return PendingRRPairTarget(minus_target=minus_target, plus_target=plus_target, identity=identity)

    def _ordinary_single_row_target_from_selected_candidate(
        self,
        before_rows: list[ActivationRowSnapshot],
        after_rows: list[ActivationRowSnapshot],
    ) -> PendingQuantityTarget | None:
        """Bind one newly activated ordinary row without reading quantity.

        The selected CAT/SEL comes from exact live UI Automation text. The
        target row must uniquely match that code plus the existing normalized
        description rule, must not carry a real R&R activity, and every
        baseline row must remain structurally accountable after excluding the
        target. Baseline descriptions are deliberately not re-litigated here:
        the saved 0006 frames show an unchanged 300S row repainting ``rfg.``
        as ``rig.`` while its CAT/SEL/activity structure stayed fixed.

        Quantity is neither accepted nor inspected by this method.
        """
        selected = self._last_selected
        if selected is None or len(after_rows) != len(before_rows) + 1:
            return None
        if not all((selected.category, selected.selector)):
            return None

        target_index = self._unique_clean_ordinary_delta_index(before_rows, after_rows)
        if target_index is None:
            return None

        return PendingQuantityTarget(
            identity=self._activation_identity(after_rows[target_index]),
            activity=None,
            after_index=target_index,
            activity_ordinal=1,
            physical_row_delta=1,
            allow_initial_quantity_overwrite=True,
            write_source_quantity_once=True,
            activation_baseline_rows=tuple(before_rows),
        )

    @classmethod
    def _unique_clean_ordinary_delta_index(
        cls,
        before_rows: list[ActivationRowSnapshot],
        after_rows: list[ActivationRowSnapshot],
    ) -> int | None:
        """Return the sole post-click row not explainable by the baseline.

        Search-result UIA identity and the single click establish intent.
        OCR only proves continuity of the pre-existing physical rows; the
        unmatched row's own OCR is never used as authoritative identity.
        """
        if len(after_rows) != len(before_rows) + 1:
            return None
        candidates = [
            index for index in range(len(after_rows))
            if cls._baseline_rows_accounted(
                before_rows, [row for row_index, row in enumerate(after_rows) if row_index != index],
            )
        ]
        return candidates[0] if len(candidates) == 1 else None

    @classmethod
    def _baseline_rows_accounted(
        cls,
        before_rows: list[ActivationRowSnapshot],
        after_rows: list[ActivationRowSnapshot],
    ) -> bool:
        if len(before_rows) != len(after_rows):
            return False
        edges = [
            [index for index, after in enumerate(after_rows) if cls._activation_rows_correspond(before, after)]
            for before in before_rows
        ]
        if any(not row_edges for row_edges in edges):
            return False
        matched_before: dict[int, int] = {}

        def assign(before_index: int, seen: set[int]) -> bool:
            for after_index in edges[before_index]:
                if after_index in seen:
                    continue
                seen.add(after_index)
                if after_index not in matched_before or assign(matched_before[after_index], seen):
                    matched_before[after_index] = before_index
                    return True
            return False

        return all(assign(index, set()) for index in sorted(range(len(edges)), key=lambda i: len(edges[i])))

    @classmethod
    def _activation_rows_correspond(
        cls, before: ActivationRowSnapshot, after: ActivationRowSnapshot,
    ) -> bool:
        before_activity = cls._activity_token(before)
        after_activity = cls._activity_token(after)
        if before_activity in ("-", "+") and after_activity in ("-", "+") and before_activity != after_activity:
            return False
        category_close = cls._normalized_text_is_close(before.category, after.category, max_edits=1)
        selector_close = cls._normalized_text_is_close(before.selector, after.selector, max_edits=1)
        description_close = cls._normalized_text_is_close(
            before.description, after.description, allow_long_prefix=True,
        )
        return description_close and (category_close or selector_close)

    @classmethod
    def _normalized_text_is_close(
        cls, left: str | None, right: str | None, *, max_edits: int | None = None,
        allow_long_prefix: bool = False,
    ) -> bool:
        left_text = cls._normalized_pair_text(left).replace(" ", "")
        right_text = cls._normalized_pair_text(right).replace(" ", "")
        if not left_text or not right_text:
            return False
        if left_text == right_text:
            return True
        shorter, longer = sorted((left_text, right_text), key=len)
        if allow_long_prefix and len(shorter) >= 20 and longer.startswith(shorter):
            return len(shorter) / len(longer) >= 0.65
        budget = max_edits
        if budget is None:
            length = max(len(left_text), len(right_text))
            budget = 1 if length <= 20 else 2 if length <= 50 else 3
        if abs(len(left_text) - len(right_text)) > budget:
            return False
        previous = list(range(len(right_text) + 1))
        for left_index, left_char in enumerate(left_text, start=1):
            current = [left_index]
            for right_index, right_char in enumerate(right_text, start=1):
                current.append(min(
                    current[-1] + 1,
                    previous[right_index] + 1,
                    previous[right_index - 1] + (left_char != right_char),
                ))
            if min(current) > budget:
                return False
            previous = current
        return previous[-1] <= budget

    @classmethod
    def _ordinary_activation_identity_matches(
        cls,
        observed: tuple[str, str, str],
        expected: tuple[str, str, str],
    ) -> bool:
        """Conservatively corroborate an exact-CAT/SEL ordinary activation.

        CAT/SEL equality is mandatory and cannot be overridden here. After
        punctuation/case/spacing normalization, allow a small edit distance
        scaled to description length: one edit through 20 characters, two
        through 50, and three beyond that. This tolerates ordinary OCR glyph
        errors without allowing a materially different description to bind.
        It is activation-only and does not alter quantity confirmation.
        """
        if cls._quantity_identity_matches(observed, expected):
            return True
        if observed[0] != expected[0] or observed[1] != expected[1]:
            return False
        observed_description = observed[2].replace(" ", "")
        expected_description = expected[2].replace(" ", "")
        if not observed_description or not expected_description:
            return False

        return cls._normalized_text_is_close(observed_description, expected_description)

    @classmethod
    def _rows_by_activation_identity(
        cls, rows: list[ActivationRowSnapshot],
    ) -> dict[tuple[str, str, str], list[ActivationRowSnapshot]]:
        grouped: dict[tuple[str, str, str], list[ActivationRowSnapshot]] = {}
        for row in rows:
            identity = cls._activation_identity(row)
            if not all(identity):
                return {}
            grouped.setdefault(identity, []).append(row)
        return grouped

    @classmethod
    def _activity_remainder(
        cls,
        before_rows: list[ActivationRowSnapshot],
        after_rows: list[ActivationRowSnapshot],
    ) -> tuple[dict[str | None, int], int] | None:
        """Account for baseline activities and return the unexplained remainder.

        A missing OCR activity is treated only as a wildcard for a
        baseline row already known to exist. It can consume one after
        row but can never itself supply the required new ``-``/``+``
        evidence.
        """
        after_counts: dict[str | None, int] = {}
        for row in after_rows:
            token = cls._activity_token(row)
            after_counts[token] = after_counts.get(token, 0) + 1

        before_unknown = 0
        for row in before_rows:
            token = cls._activity_token(row)
            if token is None:
                before_unknown += 1
                continue
            if after_counts.get(token, 0) > 0:
                after_counts[token] -= 1
            elif after_counts.get(None, 0) > 0:
                after_counts[None] -= 1
            else:
                return None
        return after_counts, before_unknown

    @classmethod
    def _is_one_logical_rr_multiset_delta(
        cls,
        before_rows: list[ActivationRowSnapshot],
        after_rows: list[ActivationRowSnapshot],
    ) -> bool:
        if len(after_rows) != len(before_rows) + 2:
            return False
        before_groups = cls._rows_by_activation_identity(before_rows)
        after_groups = cls._rows_by_activation_identity(after_rows)
        if (before_rows and not before_groups) or not after_groups:
            return False

        changed_identity = None
        for identity in set(before_groups) | set(after_groups):
            before_group = before_groups.get(identity, [])
            after_group = after_groups.get(identity, [])
            count_delta = len(after_group) - len(before_group)
            if count_delta not in (0, 2):
                return False
            if count_delta == 2:
                if changed_identity is not None:
                    return False
                changed_identity = identity
            remainder = cls._activity_remainder(before_group, after_group)
            if remainder is None:
                return False
            remaining, before_unknown = remainder
            remaining_total = sum(remaining.values())
            if count_delta == 0:
                if remaining_total != before_unknown:
                    return False
            else:
                # Baseline unknowns may consume any existing activity,
                # but the two rows left over must be a readable -/+ pair.
                if remaining_total != before_unknown + 2:
                    return False
                if remaining.get("-", 0) < 1 or remaining.get("+", 0) < 1:
                    return False
        return changed_identity is not None

    @classmethod
    def _is_one_safe_single_row_delta(
        cls,
        before_rows: list[ActivationRowSnapshot],
        after_rows: list[ActivationRowSnapshot],
    ) -> bool:
        if len(after_rows) != len(before_rows) + 1:
            return False
        before_groups = cls._rows_by_activation_identity(before_rows)
        after_groups = cls._rows_by_activation_identity(after_rows)
        if (before_rows and not before_groups) or not after_groups:
            return False
        changed = 0
        for identity in set(before_groups) | set(after_groups):
            before_group = before_groups.get(identity, [])
            after_group = after_groups.get(identity, [])
            delta = len(after_group) - len(before_group)
            if delta not in (0, 1):
                return False
            remainder = cls._activity_remainder(before_group, after_group)
            if remainder is None:
                return False
            remaining, before_unknown = remainder
            if delta == 0:
                if sum(remaining.values()) != before_unknown:
                    return False
                continue
            changed += 1
            # The sole unexplained row may be ordinary or Xactimate's
            # complete one-row representation of a quantity-bearing R&R
            # ``+`` item. A lone ``-`` row is never quantity-bearing and
            # remains incomplete/fail-closed.
            possible_new = [
                token for token, count in remaining.items()
                for _ in range(count)
            ]
            if len(possible_new) != before_unknown + 1:
                return False
            if before_unknown == 0 and possible_new[0] == "-":
                return False
        return changed == 1

    @staticmethod
    def _normalized_pair_text(value: str | None) -> str:
        return " ".join(re.findall(r"[a-z0-9]+", str(value or "").lower()))

    @classmethod
    def _is_logical_rr_pair(cls, remove_row: ActivationRowSnapshot, add_row: ActivationRowSnapshot) -> bool:
        remove_identity = (
            cls._normalized_pair_text(remove_row.category),
            cls._normalized_pair_text(remove_row.selector),
        )
        add_identity = (
            cls._normalized_pair_text(add_row.category),
            cls._normalized_pair_text(add_row.selector),
        )
        if not all(remove_identity) or remove_identity != add_identity:
            return False

        remove_description = cls._normalized_pair_text(remove_row.description)
        add_description = cls._normalized_pair_text(add_row.description)
        if not remove_description or remove_description != add_description:
            return False

        activities = [str(remove_row.activity or "").strip(), str(add_row.activity or "").strip()]
        # Blank means OCR supplied no activity evidence.  Any readable
        # value must agree with its expected side of the R&R pair.
        return all(not observed or observed == expected for observed, expected in zip(activities, ("-", "+")))

    def _dismiss_stray_results_popup(self) -> bool:
        """Phase 5.8A (live-caught, defense-in-depth). orchestrator.
        execute_plan() now calls recover() itself on every NO_MATCH/
        REVIEW_REQUIRED decision (the actual fix for the reproduced
        defect: a task that never reaches select_candidate() never
        clicks the results popup closed, so it was still open when the
        NEXT group's setup started interacting with the window). This
        is a cheap, independent second line of defense at every group-
        tree entry point, matching the exact self-heal pattern already
        used for _handle_duplicate_item_dialog(): if some OTHER, future
        code path ever returns without dismissing its own popup, this
        catches it here rather than letting a stray popup interfere
        with group-tree clicks. Returns True if a popup was found and
        dismissed, False if none was open. Never raises."""
        try:
            if self._find_dropdown_window() is None:
                return False
            self.recover()
            return True
        except Exception:
            return False

    def commit_item(self) -> None:
        VK_S = 0x53
        self._press_ctrl(VK_S)
        time.sleep(1.0)
        self._handle_duplicate_item_dialog()
        time.sleep(0.5)

    def verify_quantity_committed(
        self, expected_quantity: float, timeout_s: float = 3.0
    ) -> QuantityVerificationResult:
        """Not part of the abstract contract -- bounded-polling
        replacement for a single-shot ``read_quantity()`` call used to
        confirm a quantity actually landed after a grid-mutating
        action (selection, quantity entry, or commit).

        Live investigation (Phase 4.5) found a single-shot read taken
        immediately after such an action intermittently returns `None`
        or a wrong value. Live timing evidence (Phase 4.6, 5 fresh
        commits, `read_quantity()` fixed to a majority-vote-across-
        scales read -- see that method's docstring) found the REAL
        settle time is near-instant once the OCR itself is reliable:
        all 5 commits read the correct quantity on the very first poll
        (sub-millisecond). The `timeout_s` default is set from that
        evidence with a wide conservative margin (3.0s -- roughly
        1000x the observed real-world time), not tuned tightly to it,
        since an occasional genuinely slower render is still possible
        and this must never become an unbounded wait.

        Polls with PROGRESSIVE intervals -- five fast attempts at
        0.1s apart (covers the common near-instant case cheaply),
        then 0.4s apart afterward (avoids hammering the OCR pipeline
        during a genuinely slow settle) -- and terminates on the first
        of:
        - the observed value equals `expected_quantity` (success,
          ``stop_reason="matched"``);
        - the SAME non-matching value is observed twice in a row
          (``stop_reason="wrong_value"`` -- a stable wrong reading
          isn't a settle-timing issue polling can fix; surfacing it
          immediately is more honest than waiting out the full
          budget);
        - the row's identity check fails after previously succeeding,
          i.e. the grid changed under us mid-poll
          (``stop_reason="conflicting_row"``);
        - `_unexpected_dialog_present()` is true
          (``stop_reason="wrong_context"`` -- aborts immediately
          rather than continuing to poll past something that needs a
          human);
        - `timeout_s` elapses (``stop_reason="timeout"``).

        Every attempt's elapsed time, whether a grid row was located,
        and the observed value are recorded in the returned result's
        `.samples` for diagnostics. See docs/xactimate-lookup.md
        Phase 4.5/4.6."""
        start = time.time()
        samples: list[tuple[float, bool, float | None]] = []
        attempts = 0
        observed: float | None = None
        previous: float | None = None
        previous_row_found: bool | None = None
        while True:
            attempts += 1
            elapsed = time.time() - start
            if self._unexpected_dialog_present():
                return QuantityVerificationResult(
                    matched=False, stop_reason="wrong_context", expected=expected_quantity,
                    observed=observed, attempts=attempts, elapsed_s=elapsed, samples=samples,
                )
            hwnd = self._ensure_main_window()
            image, offset = self._capture_and_locate(hwnd, attempts=1, delay_s=0)
            row_found = offset is not None and self._last_row_geometry(image, offset) is not None
            if previous_row_found and not row_found:
                return QuantityVerificationResult(
                    matched=False, stop_reason="conflicting_row", expected=expected_quantity,
                    observed=observed, attempts=attempts, elapsed_s=elapsed, samples=samples,
                )
            observed = self.read_quantity() if row_found else None
            samples.append((round(elapsed, 3), row_found, observed))
            if observed == expected_quantity:
                return QuantityVerificationResult(
                    matched=True, stop_reason="matched", expected=expected_quantity,
                    observed=observed, attempts=attempts, elapsed_s=elapsed, samples=samples,
                )
            if observed is not None and observed == previous:
                return QuantityVerificationResult(
                    matched=False, stop_reason="wrong_value", expected=expected_quantity,
                    observed=observed, attempts=attempts, elapsed_s=elapsed, samples=samples,
                )
            if elapsed >= timeout_s:
                return QuantityVerificationResult(
                    matched=False, stop_reason="timeout", expected=expected_quantity,
                    observed=observed, attempts=attempts, elapsed_s=elapsed, samples=samples,
                )
            previous = observed
            previous_row_found = row_found
            time.sleep(0.1 if attempts < 5 else 0.4)

    #: Fixed 0-based index of the "Delete" item within the row context
    #: menu's flat UIA child list. Live-caught (Phase 4.6): the menu is
    #: a Telerik `RadMenuItem` tree whose items expose NO usable name
    #: via `CurrentName`, `CurrentAutomationId`, or `LegacyIAccessible`
    #: (all return either empty or a generic `"Telerik.Windows.
    #: Controls.RadMenuItem Header: Items.Count:N"` ToString() dump) --
    #: so items cannot be found by text at all, OCR or otherwise. What
    #: IS reliable: the "Undo ..." slot (index 5) is ALWAYS present as
    #: an element (it's the only one with a real `CurrentName`, `"Undo
    #: "`), just collapsed to a (0,0,0,0) rect when there's nothing to
    #: undo -- so the total flat child count (26) and every other
    #: item's index are stable regardless of undo-history state,
    #: confirmed by re-deriving this index twice in the same session
    #: (once with a fresh add, once after a prior delete) and getting
    #: identical results both times. `_click_delete_via_uia()` verifies
    #: this structural invariant (total count + real-item height, not
    #: a ~7px separator) before ever clicking, and refuses rather than
    #: guessing if it doesn't hold. See docs/xactimate-lookup.md Phase
    #: 4.6.
    _CONTEXT_MENU_DELETE_INDEX = 11
    _CONTEXT_MENU_EXPECTED_ITEM_COUNT = 26

    #: Phase 5.1: the group tree's OWN right-click context menu is a
    #: DIFFERENT menu from the grid row's (different real actions,
    #: different structural indices) despite coincidentally having the
    #: same total flat item count. Live-measured (docs/build-estimate.md
    #: Phase 5.1 Stage 3): Cut, Copy, Paste, [sep], Select>, Deselect>,
    #: [sep], Expand>, Collapse>, [sep], Filter Options..., Tree View,
    #: List View, Grouping Selection>, [sep], New..., Edit..., Delete,
    #: Dimension, [sep], Grouping..., Global Changes..., Global Item Sort
    #: by>, [sep], Save Macro..., Retrieve Macro... -- "New..." and
    #: "Delete" are the two real indices used here.
    _GROUP_MENU_EXPECTED_ITEM_COUNT = 26
    _GROUP_MENU_NEW_INDEX = 15
    _GROUP_MENU_DELETE_INDEX = 17

    #: Phase 6.1 (live-caught): ensure_group()'s post-creation discovery
    #: loop used to be range(3)/0.8s (~2.4s of sleeps, but each
    #: snapshot_group_names() call itself costs ~2.1-2.5s live, so the
    #: REAL bounded window was already closer to ~8-9s) -- yet a genuine
    #: production run still hit "group still not found after the
    #: creation sequence completed" creating the very first group
    #: ("Roof") in an otherwise-empty TEST estimate (Phase 6.0), cascading
    #: all 17 of that group's tasks to REVIEW_REQUIRED without ever
    #: attempting a search. Direct instrumentation of the real, unmodified
    #: method (never a hand-rolled reimplementation) measured newly-
    #: created-group discoverability taking as long as ~4.5s past the
    #: Attach click across repeated empty-TEST trials, uncomfortably
    #: close to the old budget -- consistent with the one observed live
    #: failure being a genuine, intermittent settle-timing race (slower
    #: under real multi-task run conditions than in an isolated
    #: diagnostic), not a hard creation failure (Roof always physically
    #: existed, empty, once the run moved on). Widened, not removed --
    #: still a bounded, checked-every-time retry (never a blind sleep,
    #: never a weakened/skipped verification), just with real headroom
    #: above the worst latency actually measured. Applies to every
    #: ensure_group() call uniformly (not just "the first group" or
    #: group_name == "Roof") -- an already-fast, already-discoverable
    #: group still exits this loop on its first iteration, so this
    #: only extends how long a genuinely slow-to-appear case gets before
    #: failing closed.
    _GROUP_DISCOVERY_MAX_ATTEMPTS = 8
    _GROUP_DISCOVERY_RETRY_INTERVAL_S = 0.8

    #: Live-measured, self-contained relative to the group tree's own
    #: "Group" column header text (NOT the grid's "Cat" anchor -- that
    #: anchor was found live-unreliable whenever the grid has zero rows,
    #: which is exactly the common case group operations run in; see
    #: docs/build-estimate.md Phase 5.1). Row 0 is always the project
    #: root; child groups start at row 1.
    #: Live-caught (Phase 5.1 Stage 4): an earlier calibration (15px)
    #: was close enough to make row_index=1 clicks land correctly (the
    #: error was within that row's clickable tolerance) but NOT
    #: row_index=2 -- confirmed live, a click computed with the wrong
    #: height landed on row_index=1 ("Utility Room") instead of
    #: row_index=2 ("Dwelling Roof") even though OCR text-reading
    #: (which uses a taller, more forgiving crop) still read both rows
    #: correctly. Remeasured directly via OCR word-level top-position on
    #: a real 3-row tree: TEST~125, Utility Room~153, Dwelling Roof~175
    #: (relative to client origin) -- 22-23px between child rows, not
    #: 15. See docs/build-estimate.md Phase 5.1.
    #: Live-caught (Phase 5.2 Stage 3): the Phase 5.1 click formula
    #: above (27, 23) and OCR-crop formula (25, 15) each worked well
    #: enough at low row indices by margin alone, but they measure
    #: DIFFERENT effective row pitches -- so as more groups piled up
    #: (a 5th row), the two disagreed on which physical row a given
    #: row_index meant. `_find_group_row()` (via the OCR/text formula)
    #: returned an index that `_group_subtotal_pixel_count()` (via the
    #: click formula) then read from the WRONG physical row, making
    #: `verify_group()` measure content that was never there. Remeasured
    #: directly via OCR word-level top positions on a real live 5-row
    #: tree: header "Group" top=111; row text tops at 134, 154, 174,
    #: 194, 214 -- exactly 20px apart, 23px below the header, dead
    #: consistent across all 5 rows. One formula now, used everywhere a
    #: row's vertical position is needed (clicking, OCR text, subtotal
    #: pixels), so an index found one way always means the same
    #: physical row to every other consumer.
    _GROUP_TREE_ROW_TEXT_TOP_DY = 23
    _GROUP_TREE_ROW_HEIGHT = 20
    _GROUP_TREE_CLICK_DX = 79
    #: Click Y lands a few px below the text's top edge -- safely
    #: mid-row rather than right at the first pixel of the glyph.
    _GROUP_TREE_CLICK_DY_OFFSET = 8
    _GROUP_TREE_TEXT_DX = 35
    _GROUP_TREE_TEXT_WIDTH = 245
    #: Text/subtotal crop band: starts slightly above the real text top
    #: for headroom, and stays under one full row pitch (20px) so it
    #: never bleeds into the next row.
    _GROUP_TREE_ROW_CROP_MARGIN_TOP = 3
    _GROUP_TREE_ROW_CROP_HEIGHT = 18

    def _find_context_menu_popup_hwnd(self, main_hwnd: int) -> int | None:
        """Returns the HWND of the currently-open row context menu (a
        separate top-level WPF popup window, like the search-results
        dropdown -- invisible to the client-area PrintWindow capture
        used elsewhere in this file), or None if no such window is
        open. Distinguished from an unexpected dialog by size (the
        context menu is a narrow vertical strip; live-measured at
        ~300x500px, always taller than wide) rather than title, since
        this popup -- like the results dropdown -- has an empty
        title."""
        win32gui = self._win32gui()

        def cb(hwnd, acc):
            try:
                cls = win32gui.GetClassName(hwnd)
                visible = win32gui.IsWindowVisible(hwnd)
                rect = win32gui.GetWindowRect(hwnd)
            except Exception:
                return True
            if not visible or hwnd == main_hwnd or _APP_CLASS_MARKER not in cls:
                return True
            w, h = rect[2] - rect[0], rect[3] - rect[1]
            if 0 < w < 500 and h > w:
                acc.append(hwnd)
            return True

        found: list[int] = []
        win32gui.EnumWindows(cb, found)
        return found[0] if found else None

    def _click_delete_via_uia(self, popup_hwnd: int) -> bool:
        """Locates the "Delete" item in an already-open row context
        menu via UI Automation structure (not OCR, not a fixed pixel
        offset -- see `_CONTEXT_MENU_DELETE_INDEX`'s docstring for why)
        and clicks its live-read bounding-rectangle center. Verifies
        the structural invariant (exact expected item count, and that
        the target index is a real ~24px item, not a ~7px separator)
        before clicking; returns False without clicking anything if
        the structure doesn't match, rather than guessing. `Invoke()`
        and `LegacyIAccessible.DoDefaultAction()` were both tried first
        and confirmed live to be safe no-ops on this control (neither
        raises, neither does anything) -- a real mouse click at the
        UIA-derived coordinate is what actually works. See
        docs/xactimate-lookup.md Phase 4.6."""
        uia, UIA = self._uia()
        element = uia.ElementFromHandle(popup_hwnd)
        walker = uia.RawViewWalker
        menu_root = walker.GetFirstChildElement(element)
        if menu_root is None:
            return False
        child = walker.GetFirstChildElement(menu_root)
        items = []
        while child:
            items.append(child)
            try:
                child = walker.GetNextSiblingElement(child)
            except Exception:
                break

        if len(items) != self._CONTEXT_MENU_EXPECTED_ITEM_COUNT:
            return False
        target = items[self._CONTEXT_MENU_DELETE_INDEX]
        rect = target.CurrentBoundingRectangle
        height = rect.bottom - rect.top
        if not (18 <= height <= 30):  # a real item is ~24px; a separator is ~7px
            return False

        cx = (rect.left + rect.right) // 2
        cy = (rect.top + rect.bottom) // 2
        self._click_screen(cx, cy)
        return True

    def _open_row_context_menu(self, hwnd: int, row_x: int, row_y: int) -> tuple[int, int]:
        """Right-clicks at the given CLIENT coordinates and returns the
        (screen_x, screen_y) it clicked at. Split out of
        cancel_current_item()/delete_existing_item() so both share the
        exact same right-click mechanics."""
        ctypes, _ = self._win32()
        user32 = ctypes.windll.user32
        ox, oy = self._get_client_origin(hwnd)
        screen_x, screen_y = ox + row_x, oy + row_y
        user32.SetCursorPos(screen_x, screen_y)
        time.sleep(0.1)
        user32.mouse_event(0x0008, 0, 0, 0, 0)  # MOUSEEVENTF_RIGHTDOWN
        time.sleep(0.05)
        user32.mouse_event(0x0010, 0, 0, 0, 0)  # MOUSEEVENTF_RIGHTUP
        # Live-caught (Phase 4.4): 0.4s was sometimes too short for the
        # context menu to be fully rendered before the follow-up
        # interaction. See docs/xactimate-lookup.md Phase 4.4.
        time.sleep(1.0)
        return screen_x, screen_y

    def _read_category_selector_at(self, image, offset: tuple[int, int], row_top: int) -> tuple[str | None, str | None]:
        """Lighter-weight sibling of `_read_populated_fields_once()` --
        reads only category/selector at an ARBITRARY row_top (not
        assumed to be the last row), for identity-based row lookup in
        a multi-row grid. Same crop/PSM/upscale parameters as the
        equivalent fields in `_read_populated_fields_once()` (Phase
        4.4 Stage 3's live-verified settings), applied at a caller-
        supplied row position instead of the grid's last row.

        Live-caught (Phase 4.7): the `selector` column boundary (563,
        620), widened in Phase 4.4 to fit a 6-character code like
        "GUTAB>", visually overlaps the neighboring `activity` column
        for a SHORT code -- reproduced live: a real "GUTA" cell OCR'd
        as "GUTA &", the trailing "&" being the activity symbol
        bleeding in from real pixel content, not an OCR misread this
        time. Rather than re-narrow the boundary (which would risk
        re-truncating the long codes Phase 4.4 widened it for -- the
        two failure modes trade off against the same pixel range),
        the selector OCR result is split on whitespace and only the
        first token kept, since a real selector code never legitimately
        contains a space itself. See docs/xactimate-lookup.md Phase
        4.7."""
        dx = offset[0]

        def crop_col(col_name):
            col_l, col_r = _GRID_COLUMNS[col_name]
            return image.crop((col_l + dx, row_top, col_r + dx, row_top + _GRID_ROW_HEIGHT))

        cat_crop = crop_col("category")
        cat_crop = cat_crop.resize((cat_crop.width * 4, cat_crop.height * 4))
        cat = self._ocr_text(cat_crop, psm=6).strip() or None
        sel_crop = crop_col("selector")
        sel_crop = sel_crop.resize((sel_crop.width * 4, sel_crop.height * 4))
        sel_raw = self._ocr_text(sel_crop).strip()
        sel = sel_raw.split()[0] if sel_raw else None
        return cat, sel

    def _read_category_selector_for_verify_commit(self, image, offset: tuple[int, int], row_top: int) -> tuple[str | None, str | None]:
        """Phase 5.15 Pass 2 (ground-truth-guided, live-verified against
        the completed Aranda reference estimate): a dedicated sibling of
        `_read_category_selector_at()`, used ONLY by `verify_commit()`.
        Applies the SAME OCR fixes Phase 5.12 already proved live for
        `read_populated_fields()` -- psm=6 (not the `_ocr_text()`
        default psm=7) for the selector crop, and picking the LONGEST
        whitespace-separated token (not `split()[0]`) so a stray
        leading OR trailing artifact never wins over the real code --
        plus the same locally-widened, letters-only category crop.
        Live-caught: this exact class of noise (RFG misread as RFC,
        DOR misread as DO!) was independently confirmed still present
        in `verify_commit()`'s OCR during Phase 5.14/5.15 ground-truth
        comparison, downgrading objectively correct commits to
        QUANTITY_MISMATCH/REVIEW_REQUIRED trust states for no real
        reason -- see verify_commit()'s own docstring point 7.

        Deliberately NOT a change to `_read_category_selector_at()`
        itself, which `snapshot_grid_identities()` (and therefore group
        verification's probe-lifecycle proof, Phase 5.9A/5.10C) also
        depends on -- this phase's instructions are explicit that group
        verification must not be touched without a new, independently
        reproduced defect, and none was found there this phase."""
        dx = offset[0]

        def crop_col(col_name):
            col_l, col_r = _GRID_COLUMNS[col_name]
            return image.crop((col_l + dx, row_top, col_r + dx, row_top + _GRID_ROW_HEIGHT))

        cat_l, _cat_r = _GRID_COLUMNS["category"]
        sel_l, _sel_r = _GRID_COLUMNS["selector"]
        cat_crop = image.crop((cat_l + dx, row_top, sel_l + dx, row_top + _GRID_ROW_HEIGHT))
        cat_crop = cat_crop.resize((cat_crop.width * 4, cat_crop.height * 4))
        cat_raw = self._ocr_text(cat_crop, psm=6).strip()
        cat_letters_match = re.match(r"[A-Za-z]+", cat_raw)
        cat = (cat_letters_match.group(0) if cat_letters_match else cat_raw) or None

        sel_crop = crop_col("selector")
        sel_crop = sel_crop.resize((sel_crop.width * 4, sel_crop.height * 4))
        sel_raw = self._ocr_text(sel_crop, psm=6).strip()
        sel = max(sel_raw.split(), key=len) if sel_raw else None
        return cat, sel

    def snapshot_grid_identities(self, row_top_nudge: int = 0) -> list[tuple[str | None, str | None]]:
        """Returns [(category, selector), ...] top-to-bottom for every
        row currently in the grid. Used by `delete_existing_item()` to
        independently verify, before AND after, that exactly the
        targeted row changed and nothing else did; and by callers of
        `verify_commit()` (Phase 4.8), which requires a snapshot taken
        BEFORE `select_candidate()` -- the pending row is already
        present in the grid as soon as a candidate is selected, well
        before `commit_item()` -- so the committed row can be
        identified structurally (row-count delta across the whole
        select-through-commit sequence) rather than by searching OCR
        text across every row.

        `row_top_nudge` (Phase 5.15 Pass 2, default 0 -- identical to
        every existing caller's behavior): shifts every row crop by
        this many pixels before OCR. Added ONLY for
        `_verify_group_once()`'s post-cleanup re-verification, which
        live-caught a heavily-populated group (Roof, 15 pre-existing
        rows -- the first time this session a probe cycle ran against
        a group this large) reproducibly reading every row as garbage
        even though the grid content was visibly, confirmably
        unchanged: the header's own "Cat" label was independently
        re-located correctly (`_anchor_offset()` is fine), but the
        FIXED header-to-first-data-row gap baked into
        `_ANCHORS["grid_row_1"]` (24px) measured ~6px too tall for
        this specific window state (real gap ~18px), so a crop exactly
        one row-height tall landed on a thin sliver of two different
        rows instead of one whole row -- confirmed by direct pixel
        measurement and by sweeping small vertical nudges until clean
        reads reappeared. Not a transient timing issue (identical,
        stable misread across 5+ independent re-captures over
        multiple seconds), so a bare retry loop would not have helped."""
        hwnd = self._ensure_main_window()
        image, offset = self._capture_and_locate(hwnd)
        if offset is None:
            return []
        geom = self._last_row_geometry(image, offset)
        if geom is None:
            return []
        row_count, _ = geom
        row_1_top = self._shifted_anchor("grid_row_1", offset)[1] + row_top_nudge
        return [
            self._read_category_selector_at(image, offset, row_1_top + i * _GRID_ROW_HEIGHT)
            for i in range(row_count)
        ]

    def _snapshot_commit_rows(self, row_top_nudge: int = 0) -> list[CommitRowSnapshot]:
        """Capture identity plus protected quantity/unit for each row."""
        hwnd = self._ensure_main_window()
        image, offset = self._capture_and_locate(hwnd)
        if offset is None:
            return []
        if row_top_nudge == 0:
            return self._commit_rows_from_same_image(image, offset)
        # Preserve the non-default nudge path exactly: its row extent is
        # intentionally established at the unnudged geometry before fields
        # are read at the caller-supplied vertical offset.
        geom = self._last_row_geometry(image, offset)
        if geom is None:
            return []
        row_count, _ = geom
        row_1_top = self._shifted_anchor("grid_row_1", offset)[1] + row_top_nudge
        rows = []
        for index in range(row_count):
            row_top = row_1_top + index * _GRID_ROW_HEIGHT
            activation = self._read_activation_row_at(image, offset, row_top)
            _unit_raw, unit = self._read_unit_at(image, offset, row_top)
            rows.append(CommitRowSnapshot(
                category=activation.category,
                selector=activation.selector,
                description=activation.description,
                activity=activation.activity,
                quantity=self._read_quantity_at(image, offset, row_top),
                unit=unit,
            ))
        return rows

    @classmethod
    def _commit_row_multiset_key(cls, row: CommitRowSnapshot):
        """Return a strict protected-row key, or None when unreadable."""
        identity = cls._activation_identity(ActivationRowSnapshot(
            row.category, row.selector, row.description, row.activity,
        ))
        if not all(identity) or row.quantity is None or not row.unit:
            return None
        activity = cls._activity_token(ActivationRowSnapshot(
            row.category, row.selector, row.description, row.activity,
        ))
        return (*identity, activity if activity in ("-", "+") else None, round(float(row.quantity), 4), row.unit)

    @classmethod
    def _order_independent_commit_delta(
        cls,
        before_rows: list[CommitRowSnapshot],
        after_rows: list[CommitRowSnapshot],
    ) -> tuple[bool, list[int]]:
        """Consume every protected baseline row and return new indices."""
        remaining: dict[tuple, int] = {}
        for row in before_rows:
            key = cls._commit_row_multiset_key(row)
            if key is None:
                return False, []
            remaining[key] = remaining.get(key, 0) + 1

        new_indices = []
        for index, row in enumerate(after_rows):
            key = cls._commit_row_multiset_key(row)
            if key is not None and remaining.get(key, 0) > 0:
                remaining[key] -= 1
            else:
                new_indices.append(index)
        return not any(remaining.values()), new_indices

    def _retained_rr_commit_delta_indices(
        self,
        after_rows: list[CommitRowSnapshot],
        *,
        row_count_before: int,
        category: str,
        selector: str,
    ) -> list[int]:
        """Recover only a previously proven R&R activation binding.

        Activation already reconciled the full pre-selection multiset and
        retained the quantity-bearing ``+`` occurrence before any write.
        A later grid repaint can independently corrupt companion CAT/SEL OCR
        (live: ``RFG/RFC`` and ``300S/3008``) or blank the ``-`` glyph. That
        repaint must not turn the same bound R&R item into a new identity
        problem after commit. Ordinary rows never enter this path.

        The exact UIA-selected CAT/SEL, retained normalized description,
        activity occurrence, expected one-row/two-row form, and final row
        count must all agree. No quantity is used to choose the row.
        """
        target = self._pending_quantity_target
        selected = self._last_selected
        if (
            target is None
            or target.activity != "+"
            or target.physical_row_delta not in (1, 2)
            or selected is None
            or self._normalized_pair_text(selected.category) != self._normalized_pair_text(category)
            or self._normalized_pair_text(selected.selector) != self._normalized_pair_text(selector)
            or len(after_rows) != row_count_before + target.physical_row_delta
        ):
            return []

        expected_description = target.identity[2]
        same_description = [
            index for index, row in enumerate(after_rows)
            if self._activation_identity(ActivationRowSnapshot(
                row.category, row.selector, row.description, row.activity,
            ))[2] == expected_description
        ]
        plus_indices = [
            index for index in same_description
            if self._activity_token(ActivationRowSnapshot(
                after_rows[index].category, after_rows[index].selector,
                after_rows[index].description, after_rows[index].activity,
            )) == "+"
        ]
        ordinal_index = target.activity_ordinal - 1
        if ordinal_index < 0 or ordinal_index >= len(plus_indices):
            return []
        plus_index = plus_indices[ordinal_index]
        if target.physical_row_delta == 1:
            return [plus_index]

        # Prefer a readable ``-`` occurrence. If its glyph alone was lost
        # in the post-commit repaint, the corresponding same-description,
        # non-``+`` occurrence is the already-proven companion.
        remove_indices = [
            index for index in same_description
            if self._activity_token(ActivationRowSnapshot(
                after_rows[index].category, after_rows[index].selector,
                after_rows[index].description, after_rows[index].activity,
            )) == "-"
        ]
        if ordinal_index >= len(remove_indices):
            remove_indices = [
                index for index in same_description
                if index != plus_index and self._activity_token(ActivationRowSnapshot(
                    after_rows[index].category, after_rows[index].selector,
                    after_rows[index].description, after_rows[index].activity,
                )) != "+"
            ]
        if ordinal_index >= len(remove_indices):
            return []
        return [remove_indices[ordinal_index], plus_index]

    def snapshot_grid_identities_for_activation(self) -> list[tuple[str | None, str | None]]:
        """Capture both pre-activation baselines from one physical frame.

        A 16-row Roof grid was reproducibly counted as 15 because the
        bottom row's narrow item-number crop sat at the viewport edge,
        even though that row's wider CAT/SEL cells were still readable.
        Probe at most the next two physical row positions and append
        each only when both identity cells are readable.  Two covers
        the known one-row undercount plus one newly-created row.  This
        performs no scrolling and therefore cannot strand the
        following search on a displaced Price List screen.

        The former implementation called ``_snapshot_activation_rows()``
        and then ``_snapshot_commit_rows()`` back-to-back. Nothing mutated
        the UI between those calls, yet both captured and OCR-read the same
        pre-click grid. A rich commit row already contains every activation
        field, so one rich same-image walk now supplies both baselines. The
        activation-only edge probes remain exactly where they were; if one
        finds a row outside the rich snapshot's established extent, the
        identity lists deliberately differ and the rich commit baseline is
        still discarded exactly as before.
        """
        self._pending_quantity_target = None
        hwnd = self._ensure_main_window()
        image, offset = self._capture_and_locate(hwnd)
        if offset is None:
            activation_rows: list[ActivationRowSnapshot] = []
            commit_rows: list[CommitRowSnapshot] = []
        else:
            commit_rows = self._commit_rows_from_same_image(image, offset)
            activation_rows = [
                ActivationRowSnapshot(
                    row.category, row.selector, row.description, row.activity,
                )
                for row in commit_rows
            ]
            row_1_top = self._shifted_anchor("grid_row_1", offset)[1]
            for _probe in range(2):
                next_row_top = row_1_top + len(activation_rows) * _GRID_ROW_HEIGHT
                if next_row_top < 0 or next_row_top + _GRID_ROW_HEIGHT > image.height:
                    break
                trailing = self._read_activation_row_at(image, offset, next_row_top)
                if not all(str(value or "").strip() for value in (trailing.category, trailing.selector)):
                    break
                activation_rows.append(trailing)

        self._last_activation_baseline_rows = list(activation_rows)
        identities = [(row.category, row.selector) for row in activation_rows]
        self._last_commit_baseline_rows = (
            commit_rows
            if [(row.category, row.selector) for row in commit_rows] == identities
            else None
        )
        return identities

    def _snapshot_activation_rows(self, *, require_located: bool = False) -> list[ActivationRowSnapshot]:
        """Capture physical rows plus pair evidence without scrolling."""
        hwnd = self._ensure_main_window()
        image, offset = self._capture_and_locate(hwnd)
        if offset is None:
            self._last_activation_snapshot_image = None
            self._last_activation_snapshot_offset = None
            self._last_activation_snapshot_row_1_top = None
            self._last_activation_snapshot_activity_ocr = {}
            if require_located:
                raise AdapterError(
                    "Cannot positively locate the current Items grid for search-fallback state verification."
                )
            return []
        activity_ocr: dict[int, dict[str, str | None]] = {}
        self._active_activation_activity_ocr = activity_ocr
        try:
            rows, row_1_top = self._activation_rows_from_same_image(image, offset)
            self._last_activation_snapshot_image = image
            self._last_activation_snapshot_offset = offset
            self._last_activation_snapshot_row_1_top = row_1_top
            for _probe in range(2):
                next_row_top = row_1_top + len(rows) * _GRID_ROW_HEIGHT
                if next_row_top < 0 or next_row_top + _GRID_ROW_HEIGHT > image.height:
                    break
                trailing = self._read_activation_row_at(image, offset, next_row_top)
                if not all(str(value or "").strip() for value in (trailing.category, trailing.selector)):
                    break
                rows.append(trailing)
        finally:
            self._active_activation_activity_ocr = None
        self._last_activation_snapshot_activity_ocr = activity_ocr
        return rows

    def _read_activation_row_at(self, image, offset: tuple[int, int], row_top: int) -> ActivationRowSnapshot:
        category, selector = self._read_category_selector_at(image, offset, row_top)
        return ActivationRowSnapshot(
            category=category,
            selector=selector,
            description=self._read_description_at(image, offset, row_top),
            activity=self._read_activation_activity_at(image, offset, row_top),
        )

    def _read_activation_activity_at(self, image, offset: tuple[int, int], row_top: int) -> str | None:
        """Read an R&R activity glyph without including the right gridline.

        On the live Roof pair, the ordinary full activity-column crop
        rendered visible ``-``/``+`` glyphs as ``_``/``__`` because the
        gridline dominated OCR.  The glyph itself sits across the
        selector/activity boundary (an already-documented Xactimate
        rendering quirk), so this activation-only crop straddles that
        boundary and corroborates two OCR segmentation modes.  It does
        not alter the shared grid columns used by quantity/commit logic.
        """
        crop = self._activation_activity_crop(image, offset, row_top)
        reads = [self._ocr_text(crop, psm=psm).strip() for psm in (6, 7)]
        for expected in ("-", "+"):
            if expected in reads:
                if self._active_activation_activity_ocr is not None:
                    self._active_activation_activity_ocr[row_top] = {
                        "psm_6_raw": reads[0],
                        "psm_7_raw": reads[1],
                        "selected_raw": expected,
                    }
                return expected
        selected = next((read for read in reads if read), None)
        if self._active_activation_activity_ocr is not None:
            self._active_activation_activity_ocr[row_top] = {
                "psm_6_raw": reads[0],
                "psm_7_raw": reads[1],
                "selected_raw": selected,
            }
        return selected

    @staticmethod
    def _activation_activity_crop(image, offset: tuple[int, int], row_top: int):
        """Return the exact resized image supplied to activity OCR."""
        activity_left, _activity_right = _GRID_COLUMNS["activity"]
        dx = offset[0]
        crop = image.crop((
            activity_left - 14 + dx,
            row_top,
            activity_left + 12 + dx,
            row_top + _GRID_ROW_HEIGHT,
        ))
        return crop.resize((crop.width * 4, crop.height * 4))

    def _read_description_at(self, image, offset: tuple[int, int], row_top: int) -> str | None:
        """Lighter-weight description-only read at an ARBITRARY
        row_top, same crop/PSM as `_read_populated_fields_once()`.
        Used by `verify_commit()` for corroborating/instrumentation
        purposes only -- row identity itself is decided structurally
        (row-count delta), not by description text (Phase 4.8)."""
        col_l, col_r = _GRID_COLUMNS["description"]
        dx = offset[0]
        crop = image.crop((col_l + dx, row_top, col_r + dx, row_top + _GRID_ROW_HEIGHT))
        return self._normalize_inch_mark(self._ocr_text(crop, psm=6)) or None

    #: Maps `UnitVerificationResult.unit_match_state` to Stage 8's
    #: three-tier compatibility outcome. A quantity match never
    #: overrides this -- `verify_commit()` sets `compatibility` from
    #: this table alone, regardless of `quantity_matched`. See
    #: docs/xactimate-lookup.md Phase 4.7 Stage 8.
    _UNIT_STATE_TO_COMPATIBILITY = {
        "exact_match": "compatible",
        "normalized_synonym": "compatible",
        "verified_conversion": "compatible",
        "source_unit_missing": "review_required",
        "expected_unit_missing": "review_required",
        "observed_unit_missing": "review_required",
        "unreadable": "review_required",
        "ambiguous": "review_required",
        "incompatible": "hard_stop",
    }

    def check_unit_compatibility(
        self, source_unit: str | None, expected_xactimate_unit: str | None, observed_xactimate_unit: str | None,
    ) -> UnitVerificationResult:
        """Not part of the abstract contract (Phase 5.6). Thin instance
        wrapper around the pure, module-level `check_unit_compatibility()`
        so orchestrator.py can call it duck-typed (`hasattr(adapter,
        "check_unit_compatibility")`, exactly like `verify_commit`/
        `snapshot_grid_identities`) without importing this Windows-only
        module directly -- keeping orchestrator.py adapter-agnostic.
        See the module-level function's own docstring for the actual
        compatibility logic (unchanged, reused as-is)."""
        return check_unit_compatibility(source_unit, expected_xactimate_unit, observed_xactimate_unit)

    def check_category_selector_match(
        self, expected_category: str | None, expected_selector: str | None,
        observed_category: str | None, observed_selector: str | None,
    ) -> CategorySelectorMatchResult:
        """Not part of the abstract contract (Phase 5.12). Thin instance
        wrapper around the pure, module-level
        `check_category_selector_match()`, exactly like
        `check_unit_compatibility` above -- see that function's own
        docstring for the actual comparison logic."""
        return check_category_selector_match(expected_category, expected_selector, observed_category, observed_selector)

    def verify_commit(
        self,
        before_snapshot: list[tuple[str | None, str | None]],
        category: str,
        selector: str,
        expected_quantity: float,
        source_unit: str | None = None,
        expected_xactimate_unit: str | None = None,
        timeout_s: float = 3.0,
        populated_unit: str | None = None,
    ) -> CommitVerification:
        """Not part of the abstract contract -- Phase 4.8's row
        identification and verification strategy, replacing Phase
        4.7's `verify_committed_row()` (retired; its approach of
        searching for a CAT+SEL OCR text match across every row was
        found unreliable within the bounded polling window for
        catalog categories never exercised before Phase 4.7, and
        still intermittently for categories every prior phase
        validated -- see docs/xactimate-lookup.md Phase 4.7 and 4.8).

        Callers MUST call `snapshot_grid_identities()` BEFORE
        `select_candidate()` -- NOT merely before `commit_item()` --
        and pass the result as `before_snapshot`. Live validation
        (Phase 4.8) found the pending row is already present in the
        grid as soon as a candidate is selected (well before Ctrl+S);
        `commit_item()` finalizes/saves that row rather than
        inserting a new one, so a snapshot taken right before
        `commit_item()` sees a row count that has already incremented
        and never observes the delta this method depends on. Row
        identity is established STRUCTURALLY, never by searching OCR
        text across rows:

        1. Poll (bounded by `timeout_s`) until the grid's row count
           differs from `len(before_snapshot)`.
        2. Consume the rich pre-selection baseline (logical identity,
           activity, quantity, and unit) from the post-commit rows as
           an order-independent multiset. Every protected baseline row
           must still exist with the same protected values.
        3. The unconsumed delta must be exactly one expected ordinary
           row, or one corroborated R&R -/+ pair. Its live row index is
           taken from that delta, never presumed to be last.
        4. A delta other than one physical row or one corroborated R&R
           pair, or changed pre-existing rows ->
           `trust_state="CONFLICTING_ROW"` (refuses to guess which
           row is the committed one).
        5. `delta == 0` through `timeout_s` ->
           `trust_state="VERIFICATION_FAILED"` (commit not detected
           in the grid within the bounded window).
        6. Once the row is structurally identified, quantity and unit
           are read AT THAT KNOWN POSITION for the evidence record.
           Phase 5.27 (live-caught, repeatedly): numeric quantity OCR
           at this zoom/DPI is not reliable enough to gate success --
           real, physically correct quantities were read back wrong
           (30.19 misread as 19, 33.33 as 33, 9.36 as 6) and produced
           false QUANTITY_MISMATCH outcomes for rows that were written
           exactly as intended. `quantity_observed`/`quantity_matched`
           are still recorded on the returned `CommitVerification` as
           ADVISORY diagnostics/evidence, but no longer influence
           `trust_state` -- a deterministic write to a structurally,
           positively-identified row (this method's whole reason for
           existing) is its own authoritative evidence of success. An
           incompatible unit -> `trust_state="UNIT_MISMATCH"`; a unit
           state of "review_required" -> `trust_state="REVIEW_REQUIRED"`
           (unit compatibility is a genuinely separate signal from
           quantity OCR and is unaffected by this change).

           Phase 5.6 Stage 4 (live-caught): a real committed row
           (line_0001, R&R Gutter, 200 LF, correct SFG/GUTA selection,
           quantity read back exactly) was downgraded to
           REVIEW_REQUIRED because this method's own post-commit OCR
           misread the unit cell as "a". `populated_unit` -- the
           majority-voted OCR read of the SAME cell taken by
           `read_populated_fields()` right after `select_candidate()`,
           BEFORE commit -- is preferred as the observed unit whenever
           it resolves to a known Xactimate unit; the post-commit
           single-shot OCR read here is used only when `populated_unit`
           is missing or unreadable. This does not weaken the check --
           an incompatible unit is still incompatible regardless of
           which read supplied it -- it only avoids letting an
           unrelated OCR miss on ONE of two independent reads of the
           same cell override a genuinely correct commit.
        7. Category and selector OCR at the known position (Phase
           4.8's explicit directive: category OCR becomes supporting
           evidence whenever stronger evidence exists, and this phase
           does not add new OCR correction rules) are read too, but
           ONLY as corroboration. Unreadable category/selector OCR
           does NOT prevent `trust_state="VERIFIED"`. Category/
           selector OCR that IS readable but contradicts the expected
           identity downgrades the result to
           `trust_state="REVIEW_REQUIRED"`, even though nothing
           structural or numeric disagreed. Description OCR is read
           and recorded (`description_observed`) for the evidence
           record but is not used in any automated pass/fail decision
           -- fuzzy-matching noisy OCR description text reliably is
           exactly the kind of new OCR-tuning work this phase does
           not do.
        8. `trust_state="VERIFIED"` requires ALL of: one logical item
           appended (one row or one corroborated R&R pair),
           pre-existing rows unchanged, unit compatible, and
           category/selector OCR either agrees or is unreadable
           (never contradicts). Quantity OCR is NOT a requirement
           (Phase 5.27) -- see point 6 above.

        `_unexpected_dialog_present()` at any point during polling ->
        `trust_state="VERIFICATION_FAILED"` (wrong context).

        Every attempt's instrumentation is recorded in `.samples`,
        matching Phase 4.7 Stage 4's precedent. See
        docs/xactimate-lookup.md Phase 4.8."""
        start = time.time()
        samples: list[dict] = []
        attempts = 0
        row_count_before = len(before_snapshot)
        protected_activation_baseline_rows = list(self._last_commit_baseline_rows or [])
        baseline_rows = list(protected_activation_baseline_rows)
        baseline_identities = [(row.category, row.selector) for row in baseline_rows]
        supplied_identities = list(before_snapshot)
        baseline_identity_multiset_matches = len(baseline_identities) == len(supplied_identities)
        for identity in baseline_identities:
            if identity in supplied_identities:
                supplied_identities.remove(identity)
            else:
                baseline_identity_multiset_matches = False
                break
        baseline_identity_multiset_matches = baseline_identity_multiset_matches and not supplied_identities
        if row_count_before and (
            len(baseline_rows) != row_count_before
            or not baseline_identity_multiset_matches
        ):
            baseline_rows = []

        def fail(
            trust_state: str, reason: str, row_count_after: int | None = None,
            preexisting_rows_unchanged: bool | None = None,
        ) -> CommitVerification:
            return CommitVerification(
                trust_state=trust_state, reason=reason,
                row_count_before=row_count_before, row_count_after=row_count_after, row_index=None,
                preexisting_rows_unchanged=preexisting_rows_unchanged,
                category_expected=category, selector_expected=selector,
                category_observed=None, selector_observed=None, category_selector_ocr_agrees=None,
                description_observed=None,
                quantity_expected=expected_quantity, quantity_observed=None, quantity_matched=False,
                unit=None, compatibility="not_evaluated", compatibility_reason=reason,
                attempts=attempts, elapsed_s=time.time() - start, samples=samples,
            )

        while True:
            attempts += 1
            elapsed = time.time() - start
            if self._unexpected_dialog_present():
                return fail("VERIFICATION_FAILED", "an unexpected dialog appeared while verifying the commit")

            after_rows = self._snapshot_commit_rows()
            after = [(row.category, row.selector) for row in after_rows]
            row_count_after = len(after)
            delta = row_count_after - row_count_before
            sample: dict = {"elapsed_s": round(elapsed, 3), "row_count_after": row_count_after, "delta": delta}
            samples.append(sample)

            logical_rr_pair = False
            new_indices: list[int] = []
            baseline_available = row_count_before == 0 or bool(baseline_rows)
            preexisting_unchanged = False
            if delta in (1, 2) and baseline_available:
                preexisting_unchanged, new_indices = self._order_independent_commit_delta(
                    baseline_rows, after_rows,
                )

            retained_rr_indices = []
            retained_rr_binding = False
            if delta in (1, 2):
                retained_rr_indices = self._retained_rr_commit_delta_indices(
                    after_rows,
                    row_count_before=row_count_before,
                    category=category,
                    selector=selector,
                )
                retained_rr_binding = len(retained_rr_indices) == delta
                if retained_rr_binding:
                    # This is not a fresh identity reconstruction: the full
                    # multiset and exact selected CAT/SEL were proven at
                    # activation, and quantity entry retained that binding.
                    # Only the expected final row count and bound R&R
                    # description/activity occurrence are rechecked here.
                    preexisting_unchanged = True
                    new_indices = retained_rr_indices
                    sample["retained_rr_activation_binding"] = True

            if delta == 2 and preexisting_unchanged and len(new_indices) == 2:
                activation_delta = [ActivationRowSnapshot(
                    after_rows[index].category,
                    after_rows[index].selector,
                    after_rows[index].description,
                    after_rows[index].activity,
                ) for index in new_indices]
                activation_delta.sort(key=lambda row: 0 if self._activity_token(row) == "-" else 1)
                logical_rr_pair = (
                    self._is_logical_rr_pair(activation_delta[0], activation_delta[1])
                    or retained_rr_binding
                )
                sample["rr_pair_evidence"] = [
                    {
                        "category": row.category,
                        "selector": row.selector,
                        "description": row.description,
                        "activity": row.activity,
                    }
                    for row in activation_delta
                ]
                sample["logical_rr_pair"] = logical_rr_pair
                if not logical_rr_pair:
                    return fail(
                        "CONFLICTING_ROW",
                        "row count increased by 2, but the order-independent logical delta was not exactly one "
                        "corroborated R&R -/+ pair -- refusing to guess which row is the committed item",
                        row_count_after=row_count_after,
                        preexisting_rows_unchanged=preexisting_unchanged,
                    )

            if delta == 1 or logical_rr_pair:
                if not preexisting_unchanged or len(new_indices) != delta:
                    return fail(
                        "CONFLICTING_ROW",
                        "the order-independent post-commit multiset does not contain every protected baseline row "
                        "unchanged plus exactly one expected logical delta",
                        row_count_after=row_count_after,
                        preexisting_rows_unchanged=False,
                    )

                if logical_rr_pair:
                    plus_indices = [
                        index for index in new_indices
                        if self._activity_token(ActivationRowSnapshot(
                            after_rows[index].category, after_rows[index].selector,
                            after_rows[index].description, after_rows[index].activity,
                        )) == "+"
                    ]
                    if len(plus_indices) != 1:
                        return fail(
                            "CONFLICTING_ROW", "the new R&R logical delta has no unique quantity-bearing + row",
                            row_count_after=row_count_after, preexisting_rows_unchanged=True,
                        )
                    row_index = plus_indices[0]
                else:
                    row_index = new_indices[0]

                hwnd = self._ensure_main_window()
                image, offset = self._capture_and_locate(hwnd, attempts=1, delay_s=0)
                if offset is None:
                    return fail(
                        "VERIFICATION_FAILED", "could not re-locate the grid to read the committed row",
                        row_count_after=row_count_after,
                    )
                row_top = self._shifted_anchor("grid_row_1", offset)[1] + row_index * _GRID_ROW_HEIGHT

                cat_observed, sel_observed = self._read_category_selector_for_verify_commit(image, offset, row_top)
                description_observed = self._read_description_at(image, offset, row_top)
                quantity_observed = self._read_quantity_at(image, offset, row_top)
                unit_raw, unit_normalized = self._read_unit_at(image, offset, row_top)

                # Live-caught (Phase 4.8): immediately after the
                # row-count delta is first observed, the row's own
                # cell content can still be mid-repaint -- quantity or
                # unit occasionally read back empty on the very first
                # attempt even though the row identity itself (row
                # count, position) is already solid. One bounded
                # settle-and-reread (not an unbounded retry, not a new
                # OCR rule) mirrors the polling pattern used
                # everywhere else in this method.
                if quantity_observed is None or unit_raw is None:
                    time.sleep(0.4)
                    image, offset = self._capture_and_locate(hwnd, attempts=1, delay_s=0)
                    if offset is not None:
                        row_top = self._shifted_anchor("grid_row_1", offset)[1] + row_index * _GRID_ROW_HEIGHT
                        if quantity_observed is None:
                            quantity_observed = self._read_quantity_at(image, offset, row_top)
                        if unit_raw is None:
                            unit_raw, unit_normalized = self._read_unit_at(image, offset, row_top)

                sample["category_observed"] = cat_observed
                sample["selector_observed"] = sel_observed
                sample["description_observed"] = description_observed
                sample["quantity_observed"] = quantity_observed
                sample["unit_raw"] = unit_raw
                sample["unit_normalized"] = unit_normalized

                quantity_matched = quantity_observed is not None and quantity_observed == expected_quantity

                # Phase 5.6 Stage 4: prefer the pre-commit, majority-
                # voted populated-field unit read over this method's own
                # single-shot post-commit OCR read whenever it resolves
                # to a known unit -- see class docstring above.
                populated_unit_vocab = _resolve_observed_unit_vocab(populated_unit)
                unit_source = "populated_field" if populated_unit_vocab is not None else "post_commit_ocr"
                observed_unit_for_check = (
                    populated_unit if populated_unit_vocab is not None
                    else (unit_normalized or unit_raw)
                )

                unit_result = check_unit_compatibility(source_unit, expected_xactimate_unit, observed_unit_for_check)
                # _read_unit_at() keeps the literal first OCR read and
                # independently requires a 2+ vote among known units.
                # Verify against that voted value, but retain the raw
                # artifact in evidence rather than rewriting history.
                if populated_unit_vocab is None and unit_normalized is not None:
                    unit_result.observed_xactimate_unit = unit_raw
                compatibility = self._UNIT_STATE_TO_COMPATIBILITY.get(unit_result.unit_match_state, "review_required")
                sample["unit_source"] = unit_source

                cat_sel_present = cat_observed is not None or sel_observed is not None
                cat_sel_agrees = (cat_observed == category and sel_observed == selector) if cat_sel_present else None
                cat_sel_contradicts = cat_sel_present and not cat_sel_agrees

                # Phase 5.27: quantity_observed/quantity_matched are
                # recorded on the returned CommitVerification below as
                # ADVISORY evidence only -- neither an unreadable nor a
                # disagreeing quantity OCR read gates trust_state
                # anymore (see this method's own docstring, point 6).
                # Unit compatibility is a separate, still-authoritative
                # signal (never derived from the quantity cell).
                if compatibility == "hard_stop":
                    trust_state = "UNIT_MISMATCH"
                    reason = unit_result.unit_match_reason
                elif compatibility == "review_required":
                    trust_state = "REVIEW_REQUIRED"
                    reason = unit_result.unit_match_reason
                elif cat_sel_contradicts:
                    trust_state = "REVIEW_REQUIRED"
                    reason = (
                        f"structural evidence (row-count delta and unchanged pre-existing rows) and unit "
                        f"agree, but category/selector OCR at the committed row read "
                        f"{cat_observed}/{sel_observed}, which contradicts the expected {category}/{selector} "
                        f"-- a human should confirm"
                    )
                else:
                    trust_state = "VERIFIED"
                    if quantity_matched:
                        quantity_note = "quantity matched"
                    elif quantity_observed is None:
                        quantity_note = "quantity OCR unreadable (advisory only, not gating)"
                    else:
                        quantity_note = (
                            f"quantity OCR observed {quantity_observed!r} instead of {expected_quantity!r} "
                            "(advisory only, not gating -- the deterministic write to this positively-identified "
                            "row is treated as authoritative)"
                        )
                    reason = (
                        ("one corroborated R&R -/+ logical item added" if logical_rr_pair
                         else (
                             "one retained quantity-bearing R&R + row added"
                             if retained_rr_binding else "exactly one physical row added"
                         ))
                        + (
                            " by the retained activation-time R&R binding, expected row count remained stable, "
                            if retained_rr_binding
                            else " by order-independent logical multiset delta, pre-existing rows unchanged, "
                        )
                        + quantity_note + ", unit compatible, and category/selector OCR "
                        + ("agrees" if cat_sel_agrees else "was unreadable (not treated as a conflict)")
                    )

                return CommitVerification(
                    trust_state=trust_state, reason=reason,
                    row_count_before=row_count_before, row_count_after=row_count_after, row_index=row_index,
                    preexisting_rows_unchanged=preexisting_unchanged,
                    category_expected=category, selector_expected=selector,
                    category_observed=cat_observed, selector_observed=sel_observed,
                    category_selector_ocr_agrees=cat_sel_agrees,
                    description_observed=description_observed,
                    quantity_expected=expected_quantity, quantity_observed=quantity_observed, quantity_matched=quantity_matched,
                    unit=unit_result, compatibility=compatibility, compatibility_reason=unit_result.unit_match_reason,
                    attempts=attempts, elapsed_s=time.time() - start, samples=samples,
                )

            if delta != 0:
                return fail(
                    "CONFLICTING_ROW",
                    f"row count changed by {delta} (expected exactly 1) -- refusing to guess which row is the committed one",
                    row_count_after=row_count_after,
                )

            if elapsed >= timeout_s:
                retained = self._verify_via_retained_pending_target(
                    category=category, selector=selector, expected_quantity=expected_quantity,
                    source_unit=source_unit, expected_xactimate_unit=expected_xactimate_unit,
                    populated_unit=populated_unit, row_count_before=row_count_before,
                    attempts=attempts, start=start, samples=samples,
                    baseline_rows=protected_activation_baseline_rows, after_rows=after_rows,
                )
                if retained is not None:
                    return retained
                return fail(
                    "VERIFICATION_FAILED",
                    f"row count did not change within {timeout_s}s ({attempts} attempts) -- commit not detected",
                    row_count_after=row_count_after,
                )
            if delta == 0:
                self._persist_zero_delta_commit_diagnostic({
                    "attempt": attempts,
                    "elapsed_s": round(elapsed, 3),
                    "row_count_before": row_count_before,
                    "row_count_after": row_count_after,
                    "pre_activation_protected_baseline_rows": self._safe_zero_delta_rows_diagnostic(
                        protected_activation_baseline_rows,
                    ),
                    "fresh_post_commit_rows": self._safe_zero_delta_rows_diagnostic(after_rows),
                    "baseline_alignment_result": "not_evaluated_before_timeout",
                    "protected_multiset_reconciliation_result": "not_evaluated_before_timeout",
                    "candidate_activation_delta_rows": [],
                    "retained_target": self._safe_zero_delta_target_diagnostic(self._pending_quantity_target),
                    "identity_activity_match_result": "not_evaluated_before_timeout",
                    "duplicate_extra_row_result": "not_evaluated_before_timeout",
                    "final_fresh_reread": None,
                    "first_rejection_reason": None,
                    "final_zero_delta_reconciliation_result": "pending",
                })
            time.sleep(0.1 if attempts < 5 else 0.4)

    @classmethod
    def _zero_delta_rows_diagnostic(cls, rows: list[CommitRowSnapshot]) -> list[dict]:
        result = []
        for index, row in enumerate(rows):
            activation = ActivationRowSnapshot(row.category, row.selector, row.description, row.activity)
            result.append({
                "row_index": index,
                "raw": {
                    "category": row.category, "selector": row.selector,
                    "description": row.description, "activity": row.activity,
                    "quantity": row.quantity, "unit": row.unit,
                },
                "normalized": {
                    "category": cls._normalized_pair_text(row.category),
                    "selector": cls._normalized_pair_text(row.selector),
                    "description": cls._activation_identity(activation)[2],
                    "activity": cls._activity_token(activation),
                    "unit": str(row.unit or "").strip().upper() or None,
                },
            })
        return result

    def _safe_zero_delta_rows_diagnostic(self, rows: list[CommitRowSnapshot]) -> list[dict]:
        try:
            return self._zero_delta_rows_diagnostic(rows)
        except Exception:
            return [{"diagnostic_error": "row_serialization_failed", "row_count": len(rows)}]

    @staticmethod
    def _zero_delta_target_diagnostic(target: PendingQuantityTarget | None) -> dict | None:
        if target is None:
            return None
        return {
            "identity": list(target.identity), "activity": target.activity,
            "after_index": target.after_index, "physical_row_delta": target.physical_row_delta,
        }

    def _safe_zero_delta_target_diagnostic(self, target: PendingQuantityTarget | None) -> dict | None:
        try:
            return self._zero_delta_target_diagnostic(target)
        except Exception:
            return {"diagnostic_error": "target_serialization_failed"}

    def _persist_zero_delta_commit_diagnostic(self, entry: dict) -> None:
        """Observational only; construction or persistence can never affect verification."""
        try:
            context = self._execution_context
            self._zero_delta_commit_diagnostic_ledger.record({
                "run_id": context.run_id, "task_id": context.task_id,
                "source_row": context.source_row, "group": context.group,
                **entry,
            })
        except Exception:
            pass

    def _verify_via_retained_pending_target(
        self,
        *,
        category: str,
        selector: str,
        expected_quantity: float,
        source_unit: str | None,
        expected_xactimate_unit: str | None,
        populated_unit: str | None,
        row_count_before: int,
        attempts: int,
        start: float,
        samples: list[dict],
        baseline_rows: list[CommitRowSnapshot],
        after_rows: list[CommitRowSnapshot],
    ) -> "CommitVerification | None":
        """Last-resort corroboration for verify_commit()'s zero-row-
        count-delta timeout, used ONLY there -- never in place of the
        structural delta checks above, which still run first on every
        poll and still fail exactly as strictly as before whenever a
        real (nonzero) delta is observed.

        verify_commit()'s own docstring already establishes that
        commit_item() finalizes an already-present row rather than
        inserting a new one -- the pending row is created at
        activation, well before Ctrl+S. A zero row-count delta AT
        COMMIT is therefore not evidence of failure by itself when a
        trustworthy binding from that earlier activation still exists:
        self._pending_quantity_target already positively identified
        this exact row's identity/activity before quantity was ever
        entered. The rich protected baseline comes from that same
        pre-activation frame. This re-confirms the SAME already-proven
        row as the sole post-commit multiset delta -- it never searches
        for an arbitrary matching row -- and then positively rereads
        identity, activity, and unit at that freshly derived position.
        Any missing/extra/changed row or failed positive check returns
        None and falls through to the existing VERIFICATION_FAILED.
        Phase
        5.27: quantity is deliberately NOT one of those gating checks
        (see verify_commit()'s own docstring, point 6) -- this target
        was already positively bound at activation time, before
        quantity was ever entered, so a disagreeing/unreadable
        post-write quantity OCR read here is advisory evidence only,
        recorded on the returned CommitVerification, never a reason to
        report VERIFICATION_FAILED for a row whose identity and
        deterministic write are otherwise fully corroborated.

        Deliberately restricted to physical_row_delta == 1 (an ordinary
        single-row binding) -- an R&R -/+ pair binding is never
        corroborated here, so R&R reconciliation behavior is completely
        untouched by this method."""
        diagnostic = {
            "attempt": attempts, "elapsed_s": round(time.time() - start, 3),
            "row_count_before": row_count_before, "row_count_after": len(after_rows),
            "pre_activation_protected_baseline_rows": self._safe_zero_delta_rows_diagnostic(baseline_rows),
            "fresh_post_commit_rows": self._safe_zero_delta_rows_diagnostic(after_rows),
            "baseline_alignment_result": None,
            "protected_multiset_reconciliation_result": None,
            "candidate_activation_delta_rows": [],
            "retained_target": self._safe_zero_delta_target_diagnostic(self._pending_quantity_target),
            "identity_activity_match_result": None,
            "duplicate_extra_row_result": None,
            "final_fresh_reread": None,
            "first_rejection_reason": None,
            "final_zero_delta_reconciliation_result": None,
        }

        def reject(reason: str) -> None:
            diagnostic["first_rejection_reason"] = reason
            diagnostic["final_zero_delta_reconciliation_result"] = "rejected"
            self._persist_zero_delta_commit_diagnostic(diagnostic)
            return None

        target = self._pending_quantity_target
        if target is None or target.physical_row_delta != 1:
            return reject("retained_target_missing_or_not_ordinary_single_row")
        expected_code = (
            self._normalized_pair_text(category),
            self._normalized_pair_text(selector),
        )
        if target.identity[:2] != expected_code:
            diagnostic["identity_activity_match_result"] = False
            return reject("retained_target_cat_sel_mismatch")

        rich_activation_baseline = [
            ActivationRowSnapshot(row.category, row.selector, row.description, row.activity)
            for row in baseline_rows
        ]
        baseline_aligned = self._baseline_rows_accounted(
            list(target.activation_baseline_rows), rich_activation_baseline,
        )
        diagnostic["baseline_alignment_result"] = baseline_aligned
        if not baseline_aligned:
            return reject("activation_baseline_not_aligned_with_rich_protected_baseline")

        # Candidate activation, not Ctrl+S, creates the pending row.  The
        # supplied count can therefore already include it.  Reconcile the
        # fresh post-commit rows against the rich pre-activation baseline:
        # all protected rows must survive unchanged and exactly one ordinary
        # delta must remain, matching the target bound at activation.
        if len(after_rows) != len(baseline_rows) + 1:
            diagnostic["duplicate_extra_row_result"] = (
                "missing_row" if len(after_rows) < len(baseline_rows) + 1 else "duplicate_or_extra_rows"
            )
            return reject("post_commit_row_count_not_protected_baseline_plus_one")
        preexisting_unchanged, new_indices = self._order_independent_commit_delta(
            baseline_rows, after_rows,
        )
        diagnostic["protected_multiset_reconciliation_result"] = preexisting_unchanged
        diagnostic["candidate_activation_delta_rows"] = self._safe_zero_delta_rows_diagnostic(
            [after_rows[index] for index in new_indices],
        )
        if not preexisting_unchanged or len(new_indices) != 1:
            diagnostic["duplicate_extra_row_result"] = (
                "protected_rows_changed" if not preexisting_unchanged else "duplicate_or_extra_rows"
            )
            return reject("protected_multiset_not_unchanged_plus_one_unique_delta")
        diagnostic["duplicate_extra_row_result"] = "none"
        row_index = new_indices[0]
        delta_row = after_rows[row_index]
        delta_activation = ActivationRowSnapshot(
            delta_row.category, delta_row.selector,
            delta_row.description, delta_row.activity,
        )
        identity_matches = self._activation_identity(delta_activation) == target.identity
        snapshot_activity = self._activity_token(delta_activation)
        activity_matches = target.activity is None or snapshot_activity == target.activity
        diagnostic["identity_activity_match_result"] = {
            "identity_matches": identity_matches, "activity_matches": activity_matches,
        }
        if not identity_matches:
            return reject("unique_delta_identity_mismatch")
        if not activity_matches:
            return reject("unique_delta_activity_mismatch")

        hwnd = self._ensure_main_window()
        image, offset = self._capture_and_locate(hwnd, attempts=1, delay_s=0)
        if offset is None:
            return reject("final_fresh_grid_relocation_failed")
        row_top = self._shifted_anchor("grid_row_1", offset)[1] + row_index * _GRID_ROW_HEIGHT

        cat_observed, sel_observed = self._read_category_selector_for_verify_commit(image, offset, row_top)
        diagnostic["final_fresh_reread"] = {
            "category": cat_observed, "selector": sel_observed,
            "description": None, "activity": None, "quantity": None,
            "unit_raw": None, "unit_normalized": None,
        }
        if cat_observed != category or sel_observed != selector:
            return reject("final_fresh_cat_sel_mismatch")

        activity_observed = self._read_activation_activity_at(image, offset, row_top)
        diagnostic["final_fresh_reread"]["activity"] = activity_observed
        if target.activity is not None and activity_observed != target.activity:
            return reject("final_fresh_activity_mismatch")

        description_observed = self._read_description_at(image, offset, row_top)
        quantity_observed = self._read_quantity_at(image, offset, row_top)
        unit_raw, unit_normalized = self._read_unit_at(image, offset, row_top)
        diagnostic["final_fresh_reread"].update({
            "description": description_observed, "quantity": quantity_observed,
            "unit_raw": unit_raw, "unit_normalized": unit_normalized,
        })
        # Phase 5.27: quantity is advisory evidence here, not a gate --
        # see this method's own docstring.
        quantity_matched = quantity_observed is not None and quantity_observed == expected_quantity

        populated_unit_vocab = _resolve_observed_unit_vocab(populated_unit)
        unit_source = "populated_field" if populated_unit_vocab is not None else "post_commit_ocr"
        observed_unit_for_check = (
            populated_unit if populated_unit_vocab is not None
            else (unit_normalized or unit_raw)
        )
        unit_result = check_unit_compatibility(source_unit, expected_xactimate_unit, observed_unit_for_check)
        if populated_unit_vocab is None and unit_normalized is not None:
            unit_result.observed_xactimate_unit = unit_raw
        compatibility = self._UNIT_STATE_TO_COMPATIBILITY.get(unit_result.unit_match_state, "review_required")
        if compatibility == "hard_stop":
            return reject("final_fresh_unit_hard_stop")

        samples.append({
            "elapsed_s": round(time.time() - start, 3),
            "retained_pending_target_corroboration": True,
            "after_index": row_index,
            "protected_baseline_reconciled": True,
            "unique_activation_delta_reconfirmed": True,
            "category_observed": cat_observed, "selector_observed": sel_observed,
            "activity_observed": activity_observed, "quantity_observed": quantity_observed,
            "unit_raw": unit_raw, "unit_normalized": unit_normalized, "unit_source": unit_source,
        })
        diagnostic["first_rejection_reason"] = None
        diagnostic["final_zero_delta_reconciliation_result"] = "verified"
        self._persist_zero_delta_commit_diagnostic(diagnostic)
        return CommitVerification(
            trust_state="VERIFIED",
            reason=(
                "row count did not change at commit, but the identity/activity already positively bound "
                "during activation was re-confirmed at its known position -- commit_item() finalizes an "
                "already-present row rather than inserting a new one, so no further row-count growth was expected "
                "(quantity OCR is advisory only; the deterministic write to this positively-identified row is "
                "treated as authoritative)"
            ),
            row_count_before=row_count_before, row_count_after=len(after_rows), row_index=row_index,
            preexisting_rows_unchanged=True,
            category_expected=category, selector_expected=selector,
            category_observed=cat_observed, selector_observed=sel_observed, category_selector_ocr_agrees=True,
            description_observed=description_observed,
            quantity_expected=expected_quantity, quantity_observed=quantity_observed, quantity_matched=quantity_matched,
            unit=unit_result, compatibility=compatibility, compatibility_reason=unit_result.unit_match_reason,
            attempts=attempts, elapsed_s=time.time() - start, samples=samples,
        )

    def delete_existing_item(self, category: str, selector: str) -> None:
        """Not part of the abstract contract. Targeted deletion of a
        SPECIFIC row by CAT/SEL identity, anywhere in the grid -- not
        just the last row (`cancel_current_item()`'s scope). Required
        because a real recovery/cleanup pass may need to remove one
        row out of several without disturbing the others (see
        docs/xactimate-lookup.md Phase 4.6, Stage 3's multi-row
        trials). A context menu opened for one row is never reused for
        another -- every call re-locates the target fresh and opens
        its own menu.

        Verifies, independently of the click succeeding without an
        exception: the target identity is no longer present, exactly
        one row disappeared (not more, not fewer), and every other
        row's identity is unchanged and in the same relative order."""
        before = self.snapshot_grid_identities()
        target_positions = [i for i, (c, s) in enumerate(before) if c == category and s == selector]
        if not target_positions:
            raise AdapterError(f"delete_existing_item(): no row matching {category}/{selector} found in the grid.")
        target_index = target_positions[0]

        hwnd = self._ensure_main_window()
        image, offset = self._capture_and_locate(hwnd)
        if offset is None:
            raise AdapterError("delete_existing_item(): could not locate the grid.")
        row_1_top = self._shifted_anchor("grid_row_1", offset)[1]
        row_top = row_1_top + target_index * _GRID_ROW_HEIGHT
        dx = offset[0]

        # Re-verify the target is still at this exact position
        # immediately before acting -- the grid may have changed
        # between the snapshot above and now.
        cat_now, sel_now = self._read_category_selector_at(image, offset, row_top)
        if cat_now != category or sel_now != selector:
            raise AdapterError(
                f"delete_existing_item(): row at index {target_index} changed between locating "
                f"{category}/{selector} and acting on it (now reads {cat_now}/{sel_now}) -- refusing to act."
            )

        col_l, col_r = _GRID_COLUMNS["description"]
        row_x = (col_l + col_r) // 2 + dx
        row_y = row_top + _GRID_ROW_HEIGHT // 2
        self._open_row_context_menu(hwnd, row_x, row_y)

        popup_hwnd = self._find_context_menu_popup_hwnd(hwnd)
        if popup_hwnd is None or not self._click_delete_via_uia(popup_hwnd):
            self._press_key(0x1B)  # VK_ESCAPE -- dismiss whatever menu is open
            raise AdapterError(f"delete_existing_item(): could not invoke Delete for {category}/{selector}.")
        time.sleep(1.2)

        if self._unexpected_dialog_present():
            self.close_transient_dialogs()
            raise AdapterError(
                f"delete_existing_item(): an unexpected window appeared after deleting {category}/{selector} "
                f"-- refusing to trust the result until this is resolved."
            )

        after = self.snapshot_grid_identities()
        expected = before[:target_index] + before[target_index + 1 :]
        if len(after) != len(before) - 1:
            raise AdapterError(
                f"delete_existing_item(): row count changed by {len(before) - len(after)}, expected exactly 1 "
                f"(before={len(before)}, after={len(after)})."
            )
        if any(c == category and s == selector for c, s in after):
            raise AdapterError(f"delete_existing_item(): {category}/{selector} still present after deletion.")
        if after != expected:
            raise AdapterError(
                f"delete_existing_item(): unexpected change to other rows -- expected {expected}, got {after}."
            )

    def cancel_current_item(self, *, reason: str, caller: str) -> None:
        """Not part of the abstract contract -- used by the
        non-destructive and assisted-selection trials to remove the
        LAST row WITHOUT ever calling commit_item(). For removing a
        specific row out of several, use `delete_existing_item()`
        instead.

        Live investigation found the Delete key alone does NOT remove
        a grid row (an earlier version of this method wrongly assumed
        it did -- see docs/xactimate-lookup.md Phase 4.3). The real
        mechanism is the row's right-click context menu's "Delete"
        item, invoked via `_click_delete_via_uia()` (Phase 4.6) -- see
        that method's docstring for why OCR-based menu-item location
        (Phase 4.4/4.5) was retired rather than patched further: three
        rounds of conservative fixes each closed one OCR failure mode
        and exposed a new one, which is a structural fragility of that
        approach, not a bug count converging on zero.

        Phase 5.5D: `reason`/`caller` are now REQUIRED, not optional --
        a live incident showed this method (via `_cleanup_probe_item()`)
        deleting rows Execute had just successfully committed, with no
        record of why. `reason` must be one of destructive_audit.
        DESTRUCTIVE_REASONS (raises InvalidDestructiveReason otherwise).
        Every call is logged via self._destructive_auditor regardless of
        outcome. Before deleting anything, this refuses (raises
        ProtectedCommittedRowError, still logged) if doing so would
        drop the CURRENT execution context's group below the number of
        rows record_protected_commit() has protected there this
        session -- "the row is last" or "row count exceeds some
        target" is never sufficient justification on its own."""
        if reason not in DESTRUCTIVE_REASONS:
            # Validated BEFORE touching the window/grid at all -- an
            # invalid reason is a pure programming error, independent
            # of live Xactimate state, and must fail the same way
            # whether or not a window happens to be found right now.
            raise InvalidDestructiveReason(
                f"cancel_current_item(): reason {reason!r} is not one of {sorted(DESTRUCTIVE_REASONS)}."
            )
        hwnd = self._ensure_main_window()
        image, offset = self._capture_and_locate(hwnd)
        group = self._execution_context.group
        protected_floor = self._protected_row_ledger.count_for_group(group)

        if offset is None:
            self._destructive_auditor.record(
                context=self._execution_context, method="cancel_current_item", reason=reason, caller=caller,
                target_type="last_grid_row", target_identity=None,
                row_count_before=None, row_identities_before=None,
                row_count_after=None, row_identities_after=None,
                result="refused", exception="grid could not be located",
            )
            raise AdapterError("Could not locate the grid to cancel the current item.")

        row_identities_before = [
            self._read_category_selector_at(image, offset, self._shifted_anchor("grid_row_1", offset)[1] + i * _GRID_ROW_HEIGHT)
            for i in range(self._count_grid_rows(image, offset))
        ]
        geom = self._last_row_geometry(image, offset)
        if geom is None:
            self._destructive_auditor.record(
                context=self._execution_context, method="cancel_current_item", reason=reason, caller=caller,
                target_type="last_grid_row", target_identity=None,
                row_count_before=0, row_identities_before=row_identities_before,
                row_count_after=0, row_identities_after=row_identities_before,
                result="no_op_empty_grid", exception=None,
            )
            return
        row_count_before, last_row_top = geom
        target_identity = row_identities_before[-1] if row_identities_before else None

        if row_count_before - 1 < protected_floor:
            self._destructive_auditor.record(
                context=self._execution_context, method="cancel_current_item", reason=reason, caller=caller,
                target_type="last_grid_row", target_identity=str(target_identity),
                row_count_before=row_count_before, row_identities_before=row_identities_before,
                row_count_after=row_count_before, row_identities_after=row_identities_before,
                result="refused",
                exception=(
                    f"would drop group {group!r} to {row_count_before - 1} row(s), below its "
                    f"{protected_floor} protected committed row(s)"
                ),
            )
            raise ProtectedCommittedRowError(
                f"cancel_current_item(): refusing -- deleting the last row in group {group!r} would drop its "
                f"row count to {row_count_before - 1}, below the {protected_floor} row(s) already protected "
                f"there this session (reason={reason!r}, caller={caller!r}, target={target_identity!r})."
            )

        col_l, col_r = _GRID_COLUMNS["description"]
        dx = offset[0]
        row_x = (col_l + col_r) // 2 + dx
        row_y = last_row_top + _GRID_ROW_HEIGHT // 2

        try:
            self._open_row_context_menu(hwnd, row_x, row_y)

            popup_hwnd = self._find_context_menu_popup_hwnd(hwnd)
            if popup_hwnd is None or not self._click_delete_via_uia(popup_hwnd):
                # Best-effort: dismiss whatever menu is open rather than
                # leaving it hanging over the next call.
                self._press_key(0x1B)  # VK_ESCAPE
                raise AdapterError("cancel_current_item(): could not invoke the 'Delete' context-menu item.")
            time.sleep(1.2)  # empirically: 0.5s was too short and produced a false-negative verification once

            # Live-caught false-positive bug (see docs/xactimate-lookup.md
            # Phase 4.4): if the click misses "Delete" and instead lands on
            # something that opens a different window, that window can
            # visually obscure the grid row at the moment of the post-click
            # capture, making row_count_after read as lower than it really
            # is -- a false "success" that doesn't actually delete
            # anything. Checking for an unexpected window FIRST, and
            # treating its mere presence as a hard failure (not just
            # closing it and re-checking), catches this rather than
            # trusting a row-count read that could have been taken while
            # occluded.
            if self._unexpected_dialog_present():
                self.close_transient_dialogs()
                raise AdapterError(
                    "cancel_current_item(): an unexpected window appeared after the context-menu click "
                    "(the click likely missed 'Delete') -- refusing to trust the row count until this is resolved."
                )

            image_after, offset_after = self._capture_and_locate(hwnd)
            row_count_after = self._count_grid_rows(image_after, offset_after) if offset_after is not None else None
            if row_count_after is None or row_count_after >= row_count_before:
                raise AdapterError(
                    f"cancel_current_item(): row count did not decrease (before={row_count_before}, after={row_count_after})."
                )
        except Exception as exc:
            self._destructive_auditor.record(
                context=self._execution_context, method="cancel_current_item", reason=reason, caller=caller,
                target_type="last_grid_row", target_identity=str(target_identity),
                row_count_before=row_count_before, row_identities_before=row_identities_before,
                row_count_after=None, row_identities_after=None,
                result="failed", exception=repr(exc),
            )
            raise

        row_identities_after = [
            self._read_category_selector_at(image_after, offset_after, self._shifted_anchor("grid_row_1", offset_after)[1] + i * _GRID_ROW_HEIGHT)
            for i in range(row_count_after)
        ] if offset_after is not None else None
        self._destructive_auditor.record(
            context=self._execution_context, method="cancel_current_item", reason=reason, caller=caller,
            target_type="last_grid_row", target_identity=str(target_identity),
            row_count_before=row_count_before, row_identities_before=row_identities_before,
            row_count_after=row_count_after, row_identities_after=row_identities_after,
            result="deleted", exception=None,
        )

    def capture_evidence(self) -> str:
        hwnd = self._ensure_main_window()
        image = self._capture_client_image(hwnd)
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d-%H%M%S")
        safe_query = "".join(c if c.isalnum() else "_" for c in (self._current_query or "none"))[:60]
        path = self.evidence_dir / f"{ts}_{safe_query}.png"
        image.save(path)
        return str(path)

    def close_transient_dialogs(self) -> bool:
        """Not part of the abstract contract -- checks for any owned
        window that isn't the main window, the results popup, or the
        transient 'Loading' overlay, and closes it with Escape if
        found. Returns True if a dialog was found and an Escape was
        sent, False if nothing needed closing. The one real dialog
        observed live (the "Duplicate Item(s)" Yes/No confirmation) was
        confirmed dismissable by clicking its "No" button directly;
        whether Escape maps to that same Cancel-equivalent choice on
        this specific dialog was not independently re-verified this
        session -- see docs/xactimate-lookup.md Phase 4.3 for the
        exact trial this is based on."""
        if not self._unexpected_dialog_present():
            return False
        VK_ESCAPE = 0x1B
        self._press_key(VK_ESCAPE)
        time.sleep(0.5)
        return True

    def recover(self) -> None:
        """Best-effort, never raises. Escape (safe per live testing --
        confirmed to close the results popup without side effects
        across every trial), clear transient state, re-verify the
        expected project is still active."""
        try:
            self.close_transient_dialogs()
        except Exception:
            pass
        try:
            VK_ESCAPE = 0x1B
            self._press_key(VK_ESCAPE)
            time.sleep(0.3)
        except Exception:
            pass
        self._last_dropdown_hwnd = None
        self._last_dropdown_rows = []
        self._last_selected = None
        self._current_query = None

    def snapshot_search_fallback_state(self):
        """Capture read-only proof before a multi-query retrieval sequence."""
        return {
            "candidate_selection_count": self._candidate_selection_count,
            "physical_rows": self._snapshot_activation_rows(require_located=True),
        }

    def verify_search_fallback_state(self, baseline) -> tuple[bool, str]:
        """Fail closed unless another query is safe in the unchanged pane."""
        if self._candidate_selection_count != baseline.get("candidate_selection_count"):
            return False, "a candidate-result row was clicked during the prior attempt"
        if self._last_selected is not None:
            return False, "a candidate remains selected"
        if self._unexpected_dialog_present():
            return False, "a dialog remains open"
        current_rows = self._snapshot_activation_rows(require_located=True)
        if current_rows != baseline.get("physical_rows"):
            return False, "the physical grid changed during the prior attempt"
        return True, "no candidate click, dialog, or physical-row delta was observed"

    def get_adapter_diagnostics(self) -> dict:
        """Not part of the abstract contract -- a read-only status
        snapshot, analogous in spirit to the existing `automation
        diagnostics` CLI command (Phase 4.0), but adapter-scoped."""
        found = None
        try:
            found = self._find_main_window()
        except Exception:
            pass
        foreground = False
        if found is not None:
            ctypes, _ = self._win32()
            user32 = ctypes.windll.user32
            foreground = user32.GetForegroundWindow() == found[0]
        dropdown_hwnd = None
        try:
            dropdown_hwnd = self._find_dropdown_window()
        except Exception:
            pass
        return {
            "main_window_found": found is not None,
            "main_window_hwnd": found[0] if found else None,
            "main_window_title": found[1] if found else None,
            "project_matches": (found[1].strip().lower() == self.expected_project_name.strip().lower()) if found else False,
            "foreground": foreground,
            "dropdown_open": dropdown_hwnd is not None,
            "supports_live_execution": self.supports_live_execution,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }

    #: The client size / DPI this adapter's pixel anchors (_ANCHORS,
    #: group-tree row constants) were calibrated against -- see
    #: _ANCHORS' own module-level docstring. Live automation run
    #: outside this profile cannot trust any fixed-pixel coordinate.
    _VALIDATED_CLIENT_WIDTH = 1920
    _VALIDATED_CLIENT_HEIGHT = 1021
    _VALIDATED_DPI = 96
    #: Small tolerance for minor chrome/scrollbar-state variance
    #: (measured live: 1920x1023 vs. the 1920x1021 calibration
    #: baseline -- a 2px difference from an unrelated scrollbar state,
    #: not a real profile mismatch).
    _DISPLAY_PROFILE_SIZE_TOLERANCE = 6

    def verify_display_profile(self) -> dict:
        """Not part of the abstract contract (Phase 5.2 Stage 10):
        pre-flight safety gate for live execution. Every pixel
        coordinate and OCR crop in this file was calibrated against one
        specific window size/DPI/layout -- running against a
        differently-sized or differently-scaled window would silently
        misdirect every click. This never guesses whether a different
        profile is "close enough"; it reports specific, checkable
        reasons and leaves the caller to decide whether that's blocking.
        Never raises -- an exception here means the environment can't be
        verified, which is itself a blocking result, not a crash."""
        checks: list[str] = []
        blocking: list[str] = []
        try:
            found = self._find_main_window()
        except Exception as exc:
            return {
                "ok": False, "window_found": False, "client_width": None, "client_height": None, "dpi": None,
                "dimensions_match": False, "group_tree_visible": False, "grid_anchor_visible": False,
                "checks": [], "blocking_reasons": [f"Could not enumerate windows: {exc!r}"],
            }
        if found is None:
            return {
                "ok": False, "window_found": False, "client_width": None, "client_height": None, "dpi": None,
                "dimensions_match": False, "group_tree_visible": False, "grid_anchor_visible": False,
                "checks": [], "blocking_reasons": ["Xactimate main window not found -- cannot verify the display profile."],
            }

        hwnd = found[0]
        win32gui = self._win32gui()
        try:
            client_rect = win32gui.GetClientRect(hwnd)
            width, height = client_rect[2] - client_rect[0], client_rect[3] - client_rect[1]
        except Exception as exc:
            width = height = None
            blocking.append(f"Could not read the window's client size: {exc!r}")

        dpi = None
        try:
            ctypes, _ = self._win32()
            dpi = ctypes.windll.user32.GetDpiForWindow(hwnd)
        except Exception:
            pass  # not fatal -- DPI check is skipped, not failed, if unavailable

        dims_ok = (
            width is not None and height is not None
            and abs(width - self._VALIDATED_CLIENT_WIDTH) <= self._DISPLAY_PROFILE_SIZE_TOLERANCE
            and abs(height - self._VALIDATED_CLIENT_HEIGHT) <= self._DISPLAY_PROFILE_SIZE_TOLERANCE
        )
        dpi_ok = dpi is None or dpi == self._VALIDATED_DPI
        checks.append(f"client_size={width}x{height} (validated {self._VALIDATED_CLIENT_WIDTH}x{self._VALIDATED_CLIENT_HEIGHT})")
        checks.append(f"dpi={dpi} (validated {self._VALIDATED_DPI})")
        if not dims_ok:
            blocking.append(
                f"Client size {width}x{height} does not match the validated profile "
                f"({self._VALIDATED_CLIENT_WIDTH}x{self._VALIDATED_CLIENT_HEIGHT}, +/-{self._DISPLAY_PROFILE_SIZE_TOLERANCE}px) "
                f"-- pixel-based automation cannot be trusted at a different window size/scale."
            )
        if not dpi_ok:
            blocking.append(f"DPI {dpi} does not match the validated profile ({self._VALIDATED_DPI}) -- re-run calibration before live execution.")

        try:
            image = self._capture_client_image(hwnd)
        except Exception as exc:
            blocking.append(f"Could not capture the window client area: {exc!r}")
            return {
                "ok": False, "window_found": True, "client_width": width, "client_height": height, "dpi": dpi,
                "dimensions_match": dims_ok and dpi_ok, "group_tree_visible": False, "grid_anchor_visible": False,
                "checks": checks, "blocking_reasons": blocking,
            }

        group_tree_visible = self._locate_group_tree_header(image) is not None
        checks.append(f"group_tree_header_visible={group_tree_visible}")
        if not group_tree_visible:
            blocking.append("Could not locate the group tree ('Group' column header) -- group control cannot be trusted.")

        grid_anchor_visible = self._anchor_offset(image) is not None
        checks.append(f"grid_anchor_visible={grid_anchor_visible}")
        if not grid_anchor_visible:
            blocking.append("Could not locate the grid's 'Cat' column header -- item search/entry targeting cannot be trusted.")

        return {
            "ok": dims_ok and dpi_ok and group_tree_visible and grid_anchor_visible,
            "window_found": True,
            "client_width": width,
            "client_height": height,
            "dpi": dpi,
            "dimensions_match": dims_ok and dpi_ok,
            "group_tree_visible": group_tree_visible,
            "grid_anchor_visible": grid_anchor_visible,
            "checks": checks,
            "blocking_reasons": blocking,
        }

    # ------------------------------------------------------------------
    # Group control (Phase 5.1) -- not part of the abstract contract.
    # The group tree's own content is NOT UI-Automation-accessible (same
    # `NULL COM pointer access` limitation as the main window's grid --
    # see docs/build-estimate.md Phase 5.1 Stage 3), so this uses the
    # same pixel/OCR methodology as everything else in this file, with
    # its own self-contained anchor (the tree's "Group" column header),
    # never the grid's "Cat" anchor -- that one was found live-unreliable
    # whenever the grid has zero rows, which is exactly the common case
    # group operations run in.
    # ------------------------------------------------------------------

    def _locate_group_tree_header(self, image) -> tuple[int, int, int, int] | None:
        return self._locate_label(image, "Group", prefer="topmost")

    def _group_tree_row_xy(self, header_pos: tuple[int, int], row_index: int) -> tuple[int, int]:
        left, top = header_pos[0], header_pos[1]
        return (
            left + self._GROUP_TREE_CLICK_DX,
            top + self._GROUP_TREE_ROW_TEXT_TOP_DY + row_index * self._GROUP_TREE_ROW_HEIGHT + self._GROUP_TREE_CLICK_DY_OFFSET,
        )

    def _group_tree_row_crop_top(self, header_top: int, row_index: int) -> int:
        return (
            header_top + self._GROUP_TREE_ROW_TEXT_TOP_DY + row_index * self._GROUP_TREE_ROW_HEIGHT
            - self._GROUP_TREE_ROW_CROP_MARGIN_TOP
        )

    def _ocr_group_tree_name_crop(self, crop) -> str:
        """Read one group-name crop, retrying only a blank selected row.

        Live-caught on the selected Fence row: the selection highlight's
        full-width border makes Tesseract PSM 7 return an empty string even
        though the crop geometry and glyph pixels are correct. The ordinary
        pass remains first and unchanged. Only when it is blank, remove the
        crop-relative highlight margins and retry the same pixels once. No
        name, row index, or screen coordinate is inferred by this fallback.
        """
        scaled = crop.resize((crop.width * 4, crop.height * 4))
        text = self._ocr_text(scaled, psm=7).strip()
        if text or crop.height <= 4:
            return text
        interior = crop.crop((0, 3, crop.width, crop.height - 1))
        interior = interior.resize((interior.width * 4, interior.height * 4))
        return self._ocr_text(interior, psm=7).strip()

    #: Wheel-scroll "clicks" (each WHEEL_DELTA=120) sent to scroll the
    #: group tree panel back to its top -- enough to clear any drift
    #: observed live (a handful of rows), bounded so this never becomes
    #: an unbounded scroll loop.
    _GROUP_TREE_SCROLL_RESET_CLICKS = 6

    def _scroll_group_tree_to_top(self, hwnd: int) -> None:
        """Not part of the abstract contract. Live-caught (Phase 5.3):
        unlike the main grid (reset via `_reset_scroll_state()`'s Items-
        tab click), the group tree panel has its OWN, independent
        vertical scroll position that nothing else resets -- observed
        live scrolled far enough down mid-pilot-run (after several
        groups existed) that the "Group"/"Subtotal" header scrolled
        completely out of the captured client area, making every
        group-tree operation fail with "could not locate the group
        tree" even though the tree itself was fine. A real mouse-wheel-
        up, sent to the tree panel's approximate screen position, is
        what actually resets it (confirmed live) -- there is no
        pixel-free alternative since the tree is not UI-Automation-
        accessible (see this class's module docstring). Called
        defensively at the start of every group-tree entry point, never
        assumed unnecessary."""
        ctypes, _ = self._win32()
        user32 = ctypes.windll.user32
        ox, oy = self._get_client_origin(hwnd)
        # Anywhere over the tree panel's known horizontal/vertical
        # range is sufficient -- the wheel event targets whatever
        # control is under the cursor, not a click target needing
        # pixel precision.
        user32.SetCursorPos(ox + 350, oy + 150)
        time.sleep(0.05)
        MOUSEEVENTF_WHEEL = 0x0800
        WHEEL_DELTA = 120
        for _ in range(self._GROUP_TREE_SCROLL_RESET_CLICKS):
            user32.mouse_event(MOUSEEVENTF_WHEEL, 0, 0, WHEEL_DELTA, 0)
            time.sleep(0.05)
        time.sleep(0.2)

    def snapshot_group_names(self, max_rows: int = 24) -> list[str]:
        """Not part of the abstract contract. Returns the group tree's
        rows top-to-bottom, OCR'd -- row 0 is always the project root
        (returned as `expected_project_name` verbatim, never OCR'd: it's
        immutable and OCR noise on it is not informative), rows 1+ are
        child groups, read fresh every call (never cached -- matches
        `snapshot_grid_identities()`'s own contract). Returns `[]` if the
        tree header can't be located.

        Live-caught (Phase 5.7B): the previous default (8, i.e. 7 child
        groups) silently truncated the row list for a real PDF needing
        MORE groups -- running the full aranda-insurance-v3 plan (9
        groups), the 8th and 9th groups (Debris Removal, Labor Minimums
        Applied) were never even present in the returned list, so every
        caller (select_group(), ensure_group(), _verify_group_once())
        reported "not found" even though the groups genuinely existed
        moments after being created. Not a matching bug -- the rows
        were simply never read. Bumped with real headroom; OCR-ing a
        handful of extra empty rows past the real content is cheap and
        already handled gracefully everywhere a "row" turns out to be
        garbage/blank (never matches a real group name)."""
        hwnd = self._ensure_main_window()
        image = self._capture_client_image(hwnd)
        return self._snapshot_group_names_from_image(image, max_rows=max_rows)

    def _snapshot_group_names_from_image(self, image, *, max_rows: int = 24) -> list[str]:
        """Read group names and geometry from one immutable capture.

        Group verification must correlate row identity, selection styling,
        Items/Search readiness, and grid geometry on the same frame.  The
        public snapshot method intentionally takes a fresh capture; this
        helper prevents those independent facts from being accidentally
        combined across different UI states.
        """
        header = self._locate_group_tree_header(image)
        if header is None:
            return []
        left, top = header[0], header[1]
        rows = [self.expected_project_name]
        for i in range(1, max_rows):
            row_top = self._group_tree_row_crop_top(top, i)
            crop = image.crop((
                left + self._GROUP_TREE_TEXT_DX, row_top,
                left + self._GROUP_TREE_TEXT_DX + self._GROUP_TREE_TEXT_WIDTH, row_top + self._GROUP_TREE_ROW_CROP_HEIGHT,
            ))
            # Phase 5.18 (live-caught): this is the SAME ~18px-tall
            # native-resolution crop _ocr_group_tree_row_text() was
            # already fixed for in Phase 5.15 -- a separate, duplicate
            # inline crop+OCR call here never got the same fix. Live-
            # reproduced: a real "Fencing" row (5th row, after two
            # nested Elevation children) read as just "ee" here while
            # _ocr_group_tree_row_text() on the identical crop position
            # correctly read "is Fencing | ee" -- same crop rectangle,
            # only the missing upscale differs. See that method's own
            # docstring for the full live-caught evidence and the 4x
            # choice (a wider sweep found 6x+ actually makes recognition
            # worse, so this is deliberately not "more is better").
            rows.append(self._ocr_group_tree_name_crop(crop))
        return rows

    _GROUP_TREE_SELECTION_MIN_BACKGROUND_DISTANCE = 35
    _GROUP_TREE_SELECTION_LINE_COLOR_TOLERANCE = 18
    _GROUP_TREE_SELECTION_MIN_CHROMA = 45
    _GROUP_TREE_SELECTION_MIN_OVERLAP_RUN = 32

    def _group_tree_row_has_selection_boundary(
        self, image, header_pos: tuple[int, int], row_index: int,
    ) -> bool:
        """Prove the row's live focus rectangle from relative pixel structure.

        Xactimate draws a long matching top/bottom boundary around the selected
        tree row.  Detection is intentionally color-agnostic: it derives edge
        color candidates from this row, requires a substantial continuous
        overlap of the same chromatic candidate on both edges, and requires
        strong separation from the row interior.  The boundary can be split by
        dynamic column geometry and its full-row coverage changes with nesting
        depth and tree content, so it need not be the dominant color or occupy
        a fixed percentage of the row.  A single average color, fixed RGB value,
        fixed indentation, or absolute screen coordinate is never used.
        """
        from collections import Counter

        left, top = header_pos[0], header_pos[1]
        row_top = self._group_tree_row_crop_top(top, row_index)
        row_left = max(0, left - 4)
        row_right = min(image.width, left + self._GROUP_TREE_TEXT_WIDTH - 10)
        if row_right - row_left < 80:
            return False
        rgb = image.convert("RGB")

        top = Counter(rgb.getpixel((x, row_top)) for x in range(row_left, row_right))
        interior = Counter(
            rgb.getpixel((x, row_top + self._GROUP_TREE_ROW_CROP_HEIGHT // 2))
            for x in range(row_left, row_right)
        )
        interior_color = interior.most_common(1)[0][0]

        def distance(a, b) -> int:
            return sum(abs(int(x) - int(y)) for x, y in zip(a, b))

        for top_color, _top_count in top.most_common():
            if max(top_color) - min(top_color) < self._GROUP_TREE_SELECTION_MIN_CHROMA:
                continue
            if distance(top_color, interior_color) < self._GROUP_TREE_SELECTION_MIN_BACKGROUND_DISTANCE:
                continue
            overlap_run = 0
            longest_overlap_run = 0
            for x in range(row_left, row_right):
                top_matches = distance(rgb.getpixel((x, row_top)), top_color) <= self._GROUP_TREE_SELECTION_LINE_COLOR_TOLERANCE
                bottom_matches = distance(
                    rgb.getpixel((x, row_top + self._GROUP_TREE_ROW_CROP_HEIGHT - 2)), top_color,
                ) <= self._GROUP_TREE_SELECTION_LINE_COLOR_TOLERANCE
                if top_matches and bottom_matches:
                    overlap_run += 1
                    longest_overlap_run = max(longest_overlap_run, overlap_run)
                else:
                    overlap_run = 0
            if longest_overlap_run >= self._GROUP_TREE_SELECTION_MIN_OVERLAP_RUN:
                return True
        return False

    #: Grand Total's own label+value block is FIXED left-sidebar chrome
    #: (not part of the scrollable grid content pane), live-measured at
    #: client-relative (30, 92)-(86, 108) for the value -- see
    #: docs/build-estimate.md Phase 5.4.
    _GRAND_TOTAL_VALUE_BOX = (18, 86, 140, 112)
    #: The "Saved"/"Unsaved changes" indicator, next to the page title
    #: -- also fixed chrome. Wide enough to catch "Unsaved changes" in
    #: full, not just "Saved".
    _SAVED_INDICATOR_BOX = (445, 20, 620, 45)

    def _read_grand_total_text(self) -> str:
        """Not part of the abstract contract (Phase 5.4). Raw OCR text
        of the Grand Total value -- never parsed to a float and
        silently trusted; callers compare this text (fuzzy-tolerant,
        like every other OCR'd field here) against a prior capture of
        the SAME field, not against a guessed numeric value."""
        hwnd = self._ensure_main_window()
        image = self._capture_client_image(hwnd)
        crop = image.crop(self._GRAND_TOTAL_VALUE_BOX)
        return self._ocr_text(crop, psm=7).strip()

    def _read_saved_state(self) -> bool | None:
        """Not part of the abstract contract (Phase 5.4). Returns
        True/False for a confidently-read Saved/Unsaved indicator, or
        None if neither can be confidently distinguished -- callers
        must treat None as "not confirmed saved", never as True by
        default (Phase 5.1's standing principle: never silently assume
        a positive result). "unsaved" is checked FIRST and with
        priority: "saved" is a literal substring of "unsaved", so
        checking "saved" first would misclassify an unsaved estimate."""
        hwnd = self._ensure_main_window()
        image = self._capture_client_image(hwnd)
        crop = image.crop(self._SAVED_INDICATOR_BOX)
        text = self._ocr_text(crop, psm=7).strip().lower().replace(" ", "")
        if not text:
            return None
        unsaved_ratio = self._best_window_fuzzy_ratio("unsavedchanges", text)
        if unsaved_ratio >= self._GROUP_NAME_FUZZY_MATCH_THRESHOLD:
            return False
        saved_ratio = self._best_window_fuzzy_ratio("saved", text)
        if saved_ratio >= self._GROUP_NAME_FUZZY_MATCH_THRESHOLD:
            return True
        return None

    def _read_group_subtotal_text(self, image, header_pos: tuple[int, int], row_index: int) -> str:
        """Not part of the abstract contract (Phase 5.4). Raw OCR text
        of one group row's Subtotal cell -- a text-comparison sibling
        of `_group_subtotal_pixel_count()` (which only detects
        "something changed", not what). Uses the SAME row-position
        formula as every other group-tree read in this file."""
        left, top = header_pos[0], header_pos[1]
        subtotal_header = self._locate_label(image, "Subtotal", prefer="topmost")
        subtotal_left = subtotal_header[0] if subtotal_header is not None else left + 168
        row_top = self._group_tree_row_crop_top(top, row_index)
        crop = image.crop((subtotal_left, row_top, subtotal_left + 130, row_top + self._GROUP_TREE_ROW_CROP_HEIGHT))
        return self._ocr_text(crop, psm=7).strip()

    def _snapshot_grid_rows_detailed(self) -> list[GroupRowSnapshot]:
        """Not part of the abstract contract (Phase 5.4). Every row
        CURRENTLY in the grid (whatever group is presently selected),
        with category/selector/quantity/unit -- a superset of
        `snapshot_grid_identities()`'s (category, selector)-only
        tuples, built for baseline/reconciliation purposes where a
        quantity or unit change with no identity change would
        otherwise go undetected."""
        hwnd = self._ensure_main_window()
        image, offset = self._capture_and_locate(hwnd)
        if offset is None:
            return []
        geom = self._last_row_geometry(image, offset)
        if geom is None:
            return []
        row_count, _ = geom
        row_1_top = self._shifted_anchor("grid_row_1", offset)[1]
        rows = []
        for i in range(row_count):
            row_top = row_1_top + i * _GRID_ROW_HEIGHT
            category, selector = self._read_category_selector_at(image, offset, row_top)
            quantity = self._read_quantity_at(image, offset, row_top)
            raw_unit, _normalized_unit = self._read_unit_at(image, offset, row_top)
            rows.append(GroupRowSnapshot(
                category=category, selector=selector,
                quantity_text=(str(quantity) if quantity is not None else None),
                unit_text=raw_unit,
            ))
        return rows

    def capture_estimate_baseline(self, group_names: list[str]) -> EstimateBaseline:
        """Not part of the abstract contract (Phase 5.4). Captures a
        full structural + financial snapshot across every named group:
        row identities/quantities/units, each group's own Subtotal
        text, and Grand Total -- explicit group names, never
        auto-discovered, matching this file's standing "never guess"
        convention for group operations. Live-caught (Phase 5.3): a
        cleanup check that only inspects the currently-active group's
        row count can miss real financial residue sitting in a
        DIFFERENT group (or a row that reads as visually empty while
        still carrying value) -- this snapshot is deliberately built to
        make that class of gap structurally impossible to miss on
        comparison, not just less likely."""
        hwnd = self._ensure_main_window()
        group_rows: dict[str, list[GroupRowSnapshot]] = {}
        group_subtotal_text: dict[str, str] = {}
        for name in group_names:
            self.select_group(name)
            group_rows[name] = self._snapshot_grid_rows_detailed()
            image = self._capture_client_image(hwnd)
            header = self._locate_group_tree_header(image)
            if header is not None:
                rows = self.snapshot_group_names()
                idx = self._find_group_row(rows, name)
                group_subtotal_text[name] = self._read_group_subtotal_text(image, header, idx) if idx is not None else ""
            else:
                group_subtotal_text[name] = ""
        return EstimateBaseline(
            group_names=list(group_names),
            group_rows=group_rows,
            group_subtotal_text=group_subtotal_text,
            grand_total_text=self._read_grand_total_text(),
            saved=self._read_saved_state(),
            captured_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        )

    @staticmethod
    def _canonicalize_financial_text(text: str) -> str:
        """Live-caught (Phase 5.4): a group Subtotal cell that is
        genuinely blank (no real dollar value) OCR's as short, near-
        random noise -- confirmed live reading two DIFFERENT 2-
        character garbage strings ("dy", then "ni") from the SAME
        physically-blank cell across two captures a few seconds apart,
        with nothing on screen actually changing. An exact-match
        comparison of that raw noise is a false positive waiting to
        happen on every reconciliation run, not a rare edge case.
        Since any REAL dollar value always contains at least one
        digit, collapsing every digit-free reading to one canonical
        empty string preserves the strict, digit-sensitive comparison
        `_text_fields_match()` needs (a real value appearing where
        there was none is still a change: no digits -> has digits) while
        treating noise-vs-noise as the non-event it actually is.

        Live-caught again (Phase 5.4 Stage 10): a genuinely empty/zero
        cell doesn't ALWAYS OCR as digit-free noise -- one read landed
        on "$0.0", a real-looking string that WOULD have compared
        unequal to a digit-free noise reading ("dy") of the SAME blank
        cell, a false residue mismatch on an actually-clean group. A
        reading whose only digit/decimal-point characters are zeros
        (0, 00, 0.0, 0.00, ...) is exactly as much "no value" as noise
        is, and also collapses to "". Any OTHER digit still forces the
        full comparison below -- this cannot mask a real nonzero
        change."""
        if not any(ch.isdigit() for ch in text):
            return ""
        digits_and_dot = "".join(ch for ch in text if ch.isdigit() or ch == ".")
        if digits_and_dot and set(digits_and_dot) <= {"0", "."}:
            return ""
        return text

    @staticmethod
    def _text_fields_match(a: str, b: str) -> bool:
        """EXACT (whitespace/case-normalized, digit-noise-canonicalized)
        comparison for two OCR reads of what should be the SAME
        financial/quantity field at two points in time.

        Live-caught (Phase 5.4): a fuzzy sliding-window comparison --
        the right tool for longer labels like group names, where a
        single dropped/garbled character in an 8+ character word is
        the documented failure mode -- is dangerously lenient on the
        SHORT strings financial fields actually are: `"$0.00"` vs.
        `"$50.00"` scored 0.91 against the same 0.75 threshold used
        for group names, well above it, because a short target gives a
        sliding window too much room to find a coincidentally-close
        overlap. That would have silently passed reconciliation on
        exactly the class of bug this feature exists to catch (Phase
        5.3's real, undetected financial residue). For a
        cleanup-verification gate, a false POSITIVE (flagging an
        unchanged field as different, e.g. from a one-off OCR misread)
        is far cheaper than a false NEGATIVE (missing a real financial
        change) -- so this is deliberately strict, not fuzzy. Both
        empty (after digit-noise canonicalization) is a match (nothing
        there, both times)."""
        a_norm = WindowsXactimateAdapter._canonicalize_financial_text(a.strip().lower().replace(" ", ""))
        b_norm = WindowsXactimateAdapter._canonicalize_financial_text(b.strip().lower().replace(" ", ""))
        return a_norm == b_norm

    def verify_estimate_matches_baseline(self, baseline: EstimateBaseline) -> ReconciliationResult:
        """Not part of the abstract contract (Phase 5.4). Re-captures
        the same fields `capture_estimate_baseline()` did and compares
        every one -- row identities, row count, quantities, units,
        every named group's Subtotal text, Grand Total, and saved
        state. `ok` is True only when ALL of them match; otherwise
        `mismatches` names each one that didn't, so a failed cleanup
        reports exactly what's still wrong rather than a bare False.
        Never raises -- an inspection failure (e.g. a group that can no
        longer be selected) is itself reported as a mismatch, not an
        exception that aborts the check."""
        mismatches: list[str] = []

        current_grand_total = self._read_grand_total_text()
        if not self._text_fields_match(baseline.grand_total_text, current_grand_total):
            mismatches.append(f"Grand Total: baseline={baseline.grand_total_text!r} now={current_grand_total!r}")

        current_saved = self._read_saved_state()
        if current_saved is not True:
            mismatches.append(f"Saved state not confirmed (read: {current_saved!r}) -- project must be Saved.")

        for name in baseline.group_names:
            try:
                self.select_group(name)
            except Exception as exc:
                mismatches.append(f"Group {name!r}: could not select for verification ({exc!r}).")
                continue

            hwnd = self._ensure_main_window()
            image = self._capture_client_image(hwnd)
            header = self._locate_group_tree_header(image)
            if header is None:
                mismatches.append(f"Group {name!r}: could not locate the group tree to verify its subtotal.")
            else:
                rows = self.snapshot_group_names()
                idx = self._find_group_row(rows, name)
                current_subtotal = self._read_group_subtotal_text(image, header, idx) if idx is not None else ""
                baseline_subtotal = baseline.group_subtotal_text.get(name, "")
                if not self._text_fields_match(baseline_subtotal, current_subtotal):
                    mismatches.append(f"Group {name!r} subtotal: baseline={baseline_subtotal!r} now={current_subtotal!r}")

            baseline_rows = baseline.group_rows.get(name, [])
            current_rows = self._snapshot_grid_rows_detailed()
            if len(current_rows) != len(baseline_rows):
                mismatches.append(f"Group {name!r} row count: baseline={len(baseline_rows)} now={len(current_rows)}")
            else:
                for i, (b_row, c_row) in enumerate(zip(baseline_rows, current_rows)):
                    if (b_row.category, b_row.selector) != (c_row.category, c_row.selector):
                        mismatches.append(f"Group {name!r} row {i}: identity baseline={(b_row.category, b_row.selector)} now={(c_row.category, c_row.selector)}")
                    if not self._text_fields_match(b_row.quantity_text or "", c_row.quantity_text or ""):
                        mismatches.append(f"Group {name!r} row {i}: quantity baseline={b_row.quantity_text!r} now={c_row.quantity_text!r}")
                    if not self._text_fields_match(b_row.unit_text or "", c_row.unit_text or ""):
                        mismatches.append(f"Group {name!r} row {i}: unit baseline={b_row.unit_text!r} now={c_row.unit_text!r}")

        return ReconciliationResult(ok=(len(mismatches) == 0), mismatches=mismatches)

    #: Minimum difflib.SequenceMatcher ratio for a fuzzy (non-substring)
    #: group-name match to count. Live-measured (Phase 5.2 Stage 2, Phase
    #: 5.3): real matches with 1-2 dropped/garbled characters (OCR
    #: consistently read "Front Elevation" as "frontelevaion", and
    #: separately "Exterior" as "eteior" -- the leading 'x' entirely
    #: absent, not just misread -- on every fresh capture, not transient
    #: noise) score 0.80-0.96 against their best-matching window; two
    #: genuinely different group names score 0.17-0.38. 0.75 sits safely
    #: between those.
    _GROUP_NAME_FUZZY_MATCH_THRESHOLD = 0.75

    @staticmethod
    def _best_window_fuzzy_ratio(needle: str, haystack: str) -> float:
        """Live-caught (Phase 5.3): comparing needle against the WHOLE
        haystack string penalizes a real match when the row's OCR text
        carries a lot of UNRELATED surrounding noise (icon glyphs
        misread as stray characters) beyond the name itself -- measured
        live, "exterior" against the full noisy row text scored only
        0.52 (would incorrectly fail), while the same needle against
        just the best-matching same-length-ish WINDOW of that same
        haystack scored 0.80. Slides a window of length
        len(needle)-1..+1 across haystack and returns the best ratio
        found -- immune to irrelevant text elsewhere in the row, unlike
        a whole-string comparison."""
        import difflib

        if not haystack:
            return 0.0
        if len(haystack) <= len(needle):
            return difflib.SequenceMatcher(None, needle, haystack).ratio()
        best = 0.0
        for width in (len(needle) - 1, len(needle), len(needle) + 1):
            if width <= 0 or width > len(haystack):
                continue
            for start in range(0, len(haystack) - width + 1):
                ratio = difflib.SequenceMatcher(None, needle, haystack[start : start + width]).ratio()
                if ratio > best:
                    best = ratio
        return best

    @staticmethod
    def _group_name_matches(ocr_text: str, group_name: str) -> bool:
        """OCR on the group tree is noisy (icons/gridlines bleed into
        the crop -- see docs/build-estimate.md Phase 5.1 Stage 3), so
        this starts as a whitespace-insensitive substring match, not
        equality -- the same tolerance level established for every
        other OCR'd label in this file.

        Live-caught (Phase 5.2 Stage 2, Phase 5.3): substring
        containment alone misses real, CONSISTENT (not transient-noise)
        misreads -- see `_GROUP_NAME_FUZZY_MATCH_THRESHOLD`'s docstring
        for two live-reproduced examples. A caller trusting
        substring-only matching here (e.g. `delete_group()`,
        `ensure_group()`'s post-creation check) would conclude the
        group doesn't exist and silently report success/failure
        incorrectly. Falls back to `_best_window_fuzzy_ratio()` when
        substring containment fails.

        Live-caught (Phase 5.7A): the whole-string fuzzy fallback alone
        is unsafe for a FAMILY of multi-word names sharing one common
        word -- "Rear Elevation" and "Left Elevation" each scored
        0.77/0.81 against an EXISTING, genuinely different "Front
        Elevation" row (both above threshold), because "elevation" (9
        of ~14 characters) dominates the concatenated-string ratio
        regardless of how different the distinguishing leading word is.
        This let ensure_group()/select_group() silently target the
        wrong group for a same-family sibling name that was never
        actually in the tree -- a genuine wrong-group-write risk
        (reproduced live building the real aranda-insurance-v3 group
        set). Fixed: for a `group_name` with 2+ words, EVERY word must
        ALSO individually clear the threshold somewhere in the haystack
        (via the same window-search, so it stays robust to OCR text
        that has lost its own spaces) -- not just the blended whole-
        string ratio. Legitimate OCR noise on the correct name still
        passes easily (each real word individually scores 0.86-1.0 in
        the live-measured cases this threshold was calibrated against);
        a different sibling name whose distinguishing word doesn't
        match anywhere (e.g. "rear"/"left" score 0.57/0.67 against
        "frontelevation", well under threshold) now correctly fails."""
        needle = group_name.strip().lower().replace(" ", "")
        haystack = ocr_text.strip().lower().replace(" ", "")
        if not needle:
            return False
        if needle in haystack:
            return True
        ratio = WindowsXactimateAdapter._best_window_fuzzy_ratio(needle, haystack)
        if ratio < WindowsXactimateAdapter._GROUP_NAME_FUZZY_MATCH_THRESHOLD:
            return False
        words = [w for w in group_name.strip().lower().split() if w]
        if len(words) < 2:
            return True
        return all(
            WindowsXactimateAdapter._best_window_fuzzy_ratio(word, haystack)
            >= WindowsXactimateAdapter._GROUP_NAME_FUZZY_MATCH_THRESHOLD
            for word in words
        )

    def _find_group_row(self, rows: list[str], group_name: str) -> int | None:
        """Live-caught (Phase 5.4 Stage 8): a first-match scan is unsafe
        for short `group_name` needles (e.g. an unreviewed suggested
        Xactimate group name like "Roof" derived from a section named
        "Dwelling Roof"). Reproduced live: "Roof" scored an exact
        substring match against the correct "Dwelling Roof" row, but
        ALSO scored 0.857 (above `_GROUP_NAME_FUZZY_MATCH_THRESHOLD`)
        against an unrelated earlier row, "Utility Room" -- sharing
        "Roo" with "Room". A first-match scan picked the wrong,
        earlier row and committed a real financial row into the wrong
        group. An exact (whitespace/case-insensitive) substring match
        is unambiguous and always preferred, searched in row order;
        only when NO row contains an exact substring match do we fall
        back to the single best (highest-ratio) fuzzy match across all
        rows, never the first one to merely clear the threshold."""
        needle = group_name.strip().lower().replace(" ", "")
        for i, text in enumerate(rows):
            haystack = text.strip().lower().replace(" ", "")
            if needle and needle in haystack:
                return i
        best_index: int | None = None
        best_ratio = 0.0
        for i, text in enumerate(rows):
            if self._group_name_matches(text, group_name):
                haystack = text.strip().lower().replace(" ", "")
                ratio = self._best_window_fuzzy_ratio(needle, haystack)
                if ratio > best_ratio:
                    best_ratio = ratio
                    best_index = i
        return best_index

    def _matching_group_rows(self, rows: list[str], group_name: str) -> list[int]:
        """Phase 5.7: returns EVERY row index that plausibly matches
        `group_name`, unlike `_find_group_row()` (which silently
        resolves to a single best guess). Exact (whitespace/case-
        insensitive) substring matches are strongly preferred -- if any
        exist, only those are returned; fuzzy matches are considered
        only when no exact-substring match exists at all. Used wherever
        more than one plausible match must be surfaced as a genuine
        ambiguity rather than silently resolved."""
        needle = group_name.strip().lower().replace(" ", "")
        if not needle:
            return []
        exact = [i for i, text in enumerate(rows) if needle in text.strip().lower().replace(" ", "")]
        if exact:
            return exact
        return [i for i, text in enumerate(rows) if self._group_name_matches(text, group_name)]

    def _find_unique_group_row(self, rows: list[str], group_name: str) -> int | None:
        """Phase 5.7: returns the row index for `group_name` ONLY when
        it identifies exactly one row. Returns None when no row
        matches (not found -- not itself an error, callers decide what
        to do). Raises AdapterError when MORE than one row matches --
        "the group exists somewhere in the tree, identifiable by name"
        is no longer sufficient once creation is allowed to land at any
        depth (Phase 5.7): a caller that silently picked one of several
        same-named groups could select/verify/write into the wrong one.
        Ambiguity must fail closed here, not be resolved by a best-
        ratio guess."""
        matches = self._matching_group_rows(rows, group_name)
        if len(matches) > 1:
            raise AdapterError(
                f"{len(matches)} groups in the tree match {group_name!r} ({[rows[i] for i in matches]!r}) -- "
                f"refusing to guess which one is the intended group."
            )
        return matches[0] if matches else None

    def _open_group_tree_context_menu(self, hwnd: int, header_pos: tuple[int, int], row_index: int) -> list:
        """Left-clicks the row (to select/focus it), forces a repaint
        (a screenshot capture -- NOT a plain sleep), then right-clicks
        the SAME position and returns the raw UIA menu item elements.
        Live-caught (Phase 5.1 Stage 3): a right-click alone, or a
        left-click followed only by `time.sleep()` (tried up to 1.5s),
        do NOT reliably make Delete operate on the right-clicked row --
        two independent reproducible live failures each way. What DOES
        work, twice reproduced: force a real repaint (PrintWindow via a
        screenshot) between the two clicks. The selection change
        apparently only commits internally once the window processes a
        paint cycle; idle sleeping never forces that on its own.
        Raises AdapterError if the menu doesn't appear or doesn't have
        the expected structural shape (never guesses)."""
        xy = self._group_tree_row_xy(header_pos, row_index)
        last_count = None
        for attempt in range(6):
            self._click_client(hwnd, *xy)
            time.sleep(0.3)
            self._capture_client_image(hwnd)  # force the repaint -- see docstring
            time.sleep(0.3)
            self._open_row_context_menu(hwnd, *xy)
            time.sleep(0.5)
            popup_hwnd = self._find_context_menu_popup_hwnd(hwnd)
            if popup_hwnd is None:
                raise AdapterError("Group tree context menu did not appear.")
            uia, UIA = self._uia()
            element = uia.ElementFromHandle(popup_hwnd)
            walker = uia.RawViewWalker
            menu_root = walker.GetFirstChildElement(element)
            items = []
            child = walker.GetFirstChildElement(menu_root) if menu_root else None
            while child is not None:
                items.append(child)
                try:
                    child = walker.GetNextSiblingElement(child)
                except Exception:
                    break
            # Live-caught (Phase 5.1): the raw UIA walk occasionally
            # yields one extra child with the SAME bounding rectangle as
            # another real item (a phantom/duplicate node -- the popup
            # window's own pixel size is identical whether this happens
            # or not, confirming it's a UIA enumeration artifact, not a
            # real extra menu entry). Deduplicating by exact rect before
            # counting makes the structural-index check robust to it.
            deduped = []
            seen_rects = set()
            for it in items:
                try:
                    r = it.CurrentBoundingRectangle
                except Exception:
                    continue  # stale/invalid COM reference -- same class of phantom node
                key = (r.left, r.top, r.right, r.bottom)
                if key in seen_rects:
                    continue
                seen_rects.add(key)
                deduped.append(it)
            items = deduped
            if len(items) == self._GROUP_MENU_EXPECTED_ITEM_COUNT:
                return items
            # Live-caught (Phase 5.1): a transient wrong item count (seen
            # once: 27 instead of 26) self-corrected on the very next
            # fresh open -- dismiss and retry a bounded number of times
            # before refusing to guess.
            last_count = len(items)
            self._press_key(0x1B)
            time.sleep(0.8)
        raise AdapterError(
            f"Group tree context menu had {last_count} items, expected "
            f"{self._GROUP_MENU_EXPECTED_ITEM_COUNT} -- refusing to guess which one is which."
        )

    def _click_group_menu_item(self, items: list, index: int) -> None:
        target = items[index]
        rect = target.CurrentBoundingRectangle
        height = rect.bottom - rect.top
        if not (18 <= height <= 30):
            self._press_key(0x1B)
            raise AdapterError(f"Group menu index {index} doesn't look like a real item (height={height}px).")
        cx = (rect.left + rect.right) // 2
        cy = (rect.top + rect.bottom) // 2
        self._click_screen(cx, cy)

    def _find_window_by_title(self, title: str) -> int | None:
        win32gui = self._win32gui()
        found: list[int] = []

        def cb(h, acc):
            try:
                if win32gui.IsWindowVisible(h) and win32gui.GetWindowText(h) == title:
                    acc.append(h)
            except Exception:
                pass
            return True

        win32gui.EnumWindows(cb, found)
        return found[0] if found else None

    def _ocr_group_tree_row_text(self, image, header_pos: tuple[int, int], row_index: int) -> str:
        """Independently OCRs ANY group-tree row, including row 0 --
        unlike `snapshot_group_names()`, which always LABELS row 0 as
        `expected_project_name` verbatim without reading it (a
        reasonable shortcut for read-only display of an immutable
        label, but not safe to trust immediately before a MUTATING
        operation like right-clicking it to create a new group under
        it -- see `ensure_group()`).

        Live-caught (Phase 5.15 Pass 2, Stage 17): this crop is only
        ~18px tall at native resolution, which reproduced as
        unreliable on its own -- the SAME unscaled row-0 crop of a
        clearly-legible "TEST" label OCR'd as pure empty string in one
        capture and as "TESTO"-type noise in another, on a tree with
        very few rows (2 groups) where this method had not previously
        been exercised. A 4x upscale before OCR (the same factor
        already used for reliable small-text reads elsewhere in this
        file, e.g. `_read_category_selector_for_verify_commit`) was
        verified live to consistently keep the real text as the
        leading recognized token; a wider test sweep (3x-8x) showed
        6x+ actually makes recognition worse, so 4x -- not "more is
        better" -- is the deliberate choice. `_group_name_matches()`
        already tolerates the residual trailing OCR noise via
        substring/fuzzy matching, so this only needs to get the real
        text recognized at all, not pixel-perfect."""
        left, top = header_pos[0], header_pos[1]
        row_top = self._group_tree_row_crop_top(top, row_index)
        crop = image.crop((
            left + self._GROUP_TREE_TEXT_DX, row_top,
            left + self._GROUP_TREE_TEXT_DX + self._GROUP_TREE_TEXT_WIDTH, row_top + self._GROUP_TREE_ROW_CROP_HEIGHT,
        ))
        return self._ocr_group_tree_name_crop(crop)

    def _reset_group_creation_stickiness(self) -> None:
        """Not part of the abstract contract (Phase 5.5C Stage 4).
        Xactimate's "New Group" command attaches the new group to
        whichever group was MOST RECENTLY CREATED in the session,
        regardless of which row is right-clicked/selected beforehand or
        which New-Group-dialog button (Append/Insert/Attach) is used --
        proven live in Phase 5.5B via a chain of four groups, each
        nesting one level deeper than the last no matter what was
        clicked, and unaffected by an intervening save.

        Live-caught (Phase 5.5C Stage 4, hypothesis 4 of 15 tried):
        switching away from the Items tab (to Components) and back
        clears that stickiness. Reproduced live twice: two groups
        created back-to-back with this reset in between landed as true
        siblings directly under the root, confirmed both by OCR and by
        an independent pixel measurement of each row's icon indent
        (identical for both rows -- see _group_tree_row_indent_x()).
        Without this reset, the second group nested under the first
        every time. Safe to call unconditionally before every group
        creation, including the first in a session (nothing to reset,
        so it's a no-op in effect).

        Phase 5.9 (live-caught): this method's own Components-tab click
        is, itself, a group-context-changing action -- and it runs
        several steps AFTER `ensure_group()`'s own opening self-heal
        check (verify_application/verify_project, the "already exists"
        snapshot, all happen in between). A results popup that
        reappeared or was still visually closing in that gap would
        previously go unchecked right up until this blind fixed-
        coordinate click -- exactly the mechanism a live user report
        described (a results dropdown still open when the automation
        moved on to Components/group setup). A fresh settle assertion
        immediately before the click closes that gap; see
        _assert_group_transition_settled()."""
        self._assert_group_transition_settled(next_group="<new group being created>")
        hwnd = self._ensure_main_window()
        image = self._capture_client_image(hwnd)
        components = self._locate_components_tab(image)
        if components is None:
            raise ItemsTabVerificationError(
                "Could not positively locate Components in the Estimate Items tab strip; refusing reset click."
            )
        l, t, r, b = components
        self._click_client(hwnd, (l + r) // 2, (t + b) // 2)
        components_verified = False
        for _attempt in range(4):
            if self._components_pane_is_verified(self._capture_client_image(hwnd)):
                components_verified = True
                break
        if not components_verified:
            raise ItemsTabVerificationError(
                "Components tab click did not produce a positively verified Components pane."
            )
        self._reset_scroll_state()

    #: Live-measured (Phase 5.5C Stage 7): a SELECTED row's highlight
    #: draws a thin (~2px-tall) border across the entire row width,
    #: including column 0 -- a naive "first dark pixel" scan reads that
    #: border as indent_x=0 for whichever row was just created (always
    #: auto-selected), a reproducible false positive that would make
    #: every freshly-created group look nested regardless of its real
    #: position. A minimum vertical ink-run filters it out: the
    #: highlight border is ~2px tall, the folder icon glyph is ~10px+.
    _GROUP_TREE_ICON_MIN_INK_RUN = 4
    #: Live-measured: a group WITH children shows an extra expand/
    #: collapse arrow glyph to the left of its folder icon, in roughly
    #: x=24-38 -- a row with children and a childless row at the SAME
    #: true depth otherwise read different indent_x (24 vs 39) if the
    #: scan starts at 0, even though neither is actually nested deeper.
    #: Starting the scan past the arrow's zone lands on the folder icon
    #: itself for both cases (confirmed live: siblings with and without
    #: an expand arrow both read indent_x=39; a nested child reads 59).
    _GROUP_TREE_ICON_SCAN_START_X = 30

    def _group_tree_row_indent_x(self, image, header_pos: tuple[int, int], row_index: int) -> int | None:
        """Not part of the abstract contract (Phase 5.5C Stage 7).
        Returns the x-pixel (relative to the tree header's left edge)
        of the group row's FOLDER ICON left edge -- which this Telerik
        tree control shifts right by a fixed amount per indentation
        level -- ignoring both the selection-highlight border and any
        expand/collapse arrow (see the constants above). This is
        independent of OCR text and of `_ocr_group_tree_row_text()`'s
        fixed-offset crop (see that method's docstring: it reads text
        from a FIXED horizontal offset regardless of true indentation,
        so it cannot itself distinguish a sibling from a nested child).
        Two rows at the SAME depth (true siblings) have IDENTICAL
        indent_x; a row nested one level deeper has a strictly greater
        indent_x. Returns None if no icon ink is found in the probed
        region -- callers must treat None as "can't confirm", never as
        a depth of zero."""
        left, top = header_pos[0], header_pos[1]
        row_top = self._group_tree_row_crop_top(top, row_index)
        crop = image.crop((
            left, row_top - 2, left + 200, row_top + self._GROUP_TREE_ROW_CROP_HEIGHT + 2,
        )).convert("L")
        px = crop.load()
        width, height = crop.size
        for x in range(self._GROUP_TREE_ICON_SCAN_START_X, width):
            ink = sum(1 for y in range(height) if px[x, y] < 200)
            if ink >= self._GROUP_TREE_ICON_MIN_INK_RUN:
                return x
        return None

    def verify_group_path(self, group_name: str, *, parent_group_name: str | None = None) -> bool:
        """Not part of the abstract contract (Phase 5.5C Stage 7).
        Read-only. Returns True only if `group_name` is confirmed to
        exist BENEATH the intended parent (default: the project root) --
        NOT merely that a row with a matching label exists somewhere in
        the tree. Returns False (never raises) for any of: the group
        isn't found, the parent isn't found, the same name matches more
        than one row (ambiguous), or the indentation check can't
        confirm the expected parent/child relationship. This is the
        distinction `verify_group()` does not make: that method only
        confirms which group currently RECEIVES new items (a mutating
        probe-commit check); this one confirms tree POSITION without
        mutating anything."""
        try:
            if not self.verify_application() or not self.verify_project():
                return False
            target_parent = parent_group_name or self.expected_project_name
            hwnd = self._ensure_main_window()
            self._scroll_group_tree_to_top(hwnd)
            rows = self.snapshot_group_names()

            matches = [
                i for i, text in enumerate(rows)
                if i != 0 and self._group_name_matches(text, group_name)
            ]
            if len(matches) != 1:
                return False  # not found, or ambiguous same-name rows
            group_index = matches[0]

            parent_index = self._find_group_row(rows, target_parent)
            if parent_index is None or parent_index == group_index:
                return False

            image = self._capture_client_image(hwnd)
            header = self._locate_group_tree_header(image)
            if header is None:
                return False

            group_indent = self._group_tree_row_indent_x(image, header, group_index)
            if group_indent is None:
                return False

            if target_parent == self.expected_project_name:
                # The root's own indent isn't a meaningful depth-zero
                # reference for its children's expected indent; compare
                # against another row already confirmed to be a direct
                # child of the root instead, when one exists.
                reference_index = None
                for i, text in enumerate(rows):
                    if i in (0, group_index):
                        continue
                    if text.strip():
                        reference_index = i
                        break
                if reference_index is None:
                    return True  # nothing else to compare against -- can't disprove
                reference_indent = self._group_tree_row_indent_x(image, header, reference_index)
                return reference_indent is not None and group_indent == reference_indent
            else:
                parent_indent = self._group_tree_row_indent_x(image, header, parent_index)
                return parent_indent is not None and group_indent > parent_indent
        except Exception:
            return False

    def ensure_group(self, group_name: str, *, parent_group_name: str | None = None) -> str | None:
        """Not part of the abstract contract. Creates `group_name` as a
        child of `parent_group_name` (default: this adapter's own
        `expected_project_name`, i.e. the project root) if it doesn't
        already exist (a no-op, verified via a fresh snapshot, if it
        does). Never guesses: raises AdapterError if the "New Group"
        dialog doesn't appear, if the group still can't be found after
        the creation sequence completes, if the intended parent can't
        be independently re-confirmed immediately before creating (see
        below), or if MORE THAN ONE row in the tree matches `group_name`
        (an ambiguity that must fail closed, never resolved by
        guessing -- Phase 5.7). Verifies the project before mutating
        anything (Phase 5.1 requirement: "verify project before every
        mutation").

        Returns None on an unambiguous, correctly-placed result (the
        common case), or a `GROUP_POSITION_WARNING` string (Phase 5.7)
        when the group was created successfully and is uniquely
        locatable/selectable by name, but landed at an unexpected
        nesting depth. Ancestry/position is informational evidence
        only as of Phase 5.7 -- see the docstring block below the
        creation sequence for why, and `verify_group_path()` for a
        read-only way to re-check position later without mutating
        anything.

        Live-caught (Phase 5.5B): the previous version always right-
        clicked row index 0, trusting it was always the project root by
        POSITION alone. `snapshot_group_names()`'s row 0 is a hardcoded
        label, not an OCR read (see its own docstring) -- so if the
        group tree's scroll position had drifted (the same class of
        live UI drift `_scroll_group_tree_to_top()` exists to correct,
        confirmed elsewhere in this file to occasionally fail silently)
        row 0 on screen could genuinely be some OTHER group, and "New"
        created the requested group AS A CHILD of that wrong row
        instead of as a top-level sibling. Fixed: the intended parent
        is located BY NAME, then its row is INDEPENDENTLY re-OCR'd
        (bypassing the hardcoded label) and fuzzy-matched against the
        intended name immediately before the context menu opens --
        refusing to proceed if that fails, rather than trusting a
        fixed position.

        Live-caught (Phase 5.5C): that fix alone did not solve the
        malformed tree -- the deeper cause is Xactimate always
        attaching "New Group" to the most-recently-created group in the
        session, independent of which row was right-clicked (see
        `_reset_group_creation_stickiness()`, called below, which fixes
        this reliably for groups #1-#2 of a session but not #3+ -- two
        further hypotheses tried live in Phase 5.7, double stickiness-
        reset and an explicit save between creations, neither helped
        either).

        Product-requirement change (Phase 5.7): group ancestry/position
        is no longer a blocking safety condition for this TEST
        workflow. What matters is that every required group EXISTS,
        can be located BY NAME, and can be independently selected and
        verified -- not where in the tree Xactimate physically placed
        it (the owner can reorganize the visual hierarchy manually
        afterward). A mis-nested group whose name is still uniquely
        identifiable is therefore a successful creation with a
        GROUP_POSITION_WARNING, not a failure. What STILL fails closed:
        more than one row matching `group_name` (genuine ambiguity --
        cannot safely pick one) and the intended parent row vanishing
        outright after creation (a much more fundamental problem than
        nesting depth, since it means the tree can no longer be
        trusted to have any stable row identities at all).

        Phase 5.7B: self-heals from a "Duplicate Item(s)" dialog left
        open by an EARLIER commit (see commit_item()'s own docstring) --
        every group-tree entry point checks for and dismisses it first,
        so one earlier miss doesn't cascade into every later group
        looking unreachable ("context menu did not appear") for the
        rest of the run, exactly as reproduced live. Phase 5.9: replaces
        the earlier silent stray-popup self-heal with a hard, fail-loud
        settle assertion -- see _assert_group_transition_settled()."""
        self._handle_duplicate_item_dialog()
        self._assert_group_transition_settled(next_group=group_name)
        if not self.verify_application() or not self.verify_project():
            raise AdapterError(f"ensure_group({group_name!r}): could not verify the expected project is active.")

        target_parent = parent_group_name or self.expected_project_name

        hwnd = self._ensure_main_window()
        self._force_foreground(hwnd)
        self._scroll_group_tree_to_top(hwnd)
        rows = self.snapshot_group_names()
        if self._find_unique_group_row(rows, group_name) is not None:
            return None  # already exists, unambiguously -- nothing to do

        # Only reset stickiness (and re-scan) when a creation is
        # actually about to happen -- a no-op call above must never
        # touch the live UI at all (see the no-op test).
        self._reset_group_creation_stickiness()
        self._scroll_group_tree_to_top(hwnd)
        rows = self.snapshot_group_names()

        parent_index = self._find_group_row(rows, target_parent)
        if parent_index is None:
            raise AdapterError(f"ensure_group({group_name!r}): parent group {target_parent!r} not found in the tree.")

        # Phase 5.7 (live-caught): the SAME class of transient-repaint
        # flakiness `_capture_and_locate()` already retries for elsewhere
        # in this file also affects this read -- reproduced live 3/4
        # trials immediately after `_reset_group_creation_stickiness()`'s
        # tab-switch, where the freshly-repainting group tree's row 0
        # reads back completely blank for 0-3 consecutive attempts before
        # settling to the correct text moments later (same header
        # position each time -- this is a paint-timing gap, not a
        # position/ancestry problem). A single unretried read here was
        # indistinguishable from a genuine ancestry drift and caused
        # ensure_group() to refuse a perfectly valid parent row. Bounded,
        # matching every other OCR-retry precedent in this file -- never
        # an unbounded wait, and still refuses (raises) if every attempt
        # comes back blank/mismatched.
        actual_parent_text = None
        header = None
        for _attempt in range(5):
            image = self._capture_client_image(hwnd)
            header = self._locate_group_tree_header(image)
            if header is None:
                time.sleep(0.4)
                continue
            actual_parent_text = self._ocr_group_tree_row_text(image, header, parent_index)
            if self._group_name_matches(actual_parent_text, target_parent):
                break
            time.sleep(0.4)
        if header is None:
            raise AdapterError(f"ensure_group({group_name!r}): could not locate the group tree.")

        # Independent re-OCR of the exact row about to be right-clicked
        # -- never trust `rows[parent_index]` alone (row 0's text is a
        # hardcoded label, not a live read; see docstring above).
        if not self._group_name_matches(actual_parent_text, target_parent):
            raise AdapterError(
                f"ensure_group({group_name!r}): row {parent_index} was expected to be the parent "
                f"{target_parent!r} but independently re-reads as {actual_parent_text!r} -- the group tree's "
                f"position has drifted; refusing to create a group under an unverified row."
            )

        items = self._open_group_tree_context_menu(hwnd, header, parent_index)
        self._click_group_menu_item(items, self._GROUP_MENU_NEW_INDEX)
        time.sleep(0.8)

        dialog_hwnd = self._find_window_by_title("New Group")
        if dialog_hwnd is None:
            raise AdapterError(f"ensure_group({group_name!r}): the 'New Group' dialog did not appear.")

        NAME_FIELD = (185, 18)
        ATTACH_BUTTON = (305, 75)
        self._click_client(dialog_hwnd, *NAME_FIELD)
        time.sleep(0.3)
        self._select_all_and_delete()
        time.sleep(0.2)
        self._type_keybdevent(group_name)
        time.sleep(0.3)
        self._click_client(dialog_hwnd, *ATTACH_BUTTON)
        time.sleep(1.0)

        # Live-caught (Phase 5.2 Stage 3): a single post-creation check
        # right after a 1.0s sleep occasionally missed a group that WAS
        # actually created (confirmed by a subsequent independent
        # snapshot moments later) -- the tree can take slightly longer
        # than 1.0s to settle for some creations. Bounded retry with an
        # extra settle sleep, never an unbounded wait. Widened in Phase
        # 6.1 -- see _GROUP_DISCOVERY_MAX_ATTEMPTS's own docstring.
        for _attempt in range(self._GROUP_DISCOVERY_MAX_ATTEMPTS):
            rows_after = self.snapshot_group_names()
            # Phase 5.7: uniqueness, not mere presence, is what's load-
            # bearing now -- raises AdapterError if creation somehow
            # produced (or exposed) more than one row matching this
            # name, which must fail closed rather than let a later
            # select_group() guess.
            new_index = self._find_unique_group_row(rows_after, group_name)
            if new_index is not None:
                # The parent row vanishing outright is a different,
                # more fundamental problem than nesting depth (Phase
                # 5.5B) -- it means the tree's row identities can no
                # longer be trusted at all, not just that this one
                # group landed somewhere unexpected. Still fails closed.
                parent_index_after = self._find_group_row(rows_after, target_parent)
                if parent_index_after is None:
                    raise AdapterError(
                        f"ensure_group({group_name!r}): group was created, but the intended parent "
                        f"{target_parent!r} can no longer be found in the tree -- refusing to trust the result."
                    )
                # Phase 5.7 product-requirement change: ancestry/nesting
                # depth is informational evidence only, never a reason
                # to fail an otherwise-successful, uniquely-identifiable
                # creation (see this method's own docstring). A
                # confirmed indentation mismatch (both indents actually
                # read, both real) now downgrades to a returned
                # GROUP_POSITION_WARNING string instead of raising; an
                # unreadable indent (OCR/pixel noise) is silently not a
                # warning, same as before -- a false negative here is
                # safe, a false positive would wrongly flag a correctly
                # -placed group.
                position_warning = None
                image_after = self._capture_client_image(hwnd)
                header_after = self._locate_group_tree_header(image_after)
                if header_after is not None and target_parent == self.expected_project_name:
                    new_indent = self._group_tree_row_indent_x(image_after, header_after, new_index)
                    reference_indent = None
                    for i, text in enumerate(rows_after):
                        if i in (0, new_index):
                            continue
                        if text.strip():
                            reference_indent = self._group_tree_row_indent_x(image_after, header_after, i)
                            if reference_indent is not None:
                                break
                    if new_indent is not None and reference_indent is not None and new_indent != reference_indent:
                        position_warning = (
                            f"GROUP_POSITION_WARNING: ensure_group({group_name!r}) created the group, but its "
                            f"indentation ({new_indent}px) differs from an existing top-level group's "
                            f"({reference_indent}px) -- it likely nested under another group instead of the "
                            f"root. The group is still uniquely identifiable by name and safe to select/verify/"
                            f"use; reorganize the visual tree manually later if a specific layout is desired."
                        )
                return position_warning
            time.sleep(self._GROUP_DISCOVERY_RETRY_INTERVAL_S)
        raise AdapterError(
            f"ensure_group({group_name!r}): group still not found after the creation sequence completed."
        )

    def select_group(self, group_name: str) -> None:
        """Not part of the abstract contract. Left-clicks the row for
        `group_name`, found fresh by OCR (never a presumed index -- the
        tree does not preserve insertion order, see docs/build-estimate.md
        Phase 5.1 Stage 3). Raises AdapterError if the group doesn't
        exist -- select_group() never creates one; call ensure_group()
        first. Also raises (Phase 5.7) if MORE THAN ONE row matches
        `group_name` -- a duplicate/ambiguous name must fail closed,
        never resolved by picking one. Phase 5.7B: self-heals from a
        "Duplicate Item(s)" dialog left open by an earlier commit --
        see ensure_group()'s docstring. Phase 5.9: replaces the earlier
        silent stray-popup self-heal with a hard, fail-loud settle
        assertion -- see _assert_group_transition_settled()."""
        self._handle_duplicate_item_dialog()
        self._assert_group_transition_settled(next_group=group_name)
        if not self.verify_application() or not self.verify_project():
            raise AdapterError(f"select_group({group_name!r}): could not verify the expected project is active.")

        hwnd = self._ensure_main_window()
        self._force_foreground(hwnd)
        self._scroll_group_tree_to_top(hwnd)
        image = self._capture_client_image(hwnd)
        header = self._locate_group_tree_header(image)
        if header is None:
            raise AdapterError(f"select_group({group_name!r}): could not locate the group tree.")

        rows = self.snapshot_group_names()
        row_index = self._find_unique_group_row(rows, group_name)
        if row_index is None:
            raise AdapterError(f"select_group({group_name!r}): group not found in the tree (rows: {rows!r}).")

        xy = self._group_tree_row_xy(header, row_index)
        self._click_client(hwnd, *xy)
        time.sleep(0.3)
        # Force a repaint before returning -- see
        # `_open_group_tree_context_menu()`'s docstring: the selection
        # change only reliably commits internally once the window
        # processes a paint cycle, which idle sleeping alone does not
        # force. verify_group() relies on this having already happened.
        self._capture_client_image(hwnd)
        time.sleep(0.3)

    #: Cheapest, always-present catalog item used as a disposable probe
    #: by verify_group() -- the same CAT/SEL relied on throughout every
    #: prior phase's live trials.
    _VERIFY_GROUP_PROBE_CATEGORY = "SFG"
    _VERIFY_GROUP_PROBE_SELECTOR = "GUTA"

    #: Minimum dark-pixel increase in a row's own Subtotal cell, before
    #: vs. after the probe commit, to count as "content appeared".
    #: Live-measured (Phase 5.2 Stage 2): a populated cell's dark-pixel
    #: count rises from ~119 (border-only) to ~182 (border + "$11.56"
    #: text) -- a delta of ~63 -- while an unaffected row's count is
    #: exactly stable (0 -> 0) across repeated captures. 30 sits safely
    #: below the observed real delta and safely above measured noise.
    _VERIFY_GROUP_SUBTOTAL_DELTA_THRESHOLD = 30

    #: Grayscale luminance below which a pixel counts as "dark" (text
    #: glyph), not "highlight fill" or "border line". Live-measured:
    #: text glyphs render near-black; the row-selection highlight/focus
    #: border used by Xactimate's group tree does not.
    _GROUP_SUBTOTAL_DARK_THRESHOLD = 110

    def _group_subtotal_pixel_count(self, image, header_pos: tuple[int, int], row_index: int) -> int:
        """Live-caught (Phase 5.1): OCR on the Subtotal cell was tried
        first and found unreliable at this crop size (garbled text even
        with the correct crop region located via a fresh "Subtotal"
        header search). A pixel-based count is used instead.

        Live-caught (Phase 5.2 Stages 2-3), two compounding bugs fixed:
        1. The crop's vertical position originally used a click-tuned
           (27, 23) constant pair that was never re-derived from a real
           measurement, and separately drifted from the OCR-text crop's
           OWN (25, 15) constants -- close enough at low row indices for
           both to work by margin alone, but they disagreed on which
           physical row a given row_index meant once a 5th group
           existed (confirmed live: `_find_group_row()`, via the OCR
           formula, returned index 5 for a row this formula, via the
           click formula, would have read as index 4). Fixed by
           re-measuring real row positions directly via OCR word-level
           top coordinates on a live 5-row tree (exactly 20px apart,
           23px below the header -- see `_GROUP_TREE_ROW_HEIGHT`) and
           using that ONE formula (`_group_tree_row_crop_top()`)
           everywhere a row's vertical position is needed, so every
           consumer agrees on what row_index N means.
        2. Counting non-white pixels conflates real text with the
           selected/focused row's highlight or focus-border rendering,
           which spans the full row width regardless of dollar content
           (measured live: a genuinely-empty selected row still reads
           ~1600-2450 non-white pixels; comparing against a DIFFERENT
           row's baseline, or even this row's own pre-probe baseline
           when saturated near the crop's pixel ceiling, both produced
           false positives). Counting only DARK (near-black,
           text-glyph-colored) pixels is immune to the highlight/border
           fill color and isolates real digit strokes. The caller must
           still compare this row's own count before vs. after the
           probe (see `verify_group()`), never against another row --
           the border contributes a stable but nonzero baseline that a
           cross-row or fixed-threshold comparison cannot account for."""
        left, top = header_pos[0], header_pos[1]
        subtotal_header = self._locate_label(image, "Subtotal", prefer="topmost")
        subtotal_left = subtotal_header[0] if subtotal_header is not None else left + 168
        row_top = self._group_tree_row_crop_top(top, row_index)
        crop = image.crop((subtotal_left, row_top, subtotal_left + 130, row_top + self._GROUP_TREE_ROW_CROP_HEIGHT))
        count = 0
        for pixel in crop.getdata():
            r, g, b = pixel[:3]
            gray = 0.299 * r + 0.587 * g + 0.114 * b
            if gray < self._GROUP_SUBTOTAL_DARK_THRESHOLD:
                count += 1
        return count

    def verify_group(self, group_name: str, *, use_cache: bool = True) -> bool:
        """Verify the selected group using only fresh, non-destructive UI evidence.

        A cached success never substitutes for current selection evidence.  The
        cache remains a record of session successes, but every call revalidates
        the live row, selection boundary, pane, Search field, grid, and popup
        state.  No catalog search, candidate selection, item commit, or cleanup
        occurs here.
        """
        normalized = group_name.strip().lower()
        verified = self._verify_group_once(group_name)
        if verified:
            self._verified_groups_this_session.add(normalized)
        else:
            self._verified_groups_this_session.discard(normalized)
        return verified

    def _group_verification_frame(
        self, image, group_name: str, *, expected_index: int | None = None,
    ) -> int | None:
        """Return the selected requested row only when one frame proves all geometry."""
        header = self._locate_group_tree_header(image)
        if header is None:
            return None
        rows = self._snapshot_group_names_from_image(image)
        try:
            target_index = self._find_unique_group_row(rows, group_name)
        except AdapterError:
            return None
        if target_index is None or (expected_index is not None and target_index != expected_index):
            return None
        row_text = self._ocr_group_tree_row_text(image, header, target_index)
        if not self._group_name_matches(row_text, group_name):
            return None
        if not self._group_tree_row_has_selection_boundary(image, header, target_index):
            return None
        if self._items_search_pane_field(image) is None:
            return None
        if self._anchor_offset(image) is None:
            return None
        return target_index

    def _verify_group_once(self, group_name: str) -> bool:
        """Select and corroborate one unique group across two fresh captures."""
        try:
            self._assert_group_transition_settled(next_group=group_name)
            if not self.verify_application() or not self.verify_project():
                return False
            hwnd = self._ensure_main_window()
            self._force_foreground(hwnd)
            self._scroll_group_tree_to_top(hwnd)

            before = self._capture_client_image(hwnd)
            header = self._locate_group_tree_header(before)
            if header is None:
                return False
            rows = self._snapshot_group_names_from_image(before)
            target_index = self._find_unique_group_row(rows, group_name)
            if target_index is None:
                return False
            if not self._group_name_matches(
                self._ocr_group_tree_row_text(before, header, target_index), group_name,
            ):
                return False

            self._click_client(hwnd, *self._group_tree_row_xy(header, target_index))
            self._capture_client_image(hwnd)  # force selection repaint
            self._reset_scroll_state()

            first = self._capture_client_image(hwnd)
            if self._find_dropdown_window() is not None or self._unexpected_dialog_present():
                return False
            first_index = self._group_verification_frame(first, group_name, expected_index=target_index)
            if first_index is None:
                return False

            second = self._capture_client_image(hwnd)
            if self._find_dropdown_window() is not None or self._unexpected_dialog_present():
                return False
            return self._group_verification_frame(
                second, group_name, expected_index=first_index,
            ) == first_index
        except GroupTransitionUnsafeError:
            raise
        except Exception:
            return False

    def _resolve_stray_probe_before_retry(self, group_name: str) -> None:
        """Phase 5.10C Stage 4: called by verify_group() between its
        two attempts, ONLY after the first attempt's own cleanup raised
        ProbeCleanupFailedError. Positively resolves whatever that
        attempt might have left behind BEFORE the next attempt starts
        its own fresh probe cycle -- if left unresolved, the next
        attempt's own baseline capture would silently treat a leftover
        probe row as "pre-existing content" instead of recognizing it,
        permanently hiding it as an unidentified garbage row (the exact
        "do not create multiple unidentified probes" failure mode this
        stage exists to prevent). Cleans down to the group's own
        PROTECTED row count (never that failed attempt's own possibly-
        stale row_count_before) -- reuses the same identity-based
        _cleanup_probe_item() mechanism, never a distinct probe of its
        own. Propagates ProbeCleanupFailedError/ProtectedCommittedRowError
        if it cannot positively resolve the state either -- the caller
        then fails closed rather than risking a second attempt on top
        of an unresolved first one."""
        try:
            self._handle_duplicate_item_dialog()
            self.recover()
        except Exception:
            pass
        protected_floor = self._protected_row_ledger.count_for_group(group_name)
        self._cleanup_probe_item(protected_floor)

    def _verify_group_once_destructive_legacy(self, group_name: str) -> bool:
        """One full probe-commit-and-check cycle. This DOES mutate the
        estimate transiently; it always cleans up before returning,
        including on failure. Returns False, never raises, on anything
        short of a confident match -- callers must never silently
        proceed on an unverified group.

        Phase 5.9A: group activation is now proven by the PROBE'S OWN
        LIFECYCLE -- select intended group by name, snapshot a full
        row-identity baseline, commit the disposable probe, POSITIVELY
        observe it appear (via _wait_for_probe_visible(), identified by
        CAT/SEL, never by position alone), remove it, and confirm the
        baseline rows are unchanged afterward. This REPLACES the
        Grouping panel's Subtotal pixel-delta as the load-bearing
        signal (see docs/build-estimate.md Phase 5.1 Stage 2 for that
        original mechanism) because live investigation found the
        Subtotal column is not always visible in the live Grouping
        panel layout at all (confirmed independent of window size,
        group depth, or group count) -- when that happens, the OLD
        subtotal-only check always read a 0 delta and verify_group()
        failed for every group, unconditionally, blocking all real work.
        The probe-lifecycle evidence above is strictly stronger than a
        pixel count anyway: it structurally proves the exact row that
        appeared and disappeared, not just that some dollar amount
        moved somewhere on screen. The Subtotal check is kept as
        OPTIONAL corroborating evidence (self.last_verify_group_
        subtotal_evidence, one of "MATCH"/"MISMATCH"/"UNAVAILABLE") --
        never load-bearing, never a reason to fail verification on its
        own.

        Phase 5.8 Stage 8: every REAL call here (never a cache hit --
        verify_group()'s cache short-circuits before this is ever
        reached) increments self.probes_run_total and self.
        probes_by_group[group_name], live-measured at ~20-25s each --
        real cost that makes verify_group()'s per-session cache (Phase
        5.7B) worth confirming stays effective, not just assumed."""
        self.probes_run_total += 1
        self.probes_by_group[group_name] = self.probes_by_group.get(group_name, 0) + 1
        row_count_before = 0
        baseline_identities: list = []
        skip_cleanup = False
        self.last_verify_group_subtotal_evidence = None
        try:
            self._handle_duplicate_item_dialog()  # Phase 5.7B: self-heal, see ensure_group()'s docstring
            self._assert_group_transition_settled(next_group=group_name)  # Phase 5.9: hard settle assert
            if not self.verify_application() or not self.verify_project():
                return False
            hwnd = self._ensure_main_window()
            # Group creation deliberately visits Components to clear
            # creation stickiness. Never inherit that pane: establish
            # Items/Search before any probe baseline, focus, or typing.
            self._reset_scroll_state()
            self._scroll_group_tree_to_top(hwnd)
            rows = self.snapshot_group_names()
            # Phase 5.7: ambiguity (more than one matching row) must
            # fail closed like everywhere else -- _find_unique_group_row
            # raises AdapterError in that case, caught by this method's
            # own broad except below and turned into the promised
            # "False, never raises" result.
            target_index = self._find_unique_group_row(rows, group_name)
            if target_index is None:
                return False

            # Live-caught (Phase 5.2 Stage 2): clicking a row that is
            # ALREADY selected (as select_group() does when called
            # right after ensure_group() creates and auto-selects a
            # brand-new group) can leave the tree control in a
            # transient inline-rename/focus-edit state (a dotted focus
            # rectangle around the name, observed live) instead of a
            # plain selected state. Escape unconditionally dismisses it
            # (a no-op if no such state is active) before anything else.
            self._press_key(0x1B)
            time.sleep(0.3)

            # Phase 5.9A: Subtotal pixel evidence is now OPTIONAL and
            # best-effort ONLY -- captured if available, never a reason
            # to return False on its own (see this method's docstring).
            subtotal_count_before = None
            image_before = self._capture_client_image(hwnd)
            header_before = self._locate_group_tree_header(image_before)
            if header_before is not None:
                try:
                    subtotal_count_before = self._group_subtotal_pixel_count(image_before, header_before, target_index)
                except Exception:
                    subtotal_count_before = None

            # Live-caught (Phase 5.3): a group being re-verified on
            # resume can already hold real, previously-committed rows
            # from an earlier task in the SAME group. The probe's
            # cleanup must never assume the grid started empty -- it
            # must restore exactly this baseline, not zero, or it will
            # cancel real committed work along with its own disposable
            # probe row.
            #
            # Live-caught (follow-up): treating "grid could not be
            # located" as "0 rows here" and continuing anyway let
            # cleanup cancel real, already-committed rows. Fail closed
            # instead: refuse the whole probe whenever the starting
            # state can't be positively established.
            #
            # Phase 5.9A Stage 1: captures the FULL row-identity list,
            # not just a count -- the post-cleanup reconciliation below
            # compares identities, not merely "the count went back
            # down", which could hide a cleanup that removed the wrong
            # row and re-added a coincidentally-matching count.
            # _capture_and_locate() is called directly first (rather
            # than relying solely on snapshot_grid_identities()'s own
            # internal call) so "grid could not be located at all" is
            # distinguishable from "grid genuinely has zero rows" --
            # snapshot_grid_identities() returns [] for both, which
            # would otherwise silently treat a location FAILURE as an
            # empty grid and let cleanup cancel real rows down to zero.
            grid_image_before, grid_offset_before = self._capture_and_locate(hwnd)
            if grid_offset_before is None:
                skip_cleanup = True
                return False
            # The disposable probe follows the same physical-
            # instantiation contract as a source task.  Retain the rich
            # before-snapshot here so pending_item_created() can prove
            # the exact probe row appeared. A plain identity snapshot
            # cannot establish the logical before/after row delta.
            baseline_identities = self.snapshot_grid_identities_for_activation()
            row_count_before = len(baseline_identities)

            self.focus_search()
            self.clear_search()
            time.sleep(0.4)
            self.search_by_category_selector(self._VERIFY_GROUP_PROBE_CATEGORY, self._VERIFY_GROUP_PROBE_SELECTOR)
            raw = self.capture_dropdown()
            candidates = self.parse_dropdown(raw)
            target = next(
                (c for c in candidates
                 if c.category == self._VERIFY_GROUP_PROBE_CATEGORY and c.selector == self._VERIFY_GROUP_PROBE_SELECTOR),
                None,
            )
            if target is None:
                return False
            try:
                self.select_candidate(target)
            except UnexpectedDialogError:
                # Phase 5.9 Priority 3 (live-caught): the probe
                # deliberately reuses ONE fixed CAT/SEL across every
                # group (see _handle_duplicate_item_dialog()'s
                # docstring) -- a group that already legitimately
                # contains that same SFG/GUTA content (Exterior's real
                # gutter row, or any Elevation group's real downspout
                # row) pops Xactimate's "Duplicate Item(s)" dialog
                # IMMEDIATELY after the candidate is clicked, before
                # enter_quantity()/commit_item() ever run.
                # select_candidate() deliberately hard-stops there for
                # a REAL task (never auto-dismissed -- see its own
                # docstring), but the probe is EXPECTED to hit this and
                # must tolerate it, unlike a real task. Live-confirmed:
                # clicking "Yes" already completes the selection --
                # read_populated_fields() shows the candidate pending
                # in Quick Entry immediately afterward -- so this is
                # never retried (the results popup handle
                # select_candidate() would need is already gone by
                # this point regardless). Without this, EVERY group
                # whose real content already includes SFG/GUTA would
                # fail verify_group() on every fresh (uncached) probe,
                # independent of tree depth/position -- confirmed live
                # for Rear Elevation and Left Elevation. The position-
                # based probe identification below (last row = the new
                # one, since Xactimate always appends) still safely
                # distinguishes this probe from a pre-existing real
                # SFG/GUTA row in the SAME group -- see Stage 2 of the
                # Phase 5.9A report.
                if not self._handle_duplicate_item_dialog():
                    raise
            if not self.pending_item_created(baseline_identities):
                return False
            # Group activation is already positively proven by the
            # exact candidate plus its new physical-row delta.  Do not
            # route this disposable probe through source-task quantity
            # entry: selected-row quantity OCR can misread Xactimate's
            # default value, and enter_quantity() correctly refuses to
            # overwrite a non-zero read.  Commit/visible/identity-based
            # cleanup below remain the fail-closed lifecycle proof.
            self.commit_item()

            # Phase 5.9A: THE verification signal -- positive, bounded-
            # poll confirmation that a row matching the probe's own
            # CAT/SEL landed at the end of the grid. This alone proves
            # group activation: the search, selection, and commit all
            # necessarily happened against whatever group is currently
            # active, and that group is uniquely `group_name` (already
            # confirmed by _find_unique_group_row() above) -- so a
            # freshly-appeared, correctly-identified probe row IS the
            # intended group actually accepting a real write.
            status = self._wait_for_probe_visible(row_count_before)
            if status != "observed":
                try:
                    self.recover()
                except Exception:
                    pass
                return False

            # Optional corroborating evidence only -- never load-bearing
            # (see this method's docstring). Recorded for diagnostics.
            if subtotal_count_before is not None:
                try:
                    image2 = self._capture_client_image(hwnd)
                    header2 = self._locate_group_tree_header(image2)
                    subtotal_count_after = (
                        self._group_subtotal_pixel_count(image2, header2, target_index)
                        if header2 is not None else None
                    )
                    if subtotal_count_after is None:
                        self.last_verify_group_subtotal_evidence = "UNAVAILABLE"
                    elif subtotal_count_after > subtotal_count_before + self._VERIFY_GROUP_SUBTOTAL_DELTA_THRESHOLD:
                        self.last_verify_group_subtotal_evidence = "MATCH"
                    else:
                        self.last_verify_group_subtotal_evidence = "MISMATCH"
                except Exception:
                    self.last_verify_group_subtotal_evidence = "UNAVAILABLE"
            else:
                self.last_verify_group_subtotal_evidence = "UNAVAILABLE"

            return True
        except GroupTransitionUnsafeError:
            # Phase 5.9: raised by this method's own opening settle
            # assertion, BEFORE any probe search/commit ever ran --
            # nothing to clean up, and cleanup must never run with
            # row_count_before still at its default 0 (that would
            # cancel real rows down to zero). Never swallowed -- this
            # is Stage 4's "DO NOT CHANGE GROUP CONTEXT" hard stop.
            skip_cleanup = True
            raise
        except ItemsTabVerificationError:
            # Pane identity failed before probe typing. There is no
            # trustworthy Items grid to inspect or clean, so do not run
            # cleanup against whatever pane happens to be visible.
            skip_cleanup = True
            raise
        except Exception:
            # Phase 5.9 (live-caught): a failed probe attempt --
            # commonly an UnexpectedDialogError this method couldn't
            # self-heal -- previously left the "Duplicate Item(s)"
            # dialog (or a stray results popup) genuinely open in
            # Xactimate, silently blocking whatever ran next (confirmed
            # live: a leftover dialog from one failed probe made an
            # UNRELATED later script's search fail outright). Best-
            # effort recovery before returning False.
            try:
                self.recover()
            except Exception:
                pass
            return False
        finally:
            if not skip_cleanup:
                try:
                    self._cleanup_probe_item(row_count_before)
                    # Phase 5.9A Stage 1: verify pre-existing baseline
                    # rows are unchanged after cleanup -- not merely
                    # that the COUNT returned to baseline, which alone
                    # cannot distinguish "cleanup removed the right row"
                    # from "cleanup removed a different row and the
                    # counts coincidentally match."
                    if baseline_identities:
                        after_identities = self.snapshot_grid_identities()
                        if after_identities != baseline_identities:
                            # Phase 5.15 Pass 2 (live-caught): a heavily
                            # populated group's fixed header-to-row1
                            # pixel gap can measure a few px off from
                            # the general calibration (confirmed: the
                            # header's own position independently
                            # re-locates correctly, but the row crop
                            # then straddles two real rows instead of
                            # one, garbling every read identically and
                            # stably -- not a one-off timing fluke, see
                            # `snapshot_grid_identities()`'s
                            # `row_top_nudge` docstring). Re-derive it
                            # with a small bounded vertical search,
                            # picking the nudge whose row set matches
                            # the known-correct baseline most closely.
                            #
                            # Live-caught, same investigation: even at
                            # the best-aligned nudge, 3 of 15 selectors
                            # (all otherwise correct: same category,
                            # same length) each showed exactly ONE
                            # substituted character ("300S" -> "3008",
                            # "VENTCP5" -> "VENTCP:", "ALUSW+" ->
                            # "ALUSW:") -- ordinary small-crop OCR
                            # noise on a densely-populated grid, not a
                            # sign cleanup touched the wrong row (row
                            # count and every category already agree
                            # exactly; `_cleanup_probe_item()` also
                            # independently re-confirms the deleted
                            # row's own identity before every deletion,
                            # so a wrong-row cancel is already guarded
                            # against upstream of this check). Reusing
                            # `check_category_selector_match()`'s
                            # existing truncation tolerance is not
                            # enough here (mid-string substitution, not
                            # a prefix drop), so this adds one narrow,
                            # evidence-matched tolerance on top of it:
                            # a single same-position character swap in
                            # an equal-length selector. Never tolerates
                            # a category mismatch, a length mismatch,
                            # or more than one differing character.
                            def _row_close_enough(expected, observed) -> bool:
                                match = check_category_selector_match(*expected, *observed)
                                if match.match_state in ("exact_match", "normalized_match"):
                                    return True
                                exp_cat, exp_sel = expected
                                obs_cat, obs_sel = observed
                                if exp_cat != obs_cat or not exp_sel or not obs_sel:
                                    return False
                                if len(exp_sel) != len(obs_sel):
                                    return False
                                return sum(1 for a, b in zip(exp_sel, obs_sel) if a != b) <= 1

                            recovered = False
                            for nudge in (0, -10, -8, -6, -4, -2, 2, 4):
                                candidate = after_identities if nudge == 0 else self.snapshot_grid_identities(row_top_nudge=nudge)
                                if len(candidate) == len(baseline_identities) and all(
                                    _row_close_enough(exp, obs) for exp, obs in zip(baseline_identities, candidate)
                                ):
                                    recovered = True
                                    break
                            if not recovered:
                                raise ProbeCleanupFailedError(
                                    f"_verify_group_once({group_name!r}): pre-existing rows changed during probe "
                                    f"cleanup -- before={baseline_identities!r} after={after_identities!r}."
                                )
                    # A successful verification cycle must hand control
                    # back in the same positively established Items pane.
                    self._reset_scroll_state()
                except ProtectedCommittedRowError:
                    # Phase 5.5D: never swallowed -- this means cleaning
                    # up the disposable probe would have deleted a row
                    # Execute already successfully committed. That is a
                    # hard stop for the whole run, not a best-effort
                    # cleanup failure to shrug off.
                    raise
                except ProbeCleanupFailedError:
                    # Phase 5.9 Priority 2: never swallowed either --
                    # cleanup could not positively confirm the probe
                    # was removed. Surfacing this as a specific,
                    # diagnosable group-verification failure reason is
                    # the whole point; silently continuing is exactly
                    # what let garbage SFG/GUTA rows accumulate.
                    raise
                except ItemsTabVerificationError:
                    raise
                except Exception:
                    pass

    #: Phase 5.9 Priority 1: bounded settle-poll for the probe's own
    #: commit to actually become observable in the grid before cleanup
    #: trusts any read -- live-measured (see priority1_settle_timing
    #: evidence in the Phase 5.9 report) that a single immediate
    #: post-commit read can be stale.
    _PROBE_SETTLE_POLL_S = 0.15
    _PROBE_SETTLE_TIMEOUT_S = 3.0

    def _last_row_identity(self, image, offset, row_count, row_top_nudge: int = 0) -> tuple[str | None, str | None]:
        row_1_top = self._shifted_anchor("grid_row_1", offset)[1] + row_top_nudge
        last_row_top = row_1_top + (row_count - 1) * _GRID_ROW_HEIGHT
        return self._read_category_selector_at(image, offset, last_row_top)

    def _last_row_identity_matching_probe(self, image, offset, row_count) -> tuple[str | None, str | None]:
        """Phase 5.15 Pass 2 (live-caught): same root cause as
        `snapshot_grid_identities()`'s `row_top_nudge` (see its
        docstring) -- on a group already holding many rows, appending
        the probe pushes the grid past whatever triggers the
        header-to-row1 gap drift, so the UNNUDGED read of the probe's
        own last row can come back as something else entirely (live-
        reproduced on Roof at 16 rows: read as neither the probe's own
        CAT/SEL nor anything resembling the real last row, twice in a
        row -- `_cleanup_probe_item()` correctly refused to guess and
        raised rather than delete an unidentified row). Tries a small,
        bounded set of vertical nudges and returns the FIRST one that
        reads back as EXACTLY the probe's own known CAT/SEL (never a
        fuzzy/tolerant match -- there is no ambiguity to resolve here,
        only a fixed constant to find). Falls back to the unnudged
        read, unchanged, if no nudge finds it -- preserving the exact
        original "genuinely a different row" contradiction-detection
        behavior when the last row really isn't the probe."""
        probe_identity = (self._VERIFY_GROUP_PROBE_CATEGORY, self._VERIFY_GROUP_PROBE_SELECTOR)
        unnudged = self._last_row_identity(image, offset, row_count)
        if unnudged == probe_identity:
            return unnudged
        for nudge in (-10, -8, -6, -4, -2, 2, 4):
            if self._last_row_identity(image, offset, row_count, row_top_nudge=nudge) == probe_identity:
                return probe_identity
        return unnudged

    def _wait_for_probe_visible(self, target_row_count: int) -> str:
        """Phase 5.9A/5.10C: bounded-poll for the disposable group-
        verification probe (CAT/SEL = _VERIFY_GROUP_PROBE_CATEGORY/
        _VERIFY_GROUP_PROBE_SELECTOR) to become positively observable
        as the grid's LAST row -- Xactimate always appends new rows at
        the end, so "last row, identity matches" is a strong, position-
        AND-identity signal, not a guess. Returns one of:

        - "observed": the probe was seen -- a single positive read is
          trusted immediately (positive evidence doesn't need the same
          skepticism a negative result does).
        - "absent": TWO CONSECUTIVE reads confirm the grid never grew
          past target_row_count -- genuinely nothing was ever added.
          A single such read is NOT trusted alone (Phase 5.9 Priority
          1's live-caught bug: a stale read immediately after commit
          can still show the pre-probe count, which used to make
          cleanup silently declare victory without the probe having
          been removed -- see docs/build-estimate.md Phase 5.9).
        - "contradiction": TWO CONSECUTIVE reads at row_count >
          target_row_count agree on the SAME identity, and it is
          neither empty/unreadable NOR the probe's own CAT/SEL -- a
          real, repeated (not just noisy-once) signal that something
          other than the probe landed there. Phase 5.10C: a single
          unreadable OCR read (cat/sel both None, or a one-off garbled
          value that never repeats) is NEVER treated as contradictory
          on its own -- only a STABLE, repeated non-matching reading
          is, mirroring the same "two consecutive reads" skepticism
          "absent" already applies to negative evidence.
        - "timeout": none of the above could be confirmed within the
          bounded settle window (Phase 5.10C: live-measured -- see
          docs/build-estimate.md Phase 5.10C for the percentile data
          _PROBE_SETTLE_TIMEOUT_S is calibrated from).

        Every poll is recorded to self.last_probe_visibility_polls
        (elapsed_s, grid_located, row_count, last_row identity) for
        live diagnostics, regardless of outcome -- Phase 5.10C Stage 1.

        Never raises. Shared by _cleanup_probe_item() (where "absent"
        means nothing to clean up, "contradiction"/"timeout" are hard
        failures) and _verify_group_once() (Phase 5.9A: where
        "observed" IS the group-activation verification signal,
        replacing the Grouping panel's optional/frequently-unavailable
        Subtotal pixel check)."""
        hwnd = self._ensure_main_window()
        probe_identity = (self._VERIFY_GROUP_PROBE_CATEGORY, self._VERIFY_GROUP_PROBE_SELECTOR)
        start = time.monotonic()
        deadline = time.time() + self._PROBE_SETTLE_TIMEOUT_S
        stable_at_baseline_reads = 0
        last_other_identity = None
        stable_other_identity_reads = 0
        self.last_probe_visibility_polls = []
        while time.time() < deadline:
            image, offset = self._capture_and_locate(hwnd)
            grid_located = offset is not None
            row_count = self._count_grid_rows(image, offset) if grid_located else None
            last_identity = None
            outcome = None
            if row_count is not None and row_count > target_row_count:
                last_identity = self._last_row_identity_matching_probe(image, offset, row_count)
                if last_identity == probe_identity:
                    outcome = "observed"
                elif last_identity in ((None, None), (None, ""), ("", None), ("", "")) or not any(last_identity):
                    # Unreadable this poll -- inconclusive, never counts
                    # against either "absent" or "contradiction".
                    stable_at_baseline_reads = 0
                    last_other_identity = None
                    stable_other_identity_reads = 0
                else:
                    stable_at_baseline_reads = 0
                    if last_identity == last_other_identity:
                        stable_other_identity_reads += 1
                        if stable_other_identity_reads >= 2:
                            outcome = "contradiction"
                    else:
                        last_other_identity = last_identity
                        stable_other_identity_reads = 1
            elif row_count is not None:  # row_count <= target_row_count
                stable_at_baseline_reads += 1
                last_other_identity = None
                stable_other_identity_reads = 0
                if stable_at_baseline_reads >= 2:
                    outcome = "absent"
            else:
                stable_at_baseline_reads = 0
                last_other_identity = None
                stable_other_identity_reads = 0

            self.last_probe_visibility_polls.append({
                "elapsed_s": round(time.monotonic() - start, 3), "grid_located": grid_located,
                "row_count": row_count, "last_row_identity": last_identity, "outcome_this_poll": outcome,
            })
            if outcome is not None:
                return outcome
            time.sleep(self._PROBE_SETTLE_POLL_S)
        return "timeout"

    def _cleanup_probe_item(self, target_row_count: int = 0) -> None:
        """Removes whatever verify_group()'s disposable probe item left
        behind -- EXCEPT ProtectedCommittedRowError (Phase 5.5D), which
        is deliberately let through: it means this specific cancel
        would have deleted a row Execute already successfully
        committed. `target_row_count` is the grid's row count BEFORE
        the probe was entered -- this cancels down to exactly that
        count, never unconditionally to zero, so a group that already
        held real committed rows (a resumed group re-verified after an
        earlier task in it already completed) keeps them.

        Phase 5.9 Priority 1/2 (live-caught): the ORIGINAL version
        trusted a SINGLE immediate post-commit grid read -- if that read
        happened to be stale (not yet repainted) and showed row_count
        already <= target_row_count, cleanup silently declared victory
        and returned having removed nothing, even though the probe row
        genuinely landed a moment later. Confirmed live: this left real
        garbage SFG/GUTA rows sitting in Rear Elevation and Left
        Elevation, undetected, with no audit trail (nothing was ever
        attempted, so DestructiveActionAuditor never even saw a call).
        Now requires POSITIVE, bounded-poll confirmation (via
        _wait_for_probe_visible(), Phase 5.9A) the probe row is
        actually visible -- identified by CAT/SEL, never by "last row"
        position alone -- before attempting to remove it, and POSITIVE
        confirmation the grid is back at target_row_count afterward.
        Raises ProbeCleanupFailedError (never silently continues) if
        either cannot be confirmed within the bounded settle window, or
        if the last row's identity ever fails to match the probe's own
        CAT/SEL (refusing to delete some other, unrelated last row
        'just because it's last')."""
        hwnd = self._ensure_main_window()
        probe_identity = (self._VERIFY_GROUP_PROBE_CATEGORY, self._VERIFY_GROUP_PROBE_SELECTOR)

        status = self._wait_for_probe_visible(target_row_count)
        if status == "absent":
            return  # positively confirmed, twice: nothing was ever added
        if status == "timeout":
            raise ProbeCleanupFailedError(
                f"_cleanup_probe_item(): could not positively observe the probe row {probe_identity!r} become "
                f"visible within {self._PROBE_SETTLE_TIMEOUT_S}s -- refusing to guess whether cleanup is needed."
            )
        if status == "contradiction":
            raise ProbeCleanupFailedError(
                f"_cleanup_probe_item(): the last row repeatedly read as a DIFFERENT, non-probe identity "
                f"instead of {probe_identity!r} -- refusing to guess whether cleanup is needed."
            )

        # status == "observed" -- remove exactly the identified probe
        # row(s), re-confirming identity before each deletion.
        for _attempt in range(6):
            image, offset = self._capture_and_locate(hwnd)
            row_count = self._count_grid_rows(image, offset) if offset is not None else None
            if row_count is not None and row_count <= target_row_count:
                break
            if row_count is None:
                time.sleep(0.3)
                continue
            identity = self._last_row_identity_matching_probe(image, offset, row_count)
            if identity != probe_identity:
                raise ProbeCleanupFailedError(
                    f"_cleanup_probe_item(): refusing to delete the last row -- its identity {identity!r} does "
                    f"not match the probe's own {probe_identity!r}."
                )
            try:
                self.cancel_current_item(reason="disposable_group_probe", caller="_cleanup_probe_item")
            except ProtectedCommittedRowError:
                raise
            except Exception:
                pass
            time.sleep(0.3)

        image, offset = self._capture_and_locate(hwnd)
        row_count = self._count_grid_rows(image, offset) if offset is not None else None
        if row_count is None or row_count > target_row_count:
            raise ProbeCleanupFailedError(
                f"_cleanup_probe_item(): row count is {row_count!r} after cleanup attempts, expected <= "
                f"{target_row_count} -- could not positively confirm the probe was removed."
            )
        self.commit_item()
        time.sleep(0.3)

    def delete_group(self, group_name: str, *, keep_names: list[str] | None = None, max_attempts: int = 5) -> bool:
        """Not part of the abstract contract, and NOT required by the
        execution-runner contract (ensure/select/verify are) -- provided
        only as a disposable-test/cleanup helper. Live-caught (Phase 5.1
        Stage 3): a single Delete attempt is NOT always reliable when
        more than one child group exists -- it can remove a DIFFERENT
        row than the one right-clicked (reproduced live even with the
        repaint-settle fix applied). Self-verifying and defensive: after
        each attempt, checks which group actually disappeared; if it
        was the wrong one, immediately recreates it (never leaves
        `keep_names` groups missing) before retrying. Bounded retries --
        never loops forever, never silently gives up either (returns
        False, does not raise, if it exhausts `max_attempts`).

        Phase 5.9 Stage 5: this method deletes an ENTIRE group (every
        row in it), yet -- unlike cancel_current_item() -- had NO
        protected-row check and NO destructive-action audit trail at
        all before this phase, even though it is exactly the kind of
        call Stage 5's "instrument all calls capable of removing an
        item" requirement names explicitly. Not currently called from
        the live Execute path (confirmed: only execution_runner.py's
        ensure/select/verify trio is), but a future/manual caller
        invoking this against a group holding rows this run already
        committed would previously have deleted them with zero warning
        and zero record. Refuses outright (raises
        ProtectedCommittedRowError, logged) if `group_name` has ANY
        rows protected this session -- deleting the whole group is
        strictly more destructive than cancel_current_item()'s single-
        row floor check, so the bar is "zero protected rows", not "at
        most N.\""""
        keep_names = keep_names or []
        protected_count = self._protected_row_ledger.count_for_group(group_name)
        if protected_count > 0:
            self._destructive_auditor.record(
                context=self._execution_context, method="delete_group", reason="user_requested_test_cleanup",
                caller="delete_group", target_type="group", target_identity=group_name,
                row_count_before=None, row_identities_before=None, row_count_after=None, row_identities_after=None,
                result="refused",
                exception=f"group {group_name!r} has {protected_count} row(s) protected this session -- refusing to delete the whole group",
            )
            raise ProtectedCommittedRowError(
                f"delete_group({group_name!r}): refusing -- this group has {protected_count} row(s) this run "
                f"already successfully committed and protected."
            )
        if not self.verify_application() or not self.verify_project():
            return False
        hwnd = self._ensure_main_window()
        self._force_foreground(hwnd)
        self._scroll_group_tree_to_top(hwnd)

        for _attempt in range(max_attempts):
            rows_before = self.snapshot_group_names()
            target_index = self._find_group_row(rows_before, group_name)
            if target_index is None:
                return True  # already gone

            image = self._capture_client_image(hwnd)
            header = self._locate_group_tree_header(image)
            if header is None:
                return False
            try:
                items = self._open_group_tree_context_menu(hwnd, header, target_index)
                self._click_group_menu_item(items, self._GROUP_MENU_DELETE_INDEX)
                time.sleep(0.8)
                dialog_hwnd = self._find_window_by_title("Delete Options")
                if dialog_hwnd is None:
                    continue
                OK_BUTTON = (55, 111)  # default radio: "Grouping member(s)"
                self._click_client(dialog_hwnd, *OK_BUTTON)
                time.sleep(1.0)
            except AdapterError:
                continue

            rows_after = self.snapshot_group_names()
            if self._find_group_row(rows_after, group_name) is not None:
                continue  # delete had no effect -- retry

            missing_keep = [k for k in keep_names if self._find_group_row(rows_after, k) is None]
            if not missing_keep:
                self._destructive_auditor.record(
                    context=self._execution_context, method="delete_group", reason="user_requested_test_cleanup",
                    caller="delete_group", target_type="group", target_identity=group_name,
                    row_count_before=len(rows_before), row_identities_before=rows_before,
                    row_count_after=len(rows_after), row_identities_after=rows_after,
                    result="deleted", exception=None,
                )
                return True
            # wrong group vanished -- restore it before retrying
            for k in missing_keep:
                try:
                    self.ensure_group(k)
                except AdapterError:
                    pass

        self._destructive_auditor.record(
            context=self._execution_context, method="delete_group", reason="user_requested_test_cleanup",
            caller="delete_group", target_type="group", target_identity=group_name,
            row_count_before=None, row_identities_before=None, row_count_after=None, row_identities_after=None,
            result="failed", exception=f"exhausted {max_attempts} attempt(s)",
        )
        return False
