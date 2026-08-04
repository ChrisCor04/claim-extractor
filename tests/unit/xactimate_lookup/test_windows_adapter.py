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
    PopupNotFoundError,
    QuantityVerificationResult,
    StaleCandidateError,
    WindowsXactimateAdapter,
    _GRID_COLUMNS,
    _GRID_ROW_HEIGHT,
    _RawDropdownRow,
    _split_category_selector,
)


def test_module_imports_without_windows_dependencies():
    """Guards the module's own promise: no ctypes/win32gui/comtypes/
    pytesseract import happens at module load time."""
    import estimate_extractor.xactimate_lookup.windows_adapter as mod

    assert hasattr(mod, "WindowsXactimateAdapter")


def test_supports_live_execution_defaults_false():
    assert WindowsXactimateAdapter.supports_live_execution is False
    adapter = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=lambda: ([], []))
    assert adapter.supports_live_execution is False


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


def test_capture_dropdown_raises_popup_not_found_after_timeout():
    """Confirms the timeout path raises rather than hanging or
    returning an empty-but-successful result -- orchestrator.py must
    see this as an AdapterError-family exception to stop safely."""
    adapter = WindowsXactimateAdapter(
        expected_project_name="TEST",
        window_finder=lambda: ([], []),  # no popup ever appears
        dropdown_timeout_s=0.2,
    )
    with pytest.raises(PopupNotFoundError):
        adapter.capture_dropdown()


def test_select_candidate_without_prior_capture_raises():
    adapter = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=lambda: ([], []))
    candidate = DropdownResult(raw_text="x", row_position=0, category="SFG", selector="GUTA")
    with pytest.raises(PopupNotFoundError):
        adapter.select_candidate(candidate)


def test_get_adapter_diagnostics_reports_not_found_state():
    adapter = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=lambda: ([], []))
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
    assert adapter.supports_live_execution is False

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
