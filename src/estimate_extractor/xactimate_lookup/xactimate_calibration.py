"""Machine/layout-specific geometry for the experimental fast executor.

Calibration is deliberately non-destructive: it reads the current Estimate
Items frame and Win32 geometry, but never clicks or types in Xactimate.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import socket
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "1.0"
DEFAULT_CALIBRATION_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "ClaimExtractor" / "xactimate_calibrations"
LAYOUT_ERROR = "Xactimate layout differs from saved calibration — recalibrate"


@dataclass(frozen=True, slots=True)
class XactimateCalibration:
    schema_version: str
    profile_id: str
    machine_id: str
    created_at: str
    executable_path: str | None
    executable_version: str | None
    window_class: str
    window_title: str
    outer_rect: tuple[int, int, int, int]
    client_rect: tuple[int, int, int, int]
    client_screen_origin: tuple[int, int]
    monitor_rect: tuple[int, int, int, int]
    work_area_rect: tuple[int, int, int, int]
    dpi: int
    window_state: str
    landmarks: dict[str, list[int]]
    geometry: dict[str, Any]
    confidence: dict[str, str]
    validation_state: str
    unresolved: tuple[str, ...]

    @property
    def client_width(self) -> int:
        return self.client_rect[2] - self.client_rect[0]

    @property
    def client_height(self) -> int:
        return self.client_rect[3] - self.client_rect[1]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "XactimateCalibration":
        value = dict(value)
        for key in ("outer_rect", "client_rect", "client_screen_origin", "monitor_rect", "work_area_rect", "unresolved"):
            value[key] = tuple(value[key])
        return cls(**value)


def machine_identifier() -> str:
    raw = "|".join((socket.gethostname(), platform.system(), platform.machine()))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def profile_path(directory: Path = DEFAULT_CALIBRATION_DIR, machine_id: str | None = None) -> Path:
    return directory / f"{machine_id or machine_identifier()}.json"


def save_calibration(profile: XactimateCalibration, directory: Path = DEFAULT_CALIBRATION_DIR) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = profile_path(directory, profile.machine_id)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(profile.to_dict(), indent=2), encoding="utf-8")
    temporary.replace(path)
    return path


def load_calibration(directory: Path = DEFAULT_CALIBRATION_DIR) -> XactimateCalibration:
    path = profile_path(directory)
    if not path.exists():
        raise RuntimeError("No Xactimate calibration exists for this machine — calibrate Xactimate")
    profile = XactimateCalibration.from_dict(json.loads(path.read_text(encoding="utf-8")))
    if profile.schema_version != SCHEMA_VERSION or profile.machine_id != machine_identifier():
        raise RuntimeError(LAYOUT_ERROR)
    return profile


def _rect(value) -> tuple[int, int, int, int]:
    return tuple(int(part) for part in value)


def measured_group_rows(ocr_data: dict[str, list[Any]], header_top: int) -> tuple[int, int] | None:
    """Return (first-row top offset, pitch) from clustered OCR baselines."""
    tops = sorted({
        int(top) for text, top, confidence in zip(
            ocr_data.get("text", []), ocr_data.get("top", []), ocr_data.get("conf", []), strict=False,
        )
        if str(text).strip() and float(confidence) >= 35 and header_top + 8 <= int(top) <= header_top + 600
    })
    clusters: list[int] = []
    for top in tops:
        if not clusters or top - clusters[-1] > 3:
            clusters.append(top)
    if len(clusters) < 2:
        return None
    pitches = [b - a for a, b in zip(clusters, clusters[1:]) if 14 <= b - a <= 40]
    if not pitches:
        return None
    pitch = Counter(pitches).most_common(1)[0][0]
    return clusters[0] - header_top, pitch


def calibrate_xactimate(adapter, *, directory: Path = DEFAULT_CALIBRATION_DIR) -> tuple[XactimateCalibration, Path]:
    """Detect and persist the current non-destructive Estimate Items layout."""
    if not adapter.verify_application() or not adapter.verify_project():
        raise RuntimeError("Calibration refused: the expected Xactimate project is not active")
    if adapter._unexpected_dialog_present() or adapter._find_dropdown_window() is not None:
        raise RuntimeError("Calibration refused: an unexpected blocking dialog/dropdown is present")
    found = adapter._find_main_window()
    if found is None:
        raise RuntimeError("Calibration refused: Xactimate main window was not found")
    hwnd, title = found
    win32gui = adapter._win32gui()
    window_rect = _rect(win32gui.GetWindowRect(hwnd))
    client_rect = _rect(win32gui.GetClientRect(hwnd))
    origin = tuple(adapter._get_client_origin(hwnd))
    image = adapter._capture_client_image(hwnd)
    group = adapter._locate_group_tree_header(image)
    grid_cat = adapter._locate_label(image, "Cat", prefer="bottommost")
    items = adapter._locate_items_tab(image)
    search = adapter._items_search_pane_field(image)
    if group is None or grid_cat is None or items is None or search is None:
        missing = [name for name, value in (("group_tree_header", group), ("grid_header_cat", grid_cat), ("items_tab", items), ("items_search", search)) if value is None]
        raise RuntimeError("Calibration refused: missing required landmark(s): " + ", ".join(missing))
    if not adapter._tab_is_active(image, items):
        raise RuntimeError("Calibration refused: navigate Xactimate to the active Estimate Items screen")

    ctypes, wintypes = adapter._win32()
    user32 = ctypes.windll.user32
    dpi = int(user32.GetDpiForWindow(hwnd))
    monitor = user32.MonitorFromWindow(hwnd, 2)
    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]
    info = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
    if not monitor or not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        raise RuntimeError("Calibration refused: monitor geometry is unavailable")
    monitor_rect = (info.rcMonitor.left, info.rcMonitor.top, info.rcMonitor.right, info.rcMonitor.bottom)
    work_rect = (info.rcWork.left, info.rcWork.top, info.rcWork.right, info.rcWork.bottom)
    state = "maximized" if user32.IsZoomed(hwnd) else "minimized" if user32.IsIconic(hwnd) else "restored"

    offset = (grid_cat[0] - adapter._shifted_anchor("grid_header_cat_label", (0, 0))[0], grid_cat[1] - adapter._shifted_anchor("grid_header_cat_label", (0, 0))[1])
    quick_cat = adapter._shifted_anchor("quick_entry_cat_value", offset)
    grid_header = adapter._shifted_anchor("grid_header", offset)
    grid_row_1 = adapter._shifted_anchor("grid_row_1", offset)
    landmarks = {
        "group_tree_header": list(_rect(group)), "grid_header_cat": list(_rect(grid_cat)),
        "items_tab": list(_rect(items)), "items_search": list(_rect(search)),
        "quick_entry_cat": list(_rect(quick_cat)), "grid_header": list(_rect(grid_header)),
        "grid_row_1": list(_rect(grid_row_1)),
    }
    row_measurement = None
    try:
        tree_crop = image.crop((max(0, group[0] - 4), group[1], min(image.width, group[0] + 300), image.height))
        row_measurement = measured_group_rows(adapter._ocr_data(tree_crop), 0)
    except Exception:
        pass
    row_top_offset, row_height = row_measurement or (
        adapter._GROUP_TREE_ROW_TEXT_TOP_DY, adapter._GROUP_TREE_ROW_HEIGHT,
    )
    geometry = {
        "group_tree_bounds": [max(0, group[0] - 4), group[3], min(image.width, group[0] + adapter._GROUP_TREE_TEXT_WIDTH), image.height],
        "group_row_text_top_offset": row_top_offset,
        "group_row_height": row_height,
        "group_click_x_offset": adapter._GROUP_TREE_CLICK_DX,
        "group_click_y_offset": adapter._GROUP_TREE_CLICK_DY_OFFSET,
        "group_text_x_offset": adapter._GROUP_TREE_TEXT_DX,
        "group_text_width": adapter._GROUP_TREE_TEXT_WIDTH,
        "group_row_crop_margin_top": adapter._GROUP_TREE_ROW_CROP_MARGIN_TOP,
        "group_row_crop_height": adapter._GROUP_TREE_ROW_CROP_HEIGHT,
        "selection_min_overlap_run": adapter._GROUP_TREE_SELECTION_MIN_OVERLAP_RUN,
        "items_grid_bounds": [grid_header[0], items[3], grid_header[2], image.height],
        "grid_to_quick_cat": [quick_cat[i] - grid_cat[i] for i in range(4)],
        "new_group_dialog_name_click": [185, 18],
        "new_group_dialog_attach_click": [305, 75],
        "group_context_menu_new_index": adapter._GROUP_MENU_NEW_INDEX,
        "group_tree_scroll_point": [max(1, group[0] + 100), max(1, group[1] + 40)],
    }
    confidence = {name: "detected_current_frame" for name in landmarks}
    confidence.update({
        "group_row_height": "detected_from_current_tree_ocr" if row_measurement else "existing_measured_layout_constant_requires_multirow_tree",
        "selection_geometry": "existing_relative_pixel_structure",
        "new_group_dialog_geometry": "legacy_fixed_not_non_destructively_detectable",
        "group_context_menu_index": "uia_structural_semantics",
    })
    exe_path = None
    try:
        _, pid = win32gui.GetWindowThreadProcessId(hwnd)
        import psutil
        exe_path = psutil.Process(pid).exe()
    except Exception:
        pass
    machine = machine_identifier()
    stamp = datetime.now(timezone.utc).isoformat()
    profile = XactimateCalibration(
        schema_version=SCHEMA_VERSION, profile_id=f"{machine}-{dpi}-{client_rect[2]}x{client_rect[3]}",
        machine_id=machine, created_at=stamp, executable_path=exe_path, executable_version=None,
        window_class=str(win32gui.GetClassName(hwnd)), window_title=title,
        outer_rect=window_rect, client_rect=client_rect, client_screen_origin=origin,
        monitor_rect=monitor_rect, work_area_rect=work_rect, dpi=dpi, window_state=state,
        landmarks=landmarks, geometry=geometry, confidence=confidence,
        validation_state="valid_non_destructive_core_landmarks",
        unresolved=tuple(value for value in (
            None if row_measurement else "measured group row pitch needs a populated multi-row tree",
            "New Group dialog controls require a bounded interactive calibration",
        ) if value),
    )
    path = save_calibration(profile, directory)
    return profile, path


def validate_calibration(adapter, profile: XactimateCalibration, *, position_tolerance: int = 8) -> dict[str, Any]:
    """Lightweight fail-closed validation before experimental execution."""
    found = adapter._find_main_window()
    reasons: list[str] = []
    if found is None:
        reasons.append("Xactimate window missing")
        return {"ok": False, "reasons": reasons}
    hwnd = found[0]
    win32gui = adapter._win32gui()
    client = _rect(win32gui.GetClientRect(hwnd))
    ctypes, _ = adapter._win32()
    dpi = int(ctypes.windll.user32.GetDpiForWindow(hwnd))
    if (client[2] - client[0], client[3] - client[1]) != (profile.client_width, profile.client_height):
        reasons.append("client size mismatch")
    if dpi != profile.dpi:
        reasons.append("DPI mismatch")
    image = adapter._capture_client_image(hwnd)
    observed = {
        "group_tree_header": adapter._locate_group_tree_header(image),
        "grid_header_cat": adapter._locate_label(image, "Cat", prefer="bottommost"),
        "items_tab": adapter._locate_items_tab(image),
    }
    for name, rect in observed.items():
        expected = profile.landmarks[name]
        if rect is None:
            reasons.append(f"missing landmark: {name}")
        elif abs(rect[0] - expected[0]) > position_tolerance or abs(rect[1] - expected[1]) > position_tolerance:
            reasons.append(f"landmark moved: {name}")
    if adapter._items_search_pane_field(image) is None:
        reasons.append("active Items/Search context missing")
    return {"ok": not reasons, "reasons": reasons, "client_rect": client, "dpi": dpi, "landmarks": {k: list(v) if v else None for k, v in observed.items()}}


def apply_fast_geometry(adapter, profile: XactimateCalibration) -> None:
    """Apply only fast-path relative geometry; production defaults stay intact."""
    mapping = {
        "_GROUP_TREE_ROW_TEXT_TOP_DY": "group_row_text_top_offset",
        "_GROUP_TREE_ROW_HEIGHT": "group_row_height", "_GROUP_TREE_CLICK_DX": "group_click_x_offset",
        "_GROUP_TREE_CLICK_DY_OFFSET": "group_click_y_offset", "_GROUP_TREE_TEXT_DX": "group_text_x_offset",
        "_GROUP_TREE_TEXT_WIDTH": "group_text_width", "_GROUP_TREE_ROW_CROP_MARGIN_TOP": "group_row_crop_margin_top",
        "_GROUP_TREE_ROW_CROP_HEIGHT": "group_row_crop_height", "_GROUP_TREE_SELECTION_MIN_OVERLAP_RUN": "selection_min_overlap_run",
        "_GROUP_MENU_NEW_INDEX": "group_context_menu_new_index",
    }
    for attribute, key in mapping.items():
        setattr(adapter, attribute, int(profile.geometry[key]))
