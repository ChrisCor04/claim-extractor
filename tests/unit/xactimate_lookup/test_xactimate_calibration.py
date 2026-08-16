from __future__ import annotations

from dataclasses import replace

import pytest

from estimate_extractor.xactimate_lookup import xactimate_calibration as calibration
from estimate_extractor.xactimate_lookup.xactimate_calibration import (
    CALIBRATION_GROUP_NAMES, SCHEMA_VERSION, XactimateCalibration, apply_fast_geometry,
    calibrate_xactimate, complete_interactive_group_row_calibration, confident_known_group_measurement,
    describe_calibration_group_presence, load_calibration, machine_identifier, measure_group_row_pitch,
    profile_path, save_calibration, recover_existing_group_row_calibration, validate_calibration,
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


def _inventory_with_tops(*rows):
    """Like _inventory(), but each entry is (name, center_y, top) so a
    row's implied text-box height (2*(center_y-top)) can be controlled
    explicitly -- needed to simulate a geometry-corrupted row (e.g. a
    selected/highlighted row's OCR fragments reading back with an
    abnormally inflated bounding box) independently of its center."""
    return {"header": [10, 10, 40, 20], "subtotal_header": [180, 10, 220, 20],
            "source_region": [10, 10, 180, 300], "ocr_diagnostics": {},
            "rows": [{"raw_text": name, "normalized_text": "".join(ch for ch in name.casefold() if ch.isalnum()),
                      "center_y": center, "relative_center_y": center - 10, "top": top}
                     for name, center, top in rows]}


# -- geometry-corrupted sentinel row: independent center correction -----

def test_three_clean_evenly_spaced_rows_still_measure_confidently():
    result = confident_known_group_measurement(_inventory_with_tops(
        ("CAL_ROW_ALPHA", 100, 95), ("CAL_ROW_BRAVO", 122, 117), ("CAL_ROW_CHARLIE", 144, 139),
    ))
    assert result["confidence_state"] == "measured_confident"
    assert result["chosen_pitch"] == 22
    assert "geometry_correction" not in result


def test_selected_middle_row_inflated_height_does_not_cause_false_rejection():
    # BRAVO's own raw center (120) is close to, but not exactly, the
    # independently-interpolated position (122) -- exactly the kind of
    # small center-of-mass shift a selection-highlight-inflated bounding
    # box produces, proven live (CAL_ROW_CHARLIE-shaped case).
    result = confident_known_group_measurement(_inventory_with_tops(
        ("CAL_ROW_ALPHA", 100, 95), ("CAL_ROW_BRAVO", 120, 92), ("CAL_ROW_CHARLIE", 144, 139),
    ))
    assert result["confidence_state"] == "measured_confident"
    assert result["chosen_pitch"] == 22
    assert result["detected_row_centers"]["CAL_ROW_BRAVO"] == 122
    assert result["geometry_correction"]["corrected_row"] == "CAL_ROW_BRAVO"


def test_selected_first_row_inflated_height_does_not_cause_false_rejection():
    result = confident_known_group_measurement(_inventory_with_tops(
        ("CAL_ROW_ALPHA", 97, 69), ("CAL_ROW_BRAVO", 122, 117), ("CAL_ROW_CHARLIE", 144, 139),
    ))
    assert result["confidence_state"] == "measured_confident"
    assert result["chosen_pitch"] == 22
    assert result["detected_row_centers"]["CAL_ROW_ALPHA"] == 100
    assert result["geometry_correction"]["corrected_row"] == "CAL_ROW_ALPHA"


def test_selected_last_row_inflated_height_does_not_cause_false_rejection():
    result = confident_known_group_measurement(_inventory_with_tops(
        ("CAL_ROW_ALPHA", 100, 95), ("CAL_ROW_BRAVO", 122, 117), ("CAL_ROW_CHARLIE", 147, 119),
    ))
    assert result["confidence_state"] == "measured_confident"
    assert result["chosen_pitch"] == 22
    assert result["detected_row_centers"]["CAL_ROW_CHARLIE"] == 144
    assert result["geometry_correction"]["corrected_row"] == "CAL_ROW_CHARLIE"


def test_genuinely_uneven_spacing_with_normal_heights_still_fails():
    # All three rows have ordinary, mutually-consistent heights -- the
    # new correction path must never engage, and genuine bad spacing
    # among cleanly-measured rows must still fail exactly as before.
    result = confident_known_group_measurement(_inventory_with_tops(
        ("CAL_ROW_ALPHA", 100, 95), ("CAL_ROW_BRAVO", 120, 115), ("CAL_ROW_CHARLIE", 147, 142),
    ))
    assert result["confidence_state"] == "measured_low_confidence"
    assert result["chosen_pitch"] is None
    assert "geometry_correction" not in result


def test_corrupted_geometry_that_cannot_be_reconciled_fails_closed():
    # BRAVO's box height is an outlier (same signature as the live-caught
    # case), but its raw center is far from where the other two rows'
    # own consistent spacing says it should be -- too far to trust as
    # corroborating evidence, so the correction must be refused and the
    # existing tolerance check must still (correctly) reject.
    result = confident_known_group_measurement(_inventory_with_tops(
        ("CAL_ROW_ALPHA", 100, 95), ("CAL_ROW_BRAVO", 105, 62), ("CAL_ROW_CHARLIE", 144, 139),
    ))
    assert result["confidence_state"] == "measured_low_confidence"
    assert result["chosen_pitch"] is None
    assert "geometry_correction" not in result


def test_two_geometry_outliers_are_not_corrected():
    # With two of the three rows showing outlier heights, there is no
    # longer a reliable pair of clean rows to interpolate/extrapolate
    # from -- must not attempt a correction at all, and must fail closed
    # exactly as the uncorrected measurement would.
    result = confident_known_group_measurement(_inventory_with_tops(
        ("CAL_ROW_ALPHA", 97, 69), ("CAL_ROW_BRAVO", 110, 82), ("CAL_ROW_CHARLIE", 144, 139),
    ))
    assert "geometry_correction" not in result
    assert result["confidence_state"] == "measured_low_confidence"
    assert result["detected_row_centers"]["CAL_ROW_ALPHA"] == 97  # uncorrected -- no correction was attempted


def test_sentinel_identity_stays_independent_of_geometry_correction():
    # Combines the proven-live fuzzy-OCR-identity case (CHARLIE read as
    # "CHARLE)") with an independently geometry-corrupted BRAVO row --
    # both mechanisms must work correctly together without interfering.
    result = confident_known_group_measurement(_inventory_with_tops(
        ("CAL_ROW_ALPHA", 100, 95), ("Cal_ROW_BRAVO", 120, 92), ("Cal_ROW_CHARLE)", 144, 139),
    ))
    assert result["confidence_state"] == "measured_confident"
    assert result["chosen_pitch"] == 22
    assert set(result["detected_row_centers"]) == {"CAL_ROW_ALPHA", "CAL_ROW_BRAVO", "CAL_ROW_CHARLIE"}


def test_geometry_correction_tolerance_is_unchanged():
    assert calibration.GROUP_ROW_SPACING_TOLERANCE_PX == 2.0


def test_geometry_correction_introduces_no_fixed_pitch_and_scales_with_the_live_frame():
    # Same relative structure (one selected/inflated middle row, ~10%
    # off its interpolated center) reproduced at two unrelated absolute
    # scales -- the corrected pitch must come out proportional to each
    # scale, proving nothing is hardcoded to any one pixel/DPI regime.
    small = confident_known_group_measurement(_inventory_with_tops(
        ("CAL_ROW_ALPHA", 100, 95), ("CAL_ROW_BRAVO", 120, 92), ("CAL_ROW_CHARLIE", 144, 139),
    ))
    large = confident_known_group_measurement(_inventory_with_tops(
        ("CAL_ROW_ALPHA", 1000, 950), ("CAL_ROW_BRAVO", 1200, 920), ("CAL_ROW_CHARLIE", 1440, 1390),
    ))
    assert small["confidence_state"] == large["confidence_state"] == "measured_confident"
    assert small["chosen_pitch"] == 22
    assert large["chosen_pitch"] == 220
    assert small["chosen_pitch"] != large["chosen_pitch"]


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


class _HeaderFrameImage:
    width, height = 1920, 1023
    def crop(self, _rect): return self


class _HeaderFrameAdapter:
    """Replays the exact OCR shapes captured from a real, persisted, failed
    live calibration frame (audit_frame.png / diag2_frame_after_scroll.png):
    a live Xactimate Grouping panel where the Group column renders wide
    enough that "Subtotal" is truncated down to an unreadable "Sul"
    fragment. Both persisted frames produced byte-identical OCR boxes.
    _locate_group_column_headers()'s bounded fallback always OCRs with
    config="--psm 6"; _group_column_inventory()'s row read never passes a
    config -- used here to serve each its own real, correctly-relative
    captured data, exactly as the two real (differently cropped) OCR
    passes would."""

    def __init__(self, header_boxes, row_boxes=None):
        self._header_boxes = header_boxes
        self._row_boxes = row_boxes if row_boxes is not None else header_boxes
    def _locate_group_tree_header(self, _image): return (270, 155, 302, 165)
    def _locate_label(self, _image, _text, prefer=None): return None  # exact "Subtotal" is not legible live
    def _ocr_data(self, _crop, config=None):
        return self._header_boxes if config == "--psm 6" else self._row_boxes


#: (text, top, left, width, height, conf) tuples, crop-relative to the
#: (250,145)-(570,190) fallback crop -- the real OCR read from the
#: persisted failure frames. "sul" is the truncated "Subtotal" remnant;
#: "ome"/"sr" are unrelated right-pane breadcrumb bleed-through one row
#: above the header (a different y-band).
_REAL_TRUNCATED_SUBTOTAL_HEADER_BOXES = _ocr(
    ("ome", 0, 262, 31, 9, 26), ("sr", 0, 301, 19, 9, 39),
    ("Group", 10, 20, 32, 10, 87), ("sul", 10, 232, 14, 8, 94),
)

#: The same real frame's row-region OCR (relative to the Group-column
#: region (270,155)-(482,1023) that the new boundary establishes): the
#: single real root row, "Test".
_REAL_ROOT_ONLY_ROW_BOXES = _ocr(("Test", 23, 45, 11, 8, 73))


def test_truncated_subtotal_header_from_real_failed_frame_now_succeeds():
    # 2: the exact captured failure shape ("Subtotal header fallback
    # found 0 bounded candidate(s)") must now resolve, using the
    # truncated fragment's position only -- not its (illegible) text.
    adapter = _HeaderFrameAdapter(_REAL_TRUNCATED_SUBTOTAL_HEADER_BOXES)
    group, boundary, method = calibration._locate_group_column_headers(adapter, _HeaderFrameImage())
    assert group == (270, 155, 302, 165)
    assert boundary == (482, 155, 496, 163)  # the real "sul" fragment's own position
    assert method == "bounded_header_row_boundary"


def test_truncated_subtotal_boundary_keeps_rows_cropped_to_group_column():
    # 6 & 7: end to end through _group_column_inventory -- rows stay
    # bounded to the Group column (never absorb Subtotal-column content),
    # and a root-only live tree still correctly yields just "TEST".
    adapter = _HeaderFrameAdapter(_REAL_TRUNCATED_SUBTOTAL_HEADER_BOXES, _REAL_ROOT_ONLY_ROW_BOXES)
    inventory = calibration._group_column_inventory(adapter, _HeaderFrameImage())
    assert inventory["boundary_method"] == "bounded_header_row_boundary"
    assert inventory["source_region"][2] == 482  # right-bounded to the sul fragment's own left edge
    assert [row["raw_text"] for row in inventory["rows"]] == ["Test"]


def test_no_legible_subtotal_and_no_other_header_row_content_fails_closed():
    # 3: with genuinely nothing else in the header row to anchor a
    # boundary to (only out-of-band noise), calibration must still refuse
    # rather than guess -- exactly the original "0 bounded candidate(s)"
    # fail-closed behavior, now proven to still exist above the new tier.
    class Adapter:
        def _locate_group_tree_header(self, _image): return (270, 155, 302, 165)
        def _locate_label(self, _image, _text, prefer=None): return None
        def _ocr_data(self, _crop, config=None):
            return _ocr(("Group", 10, 20, 32, 10))  # nothing at all to the right
    with pytest.raises(RuntimeError, match="0 bounded candidate"):
        calibration._locate_group_column_headers(Adapter(), _HeaderFrameImage())


def test_wrong_row_band_noise_never_becomes_a_false_boundary():
    # 4: unrelated text that sits to the right of Group but in a
    # DIFFERENT header row/y-band (like the real "ome"/"sr" breadcrumb
    # bleed-through, one row above the actual header) must never be
    # mistaken for the column boundary.
    class Adapter:
        def _locate_group_tree_header(self, _image): return (270, 155, 302, 165)
        def _locate_label(self, _image, _text, prefer=None): return None
        def _ocr_data(self, _crop, config=None):
            return _ocr(("Group", 10, 20, 32, 10), ("ome", 0, 262, 31, 9), ("sr", 0, 301, 19, 9))
    with pytest.raises(RuntimeError, match="0 bounded candidate"):
        calibration._locate_group_column_headers(Adapter(), _HeaderFrameImage())


def test_multiple_ambiguous_subtotal_prefix_matches_fail_closed():
    # 5: two independently legible "subtot..." candidates -- genuinely
    # ambiguous, must refuse rather than pick either one.
    class Image:
        width, height = 900, 700
        def crop(self, _rect): return self
    class Adapter:
        def _locate_group_tree_header(self, _image): return (270, 111, 301, 121)
        def _locate_label(self, _image, _text, prefer=None): return None
        def _ocr_data(self, _crop, config=None):
            return _ocr(("Group", 10, 20, 32, 10), ("subtot!", 10, 212, 34, 8), ("subtotal", 10, 260, 40, 8))
    with pytest.raises(RuntimeError, match="2 bounded candidate"):
        calibration._locate_group_column_headers(Adapter(), Image())


def test_exact_subtotal_label_still_preferred_when_legible():
    # 1: the normal, fully-legible "Group / Subtotal / # Items" header
    # (unaffected by any of the above) still resolves via the strongest
    # tier, unchanged.
    class Adapter:
        def _locate_group_tree_header(self, _image): return (270, 155, 301, 165)
        def _locate_label(self, _image, text, prefer=None):
            return (394, 155, 437, 163) if text == "Subtotal" else None
        def _ocr_data(self, _crop, config=None):
            raise AssertionError("must not fall back when the exact label is found")
    group, subtotal, method = calibration._locate_group_column_headers(Adapter(), _HeaderFrameImage())
    assert group == (270, 155, 301, 165)
    assert subtotal == (394, 155, 437, 163)
    assert method == "exact_label"


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


@pytest.mark.parametrize("allow_creation", [False, True])
def test_complete_existing_calibration_set_routes_to_read_only_recovery(monkeypatch, tmp_path, allow_creation):
    # Recovery is read-only and must run the same way whether creation
    # permission (the checkbox) is off or on -- it is never a prerequisite
    # for recovering rows that already, positively, all exist.
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
        _InteractiveAdapter(), candidate, directory=tmp_path, evidence_dir=tmp_path, allow_creation=allow_creation,
    )
    assert calls == ["recover"]


@pytest.mark.parametrize("allow_creation", [False, True])
def test_partial_existing_calibration_set_refuses_without_creation(monkeypatch, tmp_path, allow_creation):
    # Fails closed the same way regardless of creation permission -- a
    # partial set is never safe to complete automatically either way.
    candidate = profile(); candidate.geometry["group_row_pitch_state"] = "unresolved"
    monkeypatch.setattr(calibration, "_group_column_inventory", lambda *_: _inventory(
        ("TEST", 100), ("CAL_ROW_ALPHA", 120), ("CAL_ROW_BRAVO", 140),
    ))
    monkeypatch.setattr(calibration, "complete_interactive_group_row_calibration",
                        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not create")))
    with pytest.raises(RuntimeError, match="partial calibration group set") as excinfo:
        calibration._complete_or_recover_group_rows(
            _InteractiveAdapter(), candidate, directory=tmp_path, evidence_dir=tmp_path, allow_creation=allow_creation,
        )
    assert "present=['CAL_ROW_ALPHA', 'CAL_ROW_BRAVO']" in str(excinfo.value)
    assert "missing=['CAL_ROW_CHARLIE']" in str(excinfo.value)


def test_zero_of_three_without_creation_permission_reports_actionable_status(monkeypatch, tmp_path):
    candidate = profile(); candidate.geometry["group_row_pitch_state"] = "unresolved"
    inventory_reads = []
    def fake_inventory(adapter, _image):
        inventory_reads.append(adapter.scrolled)
        return _inventory(("TEST", 100))
    monkeypatch.setattr(calibration, "_group_column_inventory", fake_inventory)
    monkeypatch.setattr(calibration, "complete_interactive_group_row_calibration",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not create")))
    adapter = _InteractiveAdapter()
    with pytest.raises(RuntimeError, match="enable temporary calibration-group creation"):
        calibration._complete_or_recover_group_rows(
            adapter, candidate, directory=tmp_path, evidence_dir=tmp_path, allow_creation=False,
        )
    # the presence check ran (and scrolled first) before the permission refusal
    assert inventory_reads == [True]
    assert adapter.events[0] == ("scroll", 1)


def test_zero_of_three_with_creation_permission_selects_bootstrap_creation(monkeypatch, tmp_path):
    candidate = profile(); candidate.geometry["group_row_pitch_state"] = "unresolved"
    inventory_reads = []
    def fake_inventory(adapter, _image):
        inventory_reads.append(adapter.scrolled)
        return _inventory(("TEST", 100))
    monkeypatch.setattr(calibration, "_group_column_inventory", fake_inventory)
    calls = []
    monkeypatch.setattr(calibration, "complete_interactive_group_row_calibration",
                        lambda *args, **kwargs: (candidate, calls.append("create") or {"mode": "create"}))
    monkeypatch.setattr(calibration, "recover_existing_group_row_calibration",
                        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not recover -- nothing exists")))
    adapter = _InteractiveAdapter()
    calibration._complete_or_recover_group_rows(
        adapter, candidate, directory=tmp_path, evidence_dir=tmp_path, allow_creation=True,
    )
    # inventory (proving exact 0/3) happened, and happened before creation was selected
    assert inventory_reads == [True]
    assert calls == ["create"]


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


@pytest.mark.parametrize("allow_interactive_group_rows", [False, True])
def test_no_profile_existing_cal_row_trio_uses_read_only_recovery(monkeypatch, tmp_path, allow_interactive_group_rows):
    # calibrate_xactimate's own generic full-tree precheck sees only the
    # root row (unresolved) -- distinct from the CAL_ROW_*-specific
    # inventory read below, exactly as they are two independent OCR passes
    # in the real implementation. Recovery must run and creation must never
    # run, REGARDLESS of the creation-permission checkbox: the checkbox is
    # not a calibration on/off switch.
    adapter = _FreshCalibrationAdapter(tree_ocr=_ocr(("Test", 22, 23, 43, 14, 0)))
    trio = _inventory(("TEST", 100), ("CAL_ROW_ALPHA", 120), ("CAL_ROW_BRAVO", 140), ("CAL_ROW_CHARLIE", 160))
    monkeypatch.setattr(calibration, "_group_column_inventory", lambda *_: trio)
    monkeypatch.setattr(calibration, "complete_interactive_group_row_calibration",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not create -- rows already exist")))
    path = profile_path(tmp_path, machine_identifier())
    assert not path.exists()
    profile, _path = calibrate_xactimate(
        adapter, directory=tmp_path, allow_interactive_group_rows=allow_interactive_group_rows,
        evidence_dir=tmp_path / "evidence",
    )
    assert profile.geometry["group_row_pitch_state"] == "measured_confident"
    assert profile.validation_state == "ready_for_fast_execution"
    assert adapter.events[0] == ("scroll", 1)  # scroll-to-top ran before the presence check


def test_no_profile_zero_of_three_checkbox_off_is_actionable_and_creates_nothing(monkeypatch, tmp_path):
    adapter = _FreshCalibrationAdapter(tree_ocr=_ocr(("Test", 22, 23, 43, 14, 0)))
    empty = _inventory(("TEST", 100))
    monkeypatch.setattr(calibration, "_group_column_inventory", lambda *_: empty)
    monkeypatch.setattr(calibration, "_create_one_calibration_group",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not create -- checkbox is off")))
    path = profile_path(tmp_path, machine_identifier())
    assert not path.exists()
    with pytest.raises(RuntimeError, match="enable temporary calibration-group creation"):
        calibrate_xactimate(
            adapter, directory=tmp_path, allow_interactive_group_rows=False,
            evidence_dir=tmp_path / "evidence",
        )
    assert adapter.events[0] == ("scroll", 1)  # the presence check still ran (and scrolled) before refusing
    assert not path.exists()  # nothing masquerading as a saved profile is left behind


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


def test_failed_recalibration_preserves_a_prior_ready_profile(monkeypatch, tmp_path):
    ready = replace(profile(), validation_state="ready_for_fast_execution")
    save_calibration(ready, tmp_path)

    adapter = _FreshCalibrationAdapter(tree_ocr=_ocr(("Test", 22, 23, 43, 14, 0)))
    empty = _inventory(("TEST", 100))
    inconsistent = _inventory(("TEST", 100), ("CAL_ROW_ALPHA", 120), ("CAL_ROW_BRAVO", 140), ("CAL_ROW_CHARLIE", 167))
    inventories = iter((empty, empty, inconsistent))
    monkeypatch.setattr(calibration, "_group_column_inventory", lambda *_: next(inventories))
    monkeypatch.setattr(calibration, "_create_one_calibration_group",
                        lambda _adapter, _profile, name: {"name": name, "state": "created"})
    with pytest.raises(RuntimeError, match="spacings disagree"):
        calibrate_xactimate(
            adapter, directory=tmp_path, allow_interactive_group_rows=True,
            evidence_dir=tmp_path / "evidence",
        )
    reloaded = load_calibration(tmp_path)
    assert reloaded == ready
    assert reloaded.validation_state == "ready_for_fast_execution"


class _BootstrapCreationAdapter:
    """Drives the REAL _create_one_calibration_group() sequence (never a
    mocked creator) against an in-memory tree that only grows when this
    fake's own New-Group dialog sequence completes -- proving positive
    root identification and tree reacquisition between ALPHA/BRAVO/CHARLIE
    without a real Xactimate window and without any pre-calibrated pitch."""

    def __init__(self, root_center=100.0, pitch=20.0):
        self.root_center, self.pitch = root_center, pitch
        self.created_names: list[str] = []
        self.scroll_events: list[int] = []
        self.context_menu_calls: list[int] = []
        self._dialog_open = False
        self._pending_name = None
        self._click_count = 0

    def _ensure_main_window(self): return 1
    def _scroll_group_tree_to_top(self, hwnd): self.scroll_events.append(hwnd)
    def _capture_client_image(self, hwnd): return _Image()
    def _unexpected_dialog_present(self): return False
    def _find_dropdown_window(self): return None
    def _open_group_tree_context_menu(self, hwnd, header_pos, row_index):
        self.context_menu_calls.append(row_index)
        return ["item"] * 20
    def _click_group_menu_item(self, items, index): self._dialog_open = True
    def _find_window_by_title(self, title):
        return 999 if (title == "New Group" and self._dialog_open) else None
    def _click_client(self, hwnd, x, y):
        self._click_count += 1
        if self._click_count == 2:  # the Attach/OK click closes the dialog and commits the row
            self._dialog_open = False
            self.created_names.append(self._pending_name)
            self._pending_name = None
            self._click_count = 0
    def _select_all_and_delete(self): pass
    def _type_keybdevent(self, text, char_interval_s=None): self._pending_name = text


def test_bootstrap_creation_reacquires_tree_between_rows_without_calibrated_pitch(monkeypatch, tmp_path):
    candidate = profile(); candidate.geometry["group_row_pitch_state"] = "unresolved"
    candidate.geometry["new_group_dialog_name_click"] = [185, 18]
    candidate.geometry["new_group_dialog_attach_click"] = [305, 75]
    # No pre-calibrated row pitch/height is available going in -- proves
    # creation does not depend on it (nothing here ever reads it).
    candidate.geometry["group_row_height"] = None

    adapter = _BootstrapCreationAdapter(root_center=100.0, pitch=20.0)
    inventory_snapshots: list[tuple[str, ...]] = []
    def fake_inventory(adapter_arg, _image):
        rows = [("TEST", adapter_arg.root_center)]
        for i, name in enumerate(adapter_arg.created_names, start=1):
            rows.append((name, adapter_arg.root_center + i * adapter_arg.pitch))
        inv = _inventory(*rows)
        inventory_snapshots.append(tuple(row["raw_text"] for row in inv["rows"]))
        return inv
    monkeypatch.setattr(calibration, "_group_column_inventory", fake_inventory)

    completed, measurement = complete_interactive_group_row_calibration(
        adapter, candidate, directory=tmp_path, evidence_dir=tmp_path / "evidence",
    )

    # 8 inventory reads total: initial presence-check, then (pre-creation
    # root-lookup + post-creation poll-confirm) per row, then one final
    # verification read.
    assert len(inventory_snapshots) == 8
    initial, alpha_root_lookup, alpha_confirm, bravo_root_lookup, bravo_confirm, \
        charlie_root_lookup, charlie_confirm, final = inventory_snapshots

    # 5: ALPHA creation used positively identified project-root geometry
    # (row_index=0, the root) with none of the three rows existing yet --
    # no dependency on any pre-existing calibrated pitch.
    assert initial == ("TEST",)
    assert alpha_root_lookup == ("TEST",)
    assert adapter.context_menu_calls[0] == 0

    # 6: tree state was reacquired after ALPHA before BRAVO's own creation
    # attempt -- BRAVO's pre-creation root-lookup sees ALPHA already there.
    assert alpha_confirm == ("TEST", "CAL_ROW_ALPHA")
    assert bravo_root_lookup == ("TEST", "CAL_ROW_ALPHA")
    assert adapter.context_menu_calls[1] == 0

    # 7: reacquired again after BRAVO before CHARLIE's creation attempt.
    assert bravo_confirm == ("TEST", "CAL_ROW_ALPHA", "CAL_ROW_BRAVO")
    assert charlie_root_lookup == ("TEST", "CAL_ROW_ALPHA", "CAL_ROW_BRAVO")
    assert adapter.context_menu_calls[2] == 0

    # 8: all three exact names positively verified (final full inventory
    # read) before pitch measurement/promotion.
    assert charlie_confirm == ("TEST", "CAL_ROW_ALPHA", "CAL_ROW_BRAVO", "CAL_ROW_CHARLIE")
    assert final == ("TEST", "CAL_ROW_ALPHA", "CAL_ROW_BRAVO", "CAL_ROW_CHARLIE")
    assert adapter.created_names == list(CALIBRATION_GROUP_NAMES)
    assert len(adapter.scroll_events) == 3  # one scroll-to-top per creation call

    # 9: measured ALPHA->BRAVO and BRAVO->CHARLIE spacings feed the
    # existing tolerance-based confidence/pitch selection, unchanged.
    assert measurement["measured_spacings"] == [20.0, 20.0]
    assert measurement["chosen_pitch"] == 20.0
    assert completed.geometry["group_row_pitch_state"] == "measured_confident"
    assert completed.geometry["group_row_height"] == 20.0
    assert completed.validation_state == "ready_for_fast_execution"


class _CaptureOnlyAdapter:
    def _capture_client_image(self, hwnd): return ("image", hwnd)


def test_settled_group_column_inventory_retries_transient_header_failure(monkeypatch):
    # Proven live mechanism: immediately after a group-tree mutation, a
    # single header-locate attempt can transiently fail even though the
    # identical frame succeeds moments later. A bounded retry must recover
    # without ever loosening what counts as valid header/boundary evidence.
    calls = []
    def flaky(_adapter, _image):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("Subtotal header fallback found 0 bounded candidate(s)")
        return {"rows": ["settled"]}
    monkeypatch.setattr(calibration, "_group_column_inventory", flaky)
    result = calibration._settled_group_column_inventory(_CaptureOnlyAdapter(), 1, timeout_s=1.0, poll_interval_s=0.01)
    assert result == {"rows": ["settled"]}
    assert len(calls) == 2


def test_settled_group_column_inventory_fails_closed_after_deadline(monkeypatch):
    # A DURABLE failure (the header genuinely never resolves) must still
    # raise -- the retry only covers transient repaint delay, it must
    # never mask a real, persistent locator failure or hang indefinitely.
    def always_fails(_adapter, _image):
        raise RuntimeError("Subtotal header fallback found 0 bounded candidate(s)")
    monkeypatch.setattr(calibration, "_group_column_inventory", always_fails)
    with pytest.raises(RuntimeError, match="0 bounded candidate"):
        calibration._settled_group_column_inventory(_CaptureOnlyAdapter(), 1, timeout_s=0.05, poll_interval_s=0.01)


def test_transient_post_alpha_header_failure_does_not_abort_bravo_creation(monkeypatch, tmp_path):
    # Reproduces the exact proven live sequence: ALPHA creation succeeds
    # on a settled frame; BRAVO's own root-lookup (the first
    # _group_column_inventory call reading the post-ALPHA-mutation frame)
    # transiently fails once, then a moment later succeeds. Calibration
    # must complete all three rows, not abort with CAL_ROW_ALPHA left
    # stranded the way the live app-driven attempt did before this fix.
    candidate = profile(); candidate.geometry["group_row_pitch_state"] = "unresolved"
    candidate.geometry["new_group_dialog_name_click"] = [185, 18]
    candidate.geometry["new_group_dialog_attach_click"] = [305, 75]
    candidate.geometry["group_row_height"] = None

    adapter = _BootstrapCreationAdapter(root_center=100.0, pitch=20.0)
    call_index = {"n": 0}
    def fake_inventory(adapter_arg, _image):
        call_index["n"] += 1
        # The 4th call overall is BRAVO's first root-lookup attempt
        # (after: complete_interactive's initial check, ALPHA's
        # root-lookup, ALPHA's confirmation poll) -- fail it exactly
        # once, simulating the live transient post-mutation frame.
        if call_index["n"] == 4:
            raise RuntimeError("Subtotal header fallback found 0 bounded candidate(s)")
        rows = [("TEST", adapter_arg.root_center)]
        for i, name in enumerate(adapter_arg.created_names, start=1):
            rows.append((name, adapter_arg.root_center + i * adapter_arg.pitch))
        return _inventory(*rows)
    monkeypatch.setattr(calibration, "_group_column_inventory", fake_inventory)

    completed, measurement = complete_interactive_group_row_calibration(
        adapter, candidate, directory=tmp_path, evidence_dir=tmp_path / "evidence",
    )
    assert adapter.created_names == list(CALIBRATION_GROUP_NAMES)
    assert measurement["chosen_pitch"] == 20.0
    assert completed.validation_state == "ready_for_fast_execution"
    assert call_index["n"] == 9  # the 8 calls the settled sequence needs, plus exactly one retry


def test_ambiguous_project_root_prevents_any_mutation(monkeypatch, tmp_path):
    candidate = profile(); candidate.geometry["group_row_pitch_state"] = "unresolved"
    candidate.geometry["new_group_dialog_name_click"] = [185, 18]
    candidate.geometry["new_group_dialog_attach_click"] = [305, 75]
    # Two rows both containing the root's identity text -- root is not
    # uniquely identifiable, so nothing may be clicked or typed.
    ambiguous = _inventory(("TEST", 100), ("TEST Copy", 120))
    monkeypatch.setattr(calibration, "_group_column_inventory", lambda *_: ambiguous)
    adapter = _BootstrapCreationAdapter()
    with pytest.raises(RuntimeError, match="could not uniquely locate the project root row"):
        calibration._create_one_calibration_group(adapter, candidate, "CAL_ROW_ALPHA")
    assert adapter.context_menu_calls == []  # no context menu, no click, no keystroke, no dialog
    assert adapter.created_names == []


def test_normal_ui_no_longer_exposes_the_temporary_group_creation_checkbox():
    # The creation-permission checkbox was removed from the normal Quick
    # Run calibration path -- manual group creation in Xactimate is now
    # the supported workflow; automatic creation stays available
    # internally (allow_interactive_group_rows is still a real, tested
    # calibrate_xactimate() parameter), just not wired to a UI toggle.
    from estimate_extractor.ui.components import quick_run_panel
    assert not hasattr(quick_run_panel, "CALIBRATION_CREATION_CHECKBOX_LABEL")
    assert not hasattr(quick_run_panel, "CALIBRATION_CREATION_CHECKBOX_HELP")


# -- calibration sentinel recognition: exact-then-guarded-fuzzy identity --

def _standard_trio_inventory(charlie_raw_text):
    return _inventory(("TEST", 100), ("CAL_ROW_ALPHA", 120), ("CAL_ROW_BRAVO", 140), (charlie_raw_text, 160))


def test_guarded_map_accepts_exact_alpha():
    mapping = calibration._guarded_calibration_row_map(_standard_trio_inventory("CAL_ROW_CHARLIE"))
    assert mapping["CAL_ROW_ALPHA"]["raw_text"] == "CAL_ROW_ALPHA"


def test_guarded_map_accepts_exact_bravo():
    mapping = calibration._guarded_calibration_row_map(_standard_trio_inventory("CAL_ROW_CHARLIE"))
    assert mapping["CAL_ROW_BRAVO"]["raw_text"] == "CAL_ROW_BRAVO"


def test_guarded_map_accepts_exact_charlie():
    mapping = calibration._guarded_calibration_row_map(_standard_trio_inventory("CAL_ROW_CHARLIE"))
    assert mapping["CAL_ROW_CHARLIE"]["raw_text"] == "CAL_ROW_CHARLIE"


def test_guarded_map_tolerates_surrounding_ocr_noise_around_exact_sentinels():
    inventory = _inventory(("TEST", 100), ("fy Cal_ROW_ALPHA", 120), ("Cal_ROW. BRAVO", 140), ("CAL_ROW_CHARLIE", 160))
    mapping = calibration._guarded_calibration_row_map(inventory)
    assert mapping["CAL_ROW_ALPHA"]["raw_text"] == "fy Cal_ROW_ALPHA"
    assert mapping["CAL_ROW_BRAVO"]["raw_text"] == "Cal_ROW. BRAVO"


def test_guarded_map_accepts_proven_live_charlie_ocr_error():
    # Real captured OCR shape from the live app: CAL_ROW_CHARLIE -> "Cal_ROW_CHARLE)" (dropped "I").
    mapping = calibration._guarded_calibration_row_map(_standard_trio_inventory("Cal_ROW_CHARLE)"))
    assert mapping["CAL_ROW_CHARLIE"]["raw_text"] == "Cal_ROW_CHARLE)"


def test_guarded_map_accepts_unique_one_character_deletion():
    mapping = calibration._guarded_calibration_row_map(_standard_trio_inventory("CAL_ROW_CHARLE"))
    assert mapping["CAL_ROW_CHARLIE"]["raw_text"] == "CAL_ROW_CHARLE"


def test_guarded_map_accepts_unique_one_character_substitution():
    mapping = calibration._guarded_calibration_row_map(_standard_trio_inventory("CAL_ROW_CHARLXE"))
    assert mapping["CAL_ROW_CHARLIE"]["raw_text"] == "CAL_ROW_CHARLXE"


def test_guarded_map_accepts_two_edit_error_only_when_uniquely_attributable():
    # "CAL_ROW_CHRLIF" is distance 2 from CHARLIE, distance 4/5 from ALPHA/BRAVO -- unambiguous.
    mapping = calibration._guarded_calibration_row_map(_standard_trio_inventory("CAL_ROW_CHRLIF"))
    assert mapping["CAL_ROW_CHARLIE"]["raw_text"] == "CAL_ROW_CHRLIF"


def test_guarded_map_rejects_error_beyond_bound_for_all_sentinels():
    # Distance >2 from every sentinel -- must fail closed, not guess the "closest" one.
    with pytest.raises(RuntimeError, match="CAL_ROW_CHARLIE"):
        calibration._guarded_calibration_row_map(_standard_trio_inventory("CAL_ROW_CHXXXX"))


def test_guarded_map_rejects_candidate_ambiguous_between_two_sentinels():
    # Synthetic close sentinel pair (real ones are >=5 apart; this exercises
    # the uniqueness guard directly): "CAL_ROW_FO" sits at distance 1 from
    # BOTH "CAL_ROW_FOO" and "CAL_ROW_FOZ" -- must never guess.
    names = ("CAL_ROW_FOO", "CAL_ROW_FOZ")
    inventory = _inventory(("CAL_ROW_FO", 100))
    with pytest.raises(RuntimeError):
        calibration._guarded_calibration_row_map(inventory, names)


def test_guarded_map_rejects_multiple_physical_candidates_for_one_sentinel():
    # Two distinct rows both within the bound of CHARLIE (and far from ALPHA/BRAVO).
    inventory = _inventory(("TEST", 100), ("CAL_ROW_ALPHA", 120), ("CAL_ROW_BRAVO", 140),
                           ("Cal_ROW_CHARLE)", 160), ("CAL_ROW_CHRLIF", 180))
    with pytest.raises(RuntimeError, match="CAL_ROW_CHARLIE"):
        calibration._guarded_calibration_row_map(inventory)


def test_strict_presence_duplicate_check_is_unaffected_by_fuzzy_recognition(monkeypatch, tmp_path):
    # complete_interactive_group_row_calibration()'s own pre-creation
    # "does this already exist" guard must stay exact-only: a near-miss
    # OCR'd row must NOT be treated as "already exists" and must not
    # suppress legitimate creation.
    candidate = profile(); candidate.geometry["group_row_pitch_state"] = "unresolved"
    near_miss_only = _inventory(("TEST", 100), ("CAL_ROW_CHRLIF", 120))  # close to CHARLIE, not exact
    monkeypatch.setattr(calibration, "_group_column_inventory", lambda *_: near_miss_only)
    created = []
    with pytest.raises(RuntimeError):
        # Fails for an unrelated reason (creator not reaching a full trio in
        # this minimal fixture) but must NOT fail with "already exists".
        complete_interactive_group_row_calibration(
            _InteractiveAdapter(), candidate, directory=tmp_path,
            group_creator=lambda name: created.append(name) or {"name": name},
        )
    assert created  # creation was attempted -- not suppressed as "already exists"


def test_ordinary_production_group_matching_stays_exact_and_unaffected():
    from estimate_extractor.xactimate_lookup.fast_group_executor import reconcile_complete_group_inventory
    with pytest.raises(RuntimeError, match="exact physical row match"):
        reconcile_complete_group_inventory(["Roaf"], ["Roof"])  # near-miss OCR must NOT fuzzy-match


def test_sentinel_pairwise_distances_exceed_the_fuzzy_bound():
    needles = {name: calibration._calibration_name_needle(name) for name in CALIBRATION_GROUP_NAMES}
    keys = list(needles)
    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            d = calibration._min_substring_edit_distance(needles[keys[i]], needles[keys[j]])
            assert d > calibration.CALIBRATION_SENTINEL_MAX_EDIT_DISTANCE, (keys[i], keys[j], d)


# -- creation: causal physical-delta binding --

class _FakeClock:
    """Advances instantly on sleep() -- lets bounded-deadline loops in
    xactimate_calibration.py be tested without a real multi-second wait."""
    def __init__(self): self.now = 0.0
    def perf_counter(self): return self.now
    def sleep(self, seconds): self.now += seconds


def test_creation_binds_via_physical_delta_with_ocr_imperfect_new_row(monkeypatch, tmp_path):
    candidate = profile(); candidate.geometry["group_row_pitch_state"] = "unresolved"
    candidate.geometry["new_group_dialog_name_click"] = [185, 18]
    candidate.geometry["new_group_dialog_attach_click"] = [305, 75]
    adapter = _BootstrapCreationAdapter(root_center=100.0, pitch=20.0)
    def fake_inventory(adapter_arg, _image):
        rows = [("TEST", adapter_arg.root_center)]
        for i, created_name in enumerate(adapter_arg.created_names, start=1):
            displayed = "Cal_ROW_CHARLE)" if created_name == "CAL_ROW_CHARLIE" else created_name
            rows.append((displayed, adapter_arg.root_center + i * adapter_arg.pitch))
        return _inventory(*rows)
    monkeypatch.setattr(calibration, "_group_column_inventory", fake_inventory)
    result = calibration._create_one_calibration_group(adapter, candidate, "CAL_ROW_CHARLIE")
    assert result["state"] == "created_and_exactly_observed"
    assert result["physical_delta"] == 1


def test_creation_fails_closed_on_zero_physical_delta(monkeypatch, tmp_path):
    candidate = profile(); candidate.geometry["group_row_pitch_state"] = "unresolved"
    candidate.geometry["new_group_dialog_name_click"] = [185, 18]
    candidate.geometry["new_group_dialog_attach_click"] = [305, 75]
    adapter = _BootstrapCreationAdapter(root_center=100.0, pitch=20.0)
    clock = _FakeClock()
    monkeypatch.setattr(calibration.time, "perf_counter", clock.perf_counter)
    monkeypatch.setattr(calibration.time, "sleep", clock.sleep)
    # No new row ever appears, no matter what the dialog flow does.
    monkeypatch.setattr(calibration, "_group_column_inventory", lambda *_: _inventory(("TEST", 100.0)))
    with pytest.raises(RuntimeError, match="was not exactly observed"):
        calibration._create_one_calibration_group(adapter, candidate, "CAL_ROW_CHARLIE")


def test_creation_fails_closed_on_multiple_new_physical_rows(monkeypatch, tmp_path):
    candidate = profile(); candidate.geometry["group_row_pitch_state"] = "unresolved"
    candidate.geometry["new_group_dialog_name_click"] = [185, 18]
    candidate.geometry["new_group_dialog_attach_click"] = [305, 75]
    adapter = _BootstrapCreationAdapter(root_center=100.0, pitch=20.0)
    clock = _FakeClock()
    monkeypatch.setattr(calibration.time, "perf_counter", clock.perf_counter)
    monkeypatch.setattr(calibration.time, "sleep", clock.sleep)
    # Two new rows appear -- one plausibly compatible, one not. Must never
    # bind to "the one that looks right" out of an ambiguous delta.
    monkeypatch.setattr(calibration, "_group_column_inventory", lambda *_: _inventory(
        ("TEST", 100.0), ("CAL_ROW_CHARLIE", 120.0), ("Unrelated Extra Row", 140.0),
    ))
    with pytest.raises(RuntimeError, match="was not exactly observed"):
        calibration._create_one_calibration_group(adapter, candidate, "CAL_ROW_CHARLIE")


def test_creation_fails_closed_when_new_row_ocr_is_incompatible(monkeypatch, tmp_path):
    candidate = profile(); candidate.geometry["group_row_pitch_state"] = "unresolved"
    candidate.geometry["new_group_dialog_name_click"] = [185, 18]
    candidate.geometry["new_group_dialog_attach_click"] = [305, 75]
    adapter = _BootstrapCreationAdapter(root_center=100.0, pitch=20.0)
    clock = _FakeClock()
    monkeypatch.setattr(calibration.time, "perf_counter", clock.perf_counter)
    monkeypatch.setattr(calibration.time, "sleep", clock.sleep)
    # Exactly one new physical row -- but its text is unrelated to any sentinel.
    monkeypatch.setattr(calibration, "_group_column_inventory", lambda *_: _inventory(
        ("TEST", 100.0), ("Completely Unrelated Text", 120.0),
    ))
    with pytest.raises(RuntimeError, match="was not exactly observed"):
        calibration._create_one_calibration_group(adapter, candidate, "CAL_ROW_CHARLIE")


# -- recovery: guarded-only (no causal delta available) --

def test_recovery_accepts_the_proven_charlie_ocr_error(monkeypatch, tmp_path):
    candidate = profile(); candidate.geometry["group_row_pitch_state"] = "unresolved"
    inventory = _standard_trio_inventory("Cal_ROW_CHARLE)")
    monkeypatch.setattr(calibration, "_group_column_inventory", lambda *_: inventory)
    adapter = _InteractiveAdapter()
    recovered, detail = recover_existing_group_row_calibration(
        adapter, candidate, directory=tmp_path, evidence_dir=tmp_path,
    )
    assert recovered.geometry["group_row_pitch_state"] == "measured_confident"
    assert recovered.validation_state == "ready_for_fast_execution"


def test_recovery_fails_closed_on_competing_fuzzy_charlie_candidates(monkeypatch, tmp_path):
    candidate = profile(); candidate.geometry["group_row_pitch_state"] = "unresolved"
    inventory = _inventory(("TEST", 100), ("CAL_ROW_ALPHA", 120), ("CAL_ROW_BRAVO", 140),
                           ("Cal_ROW_CHARLE)", 160), ("CAL_ROW_CHRLIF", 180))
    monkeypatch.setattr(calibration, "_group_column_inventory", lambda *_: inventory)
    adapter = _InteractiveAdapter()
    with pytest.raises(RuntimeError):
        recover_existing_group_row_calibration(
            adapter, candidate, directory=tmp_path, evidence_dir=tmp_path,
        )


def test_recovery_routing_recognizes_guarded_charlie_as_present(monkeypatch, tmp_path):
    # _complete_or_recover_group_rows()'s presence check must also
    # recognize the OCR-imperfect CHARLIE as present, routing to
    # read-only recovery rather than a redundant/incorrect creation.
    candidate = profile(); candidate.geometry["group_row_pitch_state"] = "unresolved"
    inventory = _standard_trio_inventory("Cal_ROW_CHARLE)")
    monkeypatch.setattr(calibration, "_group_column_inventory", lambda *_: inventory)
    monkeypatch.setattr(calibration, "complete_interactive_group_row_calibration",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not create -- CHARLIE already exists")))
    adapter = _InteractiveAdapter()
    recovered, _detail = calibration._complete_or_recover_group_rows(
        adapter, candidate, directory=tmp_path, evidence_dir=tmp_path, allow_creation=True,
    )
    assert recovered.geometry["group_row_pitch_state"] == "measured_confident"


# -- describe_calibration_group_presence(): read-only UI-facing helper --

def test_describe_calibration_group_presence_reports_full_trio(monkeypatch):
    inventory = _standard_trio_inventory("CAL_ROW_CHARLIE")
    monkeypatch.setattr(calibration, "_group_column_inventory", lambda *_: inventory)
    presence = describe_calibration_group_presence(_InteractiveAdapter())
    assert presence == {"present": list(CALIBRATION_GROUP_NAMES), "missing": []}


def test_describe_calibration_group_presence_reports_zero_of_three(monkeypatch):
    monkeypatch.setattr(calibration, "_group_column_inventory", lambda *_: _inventory(("TEST", 100)))
    presence = describe_calibration_group_presence(_InteractiveAdapter())
    assert presence == {"present": [], "missing": list(CALIBRATION_GROUP_NAMES)}


def test_describe_calibration_group_presence_reports_partial_set_names(monkeypatch):
    monkeypatch.setattr(calibration, "_group_column_inventory", lambda *_: _inventory(
        ("TEST", 100), ("CAL_ROW_ALPHA", 120),
    ))
    presence = describe_calibration_group_presence(_InteractiveAdapter())
    assert presence == {"present": ["CAL_ROW_ALPHA"], "missing": ["CAL_ROW_BRAVO", "CAL_ROW_CHARLIE"]}


def test_describe_calibration_group_presence_recognizes_proven_charlie_ocr_error(monkeypatch):
    # Must agree with _complete_or_recover_group_rows()'s own routing --
    # the same OCR-imperfect CHARLIE counts as present here too.
    inventory = _standard_trio_inventory("Cal_ROW_CHARLE)")
    monkeypatch.setattr(calibration, "_group_column_inventory", lambda *_: inventory)
    adapter = _InteractiveAdapter()
    presence = describe_calibration_group_presence(adapter)
    assert presence == {"present": list(CALIBRATION_GROUP_NAMES), "missing": []}
    assert adapter.events[0] == ("scroll", 1)  # read-only scroll-to-top ran before the presence read
