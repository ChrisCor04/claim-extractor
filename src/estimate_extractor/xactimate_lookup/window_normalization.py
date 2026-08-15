"""Deterministic window-profile normalization for pixel-calibrated fast entry."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from typing import Any

from .xactimate_calibration import LAYOUT_ERROR, XactimateCalibration, validate_calibration


def centered_rect(work_rect: tuple[int, int, int, int], width: int, height: int) -> tuple[int, int, int, int]:
    left, top, right, bottom = work_rect
    if right - left < width or bottom - top < height:
        raise RuntimeError(
            f"target {width}x{height} does not fit monitor work area {right-left}x{bottom-top}"
        )
    x = left + ((right - left) - width) // 2
    y = top + ((bottom - top) - height) // 2
    return x, y, width, height


def normalize_xactimate_window(adapter, profile: XactimateCalibration) -> dict[str, Any]:
    """Restore the saved device-specific client size and monitor-relative position."""
    if not adapter.verify_application() or not adapter.verify_project():
        raise RuntimeError("window normalization refused: expected Xactimate project is not active")
    if adapter._unexpected_dialog_present() or adapter._find_dropdown_window() is not None:
        raise RuntimeError("window normalization refused: blocking dialog/dropdown is present")
    found = adapter._find_main_window()
    if found is None:
        raise RuntimeError("window normalization refused: Xactimate main window was not found")
    hwnd = found[0]
    user32 = ctypes.windll.user32
    dpi = int(user32.GetDpiForWindow(hwnd))
    if dpi != profile.dpi:
        raise RuntimeError(f"{LAYOUT_ERROR}: DPI {dpi} != calibrated {profile.dpi}")

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

    user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
    user32.MonitorFromPoint.restype = ctypes.c_void_p
    user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.POINTER(MONITORINFO)]
    # Normalize onto the monitor currently containing Xactimate. This works
    # for negative-origin secondary monitors and does not assume (0, 0).
    primary = user32.MonitorFromWindow(hwnd, 2)  # MONITOR_DEFAULTTONEAREST
    info = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
    if not primary or not user32.GetMonitorInfoW(primary, ctypes.byref(info)):
        raise RuntimeError("window normalization refused: primary monitor work area is unavailable")
    work = (info.rcWork.left, info.rcWork.top, info.rcWork.right, info.rcWork.bottom)

    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    wrect = wintypes.RECT(); crect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(wrect)) or not user32.GetClientRect(hwnd, ctypes.byref(crect)):
        raise RuntimeError("window normalization refused: restored window geometry is unavailable")
    nonclient_w = (wrect.right - wrect.left) - (crect.right - crect.left)
    nonclient_h = (wrect.bottom - wrect.top) - (crect.bottom - crect.top)
    outer_w, outer_h = profile.client_width + nonclient_w, profile.client_height + nonclient_h
    x, y, outer_w, outer_h = centered_rect(work, outer_w, outer_h)
    if not user32.MoveWindow(hwnd, x, y, outer_w, outer_h, True):
        raise RuntimeError("window normalization refused: MoveWindow failed")
    if not adapter._force_foreground(hwnd):
        raise RuntimeError("window normalization refused: Xactimate could not be foregrounded")

    validation = validate_calibration(adapter, profile, require_safe_group_rows=False)
    if not validation["ok"]:
        raise RuntimeError(f"{LAYOUT_ERROR}: " + "; ".join(validation["reasons"]))
    win32gui = adapter._win32gui()
    final_window = tuple(win32gui.GetWindowRect(hwnd))
    return {
        "ok": True, "window_rect": final_window,
        "client_width": profile.client_width, "client_height": profile.client_height,
        "dpi": profile.dpi, "work_area": work, "maximized": bool(user32.IsZoomed(hwnd)),
        "calibration_profile_id": profile.profile_id, "validation": validation,
    }
