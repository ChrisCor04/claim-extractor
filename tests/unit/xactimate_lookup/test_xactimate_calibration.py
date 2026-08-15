from __future__ import annotations

from dataclasses import replace

import pytest

from estimate_extractor.xactimate_lookup.xactimate_calibration import (
    LAYOUT_ERROR, SCHEMA_VERSION, XactimateCalibration, apply_fast_geometry,
    load_calibration, machine_identifier, measured_group_rows, save_calibration, validate_calibration,
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
        geometry={"group_row_text_top_offset": 23, "group_row_height": 20, "group_click_x_offset": 79,
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
    data = {"text": ["TEST", "Roof", "Siding", "Fence"], "top": [23, 43, 63, 83], "conf": [90] * 4}
    assert measured_group_rows(data, 0) == (23, 20)
