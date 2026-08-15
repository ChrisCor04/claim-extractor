from __future__ import annotations

import pytest

from estimate_extractor.xactimate_lookup import xactimate_calibration as calibration
from estimate_extractor.xactimate_lookup.xactimate_calibration import (
    CALIBRATION_GROUP_NAMES, SCHEMA_VERSION, XactimateCalibration, apply_fast_geometry,
    complete_interactive_group_row_calibration, confident_known_group_measurement,
    load_calibration, machine_identifier, measure_group_row_pitch, save_calibration,
    validate_calibration,
)


def profile(*, width=1920, height=1023, dpi=96, monitor=(0, 0, 2560, 1440)):
    return XactimateCalibration(
        schema_version=SCHEMA_VERSION, profile_id="test", machine_id=machine_identifier(), created_at="now",
        executable_path=None, executable_version=None, window_class="Xactimate", window_title="TEST",
        outer_rect=(320, 185, 2240, 1208), client_rect=(0, 0, width, height),
        client_screen_origin=(320, 185), monitor_rect=monitor, work_area_rect=monitor,
        dpi=dpi, window_state="restored",
        landmarks={"group_tree_header": [10, 100, 80, 120], "grid_header_cat": [540, 630, 561, 643],
                   "items_tab": [296, 78, 342, 98], "items_search": [508, 165, 826, 186],
                   "quick_entry_cat": [563, 459, 608, 477], "grid_header": [506, 628, 1894, 645],
                   "grid_row_1": [506, 654, 1894, 671]},
        geometry={"group_row_text_top_offset": 23, "group_row_height": 20,
                  "group_row_pitch_state": "measured_confident", "group_click_x_offset": 79,
                  "group_click_y_offset": 8, "group_text_x_offset": 35, "group_text_width": 245,
                  "group_row_crop_margin_top": 3, "group_row_crop_height": 18,
                  "selection_min_overlap_run": 32, "group_context_menu_new_index": 15,
                  "group_tree_scroll_point": [110, 140], "grid_to_quick_cat": [23, -171, 47, -166]},
        confidence={}, validation_state="valid", unresolved=(),
    )


class _User32:
    def __init__(self, dpi): self.dpi = dpi
    def GetDpiForWindow(self, _hwnd): return self.dpi


class _Ctypes:
    def __init__(self, dpi): self.windll = type("Windll", (), {"user32": _User32(dpi)})()


class FakeAdapter:
    def __init__(self, *, width=1920, height=1023, dpi=96, missing=None, moved=0):
        self.width, self.height, self.dpi, self.missing, self.moved = width, height, dpi, missing, moved
    def _find_main_window(self): return (1, "TEST")
    def _win32gui(self):
        size = (0, 0, self.width, self.height)
        return type("G", (), {"GetClientRect": staticmethod(lambda _h: size)})
    def _win32(self): return _Ctypes(self.dpi), None
    def _capture_client_image(self, _h): return object()
    def _locate_group_tree_header(self, _i): return None if self.missing == "group" else (10 + self.moved, 100, 80, 120)
    def _locate_label(self, _i, _s, prefer=None): return None if self.missing == "grid" else (540 + self.moved, 630, 561, 643)
    def _locate_items_tab(self, _i): return None if self.missing == "items" else (296 + self.moved, 78, 342, 98)
    def _items_search_pane_field(self, _i): return None if self.missing == "search" else (508, 165, 826, 186)


@pytest.mark.parametrize("candidate", [profile(), profile(width=1900, height=970, monitor=(0, 0, 1920, 1080)),
                                        profile(monitor=(-1920, 0, 0, 1080))])
def test_profiles_round_trip_for_current_laptop_and_negative_origin(tmp_path, candidate):
    save_calibration(candidate, tmp_path)
    assert load_calibration(tmp_path) == candidate


def test_window_moved_with_same_layout_still_validates():
    # Client-relative landmarks do not move when the outer window moves.
    assert validate_calibration(FakeAdapter(), profile())["ok"]


def test_resized_window_fails_closed():
    result = validate_calibration(FakeAdapter(width=1600), profile())
    assert not result["ok"] and "client size mismatch" in result["reasons"]


def test_dpi_mismatch_fails_closed():
    result = validate_calibration(FakeAdapter(dpi=120), profile())
    assert not result["ok"] and "DPI mismatch" in result["reasons"]


@pytest.mark.parametrize("missing,reason", [("group", "missing landmark: group_tree_header"),
                                             ("grid", "missing landmark: grid_header_cat"),
                                             ("items", "missing landmark: items_tab"),
                                             ("search", "active Items/Search context missing")])
def test_missing_core_landmark_fails_closed(missing, reason):
    result = validate_calibration(FakeAdapter(missing=missing), profile())
    assert not result["ok"] and reason in result["reasons"]


def test_landmark_layout_shift_fails_closed():
    result = validate_calibration(FakeAdapter(moved=20), profile())
    assert not result["ok"] and any(reason.startswith("landmark moved") for reason in result["reasons"])


def test_fast_geometry_is_instance_only_and_uses_profile_values():
    adapter = type("Adapter", (), {})()
    candidate = profile()
    candidate.geometry["group_row_height"] = 24
    apply_fast_geometry(adapter, candidate)
    assert adapter._GROUP_TREE_ROW_HEIGHT == 24


def test_group_row_pitch_is_measured_from_current_ocr_baselines():
    result = measure_group_row_pitch(_ocr(("TEST", 23), ("Roof", 43), ("Siding", 63), ("Fence", 83)), region_width=160)
    assert result["chosen_pitch"] == 20
    assert result["confidence_state"] == "measured_confident"


def _ocr(*entries):
    data = {key: [] for key in ("text", "conf", "left", "top", "width", "height")}
    for entry in entries:
        text, top, *rest = entry
        left = rest[0] if rest else 20
        width = rest[1] if len(rest) > 1 else max(12, len(text) * 6)
        height = rest[2] if len(rest) > 2 else 10
        conf = rest[3] if len(rest) > 3 else 90
        for key, value in zip(data, (text, conf, left, top, width, height), strict=True): data[key].append(value)
    return data


def test_unrelated_nearby_words_cannot_create_false_27_pitch():
    # Exact live failure shape: actual root row plus right-pane labels at
    # 337/364/391. Their x positions lie outside the Group name column.
    data = _ocr(("Test", 22, 23, 43, 14, 0), ("Desc", 337, 240),
                ("Calc", 364, 239), ("Cov", 391, 240), ("_I", 36, 0, 20, 1, 45))
    result = measure_group_row_pitch(data, region_width=163, header_bottom=10)
    assert result["chosen_pitch"] is None
    assert result["confidence_state"] == "unresolved"
    assert all(not box["accepted"] for box in result["candidate_ocr_boxes"] if box["text"] in {"Desc", "Calc", "Cov", "_I"})


def test_root_glyph_only_three_pixels_below_header_is_a_row_candidate():
    result = measure_group_row_pitch(
        _ocr(("Group", 0, 0, 30, 10), ("Test", 13, 39, 27, 28), ("ap", 22, 1, 27, 14)),
        region_width=182, header_bottom=10,
    )
    accepted = {box["text"] for box in result["candidate_ocr_boxes"] if box["accepted"]}
    assert "Group" not in accepted
    assert "Test" in accepted


def test_duplicate_fragments_cluster_to_one_physical_row():
    data = _ocr(("Main", 23), ("Roof", 24, 55), ("Side", 43), ("Group", 44, 55),
                ("Fence", 63), ("Rear", 83))
    result = measure_group_row_pitch(data, region_width=160)
    assert len(result["accepted_row_centers"]) == 4
    assert result["chosen_pitch"] == pytest.approx(20, abs=0.5)


def test_one_outlier_baseline_is_rejected_without_changing_pitch():
    result = measure_group_row_pitch(
        _ocr(("Aaa", 23), ("Bbb", 43), ("Noise", 50), ("Ccc", 63), ("Ddd", 83)), region_width=160,
    )
    assert result["chosen_pitch"] == pytest.approx(20)
    assert result["confidence_state"] == "measured_confident"


@pytest.mark.parametrize("data", [_ocr(), _ocr(("TEST", 23))])
def test_empty_or_sparse_tree_is_unresolved(data):
    result = measure_group_row_pitch(data, region_width=160)
    assert result["chosen_pitch"] is None
    assert result["confidence_state"] == "unresolved"


@pytest.mark.parametrize("pitch", [18, 24, 31])
def test_legitimate_different_pitch_is_measured_not_hard_coded(pitch):
    result = measure_group_row_pitch(
        _ocr(("One", 30), ("Two", 30 + pitch), ("Three", 30 + pitch * 2)), region_width=180,
    )
    assert result["chosen_pitch"] == pitch
    assert result["confidence_state"] == "measured_confident"


def test_1080p_region_geometry_does_not_affect_relative_pitch():
    result = measure_group_row_pitch(
        _ocr(("One", 18), ("Two", 40), ("Three", 62), ("Four", 84)), region_width=135,
    )
    assert result["chosen_pitch"] == 22


def test_single_spacing_is_low_confidence_and_fast_geometry_fails_closed():
    result = measure_group_row_pitch(_ocr(("One", 23), ("Two", 43)), region_width=160)
    assert result["confidence_state"] == "measured_low_confidence"
    candidate = profile()
    candidate.geometry["group_row_pitch_state"] = "measured_low_confidence"
    candidate.geometry["group_row_height"] = 20
    with pytest.raises(RuntimeError, match="Group-row geometry requires calibration"):
        apply_fast_geometry(type("Adapter", (), {})(), candidate)
    validation = validate_calibration(FakeAdapter(), candidate)
    assert "Group-row geometry requires calibration" in validation["reasons"]


def _inventory(*rows):
    return {"header": [10, 10, 40, 20], "subtotal_header": [180, 10, 220, 20],
            "source_region": [10, 10, 180, 300], "ocr_diagnostics": {},
            "rows": [{"raw_text": name, "normalized_text": "".join(ch for ch in name.casefold() if ch.isalnum()),
                      "center_y": center, "relative_center_y": center - 10, "top": center - 5}
                     for name, center in rows]}


def test_three_known_calibration_rows_produce_confident_pitch():
    result = confident_known_group_measurement(_inventory(
        ("CAL_ROW_ALPHA", 100), ("CAL_ROW_BRAVO", 122), ("CAL_ROW_CHARLIE", 144),
    ))
    assert result["measured_spacings"] == [22.0, 22.0]
    assert result["chosen_pitch"] == 22
    assert result["confidence_state"] == "measured_confident"


def test_known_calibration_rows_with_inconsistent_spacing_fail_closed():
    result = confident_known_group_measurement(_inventory(
        ("CAL_ROW_ALPHA", 100), ("CAL_ROW_BRAVO", 120), ("CAL_ROW_CHARLIE", 147),
    ))
    assert result["confidence_state"] == "measured_low_confidence"
    assert result["chosen_pitch"] is None


class _Image:
    def save(self, path): path.write_bytes(b"png")


class _InteractiveAdapter:
    def __init__(self): self.events = []
    def _unexpected_dialog_present(self): return False
    def _find_dropdown_window(self): return None
    def _ensure_main_window(self): return 1
    def _capture_client_image(self, hwnd): self.events.append(("capture", hwnd)); return _Image()


@pytest.mark.parametrize("state", ["unresolved", "measured_low_confidence"])
def test_incomplete_empty_one_or_two_row_profile_triggers_interactive_calibration(monkeypatch, tmp_path, state):
    candidate = profile(); candidate.geometry["group_row_pitch_state"] = state
    candidate.geometry["group_row_height"] = None
    before = _inventory(("TEST", 50))
    after = _inventory(("TEST", 50), ("CAL_ROW_ALPHA", 70),
                       ("CAL_ROW_BRAVO", 90), ("CAL_ROW_CHARLIE", 110))
    inventories = iter((before, after))
    monkeypatch.setattr(calibration, "_group_column_inventory", lambda *_: next(inventories))
    adapter = _InteractiveAdapter(); created = []
    completed, detail = complete_interactive_group_row_calibration(
        adapter, candidate, directory=tmp_path,
        evidence_dir=tmp_path / "evidence",
        group_creator=lambda name: created.append(name) or {"name": name, "state": "created"},
    )
    assert created == list(CALIBRATION_GROUP_NAMES)
    assert completed.geometry["group_row_pitch_state"] == "measured_confident"
    assert completed.geometry["group_row_height"] == 20
    assert detail["cleanup"].startswith("not_attempted")
    assert load_calibration(tmp_path).geometry["group_row_pitch_state"] == "measured_confident"
    assert all(event[0] == "capture" for event in adapter.events)  # no item population API was called


def test_calibration_name_already_present_refuses_before_creation(monkeypatch, tmp_path):
    candidate = profile(); candidate.geometry["group_row_pitch_state"] = "unresolved"
    monkeypatch.setattr(calibration, "_group_column_inventory", lambda *_: _inventory(
        ("TEST", 50), ("CAL_ROW_ALPHA", 70),
    ))
    created = []
    with pytest.raises(RuntimeError, match="already exists"):
        complete_interactive_group_row_calibration(
            _InteractiveAdapter(), candidate, directory=tmp_path,
            group_creator=lambda name: created.append(name),
        )
    assert created == []


def test_interactive_inconsistent_spacing_persists_failure_and_refuses_execution(monkeypatch, tmp_path):
    candidate = profile(); candidate.geometry["group_row_pitch_state"] = "unresolved"
    inventories = iter((_inventory(("TEST", 50)), _inventory(
        ("TEST", 50), ("CAL_ROW_ALPHA", 70), ("CAL_ROW_BRAVO", 90), ("CAL_ROW_CHARLIE", 117),
    )))
    monkeypatch.setattr(calibration, "_group_column_inventory", lambda *_: next(inventories))
    with pytest.raises(RuntimeError, match="spacings disagree"):
        complete_interactive_group_row_calibration(
            _InteractiveAdapter(), candidate, directory=tmp_path,
            group_creator=lambda name: {"name": name},
        )
    saved = load_calibration(tmp_path)
    assert saved.geometry["group_row_pitch_state"] == "measured_low_confidence"
    with pytest.raises(RuntimeError, match="Group-row geometry requires calibration"):
        apply_fast_geometry(type("Adapter", (), {})(), saved)
