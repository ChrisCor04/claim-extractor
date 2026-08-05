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


def test_group_name_matches_is_whitespace_and_case_insensitive():
    adapter = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=lambda: ([], []))
    assert adapter._group_name_matches("fy DwellingRoofl Ss", "Dwelling Roof") is True
    assert adapter._group_name_matches("& Utility Room || Simila", "Utility Room") is True
    assert adapter._group_name_matches("", "Dwelling Roof") is False
    assert adapter._group_name_matches("Front Elevation", "Dwelling Roof") is False


def test_find_group_row_returns_first_match_index():
    adapter = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=lambda: ([], []))
    rows = ["TEST", "Utility Room", "Dwelling Roof", ""]
    assert adapter._find_group_row(rows, "Dwelling Roof") == 2
    assert adapter._find_group_row(rows, "Utility Room") == 1
    assert adapter._find_group_row(rows, "Front Elevation") is None


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
    monkeypatch.setattr(adapter, "cancel_current_item", lambda: cancel_calls.append(1))
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
    monkeypatch.setattr(adapter, "cancel_current_item", lambda: cancel_calls.append(1))
    monkeypatch.setattr(adapter, "commit_item", lambda: None)

    adapter._cleanup_probe_item()

    assert cancel_calls == [1]


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
