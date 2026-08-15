from __future__ import annotations

from dataclasses import replace

import pytest

from estimate_extractor.xactimate_lookup import xactimate_calibration as calibration
from estimate_extractor.xactimate_lookup.xactimate_calibration import (
    CALIBRATION_GROUP_NAMES, SCHEMA_VERSION, XactimateCalibration, apply_fast_geometry,
    calibrate_xactimate, complete_interactive_group_row_calibration, confident_known_group_measurement,
    load_calibration, machine_identifier, measure_group_row_pitch, profile_path, save_calibration,
    recover_existing_group_row_calibration, validate_calibration,
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
    def _scroll_group_tree_to_top(self, _hwnd): pass


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
    width, height = 1920, 1023
    def save(self, path): path.write_bytes(b"png")


class _InteractiveAdapter:
    def __init__(self): self.events = []; self.scrolled = False
    def _unexpected_dialog_present(self): return False
    def _find_dropdown_window(self): return None
    def _ensure_main_window(self): return 1
    def _scroll_group_tree_to_top(self, hwnd): self.events.append(("scroll", hwnd)); self.scrolled = True
    def _capture_client_image(self, hwnd): self.events.append(("capture", hwnd)); return _Image()
    def _locate_label(self, _image, text, prefer=None): return (540, 630, 561, 643)
    def _locate_items_tab(self, _image): return (296, 78, 342, 98)
    def _items_search_pane_field(self, _image): return (508, 165, 826, 186)
    def _shifted_anchor(self, name, offset):
        anchors = {"grid_header_cat_label": (540, 630, 561, 643),
                   "quick_entry_cat_value": (563, 459, 608, 477),
                   "grid_header": (506, 628, 1894, 645), "grid_row_1": (506, 654, 1894, 671)}
        l, t, r, b = anchors[name]; dx, dy = offset
        return l + dx, t + dy, r + dx, b + dy


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


def test_bounded_subtotal_prefix_recovers_visible_misread_header():
    class Image:
        width, height = 900, 700
        def crop(self, _rect): return self
    class Adapter:
        def _locate_group_tree_header(self, _image): return (270, 111, 301, 121)
        def _locate_label(self, _image, _text, prefer=None): return None
        def _ocr_data(self, _crop, config=None):
            return _ocr(("Group", 10, 20, 32, 10), ("subtot!", 10, 212, 34, 8),
                        ("unrelated", 200, 250, 50, 9))
    group, subtotal, method = calibration._locate_group_column_headers(Adapter(), Image())
    assert group == (270, 111, 301, 121)
    assert subtotal[0] == 462
    assert method == "bounded_header_prefix_ocr"


def test_read_only_existing_rows_recovery_makes_profile_executable_without_creation(monkeypatch, tmp_path):
    candidate = profile(); candidate.geometry["group_row_pitch_state"] = "unresolved"
    candidate.geometry["group_row_height"] = None
    inventory = _inventory(("TEST", 100), ("CAL_ROW_ALPHA", 120),
                           ("CAL_ROW_BRAVO", 140), ("CAL_ROW_CHARLIE", 160))
    inventory["boundary_method"] = "bounded_header_prefix_ocr"
    monkeypatch.setattr(calibration, "_group_column_inventory", lambda *_: inventory)
    adapter = _InteractiveAdapter()
    recovered, detail = recover_existing_group_row_calibration(
        adapter, candidate, directory=tmp_path, evidence_dir=tmp_path / "evidence",
    )
    assert detail["creation"] == []
    assert detail["detected_row_centers"] == {
        "CAL_ROW_ALPHA": 120.0, "CAL_ROW_BRAVO": 140.0, "CAL_ROW_CHARLIE": 160.0,
    }
    assert recovered.geometry["group_row_height"] == 20
    assert recovered.geometry["group_row_pitch_state"] == "measured_confident"
    apply_fast_geometry(type("Adapter", (), {})(), recovered)


def test_complete_existing_calibration_set_routes_to_read_only_recovery(monkeypatch, tmp_path):
    candidate = profile(); candidate.geometry["group_row_pitch_state"] = "unresolved"
    inventory = _inventory(("TEST", 100), ("CAL_ROW_ALPHA", 120),
                           ("CAL_ROW_BRAVO", 140), ("CAL_ROW_CHARLIE", 160))
    monkeypatch.setattr(calibration, "_group_column_inventory", lambda *_: inventory)
    calls = []
    monkeypatch.setattr(calibration, "recover_existing_group_row_calibration",
                        lambda *args, **kwargs: (candidate, calls.append("recover") or {"mode": "recovery"}))
    monkeypatch.setattr(calibration, "complete_interactive_group_row_calibration",
                        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not create")))
    calibration._complete_or_recover_group_rows(
        _InteractiveAdapter(), candidate, directory=tmp_path, evidence_dir=tmp_path,
    )
    assert calls == ["recover"]


def test_partial_existing_calibration_set_refuses_without_creation(monkeypatch, tmp_path):
    candidate = profile(); candidate.geometry["group_row_pitch_state"] = "unresolved"
    monkeypatch.setattr(calibration, "_group_column_inventory", lambda *_: _inventory(
        ("TEST", 100), ("CAL_ROW_ALPHA", 120), ("CAL_ROW_BRAVO", 140),
    ))
    with pytest.raises(RuntimeError, match="partial calibration group set"):
        calibration._complete_or_recover_group_rows(
            _InteractiveAdapter(), candidate, directory=tmp_path, evidence_dir=tmp_path,
        )


def test_scrolled_away_calibration_rows_become_visible_after_scroll_and_route_to_recovery(monkeypatch, tmp_path):
    # Simulates a busy project where the group tree had scrolled the three
    # calibration rows out of the captured client area: before the scroll,
    # inventory reads only see the root row; after _scroll_group_tree_to_top
    # runs, all three become visible and this must route to read-only
    # recovery, never to (redundant, failure-prone) creation.
    candidate = profile(); candidate.geometry["group_row_pitch_state"] = "unresolved"
    scrolled_away = _inventory(("TEST", 100))
    visible = _inventory(("TEST", 100), ("CAL_ROW_ALPHA", 120),
                         ("CAL_ROW_BRAVO", 140), ("CAL_ROW_CHARLIE", 160))
    def fake_inventory(adapter, _image):
        return visible if adapter.scrolled else scrolled_away
    monkeypatch.setattr(calibration, "_group_column_inventory", fake_inventory)
    monkeypatch.setattr(calibration, "complete_interactive_group_row_calibration",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not create")))
    adapter = _InteractiveAdapter()
    recovered, detail = calibration._complete_or_recover_group_rows(
        adapter, candidate, directory=tmp_path, evidence_dir=tmp_path,
    )
    assert detail["creation"] == []
    assert recovered.geometry["group_row_pitch_state"] == "measured_confident"
    assert adapter.events[0] == ("scroll", 1)


def test_direct_recovery_scrolls_before_inventory_and_root_lookup(monkeypatch, tmp_path):
    candidate = profile(); candidate.geometry["group_row_pitch_state"] = "unresolved"
    inventory = _inventory(("TEST", 100), ("CAL_ROW_ALPHA", 120),
                           ("CAL_ROW_BRAVO", 140), ("CAL_ROW_CHARLIE", 160))
    def fake_inventory(adapter, _image):
        assert adapter.scrolled, "inventory/root lookup must happen after scroll-to-top"
        return inventory
    monkeypatch.setattr(calibration, "_group_column_inventory", fake_inventory)
    adapter = _InteractiveAdapter()
    recovered, detail = recover_existing_group_row_calibration(
        adapter, candidate, directory=tmp_path, evidence_dir=tmp_path,
    )
    assert adapter.events[0] == ("scroll", 1)
    assert recovered.geometry["group_row_pitch_state"] == "measured_confident"


def test_failed_recalibration_restores_exact_previous_profile_bytes(tmp_path):
    path = tmp_path / "profile.json"
    path.write_bytes(b'{"known":"good"}\n')
    def fail():
        path.write_bytes(b'{"state":"unresolved"}')
        raise RuntimeError("calibration failed")
    with pytest.raises(RuntimeError, match="calibration failed"):
        calibration._profile_transaction(path, fail)
    assert path.read_bytes() == b'{"known":"good"}\n'


# -- calibrate_xactimate() end-to-end: brand-new-device contract -----------
#
# These simulate a live Xactimate window from scratch (no saved profile,
# real ctypes.Structure/sizeof/byref plumbing for the DPI/monitor lookup,
# fake-only at the Win32 DLL-call boundary) to prove calibrate_xactimate()
# never needs, and is never influenced by, a previously saved profile.

import ctypes as _real_ctypes
from ctypes import wintypes as _real_wintypes


class _FakeUser32ForCalibration:
    def __init__(self, dpi, monitor_rect, work_rect):
        self.dpi, self.monitor_rect, self.work_rect = dpi, monitor_rect, work_rect
    def GetDpiForWindow(self, _hwnd): return self.dpi
    def MonitorFromWindow(self, _hwnd, _flags): return 1
    def GetMonitorInfoW(self, _hmonitor, info_ref):
        info = info_ref._obj
        info.rcMonitor.left, info.rcMonitor.top, info.rcMonitor.right, info.rcMonitor.bottom = self.monitor_rect
        info.rcWork.left, info.rcWork.top, info.rcWork.right, info.rcWork.bottom = self.work_rect
        return 1
    def IsZoomed(self, _hwnd): return False
    def IsIconic(self, _hwnd): return False


class _FakeCtypesForCalibration:
    """Real ctypes.Structure/sizeof/byref; only the DLL call layer is fake."""
    Structure = _real_ctypes.Structure
    sizeof = staticmethod(_real_ctypes.sizeof)
    byref = staticmethod(_real_ctypes.byref)
    def __init__(self, user32): self.windll = type("Windll", (), {"user32": user32})()


class _FreshImage:
    def __init__(self, width=1920, height=1023): self.width, self.height = width, height
    def crop(self, _rect): return self
    def save(self, path): path.write_bytes(b"png")


class _FreshWin32Gui:
    def __init__(self, window_rect, client_rect): self._window_rect, self._client_rect = window_rect, client_rect
    def GetWindowRect(self, _hwnd): return self._window_rect
    def GetClientRect(self, _hwnd): return self._client_rect
    def GetClassName(self, _hwnd): return "Xactimate"


class _FreshCalibrationAdapter:
    """A live Xactimate window with fully controllable, fresh geometry --
    never backed by, or seeded from, any previously saved calibration."""

    _GROUP_TREE_CLICK_DX = 79
    _GROUP_TREE_CLICK_DY_OFFSET = 8
    _GROUP_TREE_TEXT_DX = 35
    _GROUP_TREE_TEXT_WIDTH = 245
    _GROUP_TREE_ROW_CROP_MARGIN_TOP = 3
    _GROUP_TREE_ROW_CROP_HEIGHT = 18
    _GROUP_TREE_SELECTION_MIN_OVERLAP_RUN = 32
    _GROUP_MENU_NEW_INDEX = 15

    def __init__(self, *, title="TEST", width=1920, height=1023, dpi=96,
                 monitor_rect=(0, 0, 1920, 1080), work_rect=(0, 0, 1920, 1040), tree_ocr=None):
        self.title = title
        self.width, self.height, self.dpi = width, height, dpi
        self._window_rect = (0, 0, width, height + 40)
        self._client_rect = (0, 0, width, height)
        self._ctypes_ns = _FakeCtypesForCalibration(_FakeUser32ForCalibration(dpi, monitor_rect, work_rect))
        self._tree_ocr = tree_ocr if tree_ocr is not None else _ocr(("Test", 22, 23, 43, 14, 0))
        self.events, self.scrolled = [], False

    def verify_application(self): return True
    def verify_project(self): return True
    def _unexpected_dialog_present(self): return False
    def _find_dropdown_window(self): return None
    def _find_main_window(self): return (1, self.title)
    def _ensure_main_window(self): return 1
    def _win32gui(self): return _FreshWin32Gui(self._window_rect, self._client_rect)
    def _get_client_origin(self, _hwnd): return (0, 40)
    def _win32(self): return self._ctypes_ns, _real_wintypes
    def _capture_client_image(self, _hwnd): return _FreshImage(self.width, self.height)
    def _locate_group_tree_header(self, _image): return (270, 155, 301, 165)
    def _locate_label(self, _image, text, prefer=None):
        if text == "Cat": return (540, 630, 561, 643)
        if text == "Subtotal": return (394, 155, 437, 163)
        raise AssertionError(f"unexpected label lookup: {text!r}")
    def _locate_items_tab(self, _image): return (296, 78, 342, 98)
    def _items_search_pane_field(self, _image): return (508, 165, 826, 186)
    def _tab_is_active(self, _image, _tab): return True
    def _shifted_anchor(self, name, offset):
        anchors = {"grid_header_cat_label": (540, 630, 561, 643),
                   "quick_entry_cat_value": (563, 459, 608, 477),
                   "grid_header": (506, 628, 1894, 645), "grid_row_1": (506, 654, 1894, 671)}
        l, t, r, b = anchors[name]; dx, dy = offset
        return l + dx, t + dy, r + dx, b + dy
    def _ocr_data(self, _crop, config=None): return self._tree_ocr
    def _scroll_group_tree_to_top(self, hwnd): self.events.append(("scroll", hwnd)); self.scrolled = True


def test_no_profile_empty_tree_full_calibration_can_proceed(monkeypatch, tmp_path):
    adapter = _FreshCalibrationAdapter(tree_ocr=_ocr(("Test", 22, 23, 43, 14, 0)))
    empty = _inventory(("TEST", 100))
    full = _inventory(("TEST", 100), ("CAL_ROW_ALPHA", 120), ("CAL_ROW_BRAVO", 140), ("CAL_ROW_CHARLIE", 160))
    inventories = iter((empty, empty, full))
    monkeypatch.setattr(calibration, "_group_column_inventory", lambda *_: next(inventories))
    created = []
    monkeypatch.setattr(calibration, "_create_one_calibration_group",
                        lambda _adapter, _profile, name: created.append(name) or {"name": name, "state": "created"})
    path = profile_path(tmp_path, machine_identifier())
    assert not path.exists()
    profile, returned_path = calibrate_xactimate(
        adapter, directory=tmp_path, allow_interactive_group_rows=True,
        evidence_dir=tmp_path / "evidence",
    )
    assert returned_path == path
    assert created == list(CALIBRATION_GROUP_NAMES)
    assert profile.geometry["group_row_pitch_state"] == "measured_confident"
    assert profile.validation_state == "ready_for_fast_execution"
    assert load_calibration(tmp_path).validation_state == "ready_for_fast_execution"


def test_no_profile_sparse_tree_creates_and_measures_temporary_groups(monkeypatch, tmp_path):
    # Two real rows: enough live content to be a "sparse" (not empty) tree,
    # but not enough physical rows for the generic pitch measurement to be
    # confident (needs >= 3 clusters) -- must still fall through to
    # temporary calibration-row creation.
    adapter = _FreshCalibrationAdapter(tree_ocr=_ocr(("Test", 22, 23, 43, 14, 0), ("Roof", 45, 23, 43, 14, 0)))
    sparse = _inventory(("TEST", 100), ("Roof", 123))
    full = _inventory(("TEST", 100), ("Roof", 123), ("CAL_ROW_ALPHA", 146),
                      ("CAL_ROW_BRAVO", 168), ("CAL_ROW_CHARLIE", 190))
    inventories = iter((sparse, sparse, full))
    monkeypatch.setattr(calibration, "_group_column_inventory", lambda *_: next(inventories))
    created = []
    monkeypatch.setattr(calibration, "_create_one_calibration_group",
                        lambda _adapter, _profile, name: created.append(name) or {"name": name, "state": "created"})
    profile, path = calibrate_xactimate(
        adapter, directory=tmp_path, allow_interactive_group_rows=True,
        evidence_dir=tmp_path / "evidence",
    )
    assert created == list(CALIBRATION_GROUP_NAMES)
    assert profile.geometry["group_row_pitch_state"] == "measured_confident"
    assert profile.validation_state == "ready_for_fast_execution"


def test_no_profile_existing_cal_row_trio_uses_read_only_recovery(monkeypatch, tmp_path):
    # calibrate_xactimate's own generic full-tree precheck sees only the
    # root row (unresolved) -- distinct from the CAL_ROW_*-specific
    # inventory read below, exactly as they are two independent OCR passes
    # in the real implementation. allow_interactive_group_rows=True (the
    # caller consents to creation IF NEEDED) but creation must never run
    # because all three calibration rows already exist.
    adapter = _FreshCalibrationAdapter(tree_ocr=_ocr(("Test", 22, 23, 43, 14, 0)))
    trio = _inventory(("TEST", 100), ("CAL_ROW_ALPHA", 120), ("CAL_ROW_BRAVO", 140), ("CAL_ROW_CHARLIE", 160))
    monkeypatch.setattr(calibration, "_group_column_inventory", lambda *_: trio)
    monkeypatch.setattr(calibration, "complete_interactive_group_row_calibration",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not create -- rows already exist")))
    path = profile_path(tmp_path, machine_identifier())
    assert not path.exists()
    profile, _path = calibrate_xactimate(
        adapter, directory=tmp_path, allow_interactive_group_rows=True,
        evidence_dir=tmp_path / "evidence",
    )
    assert profile.geometry["group_row_pitch_state"] == "measured_confident"
    assert profile.validation_state == "ready_for_fast_execution"
    assert adapter.events[0] == ("scroll", 1)  # scroll-to-top ran before the presence check


def test_stale_prior_profile_values_do_not_influence_new_measurements(tmp_path):
    stale = replace(profile(width=1600, height=900, dpi=120), validation_state="ready_for_fast_execution")
    save_calibration(stale, tmp_path)
    assert load_calibration(tmp_path).client_width == 1600  # sanity: stale really is on disk

    # Fresh live geometry disagrees with the stale profile on every axis
    # that must never be inherited: client size, DPI, and (via real-row
    # OCR) row pitch.
    fresh_tree_ocr = _ocr(("Test", 22), ("Roof", 43), ("Siding", 63), ("Fence", 83))
    adapter = _FreshCalibrationAdapter(width=1920, height=1023, dpi=96, tree_ocr=fresh_tree_ocr)
    profile_out, _path = calibrate_xactimate(
        adapter, directory=tmp_path, allow_interactive_group_rows=False,
    )
    assert (profile_out.client_width, profile_out.client_height) == (1920, 1023)
    assert profile_out.dpi == 96
    # measured live from the fresh 1920x1023 window, not the stale 1600x900 profile's stored value
    assert profile_out.geometry["group_row_height"] == pytest.approx(20, abs=0.5)
    assert profile_out.validation_state == "ready_for_fast_execution"
    assert load_calibration(tmp_path).client_width == 1920  # the stale profile was overwritten, not merged into


def test_successful_fresh_calibration_produces_a_complete_ready_profile(tmp_path):
    fresh_tree_ocr = _ocr(("Test", 22), ("Roof", 43), ("Siding", 63), ("Fence", 83))
    adapter = _FreshCalibrationAdapter(tree_ocr=fresh_tree_ocr)
    path = profile_path(tmp_path, machine_identifier())
    assert not path.exists()
    profile_out, returned_path = calibrate_xactimate(adapter, directory=tmp_path)
    assert returned_path == path and path.exists()
    assert profile_out.validation_state == "ready_for_fast_execution"
    assert profile_out.geometry["group_row_pitch_state"] == "measured_confident"
    assert profile_out.geometry["group_row_height"] is not None
    for landmark in ("group_tree_header", "grid_header_cat", "items_tab", "items_search"):
        assert profile_out.landmarks[landmark]
    reloaded = load_calibration(tmp_path)
    assert reloaded == profile_out


def test_failed_calibration_does_not_leave_a_false_ready_profile(monkeypatch, tmp_path):
    adapter = _FreshCalibrationAdapter(tree_ocr=_ocr(("Test", 22, 23, 43, 14, 0)))
    empty = _inventory(("TEST", 100))
    inconsistent = _inventory(("TEST", 100), ("CAL_ROW_ALPHA", 120), ("CAL_ROW_BRAVO", 140), ("CAL_ROW_CHARLIE", 167))
    inventories = iter((empty, empty, inconsistent))
    monkeypatch.setattr(calibration, "_group_column_inventory", lambda *_: next(inventories))
    monkeypatch.setattr(calibration, "_create_one_calibration_group",
                        lambda _adapter, _profile, name: {"name": name, "state": "created"})
    path = profile_path(tmp_path, machine_identifier())
    assert not path.exists()
    with pytest.raises(RuntimeError, match="spacings disagree"):
        calibrate_xactimate(
            adapter, directory=tmp_path, allow_interactive_group_rows=True,
            evidence_dir=tmp_path / "evidence",
        )
    assert not path.exists()  # no prior profile existed -- transaction rolls back to that, not a failed/false-ready one
    with pytest.raises(RuntimeError, match="No Xactimate calibration exists"):
        load_calibration(tmp_path)
