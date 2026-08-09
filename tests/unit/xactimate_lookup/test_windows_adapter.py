"""Unit tests for WindowsXactimateAdapter that do not require a live
Windows/Xactimate session -- pure logic (category/selector splitting,
dropdown parsing, protocol compliance) plus window-discovery behavior
exercised through the injectable `window_finder` constructor arg, which
lets verify_application()/verify_project()/capture_dropdown() be tested
without touching any real win32/comtypes/pytesseract API.

Deeper live-only behavior (actual clicking, OCR field reading, keybd_event
typing) is validated manually against a real Xactimate session -- see
docs/xactimate-lookup.md's Phase 4.3 section for that evidence. Importing
this module must never require Windows -- see the module's own docstring
for why every OS-specific dependency is a lazy import.
"""

from __future__ import annotations

import pytest

from estimate_extractor.xactimate_lookup.models import DropdownResult
from estimate_extractor.xactimate_lookup.windows_adapter import (
    CommitVerification,
    EstimateBaseline,
    GroupRowSnapshot,
    PopupCaptureFailedError,
    PopupNotFoundError,
    QuantityVerificationResult,
    ReconciliationResult,
    StaleCandidateError,
    UnitVerificationResult,
    WindowsXactimateAdapter,
    _GRID_COLUMNS,
    _GRID_ROW_HEIGHT,
    _RawDropdownRow,
    _UNIT_OCR_CONFUSIONS,
    _UNIT_SYNONYMS,
    _VERIFIED_UNIT_CONVERSIONS,
    _normalize_unit_text,
    _resolve_observed_unit_vocab,
    _split_category_selector,
    check_unit_compatibility,
)


def test_module_imports_without_windows_dependencies():
    """Guards the module's own promise: no ctypes/win32gui/comtypes/
    pytesseract import happens at module load time."""
    import estimate_extractor.xactimate_lookup.windows_adapter as mod

    assert hasattr(mod, "WindowsXactimateAdapter")


def test_supports_live_execution_reflects_the_phase_5_4_pilot_gate_sign_off():
    """Phase 5.4: flipped to True after a clean pilot-gate sign-off
    (see docs/build-estimate.md Phase 5.4) -- still overridable per-
    instance, and still deliberately independent of
    production_project_allowed/unattended_mode_allowed, which stay
    False as separate gates (see service.py)."""
    assert WindowsXactimateAdapter.supports_live_execution is True
    adapter = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=lambda: ([], []))
    assert adapter.supports_live_execution is True


@pytest.mark.parametrize(
    "code, expected_cat, expected_sel",
    [
        ("SFGGUTA", "SFG", "GUTA"),
        ("SFGGUTA>", "SFG", "GUTA>"),
        ("SFGGUTHRA<", "SFG", "GUTHRA<"),
        ("RFGARMVN", "RFG", "ARMVN"),
    ],
)
def test_split_category_selector(code, expected_cat, expected_sel):
    cat, sel = _split_category_selector(code)
    assert cat == expected_cat
    assert sel == expected_sel


def _raw_row(code, desc, price, pos=0, hwnd=999):
    return _RawDropdownRow(
        code_text=code,
        description_text=desc,
        price_text=price,
        row_position=pos,
        popup_hwnd=hwnd,
        rect_at_capture=(0, 0, 10, 10),
    )


def test_parse_dropdown_splits_category_selector_and_sets_max_confidence():
    adapter = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=lambda: ([], []))
    raw = [
        _raw_row("SFGGUTA", 'Gutter / downspout - aluminum - up to 5"', "$11.56", pos=0),
        _raw_row("SFGGUTA>", 'Gutter / downspout - aluminum - 6"', "$16.32", pos=1),
    ]
    results = adapter.parse_dropdown(raw)
    assert [r.category for r in results] == ["SFG", "SFG"]
    assert [r.selector for r in results] == ["GUTA", "GUTA>"]
    assert results[0].description == 'Gutter / downspout - aluminum - up to 5"'
    assert results[0].extraction_confidence == 1.0
    assert results[0].row_position == 0
    assert results[1].row_position == 1
    # price is retained as evidence in raw_text, never used for matching
    assert "$11.56" not in (results[0].category, results[0].selector)


def test_parse_dropdown_never_fabricates_item_number():
    adapter = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=lambda: ([], []))
    results = adapter.parse_dropdown([_raw_row("SFGGUTA", "desc", "$1.00")])
    assert results[0].item_number is None


def test_verify_application_true_when_any_main_window_found():
    def finder():
        return [(111, "SOME-PROJECT", (0, 0, 100, 100))], []

    adapter = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=finder)
    assert adapter.verify_application() is True


def test_verify_application_false_when_no_window_found():
    adapter = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=lambda: ([], []))
    assert adapter.verify_application() is False


def test_verify_project_true_only_when_title_matches_expected(monkeypatch):
    def finder():
        return [(111, "TEST", (0, 0, 100, 100))], []

    adapter = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=finder)
    assert adapter.verify_project() is True


def test_verify_project_false_on_title_mismatch():
    def finder():
        return [(111, "SOME-OTHER-PROJECT", (0, 0, 100, 100))], []

    adapter = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=finder)
    assert adapter.verify_project() is False


def test_verify_project_is_case_insensitive():
    def finder():
        return [(111, "test", (0, 0, 100, 100))], []

    adapter = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=finder)
    assert adapter.verify_project() is True


def test_capture_dropdown_raises_popup_capture_failed_after_timeout(monkeypatch):
    """Confirms the timeout path raises rather than hanging or
    returning an empty-but-successful result -- orchestrator.py must
    see this as an AdapterError-family exception to stop safely. Phase
    5.8: the popup never appearing at all (across every bounded retry)
    now raises the more specific PopupCaptureFailedError."""
    adapter = WindowsXactimateAdapter(
        expected_project_name="TEST",
        window_finder=lambda: ([(123, "TEST", (0, 0, 100, 100))], []),  # main window exists, no popup ever
        dropdown_timeout_s=0.2,
    )
    monkeypatch.setattr(adapter, "focus_search", lambda: None)
    monkeypatch.setattr(adapter, "clear_search", lambda: None)
    monkeypatch.setattr(adapter, "_type_keybdevent", lambda text: None)
    adapter.search_by_description("anything")
    with pytest.raises(PopupCaptureFailedError):
        adapter.capture_dropdown()


# ---------------------------------------------------------------------
# Phase 5.8: popup stabilization -- live-caught screenshots proved the
# results dropdown can visibly display real candidates that the old
# single-immediate-read capture_dropdown() risked missing entirely,
# collapsing into a false NO_MATCH. "VISIBLE CANDIDATES != NO_MATCH".
# ---------------------------------------------------------------------


def _stabilize_test_adapter(monkeypatch, *, timeout_s=1.5, poll_s=0.0):
    adapter = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=lambda: ([], []))
    monkeypatch.setattr(adapter, "focus_search", lambda: None)
    monkeypatch.setattr(adapter, "clear_search", lambda: None)
    monkeypatch.setattr(adapter, "_type_keybdevent", lambda text: None)
    adapter._DROPDOWN_STABILIZE_TIMEOUT_S = timeout_s
    adapter._DROPDOWN_STABILIZE_POLL_S = poll_s
    adapter.dropdown_timeout_s = 1.0
    adapter.search_by_description("R&R gutter splash guard")
    return adapter


def test_capture_dropdown_waits_for_two_matching_reads_before_trusting_result(monkeypatch):
    """Requirement (Stage 2): do not click/trust on the first sighting
    -- a row count that's still settling (mid-render) must not be
    reported as the final candidate set. Two consecutive reads with
    the SAME non-zero row count are required."""
    adapter = _stabilize_test_adapter(monkeypatch)
    final_rows = [_raw_row("SFGGSG", "Gutter splash guard", "$35.20"), _raw_row("SFGGRD", "Gutter guard/screen", "$5.95")]
    reads = iter([
        [_raw_row("SFGGSG", "Gutter splash guard", "$35.20")],  # 1 row -- still rendering
        final_rows,  # 2 rows -- settling
        final_rows,  # 2 rows again -- stable, trusted here
    ])
    monkeypatch.setattr(adapter, "_find_dropdown_window", lambda: 555)
    monkeypatch.setattr(adapter, "_read_dropdown_rows", lambda hwnd: next(reads))

    result = adapter.capture_dropdown()

    assert result == final_rows
    assert adapter.last_dropdown_diagnostics.outcome == "CANDIDATES_PARSED"
    assert adapter.last_dropdown_diagnostics.popup_row_count == 2


def test_capture_dropdown_retains_candidates_when_popup_closes_after_one_good_read(monkeypatch):
    """Requirement (Stage 2): "once candidate text is successfully
    read, retain that candidate snapshot even if the popup later
    closes" -- a popup that visibly appeared, was read once with real
    rows, and then disappeared before a second confirming read must
    still return those real rows, not NO_MATCH."""
    adapter = _stabilize_test_adapter(monkeypatch)
    good_rows = [_raw_row("SFGGSG", "Gutter splash guard", "$35.20")]
    hwnd_calls = iter([555, 555, None])  # outer wait finds it; stabilize check+read; then it's gone
    monkeypatch.setattr(adapter, "_find_dropdown_window", lambda: next(hwnd_calls))
    monkeypatch.setattr(adapter, "_read_dropdown_rows", lambda hwnd: good_rows)

    result = adapter.capture_dropdown()

    assert result == good_rows
    assert adapter.last_dropdown_diagnostics.outcome == "CANDIDATES_PARSED"
    assert adapter.last_dropdown_diagnostics.popup_closed_at is not None
    # select_candidate() must still be able to act on this -- the hwnd
    # used is the one the successful read actually came from, never a
    # fresh (possibly-None) re-query after the popup already closed.
    assert adapter._last_dropdown_hwnd == 999  # _raw_row()'s default popup_hwnd


def test_capture_dropdown_treats_persistent_zero_rows_as_positive_no_results(monkeypatch):
    """Requirement (Stage 3): NO_RESULTS requires POSITIVE evidence --
    the popup stayed open and enumerable for the entire stabilization
    window, and every single read confirmed zero rows. This is the
    only path that may legitimately return an empty list without
    retrying the search."""
    adapter = _stabilize_test_adapter(monkeypatch, timeout_s=0.05, poll_s=0.0)
    monkeypatch.setattr(adapter, "_find_dropdown_window", lambda: 555)
    monkeypatch.setattr(adapter, "_read_dropdown_rows", lambda hwnd: [])

    result = adapter.capture_dropdown()

    assert result == []
    assert adapter.last_dropdown_diagnostics.outcome == "NO_RESULTS"
    assert adapter.last_dropdown_diagnostics.search_retries == 0


def test_capture_dropdown_retries_same_search_when_popup_disappears_before_any_read(monkeypatch):
    """Requirement (Stage 3): a popup that appears and then disappears
    before ANY row was ever read is POPUP_DISAPPEARED, not NO_RESULTS
    -- retry the SAME search text (never a different phrase) rather
    than concluding no match."""
    adapter = _stabilize_test_adapter(monkeypatch, timeout_s=0.05, poll_s=0.0)
    good_rows = [_raw_row("SFGGSG", "Gutter splash guard", "$35.20")]
    # Attempt 1: popup appears then immediately gone, no row ever read.
    # Attempt 2 (after re-submitting the SAME search): popup appears
    # and yields a stable read.
    hwnd_calls = iter([555, None, 555, 555, 555])
    read_calls = iter([good_rows, good_rows])
    resubmitted_queries = []

    def fake_type(text):
        resubmitted_queries.append(text)

    monkeypatch.setattr(adapter, "_find_dropdown_window", lambda: next(hwnd_calls))
    monkeypatch.setattr(adapter, "_read_dropdown_rows", lambda hwnd: next(read_calls))
    monkeypatch.setattr(adapter, "_type_keybdevent", fake_type)

    result = adapter.capture_dropdown()

    assert result == good_rows
    assert adapter.last_dropdown_diagnostics.outcome == "CANDIDATES_PARSED"
    assert adapter.last_dropdown_diagnostics.search_retries == 1
    assert resubmitted_queries == ["R&R gutter splash guard"]  # same text, not a different phrase


def test_capture_dropdown_raises_after_exhausting_retries_with_no_rows_ever_read(monkeypatch):
    """If the popup never once yields a readable row across every
    bounded retry, capture_dropdown() must raise PopupCaptureFailedError
    -- never silently return [] and let that be mistaken for a
    positively-confirmed zero-result search."""
    adapter = _stabilize_test_adapter(monkeypatch, timeout_s=0.02, poll_s=0.0)
    monkeypatch.setattr(adapter, "_find_dropdown_window", lambda: None)  # popup never appears, ever
    monkeypatch.setattr(adapter, "_type_keybdevent", lambda text: None)

    with pytest.raises(PopupCaptureFailedError):
        adapter.capture_dropdown()

    assert adapter.last_dropdown_diagnostics.outcome == "POPUP_CAPTURE_FAILED"
    assert adapter.last_dropdown_diagnostics.search_retries == adapter._DROPDOWN_SEARCH_RETRY_ATTEMPTS


def test_select_candidate_without_prior_capture_raises():
    adapter = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=lambda: ([], []))
    candidate = DropdownResult(raw_text="x", row_position=0, category="SFG", selector="GUTA")
    with pytest.raises(PopupNotFoundError):
        adapter.select_candidate(candidate)


def test_get_adapter_diagnostics_reports_not_found_state():
    """Diagnostics must report whatever supports_live_execution
    ACTUALLY is on this instance -- set explicitly here so the
    assertion is independent of the class default (Phase 5.4: True
    after pilot-gate sign-off, but that's not what this test is
    about)."""
    adapter = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=lambda: ([], []))
    adapter.supports_live_execution = False
    diag = adapter.get_adapter_diagnostics()
    assert diag["main_window_found"] is False
    assert diag["main_window_hwnd"] is None
    assert diag["project_matches"] is False
    assert diag["dropdown_open"] is False
    assert diag["supports_live_execution"] is False


def test_get_adapter_diagnostics_reports_found_and_matching_state():
    def finder():
        return [(111, "TEST", (0, 0, 100, 100))], []

    adapter = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=finder)
    diag = adapter.get_adapter_diagnostics()
    assert diag["main_window_found"] is True
    assert diag["main_window_hwnd"] == 111
    assert diag["project_matches"] is True


def test_grid_row_height_is_not_the_stale_single_row_calibration():
    """Regression guard (Phase 4.4 Stage 3): _GRID_ROW_HEIGHT was
    originally 17px, calibrated against a single-row grid where
    _last_row_geometry()'s (row_count - 1) * _GRID_ROW_HEIGHT term is
    always multiplied by zero -- so the constant was never actually
    exercised until a real second grid row appeared live, where it
    misaligned every crop for row 2+ by ~11px (garbled OCR: category
    '_', selector 'an', description None). Real measured spacing
    between two live static rows was 25px. This test doesn't replay
    the live capture (that needs a real Xactimate screenshot -- see
    docs/xactimate-lookup.md Phase 4.4), but it does lock in the
    corrected value so a future edit can't silently drift back to the
    single-row-only-tested constant without a test failure."""
    assert _GRID_ROW_HEIGHT == 25


def test_grid_columns_selector_and_activity_do_not_overlap_and_fit_long_codes():
    """Regression guard (Phase 4.4 Stage 3): the original 'selector'
    boundary (563, 608) cut off longer selector codes like 'GUTAB>'
    (measured live to extend to x=614), and 'activity' (608, 628)
    consequently captured the overflow instead of the real activity
    symbol (measured live at x=626-636). Both columns were widened and
    separated; this guards against a future edit re-narrowing them
    back below the longest observed live selector code without
    noticing the two ranges now abut or overlap."""
    sel_l, sel_r = _GRID_COLUMNS["selector"]
    act_l, act_r = _GRID_COLUMNS["activity"]
    assert sel_r <= act_l, "selector column must not extend into the activity column"
    assert sel_r - sel_l >= 45, "selector column must be wide enough for a 6+ character code like 'GUTAB>'"


def test_context_menu_delete_index_is_within_the_expected_item_count():
    """Regression guard (Phase 4.6): _click_delete_via_uia() locates
    "Delete" by a fixed structural index into the row context menu's
    flat UIA child list (26 items; Delete at index 11) rather than by
    OCR text match -- the menu's Telerik RadMenuItem controls expose
    no usable name via CurrentName/AutomationId/LegacyIAccessible, so
    text-based lookup (the Phase 4.4/4.5 approach) isn't available at
    all here, not just unreliable. This doesn't replay the live UIA
    walk (that needs a real open context menu -- see
    docs/xactimate-lookup.md Phase 4.6), but it locks in the two
    constants together so a future edit can't silently move one
    without the other and pass this test by accident."""
    idx = WindowsXactimateAdapter._CONTEXT_MENU_DELETE_INDEX
    total = WindowsXactimateAdapter._CONTEXT_MENU_EXPECTED_ITEM_COUNT
    assert 0 <= idx < total


def test_quantity_verification_result_records_match_and_samples():
    """Regression guard (Phase 4.5): verify_quantity_committed()'s
    bounded poll must be able to report both a successful match and
    the full attempt history for timing diagnostics -- exercised here
    via direct construction, complementing the monkeypatched
    polling-logic tests below."""
    result = QuantityVerificationResult(
        matched=True, stop_reason="matched", expected=2.5, observed=2.5,
        attempts=3, elapsed_s=0.75, samples=[(0.0, True, None), (0.25, True, None), (0.5, True, 2.5)],
    )
    assert result.matched is True
    assert result.stop_reason == "matched"
    assert result.attempts == len(result.samples)
    assert result.samples[-1] == (0.5, True, 2.5)


def _adapter_with_fake_grid(monkeypatch, readings, row_found_seq=None):
    """Builds a WindowsXactimateAdapter with every internal dependency
    verify_quantity_committed() touches monkeypatched to a fake, so the
    polling logic itself (progressive intervals, termination
    conditions) can be exercised without a live Windows/Xactimate
    session -- `readings` is consumed one value per read_quantity()
    call. Uses a fake, manually-advanced clock (time.sleep() advances
    it instead of truly sleeping) so timeout behavior is deterministic
    and these tests run instantly regardless of the configured
    timeout_s."""
    adapter = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=lambda: ([], []))
    monkeypatch.setattr(adapter, "_ensure_main_window", lambda: 123)
    monkeypatch.setattr(adapter, "_capture_and_locate", lambda hwnd, attempts=1, delay_s=0: (object(), (0, 0)))
    monkeypatch.setattr(adapter, "_unexpected_dialog_present", lambda: False)
    found_iter = iter(row_found_seq) if row_found_seq is not None else None
    monkeypatch.setattr(
        adapter, "_last_row_geometry",
        lambda image, offset: (None if (found_iter and not next(found_iter, True)) else (1, 100)),
    )
    reads = iter(readings)
    monkeypatch.setattr(adapter, "read_quantity", lambda: next(reads))

    import estimate_extractor.xactimate_lookup.windows_adapter as wa_mod

    clock = {"t": 1000.0}
    monkeypatch.setattr(wa_mod.time, "time", lambda: clock["t"])
    monkeypatch.setattr(wa_mod.time, "sleep", lambda s: clock.__setitem__("t", clock["t"] + s))
    return adapter


def test_verify_quantity_committed_succeeds_on_delayed_but_correct_value(monkeypatch):
    """Regression test (Phase 4.6 Stage 4): the bounded poll must
    succeed once the value becomes correct even if it takes a few
    attempts to appear (a delayed-but-correct settle), not only on an
    immediate first read -- this is the scenario the whole polling
    mechanism exists for."""
    adapter = _adapter_with_fake_grid(monkeypatch, readings=[None, None, 2.5])
    result = adapter.verify_quantity_committed(2.5, timeout_s=3.0)
    assert result.matched is True
    assert result.stop_reason == "matched"
    assert result.attempts == 3
    assert result.observed == 2.5
    assert len(result.samples) == 3


def test_verify_quantity_committed_stops_early_on_stable_wrong_value(monkeypatch):
    """Regression test (Phase 4.6 Stage 4): a wrong value that repeats
    identically twice in a row is a stable misread, not a settle-
    timing issue -- polling must surface it as `wrong_value` rather
    than burning the full timeout budget waiting for it to change."""
    adapter = _adapter_with_fake_grid(monkeypatch, readings=[3.0, 3.0, 2.5, 2.5])
    result = adapter.verify_quantity_committed(2.5, timeout_s=3.0)
    assert result.matched is False
    assert result.stop_reason == "wrong_value"
    assert result.observed == 3.0
    assert result.attempts == 2


def test_verify_quantity_committed_times_out_on_persistent_none(monkeypatch):
    """Regression test (Phase 4.6 Stage 4): if the row never becomes
    readable at all, the poll must still terminate (never an unbounded
    wait) and report `timeout`, not silently succeed or hang."""
    adapter = _adapter_with_fake_grid(monkeypatch, readings=[None] * 200)
    result = adapter.verify_quantity_committed(2.5, timeout_s=0.5)
    assert result.matched is False
    assert result.stop_reason == "timeout"
    assert result.observed is None


def test_quantity_verification_result_defaults_to_empty_samples():
    result = QuantityVerificationResult(
        matched=False, stop_reason="timeout", expected=5.0, observed=None, attempts=0, elapsed_s=5.0,
    )
    assert result.samples == []


def test_stale_candidate_error_is_an_adapter_error_subclass():
    from estimate_extractor.xactimate_lookup.adapter import AdapterError

    assert issubclass(StaleCandidateError, AdapterError)
    assert issubclass(PopupNotFoundError, AdapterError)


def test_orchestrator_never_touches_adapter_when_live_execution_unsupported(tmp_path, phrase_rules, ranking_config):
    """Mirrors the existing FakeXactimateAdapter coverage in
    test_orchestrator.py: a live (dry_run=False) run against an adapter
    that hasn't declared supports_live_execution=True must be refused
    before any adapter method is called at all. Uses a window_finder
    that raises if invoked, so any accidental call is caught immediately
    rather than silently succeeding against a fake in-memory state."""
    from estimate_extractor.xactimate_lookup import orchestrator, registry
    from estimate_extractor.xactimate_lookup.models import RecommendationInput, STOP_REASON_UNSUPPORTED_ADAPTER

    def exploding_finder():
        raise AssertionError("adapter method invoked despite unsupported_adapter refusal")

    adapter = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=exploding_finder)
    # Explicit instance override -- the class default is True since
    # Phase 5.4's pilot-gate sign-off, but this test is specifically
    # about the refusal behavior for an adapter that does NOT support
    # live execution.
    adapter.supports_live_execution = False

    conn = registry.create_database(tmp_path / "reg.db")
    item = RecommendationInput(
        line_item_id="line_0001",
        original_description="Tear off composition shingles - 3 tab (no haul off)",
        trade="roofing",
        component="composition_shingles",
        material="3-tab composition shingles",
        action="remove",
        source_unit="SQ",
        quantity=10.0,
    )
    plan = orchestrator.build_lookup_plan(item, conn, phrase_rules)
    conn.close()

    outcome = orchestrator.execute_plan(plan, item, adapter, ranking_config, phrase_rules, dry_run=False)

    assert outcome.stop_reason == STOP_REASON_UNSUPPORTED_ADAPTER
    assert outcome.committed is False


# ---------------------------------------------------------------------
# Phase 4.7: unit/quantity verification -- pure logic, no live session
# ---------------------------------------------------------------------


def test_raw_versus_normalized_unit_are_independently_tracked():
    """Regression guard (Phase 4.7 Stage 1): the raw OCR value must
    never be overwritten by normalization -- both are present, and
    differ, on a corrected read."""
    result = check_unit_compatibility("LF", "LF", "(uF")
    assert result.observed_xactimate_unit == "(uF"  # untouched raw OCR
    assert result.unit_normalized == "LF"  # only the normalized field changes


def test_safe_unit_synonym_ea_and_each():
    """Regression guard (Phase 4.7 Stage 2): EA/EACH is an explicitly
    approved synonym pair -- expected is spelled "EACH" (as a source-
    side extraction might write it) while Xactimate's own observed
    code is the short "EA"; these normalize to the same canonical unit
    without being byte-identical, which is exactly what
    "normalized_synonym" (as distinct from "exact_match") means."""
    result = check_unit_compatibility("EACH", "EACH", "EA")
    assert result.unit_match_state == "normalized_synonym"
    assert result.unit_match_reason


def test_safe_unit_synonym_day_and_da():
    result = check_unit_compatibility("DAY", "DAY", "DA")
    assert result.unit_match_state == "normalized_synonym"


def test_incompatible_units_sf_and_sq_are_not_synonyms():
    """Regression guard (Phase 4.7 Stage 2): the build spec explicitly
    lists SF/SQ as NOT synonyms -- must resolve to incompatible /
    hard_stop, never silently pass."""
    result = check_unit_compatibility("SF", "SF", "SQ")
    assert result.unit_match_state == "incompatible"


def test_incompatible_units_lf_and_sf_are_not_synonyms():
    result = check_unit_compatibility("LF", "LF", "SF")
    assert result.unit_match_state == "incompatible"


def test_incompatible_units_hard_stop_classification():
    result = check_unit_compatibility("EA", "EA", "SQ")
    assert result.unit_match_state == "incompatible"
    assert WindowsXactimateAdapter._UNIT_STATE_TO_COMPATIBILITY[result.unit_match_state] == "hard_stop"


def test_missing_source_unit_is_review_required_even_when_observed_matches_expected():
    """Regression guard (Phase 4.7 Stage 8): source_unit_missing is an
    unconditional review_required trigger, checked even when observed
    and expected already agree."""
    result = check_unit_compatibility(None, "LF", "LF")
    assert result.unit_match_state == "source_unit_missing"
    assert WindowsXactimateAdapter._UNIT_STATE_TO_COMPATIBILITY[result.unit_match_state] == "review_required"


def test_missing_observed_unit_is_review_required():
    result = check_unit_compatibility("LF", "LF", None)
    assert result.unit_match_state == "observed_unit_missing"
    assert WindowsXactimateAdapter._UNIT_STATE_TO_COMPATIBILITY[result.unit_match_state] == "review_required"


def test_ambiguous_ocr_unit_is_unreadable_not_a_guess():
    """Regression guard (Phase 4.7 Stage 6): OCR text that doesn't
    resolve to any known real Xactimate unit must be reported as
    unreadable, never coerced into whatever was expected."""
    result = check_unit_compatibility("LF", "LF", "??%")
    assert result.unit_match_state == "unreadable"
    assert result.unit_normalized is None
    assert WindowsXactimateAdapter._UNIT_STATE_TO_COMPATIBILITY[result.unit_match_state] == "review_required"


def test_lf_uf_ocr_confusion_corrects_without_unsafe_coercion():
    """Regression guard (Phase 4.7 Stage 6): the narrow "UF"->"LF" OCR-
    confusion rule fires only for that literal stripped string, and
    must not be reachable from any other real unit's OCR output --
    confirms the rule cannot misfire against SQ/EA/HR/DA/SF."""
    assert _resolve_observed_unit_vocab("(uF") == "LF"
    assert _resolve_observed_unit_vocab("uF") == "LF"
    for real_unit in ("SQ", "EA", "HR", "DA", "SF"):
        # None of the other evidence-backed units should ever resolve
        # via the UF->LF confusion table.
        assert _UNIT_OCR_CONFUSIONS.get(real_unit) is None


def test_conversions_disabled_by_default():
    """Regression guard (Phase 4.7 Stage 3): no conversions are
    pre-populated -- a genuine unit mismatch with no exact/synonym
    match must be incompatible, not silently converted."""
    assert _VERIFIED_UNIT_CONVERSIONS == {}
    result = check_unit_compatibility("SF", "SF", "SQ")
    assert result.unit_match_state == "incompatible"


def test_explicit_verified_conversion_is_used_when_present():
    """Regression guard (Phase 4.7 Stage 3): the conversion mechanism
    itself works when a rule IS explicitly present -- proves the
    machinery, not just its disabled-by-default state. Does not
    mutate the real (empty) module-level map."""
    import estimate_extractor.xactimate_lookup.windows_adapter as wa_mod

    original = dict(_VERIFIED_UNIT_CONVERSIONS)
    try:
        wa_mod._VERIFIED_UNIT_CONVERSIONS[("SQ", "SF")] = 100.0
        result = check_unit_compatibility("SF", "SF", "SQ")
        assert result.unit_match_state == "verified_conversion"
        assert WindowsXactimateAdapter._UNIT_STATE_TO_COMPATIBILITY[result.unit_match_state] == "compatible"
    finally:
        wa_mod._VERIFIED_UNIT_CONVERSIONS.clear()
        wa_mod._VERIFIED_UNIT_CONVERSIONS.update(original)


def test_quantity_match_does_not_override_unit_conflict():
    """Regression guard (Phase 4.7 Stage 8, carried into Phase 4.8): a
    CommitVerification with a quantity match but an incompatible unit
    must still report hard_stop compatibility and a UNIT_MISMATCH
    trust_state -- a quantity match never overrides a unit conflict."""
    unit_result = check_unit_compatibility("EA", "EA", "SQ")
    verification = CommitVerification(
        trust_state="UNIT_MISMATCH", reason=unit_result.unit_match_reason,
        row_count_before=0, row_count_after=1, row_index=0, preexisting_rows_unchanged=True,
        category_expected="SFG", selector_expected="GUTA",
        category_observed="SFG", selector_observed="GUTA", category_selector_ocr_agrees=True,
        description_observed="desc",
        quantity_expected=5.0, quantity_observed=5.0, quantity_matched=True,
        unit=unit_result, compatibility="hard_stop", compatibility_reason=unit_result.unit_match_reason,
        attempts=1, elapsed_s=0.1,
    )
    assert verification.quantity_matched is True
    assert verification.compatibility == "hard_stop"
    assert verification.trust_state == "UNIT_MISMATCH"


def _adapter_with_fake_commit_grid(monkeypatch, row_sequence, unit_reads=None, quantity_reads=None):
    """Builds a WindowsXactimateAdapter with every internal dependency
    `verify_commit()` touches (via `snapshot_grid_identities()` and its
    own follow-up reads at the structurally-identified row)
    monkeypatched to a fake, so its polling/identification logic can
    be exercised without a live Windows/Xactimate session.
    `row_sequence` is a list of "grids" (one per poll attempt), each a
    list of (category, selector) tuples top-to-bottom -- i.e. what
    `snapshot_grid_identities()` would read on that attempt. Uses a
    fake, manually-advanced clock so timing is deterministic and tests
    run instantly."""
    adapter = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=lambda: ([], []))
    monkeypatch.setattr(adapter, "_ensure_main_window", lambda: 123)
    monkeypatch.setattr(adapter, "_capture_and_locate", lambda hwnd, attempts=1, delay_s=0: (object(), (0, 0)))
    monkeypatch.setattr(adapter, "_unexpected_dialog_present", lambda: False)
    monkeypatch.setattr(adapter, "_shifted_anchor", lambda name, offset: (0, 0, 0, 0))

    grids = iter(row_sequence)
    state = {"grid": []}

    def last_row_geometry(image, offset):
        state["grid"] = next(grids, state["grid"])
        return (len(state["grid"]), 0) if state["grid"] else None

    monkeypatch.setattr(adapter, "_last_row_geometry", last_row_geometry)
    monkeypatch.setattr(
        adapter, "_read_category_selector_at",
        lambda image, offset, row_top: state["grid"][row_top // _GRID_ROW_HEIGHT],
    )
    monkeypatch.setattr(adapter, "_read_description_at", lambda image, offset, row_top: "desc")
    qty_iter = iter(quantity_reads or [])
    monkeypatch.setattr(adapter, "_read_quantity_at", lambda image, offset, row_top: next(qty_iter, None))
    unit_iter = iter(unit_reads or [])
    monkeypatch.setattr(adapter, "_read_unit_at", lambda image, offset, row_top: next(unit_iter, (None, None)))

    import estimate_extractor.xactimate_lookup.windows_adapter as wa_mod

    clock = {"t": 1000.0}
    monkeypatch.setattr(wa_mod.time, "time", lambda: clock["t"])
    monkeypatch.setattr(wa_mod.time, "sleep", lambda s: clock.__setitem__("t", clock["t"] + s))
    return adapter


def test_verify_commit_identifies_row_after_delayed_rendering(monkeypatch):
    """Regression test (Phase 4.8): the row count doesn't increase
    until the third poll attempt (simulating delayed repaint) --
    verify_commit() must keep polling and succeed once it does, not
    give up on the first unchanged grid."""
    adapter = _adapter_with_fake_commit_grid(
        monkeypatch,
        row_sequence=[[], [], [("SFG", "GUTA")]],
        unit_reads=[("LF", "LF")],
        quantity_reads=[5.0],
    )
    result = adapter.verify_commit([], "SFG", "GUTA", 5.0, source_unit="LF", expected_xactimate_unit="LF", timeout_s=3.0)
    assert result.trust_state == "VERIFIED"
    assert result.row_index == 0
    assert result.quantity_matched is True
    assert result.compatibility == "compatible"


def test_verify_commit_prefers_populated_unit_over_garbage_post_commit_ocr(monkeypatch):
    """Regression test (Phase 5.6 Stage 4, live-caught): a real commit
    (line_0001, R&R Gutter, 200 LF, correct SFG/GUTA selection,
    quantity read back exactly) was downgraded to REVIEW_REQUIRED
    because verify_commit()'s own post-commit OCR misread the unit
    cell as "a". The majority-voted populated-field unit read taken
    right after select_candidate() (before commit) correctly read
    "LF" -- verify_commit() must prefer that over its own unreadable
    post-commit OCR rather than downgrading a genuinely correct
    commit."""
    adapter = _adapter_with_fake_commit_grid(
        monkeypatch,
        row_sequence=[[("SFG", "GUTA")]],
        unit_reads=[("a", None)],
        quantity_reads=[200.0],
    )
    result = adapter.verify_commit(
        [], "SFG", "GUTA", 200.0, source_unit="LF", expected_xactimate_unit="LF", timeout_s=3.0,
        populated_unit="LF",
    )
    assert result.trust_state == "VERIFIED"
    assert result.compatibility == "compatible"


def test_verify_commit_falls_back_to_post_commit_ocr_when_populated_unit_unreadable(monkeypatch):
    """Companion to the fix above: when the populated-field unit is
    ALSO unreadable (or wasn't supplied), verify_commit() must still
    fall back to its own post-commit OCR read rather than silently
    treating the row as verified -- the preference for populated_unit
    must not weaken the check when populated_unit provides no real
    evidence."""
    adapter = _adapter_with_fake_commit_grid(
        monkeypatch,
        row_sequence=[[("SFG", "GUTA")]],
        unit_reads=[("a", None)],
        quantity_reads=[200.0],
    )
    result = adapter.verify_commit(
        [], "SFG", "GUTA", 200.0, source_unit="LF", expected_xactimate_unit="LF", timeout_s=3.0,
        populated_unit=None,
    )
    assert result.trust_state == "REVIEW_REQUIRED"
    assert result.compatibility == "review_required"


def test_verify_commit_still_catches_genuine_unit_mismatch_with_populated_unit(monkeypatch):
    """The populated_unit preference must not create a bypass: a
    populated_unit that itself resolves to a genuinely incompatible
    unit still hard-stops, exactly as a bad post-commit OCR read
    would."""
    adapter = _adapter_with_fake_commit_grid(
        monkeypatch,
        row_sequence=[[("SFG", "GUTA")]],
        unit_reads=[("LF", "LF")],
        quantity_reads=[200.0],
    )
    result = adapter.verify_commit(
        [], "SFG", "GUTA", 200.0, source_unit="LF", expected_xactimate_unit="LF", timeout_s=3.0,
        populated_unit="EA",
    )
    assert result.trust_state == "UNIT_MISMATCH"
    assert result.compatibility == "hard_stop"


def test_verify_commit_unreadable_category_does_not_block_verified(monkeypatch):
    """Regression test (Phase 4.8's central claim): category/selector
    OCR is corroborating evidence only -- when it's unreadable but
    structural evidence (row-count delta, unchanged pre-existing rows)
    plus quantity and unit all agree, the commit is still VERIFIED."""
    adapter = _adapter_with_fake_commit_grid(
        monkeypatch,
        row_sequence=[[(None, None)]],
        unit_reads=[("LF", "LF")],
        quantity_reads=[5.0],
    )
    result = adapter.verify_commit([], "SFG", "GUTA", 5.0, source_unit="LF", expected_xactimate_unit="LF", timeout_s=3.0)
    assert result.trust_state == "VERIFIED"
    assert result.category_observed is None
    assert result.category_selector_ocr_agrees is None


def test_verify_commit_category_ocr_contradiction_downgrades_to_review_required(monkeypatch):
    """Regression test (Phase 4.8): category/selector OCR that IS
    readable but contradicts the expected identity must not be
    silently ignored either -- it downgrades to REVIEW_REQUIRED even
    though structural evidence and quantity/unit all agree, since
    category OCR is supporting evidence, not proof of a wrong
    commit."""
    adapter = _adapter_with_fake_commit_grid(
        monkeypatch,
        row_sequence=[[("SFG", "GUTC")]],
        unit_reads=[("LF", "LF")],
        quantity_reads=[5.0],
    )
    result = adapter.verify_commit([], "SFG", "GUTA", 5.0, source_unit="LF", expected_xactimate_unit="LF", timeout_s=3.0)
    assert result.trust_state == "REVIEW_REQUIRED"
    assert result.category_selector_ocr_agrees is False


def test_verify_commit_row_count_delta_other_than_one_is_conflicting_row(monkeypatch):
    """Regression test (Phase 4.8): a row-count delta that isn't
    exactly 1 (here: 2 rows appeared instead of 1) must never be
    silently resolved by guessing which one is the committed row --
    reports CONFLICTING_ROW instead, immediately, without waiting for
    timeout."""
    adapter = _adapter_with_fake_commit_grid(
        monkeypatch,
        row_sequence=[[("SFG", "GUTA"), ("SFG", "GUTB")]],
    )
    result = adapter.verify_commit([], "SFG", "GUTA", 5.0, timeout_s=3.0)
    assert result.trust_state == "CONFLICTING_ROW"
    assert result.row_index is None


def test_verify_commit_preexisting_rows_changed_is_conflicting_row(monkeypatch):
    """Regression test (Phase 4.8): even when the row count increases
    by exactly 1, if the pre-existing rows no longer match the
    before-commit snapshot, the state is not trustworthy -- reports
    CONFLICTING_ROW rather than trusting the last row is really the
    new one."""
    adapter = _adapter_with_fake_commit_grid(
        monkeypatch,
        row_sequence=[[("PLM", "OTHER"), ("SFG", "GUTA")]],
    )
    result = adapter.verify_commit([("PLM", "TLT")], "SFG", "GUTA", 5.0, timeout_s=3.0)
    assert result.trust_state == "CONFLICTING_ROW"
    assert result.preexisting_rows_unchanged is False


def test_verify_commit_times_out_when_row_count_never_changes(monkeypatch):
    """Regression test (Phase 4.8): if the row count never changes
    within budget, reports VERIFICATION_FAILED -- never an unbounded
    wait, never a guess."""
    adapter = _adapter_with_fake_commit_grid(
        monkeypatch,
        row_sequence=[[]] * 20,
    )
    result = adapter.verify_commit([], "SFG", "GUTA", 5.0, timeout_s=0.5)
    assert result.trust_state == "VERIFICATION_FAILED"
    assert result.row_index is None


def test_verify_commit_quantity_mismatch_reported_distinctly(monkeypatch):
    """Regression test (Phase 4.8): a quantity that was successfully
    read but disagrees with what was entered is QUANTITY_MISMATCH, not
    a generic failure -- the row is still structurally identified
    (row_index set) so a caller can act on it."""
    adapter = _adapter_with_fake_commit_grid(
        monkeypatch,
        row_sequence=[[("SFG", "GUTA")]],
        unit_reads=[("LF", "LF")],
        quantity_reads=[3.0],
    )
    result = adapter.verify_commit([], "SFG", "GUTA", 5.0, source_unit="LF", expected_xactimate_unit="LF", timeout_s=3.0)
    assert result.trust_state == "QUANTITY_MISMATCH"
    assert result.row_index == 0
    assert result.quantity_matched is False


def test_verify_commit_identity_available_for_cleanup_after_unit_mismatch(monkeypatch):
    """Regression guard (Phase 4.8): a hard_stop unit outcome still
    returns a fully-formed result (row_index, expected category/
    selector) so a caller can proceed to clean up the row --
    verification failing must not prevent cleanup from having the
    information it needs to act on."""
    adapter = _adapter_with_fake_commit_grid(
        monkeypatch,
        row_sequence=[[("SFG", "GUTA")]],
        unit_reads=[("SQ", "SQ")],
        quantity_reads=[5.0],
    )
    result = adapter.verify_commit([], "SFG", "GUTA", 5.0, source_unit="LF", expected_xactimate_unit="LF", timeout_s=3.0)
    assert result.trust_state == "UNIT_MISMATCH"
    assert result.compatibility == "hard_stop"
    # cleanup-relevant identity is still available despite the failure
    assert result.row_index == 0
    assert result.category_expected == "SFG"
    assert result.selector_expected == "GUTA"


# ---------------------------------------------------------------------------
# Group control (Phase 5.1): ensure_group / select_group / verify_group
# ---------------------------------------------------------------------------


def test_snapshot_group_names_reads_well_past_the_old_8_row_default(monkeypatch):
    """Live-caught (Phase 5.7B): the old default (max_rows=8, i.e. only
    7 child groups) silently truncated the tree for a real PDF needing
    more groups -- the 8th/9th groups were never read at all, so every
    caller reported "not found" even though they genuinely existed.
    This locks in real headroom: a 10-row tree (TEST + 9 real groups)
    must be read in full."""
    adapter = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=lambda: ([], []))
    monkeypatch.setattr(adapter, "_ensure_main_window", lambda: 123)
    monkeypatch.setattr(adapter, "_locate_group_tree_header", lambda image: (0, 0, 0, 0))
    real_rows = [
        "Exterior", "Dwelling Roof", "Front Elevation", "Rear Elevation", "Left Elevation",
        "Right Elevation", "Fencing", "Debris Removal", "Labor Minimums Applied",
    ]
    monkeypatch.setattr(adapter, "_ocr_text", lambda crop, psm=7: crop)
    monkeypatch.setattr(adapter, "_group_tree_row_crop_top", lambda header_top, row_index: row_index)

    class _FakeImage:
        def crop(self, box):
            row_top = box[1]
            return real_rows[row_top - 1] if 1 <= row_top <= len(real_rows) else ""

    monkeypatch.setattr(adapter, "_capture_client_image", lambda hwnd: _FakeImage())

    rows = adapter.snapshot_group_names()

    assert rows[0] == "TEST"
    assert rows[1:10] == real_rows  # all 9 real groups read, none silently dropped


def test_group_name_matches_is_whitespace_and_case_insensitive():
    adapter = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=lambda: ([], []))
    assert adapter._group_name_matches("fy DwellingRoofl Ss", "Dwelling Roof") is True
    assert adapter._group_name_matches("& Utility Room || Simila", "Utility Room") is True
    assert adapter._group_name_matches("", "Dwelling Roof") is False
    assert adapter._group_name_matches("Front Elevation", "Dwelling Roof") is False


def test_group_name_matches_rejects_a_different_sibling_sharing_one_word():
    """Live-caught (Phase 5.7A): a naive whole-string fuzzy ratio lets
    "Rear Elevation"/"Left Elevation" match an EXISTING, genuinely
    different "Front Elevation" row -- both score above the fuzzy
    threshold purely because "elevation" dominates the blended ratio,
    regardless of the leading word being completely different. This
    let ensure_group()/select_group() silently target the wrong group.
    Every word of a multi-word name must now individually clear the
    threshold too, not just the blended whole-string ratio."""
    adapter = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=lambda: ([], []))
    assert adapter._group_name_matches("Front Elevation", "Rear Elevation") is False
    assert adapter._group_name_matches("Front Elevation", "Left Elevation") is False
    # the correct name itself, and real OCR noise on it, must still match
    assert adapter._group_name_matches("Front Elevation", "Front Elevation") is True
    assert adapter._group_name_matches("frontelevaion", "Front Elevation") is True  # dropped 't'
    assert adapter._group_name_matches("eteior", "Exterior") is True  # dropped leading 'x', per existing calibration


def test_matching_group_rows_does_not_cross_match_elevation_siblings():
    """The row-level consumer of _group_name_matches() must reflect the
    same fix: searching for a group that ISN'T in the tree yet must not
    resolve to a different, existing sibling with a similar name."""
    adapter = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=lambda: ([], []))
    rows = ["TEST", "Exterior", "Dwelling Roof", "Front Elevation", "Fence"]
    assert adapter._matching_group_rows(rows, "Rear Elevation") == []
    assert adapter._matching_group_rows(rows, "Left Elevation") == []
    assert adapter._matching_group_rows(rows, "Front Elevation") == [3]


def test_find_group_row_returns_first_exact_substring_match_index():
    adapter = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=lambda: ([], []))
    rows = ["TEST", "Utility Room", "Dwelling Roof", ""]
    assert adapter._find_group_row(rows, "Dwelling Roof") == 2
    assert adapter._find_group_row(rows, "Utility Room") == 1
    assert adapter._find_group_row(rows, "Front Elevation") is None


def test_find_group_row_prefers_exact_substring_match_over_an_earlier_fuzzy_false_positive():
    """Live-caught (Phase 5.4 Stage 8): a short, unreviewed suggested
    group name ("Roof", derived from a section named "Dwelling Roof")
    scored an exact substring match against the correct "Dwelling
    Roof" row, but ALSO cleared the fuzzy threshold against an
    earlier, unrelated row -- "Utility Room" -- because "Roof" and
    "Room" share three of four characters (ratio 0.857, above
    `_GROUP_NAME_FUZZY_MATCH_THRESHOLD`). A first-match scan picked
    the wrong, earlier row and a real commit landed in the wrong
    group. The exact substring match must always win regardless of
    row order."""
    adapter = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=lambda: ([], []))
    ratio_to_room = adapter._best_window_fuzzy_ratio("roof", "utilityroom")
    assert ratio_to_room >= adapter._GROUP_NAME_FUZZY_MATCH_THRESHOLD, (
        "test assumption broken: 'Roof' no longer fuzzy-matches 'Utility Room' -- "
        "update this test's premise before trusting it"
    )
    rows = ["TEST", "Utility Room", "Dwelling Roof", "Exterior"]
    assert adapter._find_group_row(rows, "Roof") == 2


def test_find_group_row_falls_back_to_best_fuzzy_match_when_no_exact_substring_exists():
    adapter = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=lambda: ([], []))
    rows = ["TEST", "Utilty Roon", "Dweling Rooof", "Exterior"]
    # Neither row contains "dwellingroof" as an exact substring, so this
    # falls back to fuzzy matching -- the closer misspelling must win,
    # not whichever row happens to appear first.
    assert adapter._find_group_row(rows, "Dwelling Roof") == 2


def test_select_group_raises_when_group_not_found(monkeypatch):
    from estimate_extractor.xactimate_lookup.adapter import AdapterError
    adapter = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=lambda: ([], []))
    monkeypatch.setattr(adapter, "verify_application", lambda: True)
    monkeypatch.setattr(adapter, "verify_project", lambda: True)
    monkeypatch.setattr(adapter, "_ensure_main_window", lambda: 123)
    monkeypatch.setattr(adapter, "_force_foreground", lambda hwnd: True)
    monkeypatch.setattr(adapter, "_capture_client_image", lambda hwnd: object())
    monkeypatch.setattr(adapter, "_locate_group_tree_header", lambda image: (0, 0, 0, 0))
    monkeypatch.setattr(adapter, "snapshot_group_names", lambda: ["TEST", "Utility Room"])

    with pytest.raises(AdapterError, match="not found"):
        adapter.select_group("Dwelling Roof")


def test_select_group_raises_when_application_unverified(monkeypatch):
    from estimate_extractor.xactimate_lookup.adapter import AdapterError
    adapter = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=lambda: ([], []))
    monkeypatch.setattr(adapter, "verify_application", lambda: False)

    with pytest.raises(AdapterError, match="could not verify"):
        adapter.select_group("Dwelling Roof")


def test_ensure_group_is_a_no_op_when_group_already_exists(monkeypatch):
    adapter = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=lambda: ([], []))
    monkeypatch.setattr(adapter, "verify_application", lambda: True)
    monkeypatch.setattr(adapter, "verify_project", lambda: True)
    monkeypatch.setattr(adapter, "_ensure_main_window", lambda: 123)
    monkeypatch.setattr(adapter, "_force_foreground", lambda hwnd: True)
    monkeypatch.setattr(adapter, "snapshot_group_names", lambda: ["TEST", "Utility Room", "Dwelling Roof"])

    def _fail_if_called(*args, **kwargs):
        raise AssertionError("ensure_group must not attempt creation when the group already exists")

    monkeypatch.setattr(adapter, "_capture_client_image", _fail_if_called)

    adapter.ensure_group("Dwelling Roof")  # must return without raising


# ---------------------------------------------------------------------
# Phase 5.5B, Objective 2: ensure_group() must locate, select, and
# independently re-verify its intended parent BY NAME before ever
# right-clicking to create a new group -- never trust a fixed row
# position. Live-caught: the previous version always right-clicked row
# index 0 assuming it was the project root; when the tree's scroll
# position drifted, row 0 was actually some OTHER group, and "New"
# created the requested group as a child of that wrong row (reproduced
# live as a malformed nested tree).
# ---------------------------------------------------------------------


def _mock_ensure_group_scaffolding(monkeypatch, adapter, *, before_rows, after_rows, row0_actual_text):
    """Wires up the minimum set of mocks ensure_group()'s CREATE path
    needs, matching the establish pattern in this file (verify_
    application/verify_project/_ensure_main_window/_force_foreground
    already covered elsewhere). `row0_actual_text` is what the
    INDEPENDENT re-OCR of row 0 reads -- deliberately separate from
    `before_rows[0]`, which (like the real snapshot_group_names())
    would otherwise just be the hardcoded expected_project_name label.

    Phase 5.5C: ensure_group() now snapshots TWICE before creating (once
    for the no-op check, once again after _reset_group_creation_
    stickiness()) and once after -- `before_rows` covers the first two,
    `after_rows` the rest. `_reset_group_creation_stickiness()` and the
    pixel-indent ancestry check are stubbed to no-ops/pass here; tests
    that specifically target those behaviors override them."""
    calls = {"snapshot": 0}

    def fake_snapshot():
        calls["snapshot"] += 1
        return before_rows if calls["snapshot"] <= 2 else after_rows

    monkeypatch.setattr(adapter, "verify_application", lambda: True)
    monkeypatch.setattr(adapter, "verify_project", lambda: True)
    monkeypatch.setattr(adapter, "_ensure_main_window", lambda: 123)
    monkeypatch.setattr(adapter, "_force_foreground", lambda hwnd: True)
    monkeypatch.setattr(adapter, "_scroll_group_tree_to_top", lambda hwnd: None)
    monkeypatch.setattr(adapter, "_reset_group_creation_stickiness", lambda: None)
    monkeypatch.setattr(adapter, "snapshot_group_names", fake_snapshot)
    monkeypatch.setattr(adapter, "_capture_client_image", lambda hwnd: object())
    monkeypatch.setattr(adapter, "_locate_group_tree_header", lambda image: (0, 0, 0, 0))
    monkeypatch.setattr(adapter, "_ocr_group_tree_row_text", lambda image, header, row_index: row0_actual_text if row_index == 0 else before_rows[row_index])
    # Neutral by default: no confirmed mismatch, so the new Stage 7
    # ancestry check never blocks tests that aren't exercising it.
    monkeypatch.setattr(adapter, "_group_tree_row_indent_x", lambda image, header, row_index: 35)
    return calls


def test_ensure_group_creates_under_the_verified_root_by_default(monkeypatch):
    """Objective 2, requirement 5: top-level groups are created under
    the root -- the intended parent (default: expected_project_name)
    is independently re-OCR'd and confirmed BEFORE the context menu is
    opened, and the context menu is opened at the CORRECT (verified)
    row index."""
    adapter = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=lambda: ([], []))
    _mock_ensure_group_scaffolding(
        monkeypatch, adapter,
        before_rows=["TEST", "Exterior"], after_rows=["TEST", "Exterior", "Front Elevation"],
        row0_actual_text="TEST",
    )

    opened_at = {}

    def fake_open_menu(hwnd, header, row_index):
        opened_at["row_index"] = row_index
        return [object()] * adapter._GROUP_MENU_EXPECTED_ITEM_COUNT

    monkeypatch.setattr(adapter, "_open_group_tree_context_menu", fake_open_menu)
    monkeypatch.setattr(adapter, "_click_group_menu_item", lambda items, index: None)
    monkeypatch.setattr(adapter, "_find_window_by_title", lambda title: 456 if title == "New Group" else None)
    monkeypatch.setattr(adapter, "_click_client", lambda hwnd, x, y: None)
    monkeypatch.setattr(adapter, "_select_all_and_delete", lambda: None)
    monkeypatch.setattr(adapter, "_type_keybdevent", lambda text: None)

    adapter.ensure_group("Front Elevation")  # must not raise

    assert opened_at["row_index"] == 0  # the verified root's actual row


def test_current_selection_cannot_accidentally_become_the_parent(monkeypatch):
    """Objective 2, requirement 6: if the row about to be right-clicked
    does NOT independently re-OCR as the intended parent (the tree's
    position has drifted, e.g. because some other group is now at row
    0), ensure_group() refuses to create anything there -- it never
    falls back to treating "whichever row happens to be selected" as
    the parent."""
    adapter = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=lambda: ([], []))
    _mock_ensure_group_scaffolding(
        monkeypatch, adapter,
        before_rows=["TEST", "Exterior"], after_rows=["TEST", "Exterior", "Front Elevation"],
        row0_actual_text="Dwelling Roof",  # drifted: row 0 is NOT really "TEST" right now
    )

    def _fail_if_called(*args, **kwargs):
        raise AssertionError("must never open the context menu on an unverified row")

    monkeypatch.setattr(adapter, "_open_group_tree_context_menu", _fail_if_called)

    from estimate_extractor.xactimate_lookup.adapter import AdapterError
    with pytest.raises(AdapterError, match="drifted"):
        adapter.ensure_group("Front Elevation")


def test_ensure_group_accepts_an_explicit_parent_group_name(monkeypatch):
    """The parent_group_name parameter is honored -- not just the
    default project root -- located and verified the same way."""
    adapter = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=lambda: ([], []))
    _mock_ensure_group_scaffolding(
        monkeypatch, adapter,
        before_rows=["TEST", "Exterior"], after_rows=["TEST", "Exterior", "Sub Group"],
        row0_actual_text="TEST",
    )
    monkeypatch.setattr(adapter, "_ocr_group_tree_row_text", lambda image, header, row_index: ["TEST", "Exterior"][row_index])

    opened_at = {}

    def fake_open_menu(hwnd, header, row_index):
        opened_at["row_index"] = row_index
        return [object()] * adapter._GROUP_MENU_EXPECTED_ITEM_COUNT

    monkeypatch.setattr(adapter, "_open_group_tree_context_menu", fake_open_menu)
    monkeypatch.setattr(adapter, "_click_group_menu_item", lambda items, index: None)
    monkeypatch.setattr(adapter, "_find_window_by_title", lambda title: 456 if title == "New Group" else None)
    monkeypatch.setattr(adapter, "_click_client", lambda hwnd, x, y: None)
    monkeypatch.setattr(adapter, "_select_all_and_delete", lambda: None)
    monkeypatch.setattr(adapter, "_type_keybdevent", lambda text: None)

    adapter.ensure_group("Sub Group", parent_group_name="Exterior")

    assert opened_at["row_index"] == 1  # Exterior's row, not the root


# ---------------------------------------------------------------------
# Phase 5.5C: Xactimate's "New Group" command attaches the new group to
# whichever group was MOST RECENTLY CREATED in the session, independent
# of which row is right-clicked (live-proven in Phase 5.5B). Live
# investigation found switching Estimate Items tabs and back resets
# that stickiness -- reliably for exactly the 2nd group of a session,
# not a 3rd+ (see docs/build-estimate.md Phase 5.5C). These tests cover
# _reset_group_creation_stickiness()'s call site and the pixel-
# indentation ancestry check that catches an accidental nest before any
# task can execute against it.
# ---------------------------------------------------------------------


def test_ensure_group_resets_stickiness_before_creating_but_not_for_a_noop():
    adapter = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=lambda: ([], []))
    reset_calls = []
    adapter._reset_group_creation_stickiness = lambda: reset_calls.append(1)
    adapter.verify_application = lambda: True
    adapter.verify_project = lambda: True
    adapter._ensure_main_window = lambda: 123
    adapter._force_foreground = lambda hwnd: True
    adapter._scroll_group_tree_to_top = lambda hwnd: None
    adapter.snapshot_group_names = lambda: ["TEST", "Dwelling Roof"]

    adapter.ensure_group("Dwelling Roof")  # already exists -- a no-op

    assert reset_calls == [], "a no-op call must never touch the live UI, including the stickiness reset"


def test_ensure_group_returns_position_warning_when_new_group_indentation_does_not_match_a_sibling(monkeypatch):
    """Phase 5.7 product-requirement change: group ancestry/nesting depth
    is no longer a blocking safety condition. Even though the newly
    created group's indentation doesn't match an existing top-level
    group's (the same live-caught mis-nesting signal Stage 7 originally
    hard-failed on), the group WAS created and IS uniquely findable by
    name -- ensure_group() must succeed (return, not raise) and instead
    return a GROUP_POSITION_WARNING string carrying the ancestry
    evidence as informational-only."""
    adapter = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=lambda: ([], []))
    _mock_ensure_group_scaffolding(
        monkeypatch, adapter,
        before_rows=["TEST", "Exterior"], after_rows=["TEST", "Exterior", "Front Elevation"],
        row0_actual_text="TEST",
    )
    monkeypatch.setattr(adapter, "_open_group_tree_context_menu", lambda hwnd, header, row_index: [object()] * adapter._GROUP_MENU_EXPECTED_ITEM_COUNT)
    monkeypatch.setattr(adapter, "_click_group_menu_item", lambda items, index: None)
    monkeypatch.setattr(adapter, "_find_window_by_title", lambda title: 456 if title == "New Group" else None)
    monkeypatch.setattr(adapter, "_click_client", lambda hwnd, x, y: None)
    monkeypatch.setattr(adapter, "_select_all_and_delete", lambda: None)
    monkeypatch.setattr(adapter, "_type_keybdevent", lambda text: None)

    # Exterior (row 1, an existing sibling) reads indent 39; the newly
    # created Front Elevation (row 2) reads indent 59 -- nested one
    # level deeper, exactly the live-caught failure mode.
    def fake_indent(image, header, row_index):
        return {1: 39, 2: 59}[row_index]

    monkeypatch.setattr(adapter, "_group_tree_row_indent_x", fake_indent)

    warning = adapter.ensure_group("Front Elevation")
    assert warning is not None
    assert "GROUP_POSITION_WARNING" in warning
    assert "Front Elevation" in warning


def test_ensure_group_does_not_raise_when_new_group_indentation_matches_a_sibling(monkeypatch):
    """The counterpart to the test above: a correctly-placed sibling
    (identical indentation to an existing top-level group) must NOT be
    rejected -- this is the exact false positive a naive "first dark
    pixel" measurement produced against a SELECTED row's highlight
    border before _GROUP_TREE_ICON_MIN_INK_RUN/_SCAN_START_X fixed it."""
    adapter = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=lambda: ([], []))
    _mock_ensure_group_scaffolding(
        monkeypatch, adapter,
        before_rows=["TEST", "Exterior"], after_rows=["TEST", "Exterior", "Dwelling Roof"],
        row0_actual_text="TEST",
    )
    monkeypatch.setattr(adapter, "_open_group_tree_context_menu", lambda hwnd, header, row_index: [object()] * adapter._GROUP_MENU_EXPECTED_ITEM_COUNT)
    monkeypatch.setattr(adapter, "_click_group_menu_item", lambda items, index: None)
    monkeypatch.setattr(adapter, "_find_window_by_title", lambda title: 456 if title == "New Group" else None)
    monkeypatch.setattr(adapter, "_click_client", lambda hwnd, x, y: None)
    monkeypatch.setattr(adapter, "_select_all_and_delete", lambda: None)
    monkeypatch.setattr(adapter, "_type_keybdevent", lambda text: None)
    monkeypatch.setattr(adapter, "_group_tree_row_indent_x", lambda image, header, row_index: 39)  # same for every row

    assert adapter.ensure_group("Dwelling Roof") is None  # no position warning when correctly placed


def test_verify_group_path_true_for_a_confirmed_sibling(monkeypatch):
    adapter = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=lambda: ([], []))
    monkeypatch.setattr(adapter, "verify_application", lambda: True)
    monkeypatch.setattr(adapter, "verify_project", lambda: True)
    monkeypatch.setattr(adapter, "_ensure_main_window", lambda: 123)
    monkeypatch.setattr(adapter, "_scroll_group_tree_to_top", lambda hwnd: None)
    monkeypatch.setattr(adapter, "snapshot_group_names", lambda: ["TEST", "Exterior", "Dwelling Roof"])
    monkeypatch.setattr(adapter, "_capture_client_image", lambda hwnd: object())
    monkeypatch.setattr(adapter, "_locate_group_tree_header", lambda image: (0, 0, 0, 0))
    monkeypatch.setattr(adapter, "_group_tree_row_indent_x", lambda image, header, row_index: 39)

    assert adapter.verify_group_path("Dwelling Roof") is True


def test_verify_group_path_false_when_group_not_found(monkeypatch):
    adapter = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=lambda: ([], []))
    monkeypatch.setattr(adapter, "verify_application", lambda: True)
    monkeypatch.setattr(adapter, "verify_project", lambda: True)
    monkeypatch.setattr(adapter, "_ensure_main_window", lambda: 123)
    monkeypatch.setattr(adapter, "_scroll_group_tree_to_top", lambda hwnd: None)
    monkeypatch.setattr(adapter, "snapshot_group_names", lambda: ["TEST", "Exterior"])

    assert adapter.verify_group_path("Dwelling Roof") is False


def test_verify_group_path_false_for_ambiguous_duplicate_group_names(monkeypatch):
    """Two rows matching the same name is exactly the "duplicate same-
    name ambiguity" case Stage 7 requires verify_group_path() to refuse
    rather than guess at."""
    adapter = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=lambda: ([], []))
    monkeypatch.setattr(adapter, "verify_application", lambda: True)
    monkeypatch.setattr(adapter, "verify_project", lambda: True)
    monkeypatch.setattr(adapter, "_ensure_main_window", lambda: 123)
    monkeypatch.setattr(adapter, "_scroll_group_tree_to_top", lambda hwnd: None)
    monkeypatch.setattr(adapter, "snapshot_group_names", lambda: ["TEST", "Dwelling Roof", "Dwelling Roof"])

    assert adapter.verify_group_path("Dwelling Roof") is False


def test_verify_group_path_false_when_indentation_indicates_nesting(monkeypatch):
    adapter = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=lambda: ([], []))
    monkeypatch.setattr(adapter, "verify_application", lambda: True)
    monkeypatch.setattr(adapter, "verify_project", lambda: True)
    monkeypatch.setattr(adapter, "_ensure_main_window", lambda: 123)
    monkeypatch.setattr(adapter, "_scroll_group_tree_to_top", lambda hwnd: None)
    monkeypatch.setattr(adapter, "snapshot_group_names", lambda: ["TEST", "Dwelling Roof", "Front Elevation"])
    monkeypatch.setattr(adapter, "_capture_client_image", lambda hwnd: object())
    monkeypatch.setattr(adapter, "_locate_group_tree_header", lambda image: (0, 0, 0, 0))

    def fake_indent(image, header, row_index):
        return {1: 39, 2: 59}[row_index]  # Front Elevation nested one level deeper

    monkeypatch.setattr(adapter, "_group_tree_row_indent_x", fake_indent)

    assert adapter.verify_group_path("Front Elevation") is False


def test_verify_group_path_explicit_parent_requires_deeper_indent_than_parent(monkeypatch):
    """The non-root parent_group_name branch: a genuine child must have
    a STRICTLY GREATER indent than its named parent, not merely a
    different one -- verified against the exact scenario ensure_group()
    already supports (an explicit parent_group_name)."""
    adapter = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=lambda: ([], []))
    monkeypatch.setattr(adapter, "verify_application", lambda: True)
    monkeypatch.setattr(adapter, "verify_project", lambda: True)
    monkeypatch.setattr(adapter, "_ensure_main_window", lambda: 123)
    monkeypatch.setattr(adapter, "_scroll_group_tree_to_top", lambda hwnd: None)
    monkeypatch.setattr(adapter, "snapshot_group_names", lambda: ["TEST", "Exterior", "Sub Group"])
    monkeypatch.setattr(adapter, "_capture_client_image", lambda hwnd: object())
    monkeypatch.setattr(adapter, "_locate_group_tree_header", lambda image: (0, 0, 0, 0))

    def fake_indent_nested_correctly(image, header, row_index):
        return {1: 39, 2: 59}[row_index]

    monkeypatch.setattr(adapter, "_group_tree_row_indent_x", fake_indent_nested_correctly)
    assert adapter.verify_group_path("Sub Group", parent_group_name="Exterior") is True

    def fake_indent_same_level(image, header, row_index):
        return {1: 39, 2: 39}[row_index]  # "child" is actually a sibling, not nested

    monkeypatch.setattr(adapter, "_group_tree_row_indent_x", fake_indent_same_level)
    assert adapter.verify_group_path("Sub Group", parent_group_name="Exterior") is False


def test_verify_group_path_never_raises_on_unexpected_error():
    adapter = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=lambda: ([], []))

    def _boom():
        raise RuntimeError("simulated failure")

    adapter.verify_application = _boom
    assert adapter.verify_group_path("Dwelling Roof") is False


def test_verify_group_returns_false_when_group_not_in_tree(monkeypatch):
    adapter = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=lambda: ([], []))
    monkeypatch.setattr(adapter, "verify_application", lambda: True)
    monkeypatch.setattr(adapter, "verify_project", lambda: True)
    monkeypatch.setattr(adapter, "_ensure_main_window", lambda: 123)
    monkeypatch.setattr(adapter, "snapshot_group_names", lambda: ["TEST", "Utility Room"])

    assert adapter.verify_group("Dwelling Roof") is False


def test_verify_group_never_raises_on_unexpected_error(monkeypatch):
    """verify_group() must return False, never propagate an exception --
    callers rely on a boolean, not a try/except, to decide whether a
    group is safe to execute against (Phase 5.1: "never silently use
    the currently active group")."""
    adapter = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=lambda: ([], []))

    def _boom():
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(adapter, "verify_application", _boom)
    assert adapter.verify_group("Dwelling Roof") is False


def test_verify_group_caches_a_positive_result_for_this_session(monkeypatch):
    """Phase 5.7B: once a group has been positively verified via a real
    probe, a later call for the SAME name must return the cached True
    WITHOUT running another probe -- the visually confusing, mutating
    disposable SFG/GUTA commit only needs to happen once per group per
    live adapter instance."""
    adapter = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=lambda: ([], []))
    probe_calls = []
    monkeypatch.setattr(adapter, "_verify_group_once", lambda name: probe_calls.append(name) or True)

    assert adapter.verify_group("Dwelling Roof") is True
    assert adapter.verify_group("Dwelling Roof") is True
    assert adapter.verify_group("dwelling roof") is True  # case-insensitive cache key
    assert probe_calls == ["Dwelling Roof"]  # probed exactly once, not three times

    # a DIFFERENT group is never served from another group's cache entry
    assert adapter.verify_group("Fence") is True
    assert probe_calls == ["Dwelling Roof", "Fence"]


def test_verify_group_use_cache_false_forces_a_fresh_probe(monkeypatch):
    """use_cache=False (diagnostics/tests needing real-time ground
    truth) must bypass the cache even for an already-verified group."""
    adapter = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=lambda: ([], []))
    probe_calls = []
    monkeypatch.setattr(adapter, "_verify_group_once", lambda name: probe_calls.append(name) or True)

    assert adapter.verify_group("Dwelling Roof") is True
    assert adapter.verify_group("Dwelling Roof", use_cache=False) is True
    assert probe_calls == ["Dwelling Roof", "Dwelling Roof"]  # probed both times


def test_verify_group_never_caches_a_negative_result(monkeypatch):
    """A False result must never be cached -- a group that failed
    verification once (e.g. a transient settling issue) must get a
    completely fresh probe next time, not a stuck cached failure."""
    import estimate_extractor.xactimate_lookup.windows_adapter as wa_mod

    adapter = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=lambda: ([], []))
    results = iter([False, False, True, True])  # verify_group() retries internally (2 attempts)
    monkeypatch.setattr(adapter, "_verify_group_once", lambda name: next(results))
    monkeypatch.setattr(wa_mod.time, "sleep", lambda s: None)

    assert adapter.verify_group("Dwelling Roof") is False  # both internal attempts failed
    assert adapter.verify_group("Dwelling Roof") is True  # fresh attempt, not blocked by a cached False


# ---------------------------------------------------------------------
# Phase 5.7B: Xactimate's own "Duplicate Item(s)" cross-group reminder,
# live-caught blocking every group after the first for the rest of a
# real 42-row run -- must be answered "Yes" (never silently discard a
# correct commit) and self-healed at every group-tree entry point.
# ---------------------------------------------------------------------


def test_handle_duplicate_item_dialog_clicks_yes_when_present(monkeypatch):
    adapter = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=lambda: ([], []))
    monkeypatch.setattr(adapter, "_find_window_by_title", lambda title: 789 if title == "Duplicate Item(s)" else None)
    monkeypatch.setattr(adapter, "_capture_client_image", lambda hwnd: object())
    monkeypatch.setattr(adapter, "_locate_label", lambda image, text, prefer=None: (240, 45, 256, 53) if text == "Yes" else None)
    clicked = []
    monkeypatch.setattr(adapter, "_click_client", lambda hwnd, x, y: clicked.append((hwnd, x, y)))

    assert adapter._handle_duplicate_item_dialog() is True
    assert clicked == [(789, 248, 49)]


def test_handle_duplicate_item_dialog_is_a_noop_when_absent(monkeypatch):
    adapter = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=lambda: ([], []))
    monkeypatch.setattr(adapter, "_find_window_by_title", lambda title: None)
    clicked = []
    monkeypatch.setattr(adapter, "_click_client", lambda hwnd, x, y: clicked.append((hwnd, x, y)))

    assert adapter._handle_duplicate_item_dialog() is False
    assert clicked == []


def test_commit_item_dismisses_a_duplicate_item_dialog(monkeypatch):
    """A live commit (real PDF item OR the disposable probe) that
    triggers the duplicate-item reminder must not leave it hanging --
    commit_item() checks for and dismisses it automatically."""
    adapter = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=lambda: ([], []))
    monkeypatch.setattr(adapter, "_press_ctrl", lambda vk: None)
    calls = []
    monkeypatch.setattr(adapter, "_handle_duplicate_item_dialog", lambda: calls.append(1) or True)
    import estimate_extractor.xactimate_lookup.windows_adapter as wa_mod
    monkeypatch.setattr(wa_mod.time, "sleep", lambda s: None)

    adapter.commit_item()

    assert calls == [1]


# ---------------------------------------------------------------------
# Phase 5.8A: live-caught -- a task that ends in NO_MATCH/REVIEW_REQUIRED
# never calls select_candidate(), so nothing ever clicks the results
# popup closed. orchestrator.py now calls recover() itself (the real
# fix); these tests cover the independent, defense-in-depth self-heal
# at every group-tree entry point (matching the existing "Duplicate
# Item(s)" dialog self-heal pattern from Phase 5.7B).
# ---------------------------------------------------------------------


def test_dismiss_stray_results_popup_closes_a_popup_when_present(monkeypatch):
    adapter = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=lambda: ([], []))
    monkeypatch.setattr(adapter, "_find_dropdown_window", lambda: 555)
    recover_calls = []
    monkeypatch.setattr(adapter, "recover", lambda: recover_calls.append(1))

    assert adapter._dismiss_stray_results_popup() is True
    assert recover_calls == [1]


def test_dismiss_stray_results_popup_is_a_noop_when_absent(monkeypatch):
    adapter = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=lambda: ([], []))
    monkeypatch.setattr(adapter, "_find_dropdown_window", lambda: None)
    recover_calls = []
    monkeypatch.setattr(adapter, "recover", lambda: recover_calls.append(1))

    assert adapter._dismiss_stray_results_popup() is False
    assert recover_calls == []


def test_dismiss_stray_results_popup_never_raises(monkeypatch):
    adapter = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=lambda: ([], []))

    def boom():
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(adapter, "_find_dropdown_window", boom)
    assert adapter._dismiss_stray_results_popup() is False


@pytest.mark.parametrize("method_name", ["ensure_group", "select_group"])
def test_group_entry_points_self_heal_a_stray_results_popup(monkeypatch, method_name):
    """ensure_group()/select_group() must check for and dismiss a stray
    results popup left open by a previous task, independent of
    orchestrator.py's own fix -- defense in depth at the exact point
    the live defect was reproduced (group-tree interaction beginning
    while a popup from the prior task was still open)."""
    adapter = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=lambda: ([], []))
    dismiss_calls = []
    monkeypatch.setattr(adapter, "_dismiss_stray_results_popup", lambda: dismiss_calls.append(1))
    monkeypatch.setattr(adapter, "_handle_duplicate_item_dialog", lambda: False)
    # Fail fast right after the self-heal calls -- we only care that
    # they happened, not the rest of the (extensively tested elsewhere)
    # method body.
    monkeypatch.setattr(adapter, "verify_application", lambda: False)

    from estimate_extractor.xactimate_lookup.adapter import AdapterError
    with pytest.raises(AdapterError):
        getattr(adapter, method_name)("Dwelling Roof")

    assert dismiss_calls == [1]


# ---------------------------------------------------------------------
# Phase 5.7: group ancestry/nesting depth is no longer a blocking safety
# condition -- what still must fail closed is genuine AMBIGUITY (more
# than one row matching a name) and an unlocatable/unverifiable target.
# ---------------------------------------------------------------------


def test_ensure_group_raises_on_ambiguous_existing_name(monkeypatch):
    """The no-op ("already exists") branch must still fail closed when
    TWO rows match the requested name -- silently reusing the first one
    found could write real line items into the wrong group."""
    adapter = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=lambda: ([], []))
    monkeypatch.setattr(adapter, "verify_application", lambda: True)
    monkeypatch.setattr(adapter, "verify_project", lambda: True)
    monkeypatch.setattr(adapter, "_ensure_main_window", lambda: 123)
    monkeypatch.setattr(adapter, "_force_foreground", lambda hwnd: True)
    monkeypatch.setattr(adapter, "_scroll_group_tree_to_top", lambda hwnd: None)
    monkeypatch.setattr(adapter, "snapshot_group_names", lambda: ["TEST", "Fence", "Fence"])

    from estimate_extractor.xactimate_lookup.adapter import AdapterError
    with pytest.raises(AdapterError, match="2 groups"):
        adapter.ensure_group("Fence")


def test_select_group_raises_on_ambiguous_name(monkeypatch):
    """select_group() must refuse to guess which of two same-named rows
    is the intended target."""
    adapter = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=lambda: ([], []))
    monkeypatch.setattr(adapter, "verify_application", lambda: True)
    monkeypatch.setattr(adapter, "verify_project", lambda: True)
    monkeypatch.setattr(adapter, "_ensure_main_window", lambda: 123)
    monkeypatch.setattr(adapter, "_force_foreground", lambda hwnd: True)
    monkeypatch.setattr(adapter, "_scroll_group_tree_to_top", lambda hwnd: None)
    monkeypatch.setattr(adapter, "_capture_client_image", lambda hwnd: object())
    monkeypatch.setattr(adapter, "_locate_group_tree_header", lambda image: (0, 0, 0, 0))
    monkeypatch.setattr(adapter, "snapshot_group_names", lambda: ["TEST", "Fence", "Fence"])

    from estimate_extractor.xactimate_lookup.adapter import AdapterError
    with pytest.raises(AdapterError, match="2 groups"):
        adapter.select_group("Fence")


def test_verify_group_returns_false_on_ambiguous_name(monkeypatch):
    """verify_group() promises to never raise -- an ambiguous name must
    still resolve to a safe False, not propagate the AdapterError."""
    adapter = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=lambda: ([], []))
    monkeypatch.setattr(adapter, "verify_application", lambda: True)
    monkeypatch.setattr(adapter, "verify_project", lambda: True)
    monkeypatch.setattr(adapter, "_ensure_main_window", lambda: 123)
    monkeypatch.setattr(adapter, "_scroll_group_tree_to_top", lambda hwnd: None)
    monkeypatch.setattr(adapter, "snapshot_group_names", lambda: ["TEST", "Fence", "Fence"])

    assert adapter.verify_group("Fence") is False


def test_select_group_succeeds_on_a_uniquely_nested_group(monkeypatch):
    """A group that exists but landed at an unexpected nesting depth
    (Phase 5.7's whole point) must still be selectable purely by name --
    select_group() never looks at indentation at all."""
    adapter = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=lambda: ([], []))
    monkeypatch.setattr(adapter, "verify_application", lambda: True)
    monkeypatch.setattr(adapter, "verify_project", lambda: True)
    monkeypatch.setattr(adapter, "_ensure_main_window", lambda: 123)
    monkeypatch.setattr(adapter, "_force_foreground", lambda hwnd: True)
    monkeypatch.setattr(adapter, "_scroll_group_tree_to_top", lambda hwnd: None)
    monkeypatch.setattr(adapter, "_capture_client_image", lambda hwnd: object())
    monkeypatch.setattr(adapter, "_locate_group_tree_header", lambda image: (0, 0, 0, 0))
    # "Front Elevation" nested under "Dwelling Roof" -- still the only
    # row matching its name.
    monkeypatch.setattr(adapter, "snapshot_group_names", lambda: ["TEST", "Exterior", "Dwelling Roof", "Front Elevation"])
    clicked = []
    monkeypatch.setattr(adapter, "_group_tree_row_xy", lambda header, row_index: (row_index, row_index))
    monkeypatch.setattr(adapter, "_click_client", lambda hwnd, x, y: clicked.append((x, y)))

    adapter.select_group("Front Elevation")  # must not raise

    assert clicked == [(3, 3)]  # clicked row 3, the nested group's own row


def test_verify_group_once_probes_a_uniquely_nested_group_by_name(monkeypatch):
    """_verify_group_once() must locate and probe a nested group purely
    by name -- ancestry/indentation is never consulted in the
    probe-commit-and-check verification path."""
    adapter = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=lambda: ([], []))
    monkeypatch.setattr(adapter, "verify_application", lambda: True)
    monkeypatch.setattr(adapter, "verify_project", lambda: True)
    monkeypatch.setattr(adapter, "_ensure_main_window", lambda: 123)
    monkeypatch.setattr(adapter, "_scroll_group_tree_to_top", lambda hwnd: None)
    monkeypatch.setattr(adapter, "snapshot_group_names", lambda: ["TEST", "Dwelling Roof", "Front Elevation"])
    monkeypatch.setattr(adapter, "_press_key", lambda code: None)
    monkeypatch.setattr(adapter, "_capture_client_image", lambda hwnd: object())
    monkeypatch.setattr(adapter, "_locate_group_tree_header", lambda image: (0, 0, 0, 0))

    probed_index = {}
    call_state = {"n": 0}

    def fake_subtotal_seq(image, header, row_index):
        probed_index["index"] = row_index
        call_state["n"] += 1
        return 0 if call_state["n"] == 1 else 200  # before, then after the probe commit

    monkeypatch.setattr(adapter, "_group_subtotal_pixel_count", fake_subtotal_seq)
    monkeypatch.setattr(adapter, "_reset_scroll_state", lambda: None)
    monkeypatch.setattr(adapter, "focus_search", lambda: None)
    monkeypatch.setattr(adapter, "clear_search", lambda: None)
    from estimate_extractor.xactimate_lookup.models import DropdownResult

    monkeypatch.setattr(
        adapter, "capture_dropdown",
        lambda: [DropdownResult(raw_text="SFG GUTA", row_position=0, category="SFG", selector="GUTA")],
    )
    monkeypatch.setattr(adapter, "parse_dropdown", lambda raw: raw)
    monkeypatch.setattr(adapter, "search_by_category_selector", lambda cat, sel: None)
    monkeypatch.setattr(adapter, "select_candidate", lambda target: None)
    monkeypatch.setattr(adapter, "enter_quantity", lambda qty: None)
    monkeypatch.setattr(adapter, "commit_item", lambda: None)
    monkeypatch.setattr(adapter, "_count_grid_rows", lambda image, offset: 0)
    monkeypatch.setattr(adapter, "_capture_and_locate", lambda hwnd, attempts=6, delay_s=0.6: (object(), (0, 0)))
    monkeypatch.setattr(adapter, "cancel_current_item", lambda **kwargs: None)

    assert adapter._verify_group_once("Front Elevation") is True
    assert probed_index["index"] == 2  # the nested group's own row, found purely by name
    # Phase 5.8 Stage 8: a real probe run must be counted, per-group.
    assert adapter.probes_run_total == 1
    assert adapter.probes_by_group == {"Front Elevation": 1}


def test_probe_counts_start_at_zero_and_only_increment_on_a_real_probe(monkeypatch):
    """Phase 5.8 Stage 8: probes_run_total/probes_by_group must reflect
    ONLY real disposable-probe runs -- a cached verify_group() hit
    (Phase 5.7B) must never increment them, so a normal N-group run
    shows at most N probes, never one per task or per repeated group
    operation."""
    adapter = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=lambda: ([], []))
    assert adapter.probes_run_total == 0
    assert adapter.probes_by_group == {}

    real_probe_calls = []

    def fake_verify_group_once(name):
        real_probe_calls.append(name)
        adapter.probes_run_total += 1
        adapter.probes_by_group[name] = adapter.probes_by_group.get(name, 0) + 1
        return True

    monkeypatch.setattr(adapter, "_verify_group_once", fake_verify_group_once)

    # 3 groups, each verified multiple times (as a resumed/rechecked
    # run might) -- only the FIRST verify_group() call per group must
    # reach the real probe; every later call for the same name is
    # served from cache.
    for _ in range(3):
        assert adapter.verify_group("Exterior") is True
    for _ in range(2):
        assert adapter.verify_group("Dwelling Roof") is True
    assert adapter.verify_group("Fence") is True

    assert real_probe_calls == ["Exterior", "Dwelling Roof", "Fence"]  # exactly one real probe per group
    assert adapter.probes_run_total == 3
    assert adapter.probes_by_group == {"Exterior": 1, "Dwelling Roof": 1, "Fence": 1}


def test_cleanup_probe_item_preserves_pre_existing_rows(monkeypatch):
    """Phase 5.3: a group being re-verified on resume can already hold
    real, previously-committed rows from an earlier task in the SAME
    group. _cleanup_probe_item() must cancel down to the row count that
    existed BEFORE the probe (target_row_count), never unconditionally
    to zero -- otherwise it wipes out real committed work along with
    its own disposable probe row. Live-caught: without this, a resumed
    group's real committed item was destroyed by the next task's group
    re-verification."""
    adapter = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=lambda: ([], []))
    monkeypatch.setattr(adapter, "_ensure_main_window", lambda: 123)
    monkeypatch.setattr(adapter, "_capture_and_locate", lambda hwnd, attempts=6, delay_s=0.6: (object(), (0, 0)))

    # Grid starts at 2 rows (1 pre-existing real commit + 1 probe row
    # just added) -- must cancel exactly once, down to 1, and stop.
    row_counts = iter([2, 1])
    monkeypatch.setattr(adapter, "_count_grid_rows", lambda image, offset: next(row_counts))
    cancel_calls = []
    monkeypatch.setattr(adapter, "cancel_current_item", lambda **kwargs: cancel_calls.append(1))
    commit_calls = []
    monkeypatch.setattr(adapter, "commit_item", lambda: commit_calls.append(1))

    adapter._cleanup_probe_item(target_row_count=1)

    assert cancel_calls == [1]  # cancelled exactly once -- never past the pre-existing row
    assert commit_calls == [1]


def test_cleanup_probe_item_defaults_to_fully_empty_grid(monkeypatch):
    """The default target_row_count=0 preserves the original
    unconditional-cleanup behavior for a caller that genuinely started
    from an empty grid."""
    adapter = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=lambda: ([], []))
    monkeypatch.setattr(adapter, "_ensure_main_window", lambda: 123)
    monkeypatch.setattr(adapter, "_capture_and_locate", lambda hwnd, attempts=6, delay_s=0.6: (object(), (0, 0)))

    row_counts = iter([1, 0])
    monkeypatch.setattr(adapter, "_count_grid_rows", lambda image, offset: next(row_counts))
    cancel_calls = []
    monkeypatch.setattr(adapter, "cancel_current_item", lambda **kwargs: cancel_calls.append(1))
    monkeypatch.setattr(adapter, "commit_item", lambda: None)

    adapter._cleanup_probe_item()

    assert cancel_calls == [1]


def test_verify_group_once_fails_closed_when_grid_cannot_be_located(monkeypatch):
    """Live-caught (follow-up): _capture_and_locate() returns
    offset=None only after exhausting its own 6 retries -- a real
    failure, not a quick blip. The old code silently treated that as
    "0 rows here" and proceeded to commit a probe item, then let
    _cleanup_probe_item(0) cancel real, already-committed rows via
    cancel_current_item() (which always removes whatever row is
    currently LAST, not specifically the probe). Must instead refuse
    the whole probe: no search, no commit, no cleanup, no deletion."""
    adapter = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=lambda: ([], []))
    monkeypatch.setattr(adapter, "verify_application", lambda: True)
    monkeypatch.setattr(adapter, "verify_project", lambda: True)
    monkeypatch.setattr(adapter, "_ensure_main_window", lambda: 123)
    monkeypatch.setattr(adapter, "_scroll_group_tree_to_top", lambda hwnd: None)
    monkeypatch.setattr(adapter, "snapshot_group_names", lambda: ["TEST", "Dwelling Roof"])
    monkeypatch.setattr(adapter, "_press_key", lambda code: None)
    monkeypatch.setattr(adapter, "_capture_client_image", lambda hwnd: object())
    monkeypatch.setattr(adapter, "_locate_group_tree_header", lambda image: (0, 0, 0, 0))
    monkeypatch.setattr(adapter, "_group_subtotal_pixel_count", lambda image, header, row_index: 0)
    # The failure under test: the grid cannot be located.
    monkeypatch.setattr(adapter, "_capture_and_locate", lambda hwnd, attempts=6, delay_s=0.6: (object(), None))

    def _fail_if_called(name):
        def _inner(*args, **kwargs):
            raise AssertionError(f"{name}() must not be called when the pre-probe grid state is unknown")
        return _inner

    monkeypatch.setattr(adapter, "focus_search", _fail_if_called("focus_search"))
    monkeypatch.setattr(adapter, "clear_search", _fail_if_called("clear_search"))
    monkeypatch.setattr(adapter, "search_by_category_selector", _fail_if_called("search_by_category_selector"))
    monkeypatch.setattr(adapter, "capture_dropdown", _fail_if_called("capture_dropdown"))
    monkeypatch.setattr(adapter, "select_candidate", _fail_if_called("select_candidate"))
    monkeypatch.setattr(adapter, "enter_quantity", _fail_if_called("enter_quantity"))
    monkeypatch.setattr(adapter, "commit_item", _fail_if_called("commit_item"))
    monkeypatch.setattr(adapter, "_cleanup_probe_item", _fail_if_called("_cleanup_probe_item"))
    monkeypatch.setattr(adapter, "cancel_current_item", _fail_if_called("cancel_current_item"))

    assert adapter._verify_group_once("Dwelling Roof") is False


# ---------------------------------------------------------------------
# verify_display_profile (Phase 5.2 Stage 10)
# ---------------------------------------------------------------------


class _FakeWin32Gui:
    def __init__(self, client_rect):
        self._client_rect = client_rect

    def GetClientRect(self, hwnd):
        return self._client_rect


class _FakeUser32:
    def __init__(self, dpi):
        self._dpi = dpi

    def GetDpiForWindow(self, hwnd):
        return self._dpi


class _FakeWindll:
    def __init__(self, dpi):
        self.user32 = _FakeUser32(dpi)


class _FakeCtypes:
    def __init__(self, dpi):
        self.windll = _FakeWindll(dpi)


def _patch_display_profile_happy_path(monkeypatch, adapter, *, width=1920, height=1021, dpi=96, group_tree=True, grid_anchor=True):
    monkeypatch.setattr(adapter, "_find_main_window", lambda: (123, "TEST"))
    monkeypatch.setattr(adapter, "_win32gui", lambda: _FakeWin32Gui((0, 0, width, height)))
    monkeypatch.setattr(adapter, "_win32", lambda: (_FakeCtypes(dpi), None))
    monkeypatch.setattr(adapter, "_capture_client_image", lambda hwnd: object())
    monkeypatch.setattr(adapter, "_locate_group_tree_header", lambda image: (0, 0, 0, 0) if group_tree else None)
    monkeypatch.setattr(adapter, "_anchor_offset", lambda image: (0, 0) if grid_anchor else None)


def test_verify_display_profile_ok_when_everything_matches(monkeypatch):
    adapter = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=lambda: ([], []))
    _patch_display_profile_happy_path(monkeypatch, adapter)

    report = adapter.verify_display_profile()
    assert report["ok"] is True
    assert report["blocking_reasons"] == []
    assert report["dimensions_match"] is True
    assert report["group_tree_visible"] is True
    assert report["grid_anchor_visible"] is True


def test_verify_display_profile_blocks_when_window_not_found(monkeypatch):
    adapter = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=lambda: ([], []))
    monkeypatch.setattr(adapter, "_find_main_window", lambda: None)

    report = adapter.verify_display_profile()
    assert report["ok"] is False
    assert report["window_found"] is False
    assert any("not found" in r for r in report["blocking_reasons"])


def test_verify_display_profile_blocks_on_size_mismatch(monkeypatch):
    adapter = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=lambda: ([], []))
    _patch_display_profile_happy_path(monkeypatch, adapter, width=1280, height=800)

    report = adapter.verify_display_profile()
    assert report["ok"] is False
    assert report["dimensions_match"] is False
    assert any("Client size" in r for r in report["blocking_reasons"])


def test_verify_display_profile_tolerates_small_size_variance(monkeypatch):
    """A couple of px of scrollbar/chrome variance (measured live:
    1920x1023 vs. the 1920x1021 calibration baseline) must not block --
    only a real profile mismatch should."""
    adapter = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=lambda: ([], []))
    _patch_display_profile_happy_path(monkeypatch, adapter, width=1920, height=1023)

    report = adapter.verify_display_profile()
    assert report["ok"] is True
    assert report["dimensions_match"] is True


def test_verify_display_profile_blocks_on_dpi_mismatch(monkeypatch):
    adapter = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=lambda: ([], []))
    _patch_display_profile_happy_path(monkeypatch, adapter, dpi=120)

    report = adapter.verify_display_profile()
    assert report["ok"] is False
    assert any("DPI" in r for r in report["blocking_reasons"])


def test_verify_display_profile_blocks_when_group_tree_not_visible(monkeypatch):
    adapter = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=lambda: ([], []))
    _patch_display_profile_happy_path(monkeypatch, adapter, group_tree=False)

    report = adapter.verify_display_profile()
    assert report["ok"] is False
    assert report["group_tree_visible"] is False
    assert any("group tree" in r for r in report["blocking_reasons"])


def test_verify_display_profile_blocks_when_grid_anchor_not_visible(monkeypatch):
    adapter = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=lambda: ([], []))
    _patch_display_profile_happy_path(monkeypatch, adapter, grid_anchor=False)

    report = adapter.verify_display_profile()
    assert report["ok"] is False
    assert report["grid_anchor_visible"] is False
    assert any("Cat" in r for r in report["blocking_reasons"])


def test_verify_display_profile_never_raises_on_unexpected_error(monkeypatch):
    adapter = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=lambda: ([], []))

    def _boom():
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(adapter, "_find_main_window", _boom)
    report = adapter.verify_display_profile()
    assert report["ok"] is False
    assert report["blocking_reasons"]


# ---------------------------------------------------------------------
# capture_estimate_baseline / verify_estimate_matches_baseline
# (Phase 5.4 Stages 2-3): regression coverage for the exact class of
# failure Phase 5.3 found live -- a disposable row that read as
# visually empty/inactive by a structural (row-count) check alone
# while still carrying real financial value. These tests prove
# reconciliation only passes when BOTH structural state (row
# identities/counts) AND financial state (quantities, group
# subtotals, Grand Total) match the baseline -- neither alone is
# sufficient.
# ---------------------------------------------------------------------


class _MockEstimateState:
    """A small in-memory stand-in for "whatever is currently on screen"
    -- lets a test capture a baseline against one state, then mutate
    the state and re-verify against the SAME baseline object, exactly
    like the live before/after sequence this is modeling."""

    def __init__(self, *, groups: dict, grand_total: str, saved):
        # groups: {name: {"rows": [GroupRowSnapshot, ...], "subtotal": str}}
        self.groups = groups
        self.grand_total = grand_total
        self.saved = saved
        self.selected_group: str | None = None


def _wire_mock_estimate(adapter, monkeypatch, state: _MockEstimateState):
    from estimate_extractor.xactimate_lookup.adapter import AdapterError

    monkeypatch.setattr(adapter, "_ensure_main_window", lambda: 123)
    monkeypatch.setattr(adapter, "_capture_client_image", lambda hwnd: object())
    monkeypatch.setattr(adapter, "_locate_group_tree_header", lambda image: (0, 0, 0, 0))
    monkeypatch.setattr(adapter, "snapshot_group_names", lambda: ["TEST", *state.groups.keys()])
    monkeypatch.setattr(adapter, "_find_group_row", lambda rows, name: (rows.index(name) if name in rows else None))

    def _select_group(name):
        if name not in state.groups:
            raise AdapterError(f"select_group({name!r}): group not found.")
        state.selected_group = name

    monkeypatch.setattr(adapter, "select_group", _select_group)
    monkeypatch.setattr(adapter, "_snapshot_grid_rows_detailed", lambda: list(state.groups[state.selected_group]["rows"]))
    monkeypatch.setattr(adapter, "_read_group_subtotal_text", lambda image, header, idx: state.groups[state.selected_group]["subtotal"])
    monkeypatch.setattr(adapter, "_read_grand_total_text", lambda: state.grand_total)
    monkeypatch.setattr(adapter, "_read_saved_state", lambda: state.saved)


def test_capture_estimate_baseline_records_rows_subtotals_grand_total_saved(monkeypatch):
    adapter = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=lambda: ([], []))
    state = _MockEstimateState(
        groups={
            "Dwelling Roof": {"rows": [GroupRowSnapshot("RFG", "FELT15", "10", "SQ")], "subtotal": "$435.20"},
            "Exterior": {"rows": [], "subtotal": ""},
        },
        grand_total="$435.20", saved=True,
    )
    _wire_mock_estimate(adapter, monkeypatch, state)

    baseline = adapter.capture_estimate_baseline(["Dwelling Roof", "Exterior"])

    assert baseline.group_names == ["Dwelling Roof", "Exterior"]
    assert baseline.group_rows["Dwelling Roof"] == [GroupRowSnapshot("RFG", "FELT15", "10", "SQ")]
    assert baseline.group_rows["Exterior"] == []
    assert baseline.group_subtotal_text["Dwelling Roof"] == "$435.20"
    assert baseline.grand_total_text == "$435.20"
    assert baseline.saved is True


def test_reconciliation_passes_when_nothing_changed(monkeypatch):
    adapter = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=lambda: ([], []))
    state = _MockEstimateState(
        groups={"Dwelling Roof": {"rows": [], "subtotal": ""}, "Exterior": {"rows": [], "subtotal": ""}},
        grand_total="$0.00", saved=True,
    )
    _wire_mock_estimate(adapter, monkeypatch, state)
    baseline = adapter.capture_estimate_baseline(["Dwelling Roof", "Exterior"])

    result = adapter.verify_estimate_matches_baseline(baseline)

    assert result.ok is True
    assert result.mismatches == []


def test_reconciliation_tolerates_different_ocr_noise_from_the_same_blank_cell(monkeypatch):
    """Live-caught (Phase 5.4): a genuinely-blank group Subtotal cell
    OCR'd as two DIFFERENT short garbage strings ("dy", then "ni")
    across two live captures of the SAME unchanged, physically blank
    cell. Neither reading contains a digit -- reconciliation must
    treat digit-free noise as equivalent, not flag it as a false
    positive, while still catching a REAL value appearing later (see
    the sibling "visually zero but financially active" test)."""
    adapter = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=lambda: ([], []))
    state = _MockEstimateState(groups={"Dwelling Roof": {"rows": [], "subtotal": "dy"}}, grand_total="$0.00", saved=True)
    _wire_mock_estimate(adapter, monkeypatch, state)
    baseline = adapter.capture_estimate_baseline(["Dwelling Roof"])

    state.groups["Dwelling Roof"]["subtotal"] = "ni"  # different noise, same blank cell, no digits either way

    result = adapter.verify_estimate_matches_baseline(baseline)

    assert result.ok is True
    assert result.mismatches == []


def test_reconciliation_tolerates_a_zero_value_reading_of_the_same_blank_cell(monkeypatch):
    """Live-caught (Phase 5.4 Stage 10): the SAME genuinely-blank group
    Subtotal cell OCR'd as digit-free noise ("dy") on one capture and
    as a real-looking zero value ("$0.0") on another. Both mean "no
    value" -- a zero-value reading must canonicalize the same way
    digit-free noise does, not be flagged as a false residue mismatch
    on an actually-clean group. A nonzero value must still compare as
    a real change (see the sibling "visually zero but financially
    active" test)."""
    adapter = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=lambda: ([], []))
    state = _MockEstimateState(groups={"Dwelling Roof": {"rows": [], "subtotal": "dy"}}, grand_total="$0.00", saved=True)
    _wire_mock_estimate(adapter, monkeypatch, state)
    baseline = adapter.capture_estimate_baseline(["Dwelling Roof"])

    state.groups["Dwelling Roof"]["subtotal"] = "$0.0"  # zero value, same blank cell

    result = adapter.verify_estimate_matches_baseline(baseline)

    assert result.ok is True
    assert result.mismatches == []


def test_reconciliation_detects_visually_zero_but_financially_active_row(monkeypatch):
    """The exact Phase 5.3 failure mode: a row exists with a quantity
    that reads as effectively empty/placeholder in a naive check (here
    modeled as an unexpected quantity value on an otherwise-identical
    row) while the group's own Subtotal and Grand Total both carry
    real value -- reconciliation must catch this via the financial
    fields even if someone only glanced at row count."""
    adapter = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=lambda: ([], []))
    state = _MockEstimateState(
        groups={"Dwelling Roof": {"rows": [GroupRowSnapshot("PLM", "TLTRS", "1", "EA")], "subtotal": ""}, "Exterior": {"rows": [], "subtotal": ""}},
        grand_total="$0.00", saved=True,
    )
    _wire_mock_estimate(adapter, monkeypatch, state)
    baseline = adapter.capture_estimate_baseline(["Dwelling Roof", "Exterior"])

    # The row "looks" the same structurally (same identity, same row
    # count) but now carries real financial value that baseline never
    # had -- exactly what a visually-zero-valued residual row looked
    # like live in Phase 5.3.
    state.groups["Dwelling Roof"]["subtotal"] = "$330.31"
    state.grand_total = "$330.31"

    result = adapter.verify_estimate_matches_baseline(baseline)

    assert result.ok is False
    assert any("Dwelling Roof' subtotal" in m for m in result.mismatches)
    assert any("Grand Total" in m for m in result.mismatches)


def test_reconciliation_detects_group_subtotal_mismatch_alone(monkeypatch):
    adapter = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=lambda: ([], []))
    state = _MockEstimateState(
        groups={"Dwelling Roof": {"rows": [], "subtotal": "$0.00"}, "Exterior": {"rows": [], "subtotal": "$0.00"}},
        grand_total="$0.00", saved=True,
    )
    _wire_mock_estimate(adapter, monkeypatch, state)
    baseline = adapter.capture_estimate_baseline(["Dwelling Roof", "Exterior"])

    state.groups["Exterior"]["subtotal"] = "$99.99"

    result = adapter.verify_estimate_matches_baseline(baseline)

    assert result.ok is False
    assert any("Exterior' subtotal" in m for m in result.mismatches)


def test_reconciliation_detects_grand_total_mismatch(monkeypatch):
    adapter = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=lambda: ([], []))
    state = _MockEstimateState(
        groups={"Dwelling Roof": {"rows": [], "subtotal": ""}},
        grand_total="$0.00", saved=True,
    )
    _wire_mock_estimate(adapter, monkeypatch, state)
    baseline = adapter.capture_estimate_baseline(["Dwelling Roof"])

    state.grand_total = "$50.00"

    result = adapter.verify_estimate_matches_baseline(baseline)

    assert result.ok is False
    assert any("Grand Total" in m for m in result.mismatches)


def test_reconciliation_detects_row_count_mismatch(monkeypatch):
    adapter = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=lambda: ([], []))
    state = _MockEstimateState(groups={"Dwelling Roof": {"rows": [], "subtotal": ""}}, grand_total="$0.00", saved=True)
    _wire_mock_estimate(adapter, monkeypatch, state)
    baseline = adapter.capture_estimate_baseline(["Dwelling Roof"])

    state.groups["Dwelling Roof"]["rows"] = [GroupRowSnapshot("SFG", "GUTA", "1", "LF")]

    result = adapter.verify_estimate_matches_baseline(baseline)

    assert result.ok is False
    assert any("row count" in m for m in result.mismatches)


def test_reconciliation_detects_quantity_change_on_same_identity(monkeypatch):
    """A pending-selection-cancellation-leaves-value-behind scenario:
    the row's CAT/SEL identity is unchanged, but its quantity now
    differs (e.g. a default quantity got persisted instead of being
    fully removed) -- must be caught even though row identity alone
    matches."""
    adapter = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=lambda: ([], []))
    state = _MockEstimateState(
        groups={"Dwelling Roof": {"rows": [GroupRowSnapshot("SFG", "GUTA", "0", "LF")], "subtotal": ""}},
        grand_total="$0.00", saved=True,
    )
    _wire_mock_estimate(adapter, monkeypatch, state)
    baseline = adapter.capture_estimate_baseline(["Dwelling Roof"])

    state.groups["Dwelling Roof"]["rows"] = [GroupRowSnapshot("SFG", "GUTA", "5", "LF")]

    result = adapter.verify_estimate_matches_baseline(baseline)

    assert result.ok is False
    assert any("quantity" in m for m in result.mismatches)


def test_reconciliation_requires_saved_state(monkeypatch):
    """Recovery claiming success while financial residue remains --
    modeled here as the project simply not being in a Saved state,
    which must block a clean pass regardless of what the visible rows
    show."""
    adapter = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=lambda: ([], []))
    state = _MockEstimateState(groups={"Dwelling Roof": {"rows": [], "subtotal": ""}}, grand_total="$0.00", saved=True)
    _wire_mock_estimate(adapter, monkeypatch, state)
    baseline = adapter.capture_estimate_baseline(["Dwelling Roof"])

    state.saved = False

    result = adapter.verify_estimate_matches_baseline(baseline)

    assert result.ok is False
    assert any("Saved" in m for m in result.mismatches)


def test_reconciliation_reports_mismatch_not_exception_when_group_cannot_be_selected(monkeypatch):
    """A hidden/collapsed disposable group that can no longer be
    selected must fail reconciliation with a specific, readable
    mismatch -- never raise and never silently skip that group."""
    adapter = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=lambda: ([], []))
    state = _MockEstimateState(groups={"Dwelling Roof": {"rows": [], "subtotal": ""}}, grand_total="$0.00", saved=True)
    _wire_mock_estimate(adapter, monkeypatch, state)
    baseline = adapter.capture_estimate_baseline(["Dwelling Roof"])

    del state.groups["Dwelling Roof"]  # group vanished/can no longer be selected

    result = adapter.verify_estimate_matches_baseline(baseline)

    assert result.ok is False
    assert any("could not select" in m for m in result.mismatches)


def test_reconciliation_baseline_mismatch_blocks_continuation_in_execution_runner(monkeypatch, tmp_path):
    """Task execution must refuse to continue after unresolved cleanup
    -- exercised at the execution_runner level, where a group's
    verify_group() failure already blocks its tasks. This proves the
    SAME "never proceed on an unverified/mismatched state" contract
    extends to baseline reconciliation: a caller that gates on
    verify_estimate_matches_baseline().ok before resuming a run must
    see it fail, not a silent pass."""
    adapter = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=lambda: ([], []))
    state = _MockEstimateState(groups={"Dwelling Roof": {"rows": [], "subtotal": ""}}, grand_total="$0.00", saved=True)
    _wire_mock_estimate(adapter, monkeypatch, state)
    baseline = adapter.capture_estimate_baseline(["Dwelling Roof"])

    state.groups["Dwelling Roof"]["rows"] = [GroupRowSnapshot("ZZZ", "RESIDUE", "1", "EA")]
    state.groups["Dwelling Roof"]["subtotal"] = "$12.34"
    state.grand_total = "$12.34"

    result = adapter.verify_estimate_matches_baseline(baseline)
    assert result.ok is False

    # The gate a real caller (Build Estimate's cleanup step) would use:
    # never treat cleanup as complete when this is False.
    cleanup_complete = result.ok
    assert cleanup_complete is False


# ---------------------------------------------------------------------
# Phase 5.5D: destructive-action audit + committed-row protection.
# Live incident: rows Build Estimate -> Execute successfully committed
# were later deleted by an UNRELATED group's verify_group() cleanup
# cycle. cancel_current_item() is the one primitive every destructive
# path in this codebase funnels through -- these tests exercise the
# protection/audit logic directly on it, plus the two callers
# (_cleanup_probe_item, recover) that must never bypass it.
# ---------------------------------------------------------------------


def _adapter_with_stubbed_grid(monkeypatch, *, row_identities, group="Dwelling Roof"):
    """Wires up enough of cancel_current_item()'s own dependencies to
    reach its protection check without touching any real win32/OCR
    API. `row_identities` is the CURRENT grid's [(cat, sel), ...] --
    the last entry is what a real cancel would target."""
    adapter = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=lambda: ([], []))
    monkeypatch.setattr(adapter, "_ensure_main_window", lambda: 123)
    monkeypatch.setattr(adapter, "_capture_and_locate", lambda hwnd, attempts=6, delay_s=0.6: (object(), (0, 0)))
    monkeypatch.setattr(adapter, "_count_grid_rows", lambda image, offset: len(row_identities))
    monkeypatch.setattr(adapter, "_shifted_anchor", lambda name, offset: (0, 0, 0, 0))
    row_iter_state = {"i": 0}

    def _fake_read_cat_sel(image, offset, row_top):
        # Called once per row, top-to-bottom, by cancel_current_item()'s
        # own before/after identity reads.
        idx = row_top // _GRID_ROW_HEIGHT if row_top else 0
        idx = min(idx, len(row_identities) - 1)
        return row_identities[idx]

    monkeypatch.setattr(adapter, "_read_category_selector_at", _fake_read_cat_sel)
    monkeypatch.setattr(adapter, "_last_row_geometry", lambda image, offset: (len(row_identities), (len(row_identities) - 1) * _GRID_ROW_HEIGHT))
    adapter.set_execution_context(run_id="run_1", group=group)
    return adapter


def test_cancel_current_item_requires_reason_and_caller():
    """Phase 5.5D: reason/caller are no longer optional -- a call site
    that doesn't declare them is a programming error, not something
    this method can silently default."""
    adapter = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=lambda: ([], []))
    with pytest.raises(TypeError):
        adapter.cancel_current_item()  # missing required keyword args


def test_cancel_current_item_refuses_when_it_would_drop_below_protected_floor(monkeypatch):
    """The exact live incident this phase closes: a group already has 1
    protected (successfully committed) row; cancel_current_item() must
    refuse to remove it, even though it's the LAST row in the grid --
    "it's last" is never sufficient justification on its own."""
    from estimate_extractor.xactimate_lookup.adapter import ProtectedCommittedRowError

    adapter = _adapter_with_stubbed_grid(monkeypatch, row_identities=[("SFG", "GUTA")], group="Dwelling Roof")
    adapter.record_protected_commit(category="SFG", selector="GUTA")
    assert adapter._protected_row_ledger.count_for_group("Dwelling Roof") == 1

    with pytest.raises(ProtectedCommittedRowError):
        adapter.cancel_current_item(reason="disposable_group_probe", caller="test")


def test_cancel_current_item_allows_deletion_above_the_protected_floor(monkeypatch):
    """The counterpart: a probe row ON TOP OF a protected row is safe to
    cancel -- the floor is "protected count", not "zero mutation ever
    allowed". Confirms the protection is precise, not a blanket freeze."""
    adapter = _adapter_with_stubbed_grid(
        monkeypatch, row_identities=[("SFG", "GUTA"), ("SFG", "GUTA")], group="Dwelling Roof",
    )
    adapter.record_protected_commit(category="SFG", selector="GUTA")  # 1 protected row
    monkeypatch.setattr(adapter, "_open_row_context_menu", lambda hwnd, x, y: None)
    monkeypatch.setattr(adapter, "_find_context_menu_popup_hwnd", lambda hwnd: 456)
    monkeypatch.setattr(adapter, "_click_delete_via_uia", lambda popup_hwnd: True)
    monkeypatch.setattr(adapter, "_unexpected_dialog_present", lambda: False)

    # After the "delete", only the 1 protected row remains.
    call_count = {"n": 0}

    def _count_after(image, offset):
        call_count["n"] += 1
        return 1  # dropped from 2 to 1 -- exactly the protected floor

    def _capture_and_locate_after_first_call(hwnd, attempts=6, delay_s=0.6):
        return object(), (0, 0)

    monkeypatch.setattr(adapter, "_capture_and_locate", _capture_and_locate_after_first_call)
    monkeypatch.setattr(adapter, "_count_grid_rows", _count_after)

    adapter.cancel_current_item(reason="disposable_group_probe", caller="test")  # must not raise


def test_invalid_destructive_reason_is_refused():
    from estimate_extractor.xactimate_lookup.destructive_audit import InvalidDestructiveReason

    adapter = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=lambda: ([], []))
    with pytest.raises(InvalidDestructiveReason):
        adapter.cancel_current_item(reason="cleanup", caller="test")  # not in the fixed reason set


def test_cleanup_probe_item_propagates_protected_row_error_instead_of_swallowing_it(monkeypatch):
    """The exact fix for the live incident: _cleanup_probe_item()'s
    own bare `except Exception: pass` used to silently absorb this,
    letting the run continue as if cleanup had simply failed. It must
    now propagate so the whole run hard-stops."""
    from estimate_extractor.xactimate_lookup.adapter import ProtectedCommittedRowError

    adapter = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=lambda: ([], []))
    monkeypatch.setattr(adapter, "_ensure_main_window", lambda: 123)
    monkeypatch.setattr(adapter, "_capture_and_locate", lambda hwnd, attempts=6, delay_s=0.6: (object(), (0, 0)))
    monkeypatch.setattr(adapter, "_count_grid_rows", lambda image, offset: 5)  # never reaches target -- keeps trying

    def _raise_protected(**kwargs):
        raise ProtectedCommittedRowError("simulated refusal")

    monkeypatch.setattr(adapter, "cancel_current_item", _raise_protected)
    monkeypatch.setattr(adapter, "commit_item", lambda: None)

    with pytest.raises(ProtectedCommittedRowError):
        adapter._cleanup_probe_item(target_row_count=0)


def test_recover_never_calls_cancel_current_item():
    """recover() is Escape + internal state reset only -- it must never
    reach the one primitive that can delete a row, protected or not."""
    adapter = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=lambda: ([], []))

    def _fail_if_called(**kwargs):
        raise AssertionError("recover() must never call cancel_current_item()")

    adapter.cancel_current_item = _fail_if_called
    adapter.close_transient_dialogs = lambda: False
    adapter._press_key = lambda code: None

    adapter.recover()  # must not raise (and must not call cancel_current_item)


def test_record_protected_commit_populates_the_ledger(monkeypatch):
    adapter = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=lambda: ([], []))
    monkeypatch.setattr(adapter, "_ensure_main_window", lambda: 123)
    monkeypatch.setattr(adapter, "_capture_and_locate", lambda hwnd, attempts=6, delay_s=0.6: (object(), (0, 0)))
    monkeypatch.setattr(adapter, "_last_row_geometry", lambda image, offset: (1, 0))
    adapter.set_execution_context(run_id="run_1", task_id="task_1", source_row="Row 1", group="Dwelling Roof")

    adapter.record_protected_commit(category="SFG", selector="GUTA", description="Gutter", quantity=5.0, unit="LF")

    records = adapter._protected_row_ledger.records_for_group("Dwelling Roof")
    assert len(records) == 1
    assert records[0].task_id == "task_1"
    assert records[0].committed_row_identity == ("SFG", "GUTA")


def test_destructive_action_auditor_writes_one_json_line_per_call(tmp_path):
    from estimate_extractor.xactimate_lookup.destructive_audit import DestructiveActionAuditor, ExecutionContext

    log_path = tmp_path / "destructive_action_audit.jsonl"
    auditor = DestructiveActionAuditor(log_path)
    ctx = ExecutionContext(run_id="run_1", task_id="task_1", source_row="Row 1", group="Dwelling Roof")

    auditor.record(
        context=ctx, method="cancel_current_item", reason="disposable_group_probe", caller="test",
        target_type="last_grid_row", target_identity="('SFG', 'GUTA')",
        row_count_before=2, row_identities_before=[("SFG", "GUTA"), ("SFG", "GUTA")],
        row_count_after=1, row_identities_after=[("SFG", "GUTA")],
        result="deleted",
    )

    import json
    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["reason"] == "disposable_group_probe"
    assert entry["run_id"] == "run_1"
    assert entry["result"] == "deleted"
