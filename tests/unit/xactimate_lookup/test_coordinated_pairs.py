"""Unit tests for the pure, offline coordinated remove/replace pair
detection rule (Phase 5.23, R&R Stage 1). Uses lightweight fake task
objects (only the fields detect_coordinated_pairs() actually reads) so
these stay fast and fully isolated from the rest of the execution-plan
machinery -- see test_execution_plan.py for the end-to-end integration
coverage through build_execution_plan()."""

from __future__ import annotations

from types import SimpleNamespace

from estimate_extractor.xactimate_lookup.coordinated_pairs import (
    REASON_AMBIGUOUS_MULTIPLE_PARTNERS,
    REASON_NO_CANDIDATES,
    REASON_PAIRED,
    REASON_PARTNER_NOT_MUTUALLY_UNIQUE,
    detect_coordinated_pairs,
    pair_id_for,
)


def _task(
    task_id, order, action, *, trade="roofing", component="composition_shingles",
    material="3-tab", unit="SQ", area="Exterior", section="Main Roof",
):
    return SimpleNamespace(
        task_id=task_id, source_order=order, area_name=area, section_name=section,
        normalized_action=action, normalized_trade=trade, normalized_component=component,
        normalized_material=material, source_unit=unit,
    )


def _result_for(results, remove_task_id):
    return next(r for r in results if r.remove_task_id == remove_task_id)


def test_normal_remove_replace_pair_detected():
    tasks = [_task("R", 0, "remove"), _task("P", 1, "unknown")]
    results = detect_coordinated_pairs(tasks)
    assert len(results) == 1
    r = results[0]
    assert r.paired is True
    assert r.reason == REASON_PAIRED
    assert r.remove_task_id == "R"
    assert r.replace_task_id == "P"


def test_reversed_source_order_pairs_identically():
    """The replacement line appears BEFORE the remove line -- nothing in
    _evidence_match() looks at which task comes first, only at the
    bounded distance between them, so this must pair exactly like the
    normal order."""
    tasks = [_task("P", 0, "install"), _task("R", 1, "remove")]
    results = detect_coordinated_pairs(tasks)
    assert len(results) == 1
    r = results[0]
    assert r.paired is True
    assert r.remove_task_id == "R"
    assert r.replace_task_id == "P"


def test_unrelated_adjacent_tasks_do_not_pair():
    tasks = [_task("R", 0, "remove"), _task("X", 1, "unknown", component="gutter")]
    results = detect_coordinated_pairs(tasks)
    assert len(results) == 1
    assert results[0].paired is False
    assert results[0].reason == REASON_NO_CANDIDATES


def test_ambiguous_many_to_one_remains_unpaired():
    """Two REMOVE tasks both adjacent to the same replacement -- neither
    side is mutually unique, so NEITHER pairs."""
    tasks = [_task("R1", 0, "remove"), _task("P", 1, "unknown"), _task("R2", 2, "remove")]
    results = detect_coordinated_pairs(tasks)
    assert len(results) == 2
    for r in results:
        assert r.paired is False
        assert r.reason == REASON_PARTNER_NOT_MUTUALLY_UNIQUE
        assert r.replace_task_id is None


def test_ambiguous_one_to_many_remains_unpaired():
    """One REMOVE task adjacent to two plausible replacements."""
    tasks = [_task("P1", 0, "unknown"), _task("R", 1, "remove"), _task("P2", 2, "install")]
    results = detect_coordinated_pairs(tasks)
    assert len(results) == 1
    assert results[0].paired is False
    assert results[0].reason == REASON_AMBIGUOUS_MULTIPLE_PARTNERS


def test_mismatched_group_does_not_pair():
    tasks = [_task("R", 0, "remove", section="Main Roof"), _task("P", 1, "unknown", section="Shed")]
    results = detect_coordinated_pairs(tasks)
    assert results[0].paired is False
    assert results[0].reason == REASON_NO_CANDIDATES


def test_mismatched_unit_does_not_pair():
    tasks = [_task("R", 0, "remove", unit="SQ"), _task("P", 1, "unknown", unit="LF")]
    results = detect_coordinated_pairs(tasks)
    assert results[0].paired is False
    assert results[0].reason == REASON_NO_CANDIDATES


def test_mismatched_component_does_not_pair():
    tasks = [_task("R", 0, "remove", component="composition_shingles"), _task("P", 1, "unknown", component="drip_edge")]
    results = detect_coordinated_pairs(tasks)
    assert results[0].paired is False
    assert results[0].reason == REASON_NO_CANDIDATES


def test_mismatched_material_does_not_pair_when_material_is_meaningful():
    """Both sides state an EXPLICIT, different material -- a real
    conflict, not mere absence -- so this must not pair."""
    tasks = [_task("R", 0, "remove", material="3-tab composition shingles"), _task("P", 1, "unknown", material="aluminum")]
    results = detect_coordinated_pairs(tasks)
    assert results[0].paired is False
    assert results[0].reason == REASON_NO_CANDIDATES


def test_material_absent_on_one_side_is_still_compatible():
    """Mirrors ranking.py's own 'unspecified is not a conflict'
    principle -- one side stating no material at all must not by
    itself block a pair that's otherwise fully evidenced."""
    tasks = [_task("R", 0, "remove", material=None), _task("P", 1, "unknown", material="3-tab")]
    results = detect_coordinated_pairs(tasks)
    assert results[0].paired is True


def test_equal_quantities_are_not_a_detection_concern():
    """Detection itself never reads quantity at all -- pairing is
    purely evidentiary (group/trade/component/material/unit/action/
    proximity); quantities are carried through independently by the
    caller (see test_execution_plan.py's CoordinatedPair integration
    coverage) regardless of whether they happen to be equal."""
    tasks = [_task("R", 0, "remove"), _task("P", 1, "unknown")]
    results = detect_coordinated_pairs(tasks)
    assert results[0].paired is True


def test_exactly_one_pair_created_from_a_three_task_neighborhood():
    """A clean R -> P -> (unrelated) neighborhood: exactly one
    PairDetection record (only REMOVE tasks are entry points), exactly
    one pair."""
    tasks = [_task("R", 0, "remove"), _task("P", 1, "unknown"), _task("Q", 2, "unknown", component="gutter")]
    results = detect_coordinated_pairs(tasks)
    assert len(results) == 1
    assert results[0].paired is True
    assert results[0].replace_task_id == "P"


def test_pair_ids_are_stable_and_deterministic():
    tasks = [_task("task_line_0001", 0, "remove"), _task("task_line_0002", 1, "unknown")]
    first = pair_id_for("task_line_0001", "task_line_0002")
    second = pair_id_for("task_line_0001", "task_line_0002")
    assert first == second
    results = detect_coordinated_pairs(tasks)
    assert pair_id_for(results[0].remove_task_id, results[0].replace_task_id) == first


def test_remove_action_never_pairs_with_another_remove():
    tasks = [_task("R1", 0, "remove"), _task("R2", 1, "remove")]
    results = detect_coordinated_pairs(tasks)
    assert len(results) == 2
    for r in results:
        assert r.paired is False
        assert r.reason == REASON_NO_CANDIDATES


def test_unrelated_recognized_action_does_not_count_as_install_like():
    """A partner with a real, different recognized action (e.g. clean)
    is a genuinely different scope -- must not be treated as an
    implicit-install partner just because it's adjacent and otherwise
    matches."""
    tasks = [_task("R", 0, "remove"), _task("C", 1, "clean")]
    results = detect_coordinated_pairs(tasks)
    assert results[0].paired is False
    assert results[0].reason == REASON_NO_CANDIDATES


def test_outside_proximity_window_does_not_pair():
    tasks = [_task("R", 0, "remove"), _task("Filler", 1, "unknown", component="gutter"), _task("P", 2, "unknown")]
    results = detect_coordinated_pairs(tasks)
    assert results[0].paired is False
    assert results[0].reason == REASON_NO_CANDIDATES


def test_real_odom_benchmark_shape_all_three_pairs():
    """Regression lock: the exact three real pairs from
    odom-insurance-v2 that motivated this module, reconstructed here
    without any row IDs/descriptions/CAT-SEL hardcoded into production
    code -- only in this test's own fixture data."""
    tasks = [
        _task("task_line_0001", 0, "remove", material="3-tab composition shingles", unit="SQ", section="Main Roof"),
        _task("task_line_0002", 1, "unknown", material="3-tab composition shingles", unit="SQ", section="Main Roof"),
        _task("task_line_0009", 8, "remove", component="roof_surcharge", material=None, unit="SQ", section="Main Roof"),
        _task("task_line_0010", 9, "install", component="roof_surcharge", material=None, unit="SQ", section="Main Roof"),
        _task("task_line_0020", 19, "remove", material="3-tab composition shingles", unit="SQ", section="Shed"),
        _task("task_line_0021", 20, "unknown", material="3-tab composition shingles", unit="SQ", section="Shed"),
    ]
    results = detect_coordinated_pairs(tasks)
    paired = {r.remove_task_id: r.replace_task_id for r in results if r.paired}
    assert paired == {
        "task_line_0001": "task_line_0002",
        "task_line_0009": "task_line_0010",
        "task_line_0020": "task_line_0021",
    }
