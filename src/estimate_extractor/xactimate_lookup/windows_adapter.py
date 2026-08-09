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

import time
from dataclasses import dataclass, field
from pathlib import Path

from estimate_extractor.xactimate_lookup.adapter import (
    AdapterError,
    ProtectedCommittedRowError,
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
from estimate_extractor.xactimate_lookup.execution_diagnostics import RowLifecycleLedger
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
    the committed row STRUCTURALLY (row-count delta from a
    before-commit snapshot, plus the deterministic "insertions always
    append at the end" behavior observed throughout this project),
    never by searching OCR text across every row (Phase 4.7's
    `verify_committed_row()`/`CommittedRowVerification`, retired --
    see docs/xactimate-lookup.md Phase 4.7 and 4.8). WHAT was intended
    is already certain before this ever runs (`select_candidate()`
    acts on UIA-exact dropdown text, `extraction_confidence=1.0`);
    this only answers WHERE it landed and whether the data there is
    correct.

    `trust_state` is one of "VERIFIED", "REVIEW_REQUIRED",
    "VERIFICATION_FAILED", "CONFLICTING_ROW", "UNIT_MISMATCH",
    "QUANTITY_MISMATCH" -- see `verify_commit()`'s docstring for the
    exact precedence rules. A quantity or unit conflict is always a
    dedicated hard trust_state; it is never downgraded to
    "supporting" evidence. `category_observed`/`selector_observed`/
    `description_observed` are corroborating OCR reads at the
    structurally-identified row ONLY -- they never decide row
    identity, and an unreadable category/selector never blocks
    "VERIFIED" when the structural and quantity/unit evidence agree.
    `samples` holds one dict per polling attempt for diagnostics."""

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
        self._last_selected_row_count_before: int | None = None
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

    def _type_keybdevent(self, text: str) -> None:
        """The validated trigger mechanism: SendInput-based typing
        (pywinauto's default) never triggers Xactimate's live-search
        binding; the legacy keybd_event API does, reliably (5/5 trials,
        see docs/xactimate-lookup.md Phase 4.2B)."""
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
            time.sleep(0.1)

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

    def _ocr_text(self, image, psm: int = 7) -> str:
        pytesseract = self._pytesseract()
        return pytesseract.image_to_string(image, config=f"--psm {psm}").strip()

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
        pytesseract = self._pytesseract()
        from pytesseract import Output

        data = pytesseract.image_to_data(image, output_type=Output.DICT, config="--psm 11")
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

    def _count_grid_rows(self, image, offset: tuple[int, int]) -> int:
        """OCR's the '#' column beneath the grid header, counting
        distinct rows with numeric content -- used for before/after
        item-count mutation checks and to locate the most-recently-added
        row (assumed appended last, matching every row addition observed
        live).

        Serious, repeatedly-live-caught bug fixed here (see
        docs/xactimate-lookup.md Phase 4.4): the same column-gridline
        OCR bleed found elsewhere in this adapter (a stray "|" character)
        also affects the '#' column, producing text like "406 |" for a
        real, single-digit-only row number. The original `line.strip().
        isdigit()` check rejected any line with that trailing artifact
        outright, undercounting a real row as 0 -- which made
        cancel_current_item() silently return early ("nothing to
        clean up") on a grid that was NOT actually empty, reporting
        success without ever attempting a delete. This was caught only
        by repeatedly observing a "cleaned up" adapter call followed by
        the row still being visibly present on screen. Fixed by
        extracting the leading digit run from each line instead of
        requiring the whole line to be clean digits.

        Second, more serious bug (Phase 4.5): at 4+ rows, `--psm 6`
        (a "uniform block of text" assumption) misreads this narrow
        (~32px-wide) numeric column badly and non-randomly -- e.g. a
        real, clearly-legible "422" consistently read as "a2" or "ry",
        silently undercounting the grid by one or more rows. This
        directly caused a live wrong-row mutation: `enter_quantity()`
        computed the "last row" position from an undercounted total
        and entered a quantity into an existing, already-correct row
        instead of the newly-selected one, silently overwriting its
        value. `--psm 11` ("sparse text, no layout assumed") plus a 2x
        upscale reads every row correctly in the same live state where
        `--psm 6` failed -- swept scale 1x/2x/4x x five PSM modes
        before finding this combination.

        Third bug (Phase 4.6): the root cause behind both the Phase 4.5
        fix AND its own failure mode turned out to be the SAME thing --
        the crop height (a fixed, generous 400px band to accommodate
        an unknown row count) is mostly blank whenever fewer than ~15
        rows are present, and Tesseract's layout analysis is sensitive
        to that blank-space ratio in ways that flip which PSM mode
        works: `--psm 6` misreads a real "444" as "ast" on the full
        400px-tall crop, but reads it correctly on the same crop
        cropped down to just its top 100px; `--psm 11` reads a
        single row fine on the full crop but returns nothing on a
        2-row crop where the second row is legible to the eye.
        Neither a single PSM mode nor a single crop height is reliable
        across every row count. Fixed by trying multiple (crop height,
        PSM) combinations and taking the MAXIMUM row count found
        (not the first non-empty one): undercounting is the dangerous
        direction here (Phase 4.5's wrong-row mutation), while
        overcounting fails safe (a later step won't find a row that
        isn't really there and will raise, rather than silently
        acting on the wrong one). See docs/xactimate-lookup.md Phase
        4.6."""
        header = self._shifted_anchor("grid_header", offset)
        col_l, col_r = _GRID_COLUMNS["number"]
        dx, dy = offset
        col_l, col_r = col_l + dx, col_r + dx
        import re

        def rows_at(crop_height, psm, scale):
            crop = image.crop((col_l, header[3], col_r, header[3] + crop_height))
            crop = crop.resize((crop.width * scale, crop.height * scale))
            text = self._ocr_text(crop, psm=psm)
            return [line for line in text.splitlines() if re.match(r"^\s*\d+", line)]

        # Live-caught (Phase 4.6): even (crop_height, psm) alone isn't
        # enough -- the specific misread of one row's digits also
        # varies by upscale factor, non-deterministically producing
        # different non-digit-leading garbage at 2x vs. 3x on
        # otherwise-identical crops. Scale is swept alongside crop
        # height and PSM for the same reason: take the max valid count
        # across everything tried rather than trust any single
        # combination.
        combos = [(100, 6, 2), (100, 6, 3), (400, 11, 2), (400, 6, 2)]
        counts = [len(rows_at(h, psm, scale)) for h, psm, scale in combos]
        return max(counts)

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
        """Clicking the always-visible 'Items' tab (outside the
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
        l, t, r, b = _ANCHORS["items_tab"]
        self._click_client(hwnd, (l + r) // 2, (t + b) // 2)
        time.sleep(0.3)

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
        image, offset = self._capture_and_locate(hwnd)
        if offset is None:
            l, t, r, b = _ANCHORS["search_box"]
        else:
            l, t, r, b = self._shifted_anchor("search_box", offset)
        self._click_client(hwnd, (l + r) // 2, (t + b) // 2)
        time.sleep(0.2)

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

        hwnd = self._ensure_main_window()
        before_img, offset = self._capture_and_locate(hwnd)
        self._last_selected_row_count_before = (
            self._count_grid_rows(before_img, offset) if offset is not None else None
        )

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
        self._click_screen(cx, cy)
        if self.last_dropdown_diagnostics is not None:
            self.last_dropdown_diagnostics.selection_clicked_at = time.monotonic()
        time.sleep(1.0)

        # Live-discovered: re-selecting a CAT/SEL that already exists in the
        # active group pops a real "Duplicate Item(s)" modal ("SFG GUTA
        # already exists in UTILITY_ROO2, Continue?", Yes/No) instead of
        # silently adding/updating anything. Per the adapter contract this
        # is always a hard stop -- never guessed through, never
        # auto-dismissed here. recover() is what presses Escape afterward.
        if self._unexpected_dialog_present():
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
        reads = [self._read_populated_fields_once() for _ in range(3)]

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

    def _read_populated_fields_once(self) -> PopulatedFields:
        hwnd = self._ensure_main_window()
        image, offset = self._capture_and_locate(hwnd)
        if offset is None:
            raise AdapterError("Could not locate the grid ('Cat:' anchor) to read populated fields.")

        geom = self._last_row_geometry(image, offset)
        if geom is None:
            raise AdapterError("No grid row found to read populated fields from -- was select_candidate() called?")
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
        cat_crop = crop_col("category")
        cat_crop = cat_crop.resize((cat_crop.width * 4, cat_crop.height * 4))
        cat = self._ocr_text(cat_crop, psm=6)
        sel_crop = crop_col("selector")
        sel_crop = sel_crop.resize((sel_crop.width * 4, sel_crop.height * 4))
        # Live-caught (Phase 4.7): the selector crop can visually
        # include the neighboring activity symbol for a short code
        # (e.g. real "GUTA" OCR'd as "GUTA &") -- see
        # `_read_category_selector_at()`'s docstring for the full
        # explanation. Keeping only the first whitespace-separated
        # token is safe since a real selector never contains a space.
        sel_raw = self._ocr_text(sel_crop).strip()
        sel = sel_raw.split()[0] if sel_raw else ""
        act_crop = crop_col("activity")
        act_crop = act_crop.resize((act_crop.width * 4, act_crop.height * 4))
        act = self._ocr_text(act_crop)
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
        unit = self._ocr_text(unit_crop)

        return PopulatedFields(
            category=cat or None,
            selector=sel or None,
            description=desc or None,
            unit=unit or None,
            action=act or None,
            item_number=None,
        )

    def enter_quantity(self, quantity: float) -> None:
        hwnd = self._ensure_main_window()

        # Live-caught (Phase 4.5): a single-shot read of the grid
        # immediately after select_candidate()'s click can transiently
        # undercount rows -- reproduced live: a correctly-added third
        # row was reported as "row count did not increase" (raising
        # AdapterError) even though the row was visibly present and
        # correctly counted a moment later. Same class of post-
        # mutation render/OCR settle-timing gap as
        # verify_quantity_committed() below; bounded polling replaces
        # what was previously a single-shot check-and-raise. See
        # docs/xactimate-lookup.md Phase 4.5.
        start = time.time()
        geom = None
        while True:
            image, offset = self._capture_and_locate(hwnd, attempts=1, delay_s=0)
            if offset is not None:
                geom = self._last_row_geometry(image, offset)
                if geom is not None:
                    row_count, _ = geom
                    if self._last_selected_row_count_before is None or row_count > self._last_selected_row_count_before:
                        break
            if self._unexpected_dialog_present():
                raise AdapterError("enter_quantity(): an unexpected dialog appeared while waiting for the new grid row.")
            if time.time() - start >= 3.0:
                if offset is None:
                    raise AdapterError("Could not locate the grid to enter quantity.")
                if geom is None:
                    raise AdapterError("No grid row found to enter quantity into.")
                raise AdapterError(
                    f"Expected a new grid row after selection but row count did not increase "
                    f"within 3.0s (last observed row_count={geom[0]}, "
                    f"expected > {self._last_selected_row_count_before})."
                )
            time.sleep(0.3)

        row_count, last_row_top = geom
        col_l, col_r = _GRID_COLUMNS["quantity"]
        dx = offset[0]
        qx = (col_l + col_r) // 2 + dx
        qy = last_row_top + _GRID_ROW_HEIGHT // 2

        self._click_client(hwnd, qx, qy)
        time.sleep(0.2)
        self._select_all_and_delete()

        qty_text = f"{quantity:g}"
        self._type_keybdevent(qty_text)
        time.sleep(0.2)
        VK_TAB = 0x09
        self._press_key(VK_TAB)
        # Live-caught: the grid re-render/recalculation (RCV, group
        # subtotal, etc.) triggered by committing a quantity edit isn't
        # instantaneous -- a screenshot taken too soon after can land
        # mid-transition, causing anchor detection to intermittently fail
        # for a caller that reads the grid immediately afterward. 0.5s
        # was observed to be too short at least once; extended for
        # margin. See docs/xactimate-lookup.md Phase 4.4.
        time.sleep(1.0)

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

    def _read_quantity_at(self, image, offset: tuple[int, int], row_top: int) -> float | None:
        """Crops and reads the Quantity cell at an ARBITRARY row_top --
        extracted from `read_quantity()` (Phase 4.7) so the same
        live-verified reading strategy can be applied to a specific,
        identity-verified row (`verify_commit()`), not just "whatever
        the last row is"."""
        col_l, col_r = _GRID_COLUMNS["quantity"]
        dx = offset[0]
        crop = image.crop((col_l + dx, row_top, col_r + dx, row_top + _GRID_ROW_HEIGHT))
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
        return best_value if best_count >= 2 else None

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

    def snapshot_grid_identities(self) -> list[tuple[str | None, str | None]]:
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
        text across every row."""
        hwnd = self._ensure_main_window()
        image, offset = self._capture_and_locate(hwnd)
        if offset is None:
            return []
        geom = self._last_row_geometry(image, offset)
        if geom is None:
            return []
        row_count, _ = geom
        row_1_top = self._shifted_anchor("grid_row_1", offset)[1]
        return [
            self._read_category_selector_at(image, offset, row_1_top + i * _GRID_ROW_HEIGHT)
            for i in range(row_count)
        ]

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
        2. `delta == 1` (exactly one row appended) is the only
           structurally trustworthy outcome -- every row insertion
           observed anywhere in this project has always appended at
           the end, so the new row is deterministically
           `row_index = row_count_after - 1`. No text match is needed
           to find it.
        3. The first `len(before_snapshot)` rows after commit must
           equal `before_snapshot` exactly (`preexisting_rows_
           unchanged`). If not, the state is not trustworthy even
           though the count delta looks right.
        4. `delta` not in `{0, 1}`, or pre-existing rows changed ->
           `trust_state="CONFLICTING_ROW"` (refuses to guess which
           row is the committed one).
        5. `delta == 0` through `timeout_s` ->
           `trust_state="VERIFICATION_FAILED"` (commit not detected
           in the grid within the bounded window).
        6. Once the row is structurally identified, quantity and unit
           are read AT THAT KNOWN POSITION and remain load-bearing,
           exactly as in Phase 4.7 (a quantity match never overrides
           a unit conflict): unreadable quantity or a unit state of
           "review_required" -> `trust_state="REVIEW_REQUIRED"`; a
           quantity that was read but disagrees ->
           `trust_state="QUANTITY_MISMATCH"`; an incompatible unit ->
           `trust_state="UNIT_MISMATCH"` (checked after quantity, but
           neither ever downgrades to merely "supporting").

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
        8. `trust_state="VERIFIED"` requires ALL of: delta==1,
           pre-existing rows unchanged, quantity read and matched
           exactly, unit compatible, and category/selector OCR either
           agrees or is unreadable (never contradicts).

        `_unexpected_dialog_present()` at any point during polling ->
        `trust_state="VERIFICATION_FAILED"` (wrong context).

        Every attempt's instrumentation is recorded in `.samples`,
        matching Phase 4.7 Stage 4's precedent. See
        docs/xactimate-lookup.md Phase 4.8."""
        start = time.time()
        samples: list[dict] = []
        attempts = 0
        row_count_before = len(before_snapshot)

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

            after = self.snapshot_grid_identities()
            row_count_after = len(after)
            delta = row_count_after - row_count_before
            sample: dict = {"elapsed_s": round(elapsed, 3), "row_count_after": row_count_after, "delta": delta}
            samples.append(sample)

            if delta == 1:
                preexisting_unchanged = after[:row_count_before] == before_snapshot
                if not preexisting_unchanged:
                    return fail(
                        "CONFLICTING_ROW",
                        f"row count increased by exactly 1 but the first {row_count_before} rows no longer match "
                        f"the pre-commit snapshot -- refusing to trust which row is the committed one",
                        row_count_after=row_count_after,
                        preexisting_rows_unchanged=False,
                    )

                row_index = row_count_after - 1
                hwnd = self._ensure_main_window()
                image, offset = self._capture_and_locate(hwnd, attempts=1, delay_s=0)
                if offset is None:
                    return fail(
                        "VERIFICATION_FAILED", "could not re-locate the grid to read the committed row",
                        row_count_after=row_count_after,
                    )
                row_top = self._shifted_anchor("grid_row_1", offset)[1] + row_index * _GRID_ROW_HEIGHT

                cat_observed, sel_observed = self._read_category_selector_at(image, offset, row_top)
                description_observed = self._read_description_at(image, offset, row_top)
                quantity_observed = self._read_quantity_at(image, offset, row_top)
                unit_raw, _unit_norm_unused = self._read_unit_at(image, offset, row_top)

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
                            unit_raw, _unit_norm_unused = self._read_unit_at(image, offset, row_top)

                sample["category_observed"] = cat_observed
                sample["selector_observed"] = sel_observed
                sample["description_observed"] = description_observed
                sample["quantity_observed"] = quantity_observed
                sample["unit_raw"] = unit_raw

                quantity_matched = quantity_observed is not None and quantity_observed == expected_quantity

                # Phase 5.6 Stage 4: prefer the pre-commit, majority-
                # voted populated-field unit read over this method's own
                # single-shot post-commit OCR read whenever it resolves
                # to a known unit -- see class docstring above.
                populated_unit_vocab = _resolve_observed_unit_vocab(populated_unit)
                unit_source = "populated_field" if populated_unit_vocab is not None else "post_commit_ocr"
                observed_unit_for_check = populated_unit if populated_unit_vocab is not None else unit_raw

                unit_result = check_unit_compatibility(source_unit, expected_xactimate_unit, observed_unit_for_check)
                compatibility = self._UNIT_STATE_TO_COMPATIBILITY.get(unit_result.unit_match_state, "review_required")
                sample["unit_source"] = unit_source

                cat_sel_present = cat_observed is not None or sel_observed is not None
                cat_sel_agrees = (cat_observed == category and sel_observed == selector) if cat_sel_present else None
                cat_sel_contradicts = cat_sel_present and not cat_sel_agrees

                if quantity_observed is None:
                    trust_state = "REVIEW_REQUIRED"
                    reason = "quantity could not be read at the structurally-identified row"
                elif not quantity_matched:
                    trust_state = "QUANTITY_MISMATCH"
                    reason = f"expected quantity {expected_quantity}, observed {quantity_observed!r} at the structurally-identified row"
                elif compatibility == "hard_stop":
                    trust_state = "UNIT_MISMATCH"
                    reason = unit_result.unit_match_reason
                elif compatibility == "review_required":
                    trust_state = "REVIEW_REQUIRED"
                    reason = unit_result.unit_match_reason
                elif cat_sel_contradicts:
                    trust_state = "REVIEW_REQUIRED"
                    reason = (
                        f"structural evidence (row-count delta and unchanged pre-existing rows) and quantity/unit "
                        f"both agree, but category/selector OCR at the committed row read "
                        f"{cat_observed}/{sel_observed}, which contradicts the expected {category}/{selector} "
                        f"-- a human should confirm"
                    )
                else:
                    trust_state = "VERIFIED"
                    reason = (
                        "exactly one row appended at the deterministic last position, pre-existing rows unchanged, "
                        "quantity matched, unit compatible, and category/selector OCR "
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
                return fail(
                    "VERIFICATION_FAILED",
                    f"row count did not change within {timeout_s}s ({attempts} attempts) -- commit not detected",
                    row_count_after=row_count_after,
                )
            time.sleep(0.1 if attempts < 5 else 0.4)

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
            rows.append(self._ocr_text(crop, psm=7).strip())
        return rows

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
        it -- see `ensure_group()`)."""
        left, top = header_pos[0], header_pos[1]
        row_top = self._group_tree_row_crop_top(top, row_index)
        crop = image.crop((
            left + self._GROUP_TREE_TEXT_DX, row_top,
            left + self._GROUP_TREE_TEXT_DX + self._GROUP_TREE_TEXT_WIDTH, row_top + self._GROUP_TREE_ROW_CROP_HEIGHT,
        ))
        return self._ocr_text(crop, psm=7).strip()

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
        l, t, r, b = _ANCHORS["components_tab"]
        self._click_client(hwnd, (l + r) // 2, (t + b) // 2)
        time.sleep(0.5)
        self._reset_scroll_state()  # clicks back to the Items tab

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
        # extra settle sleep, never an unbounded wait.
        for _attempt in range(3):
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
            time.sleep(0.8)
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
        """Not part of the abstract contract. Independently confirms
        `group_name` is the group new items actually land in.

        Live-caught (Phase 5.2 Stage 3): a group verified moments after
        `ensure_group()` creates it can transiently fail this check even
        though it genuinely is the active group -- the tree control can
        still be settling from the creation/selection sequence (the
        same class of timing issue `ensure_group()`'s own post-creation
        check hit). A false NEGATIVE here is safe (it only routes tasks
        to REVIEW_REQUIRED, never executes against an unconfirmed
        group), but retrying once, from a completely fresh probe, before
        giving up avoids unnecessarily blocking a genuinely-fine group.
        Never masks a real negative: each attempt is a full independent
        re-measurement, not a cached/assumed result, and this still
        returns False, never raises, if every attempt fails.

        Phase 5.7B: Stage 1/2 tested whether a non-mutating (pixel/
        visual selection-highlight) check could replace the disposable
        SFG/GUTA probe entirely. Live-measured across 15 alternating
        switches over 5 real groups, cross-checked against this same
        probe as ground truth: the highlight reliably identified 3 of 5
        groups (Dwelling Roof, Front Elevation, Rear Elevation -- a
        clear, confident darkening every time) but never confidently
        detected the highlight at all for the other 2 (Exterior,
        Fence), even though the probe confirmed them genuinely active
        both times tested -- reconfirming, with fresh live evidence,
        the original Phase 5.1 finding that the visual highlight does
        not reliably track the active group for every row position.
        The probe therefore remains necessary for a confident answer,
        but IS now cached per adapter instance (`use_cache=True`,
        the default): once a group has been positively verified THIS
        session, a later call for the SAME name returns the cached
        True immediately rather than re-probing -- exactly the
        "already-verified, no context-loss" case
        _ensure_select_verify_group() hits on a partial-resume within
        the same live run. Pass use_cache=False to force a fresh probe
        regardless (used by diagnostics that need real-time ground
        truth, and by tests of the probe itself)."""
        normalized = group_name.strip().lower()
        if use_cache and normalized in self._verified_groups_this_session:
            return True
        for _attempt in range(2):
            if self._verify_group_once(group_name):
                self._verified_groups_this_session.add(normalized)
                return True
            time.sleep(0.5)
        return False

    def _verify_group_once(self, group_name: str) -> bool:
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
            baseline_identities = self.snapshot_grid_identities()
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
            self.enter_quantity(1)
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
                            raise ProbeCleanupFailedError(
                                f"_verify_group_once({group_name!r}): pre-existing rows changed during probe "
                                f"cleanup -- before={baseline_identities!r} after={after_identities!r}."
                            )
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
                except Exception:
                    pass

    #: Phase 5.9 Priority 1: bounded settle-poll for the probe's own
    #: commit to actually become observable in the grid before cleanup
    #: trusts any read -- live-measured (see priority1_settle_timing
    #: evidence in the Phase 5.9 report) that a single immediate
    #: post-commit read can be stale.
    _PROBE_SETTLE_POLL_S = 0.15
    _PROBE_SETTLE_TIMEOUT_S = 3.0

    def _last_row_identity(self, image, offset, row_count) -> tuple[str | None, str | None]:
        row_1_top = self._shifted_anchor("grid_row_1", offset)[1]
        last_row_top = row_1_top + (row_count - 1) * _GRID_ROW_HEIGHT
        return self._read_category_selector_at(image, offset, last_row_top)

    def _wait_for_probe_visible(self, target_row_count: int) -> str:
        """Phase 5.9A: bounded-poll for the disposable group-
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
        - "timeout": neither could be confirmed within the bounded
          settle window.

        Never raises. Shared by _cleanup_probe_item() (where "absent"
        means nothing to clean up and "timeout" is a hard failure) and
        _verify_group_once() (Phase 5.9A: where "observed" IS the
        group-activation verification signal, replacing the Grouping
        panel's optional/frequently-unavailable Subtotal pixel check)."""
        hwnd = self._ensure_main_window()
        probe_identity = (self._VERIFY_GROUP_PROBE_CATEGORY, self._VERIFY_GROUP_PROBE_SELECTOR)
        deadline = time.time() + self._PROBE_SETTLE_TIMEOUT_S
        stable_at_baseline_reads = 0
        while time.time() < deadline:
            image, offset = self._capture_and_locate(hwnd)
            row_count = self._count_grid_rows(image, offset) if offset is not None else None
            if row_count is not None and row_count > target_row_count:
                stable_at_baseline_reads = 0
                if self._last_row_identity(image, offset, row_count) == probe_identity:
                    return "observed"
            elif row_count is not None:  # row_count <= target_row_count
                stable_at_baseline_reads += 1
                if stable_at_baseline_reads >= 2:
                    return "absent"
            else:
                stable_at_baseline_reads = 0
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
            identity = self._last_row_identity(image, offset, row_count)
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
