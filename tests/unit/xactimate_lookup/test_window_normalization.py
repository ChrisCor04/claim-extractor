from __future__ import annotations

import ctypes as _real_ctypes

import pytest

from estimate_extractor.xactimate_lookup.window_normalization import centered_rect
from estimate_extractor.xactimate_lookup.xactimate_calibration import XactimateCalibration


def test_centered_rect_uses_monitor_work_area_not_virtual_desktop_origin():
    assert centered_rect((0, 0, 2560, 1393), 1920, 1023) == (320, 185, 1920, 1023)
    assert centered_rect((-1920, 0, 0, 1040), 1600, 900) == (-1760, 70, 1600, 900)


def test_centered_rect_fails_when_validated_geometry_cannot_fit():
    with pytest.raises(RuntimeError, match="does not fit"):
        centered_rect((0, 0, 1366, 728), 1920, 1023)


# -- normalize_xactimate_window(): monitor-info Win32 regression -----------

def _minimal_profile(*, dpi=96, client_width=1920, client_height=1023) -> XactimateCalibration:
    return XactimateCalibration(
        schema_version="1.2", profile_id="test", machine_id="test", created_at="now",
        executable_path=None, executable_version=None, window_class="Xactimate", window_title="TEST",
        outer_rect=(0, 0, client_width, client_height), client_rect=(0, 0, client_width, client_height),
        client_screen_origin=(0, 0), monitor_rect=(0, 0, 1920, 1080), work_area_rect=(0, 0, 1920, 1040),
        dpi=dpi, window_state="restored", landmarks={}, geometry={}, confidence={},
        validation_state="ready_for_fast_execution", unresolved=(),
    )


class _FakeUser32ForNormalization:
    """Real ctypes.windll.user32 is never touched -- only DPI and the
    monitor-info call are exercised here; this test's job is proving
    normalize_xactimate_window() reaches past that monitor lookup without
    the ctypes TypeError, not exercising the rest of the live move/resize
    sequence."""
    def __init__(self, *, dpi, monitor_rect, work_rect, window_rect=(0, 0, 1920, 1023), client_rect=(0, 0, 1920, 1023)):
        self.dpi, self.monitor_rect, self.work_rect = dpi, monitor_rect, work_rect
        self.window_rect, self.client_rect = window_rect, client_rect

        def GetMonitorInfoW(_hmonitor, info_ref):
            info = info_ref._obj
            info.rcMonitor.left, info.rcMonitor.top, info.rcMonitor.right, info.rcMonitor.bottom = self.monitor_rect
            info.rcWork.left, info.rcWork.top, info.rcWork.right, info.rcWork.bottom = self.work_rect
            return 1
        self.GetMonitorInfoW = GetMonitorInfoW

    def GetDpiForWindow(self, _hwnd): return self.dpi
    def MonitorFromWindow(self, _hwnd, _flags): return 1
    def ShowWindow(self, _hwnd, _cmd): return True
    def GetWindowRect(self, _hwnd, rect_ref):
        r = rect_ref._obj
        r.left, r.top, r.right, r.bottom = self.window_rect
        return 1
    def GetClientRect(self, _hwnd, rect_ref):
        r = rect_ref._obj
        r.left, r.top, r.right, r.bottom = self.client_rect
        return 1


class _FakeCtypesForNormalization:
    """Real ctypes.Structure/sizeof/byref/c_void_p/POINTER; only the DLL
    call layer (user32 itself) is fake -- matches
    test_xactimate_calibration.py's _FakeCtypesForCalibration."""
    Structure = _real_ctypes.Structure
    sizeof = staticmethod(_real_ctypes.sizeof)
    byref = staticmethod(_real_ctypes.byref)
    c_void_p = _real_ctypes.c_void_p
    POINTER = staticmethod(_real_ctypes.POINTER)
    def __init__(self, user32): self.windll = type("Windll", (), {"user32": user32})()


class _ReachedPastMonitorLookup(Exception):
    """Sentinel: centered_rect() is the very next call after the monitor-
    info lookup succeeds, so raising here proves the lookup itself
    completed without the ctypes TypeError -- without needing to also
    mock the rest of the live move/resize/validate sequence."""


def test_normalize_xactimate_window_monitor_lookup_no_longer_raises_ctypes_typeerror(monkeypatch):
    import estimate_extractor.xactimate_lookup.window_normalization as wn_module

    fake_user32 = _FakeUser32ForNormalization(dpi=96, monitor_rect=(0, 0, 1920, 1080), work_rect=(0, 0, 1920, 1040))
    monkeypatch.setattr(wn_module, "ctypes", _FakeCtypesForNormalization(fake_user32))

    def _sentinel(*_args, **_kwargs):
        raise _ReachedPastMonitorLookup()
    monkeypatch.setattr(wn_module, "centered_rect", _sentinel)

    class Adapter:
        def verify_application(self): return True
        def verify_project(self): return True
        def _unexpected_dialog_present(self): return False
        def _find_dropdown_window(self): return None
        def _find_main_window(self): return (1, "TEST")

    with pytest.raises(_ReachedPastMonitorLookup):
        wn_module.normalize_xactimate_window(Adapter(), _minimal_profile())


def test_normalize_xactimate_window_fails_closed_when_monitor_is_unavailable(monkeypatch):
    import estimate_extractor.xactimate_lookup.window_normalization as wn_module

    class _NoMonitorUser32:
        def GetDpiForWindow(self, _hwnd): return 96
        def MonitorFromWindow(self, _hwnd, _flags): return 0
    monkeypatch.setattr(wn_module, "ctypes", _FakeCtypesForNormalization(_NoMonitorUser32()))

    class Adapter:
        def verify_application(self): return True
        def verify_project(self): return True
        def _unexpected_dialog_present(self): return False
        def _find_dropdown_window(self): return None
        def _find_main_window(self): return (1, "TEST")

    with pytest.raises(RuntimeError, match="primary monitor work area is unavailable"):
        wn_module.normalize_xactimate_window(Adapter(), _minimal_profile())
