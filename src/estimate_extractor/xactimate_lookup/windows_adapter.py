"""WindowsXactimateAdapter -- a real, live-validated XactimateAdapter for
Windows desktop automation against Xactimate Online Estimate Writer.

This is not a pure UI-Automation adapter and not a pure vision/OCR
adapter -- it is a hybrid, because that is what the live investigation
(Phase 4.1 -> 4.2 -> 4.2B, see docs/xactimate-lookup.md) actually found
works:

- The application's *static* chrome (search box container, Quick Entry
  panel, the results grid) exposes **zero** UI Automation peers -- this
  was independently confirmed via raw UI Automation (all three tree
  views) and legacy MSAA (``IAccessible``). This appears to be a
  consequence of the app being a self-contained, trimmed .NET Core WPF
  ClickOnce publish (trimming is a documented cause of exactly this
  symptom). It means the search box, Quick Entry fields, and grid cells
  must be driven by verified screen coordinates, not accessibility APIs.
- The search-results dropdown, by contrast, is a **separate top-level
  owned popup window** that -- unlike the static chrome -- *is* a fully
  populated, standard WPF ``ListBox`` with real automation peers. Every
  row is read as exact UI Automation text (category/selector code,
  description, price), never OCR.
- ``PrintWindow`` against the main window's HWND cannot see that popup
  at all (it is a different top-level window). It must be located via
  window enumeration.
- Typing via the modern ``SendInput``-backed API (what ``pywinauto``
  uses by default) reliably enters text but never triggers the popup.
  The legacy ``keybd_event`` API does, reliably. Both are standard,
  documented Win32 input-injection APIs -- this is not an evasion
  technique, it is a real, reproducible discovery about which one this
  particular application's live-search binding responds to.
- Selecting a candidate is a **real mouse click** at the row's live
  (freshly re-read, immediately before clicking) UI Automation bounding
  rectangle center. ``LegacyIAccessiblePattern.DoDefaultAction()`` is
  supported by the row elements but was confirmed to be a safe no-op
  here -- it does not select anything, so it cannot be used as a
  coordinate-free selection substitute.
- Quantity is entered by clicking the grid row's own ``Quantity`` cell
  directly (the top "Quick Entry" panel is a separate "create new item"
  form that desyncs once a row already exists in the grid), typing via
  ``keybd_event``, and committing with ``Tab`` -- deliberately never
  ``Enter``.
- Committing is ``Ctrl+S``; the header's "Saved" / "Unsaved changes"
  text is the ground truth for whether it worked.

``supports_live_execution`` stays ``False`` until the pilot-gate
validation in docs/xactimate-lookup.md's Phase 4.3 section reports it
should be flipped -- see that document for the exact evidence.

All Windows-only dependencies (``ctypes``/``win32gui``/``win32ui``/
``comtypes``/``pywinauto``/``pytesseract``) are imported lazily inside
methods, never at module import time, so importing this module on a
non-Windows platform (e.g. the CI test suite) does not fail -- only
actually instantiating/using ``WindowsXactimateAdapter`` requires
Windows.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from estimate_extractor.xactimate_lookup.adapter import (
    AdapterError,
    UnexpectedDialogError,
    XactimateAdapter,
)
from estimate_extractor.xactimate_lookup.models import DropdownResult, PopulatedFields

#: Xactimate's HwndWrapper class names embed this literal substring for
#: every top-level window it owns (main window, popups, the "Loading"
#: overlay) -- confirmed live across dozens of window enumerations.
#: Matching on it avoids needing a process-name/PID lookup at all.
_APP_CLASS_MARKER = "Xactimate online Estimate Writer"

#: Client-relative pixel rects (left, top, right, bottom), calibrated
#: against the Estimate Items screen at 1920x1021 client size / 96 DPI
#: (100% scale) with the content pane scrolled to its default/top
#: position (see _reset_scroll_state). Live-drift-prone; every method
#: that clicks re-derives its target via _locate_anchor_offset() first
#: wherever the click matters for correctness (search box, OCR
#: label-relative crops) rather than trusting these blindly.
_ANCHORS = {
    "search_box": (508, 165, 826, 186),
    "search_button": (843, 165, 928, 186),
    "search_dropdown_arrow": (817, 165, 836, 186),
    "items_tab": (296, 78, 342, 98),
    "quick_entry_cat_label": (506, 461, 535, 475),
    #: The grid header's own "Cat" column label -- unlike the Quick Entry
    #: panel's "Cat:" label (which OCR intermittently fails to detect at
    #: all when its value box is empty, a real live-caught bug), the grid
    #: header is always present whenever there's a grid to read, and was
    #: found reliably in every sample tested. Used as the anchor-offset
    #: reference in preference to quick_entry_cat_label. Calibrated
    #: (dx=0/dy=0 reference frame) by back-solving from known-good
    #: offsets observed live -- see docs/xactimate-lookup.md Phase 4.4.
    "grid_header_cat_label": (540, 630, 561, 643),
    "quick_entry_cat_value": (563, 459, 608, 477),
    "quick_entry_sel_value": (663, 459, 738, 477),
    "quick_entry_act_value": (792, 459, 818, 477),
    "quick_entry_desc_box": (563, 488, 1044, 506),
    "quick_entry_calc_qty_box": (563, 514, 613, 532),
    "quick_entry_unit_dropdown": (744, 514, 771, 532),
    "grid_header": (506, 628, 1894, 645),
    "grid_row_1": (506, 654, 1894, 671),
}

#: Vertical distance between consecutive grid rows, pixels. Live-caught
#: (Phase 4.4 Stage 3): the original value of 17 was calibrated only
#: against single-row states, where _last_row_geometry()'s
#: (row_count - 1) * _GRID_ROW_HEIGHT term is always multiplied by
#: zero -- so the constant was never actually exercised until the
#: first live pilot item that reached two grid rows, where it silently
#: misaligned every crop for row 2+ by ~8px (garbled OCR: category='_',
#: selector='an', description=None). Remeasured directly via OCR
#: word-position of the "#" column across two real static rows
#: (410 at y=615, 412 at y=640) -- see docs/xactimate-lookup.md Phase
#: 4.4. Any adapter change that only ever exercised a single grid row
#: cannot validate this constant; re-verify against a 2+ row state
#: before trusting it again.
_GRID_ROW_HEIGHT = 25

#: Column x-ranges within a grid row, client-relative, matching the
#: header order (#, Cat, Sel, Act, Notes, Description, Coverage, Calc,
#: Quantity, Unit, Unit Price, Sales Tax, RCV, Depreciation, Factor, ACV).
#: Live-measured via OCR word-level bounding boxes against a real row
#: ("356 SFG GUTA & [notes icon] Gutter / downspout - aluminum - up to
#: 5" Dwelling 0 LF $11.56 ..."). The header also has a "Notes" column
#: (a small calendar-icon glyph, non-text) between Act and Description
#: that an earlier version of this table didn't account for, causing
#: the activity/description boundary to be wrong -- see
#: docs/xactimate-lookup.md Phase 4.3 for the exact live comparison
#: that caught it.
_GRID_COLUMNS = {
    "number": (506, 538),
    "category": (539, 559),
    # selector/activity/description live-caught (Phase 4.4 Stage 3): the
    # original boundaries were only ever exercised against a 4-character
    # selector code ("GUTA") and a description short enough not to hit
    # the right edge. A longer selector ("GUTAB>") visually overflows
    # column boundaries Xactimate doesn't clip to -- the trailing ">"
    # was cut off (selector) and bled into the next crop (activity read
    # a stray ">" instead of the real "&"/"-" symbol underneath it,
    # which OCR word-position measurement placed at x=626-636,
    # independent of selector length since Xactimate's activity column
    # itself doesn't move). Description's right edge similarly
    # truncated a longer description ("...aluminum - 7\" to 8\"" cut to
    # "...aluminum - 7"). Widened all three to fit the longest observed
    # real content with margin; see docs/xactimate-lookup.md Phase 4.4.
    "selector": (563, 620),
    "activity": (622, 648),
    "description": (682, 948),
    "quantity": (1020, 1080),
    # Live-caught (Phase 4.4 Stage 3): (1073, 1099) was calibrated
    # against the row highlighted by enter_quantity()'s own cell click
    # (Phase 4.3/4.4 Stage 1) -- but read_populated_fields() is actually
    # called by the real orchestrator.execute_plan() flow right after
    # select_candidate(), BEFORE enter_quantity() ever runs, i.e.
    # against the STATIC (unhighlighted) row. OCR word-position
    # measurement in that real state placed "SQ" at x=1095-1110, mostly
    # outside the old boundary. See docs/xactimate-lookup.md Phase 4.4.
    "unit": (1090, 1120),
    "unit_price": (1131, 1197),
}


class StaleCandidateError(AdapterError):
    """Raised when a previously-parsed dropdown candidate can no longer
    be found (by matching text) in a freshly re-read popup -- the
    live investigation's core selection-safety rule ("never reuse stale
    coordinates") made concrete."""


class PopupNotFoundError(AdapterError):
    """Raised when the search-results popup window does not appear
    within the timeout, or disappears/changes between capture and
    selection."""


@dataclass(slots=True)
class _RawDropdownRow:
    """Adapter-internal raw row -- carries the live popup handle and
    rect alongside the text, which DropdownResult (a shared, adapter-
    agnostic model used by ranking.py/registry.py/etc.) deliberately
    does not. Never leaks outside capture_dropdown()/parse_dropdown()/
    select_candidate()."""

    code_text: str
    description_text: str
    price_text: str
    row_position: int
    popup_hwnd: int
    rect_at_capture: tuple[int, int, int, int]


@dataclass(slots=True)
class _AdapterDiagnostics:
    main_window_found: bool
    main_window_hwnd: int | None
    main_window_title: str | None
    project_matches: bool
    foreground: bool
    dropdown_open: bool
    timestamp: str


@dataclass(slots=True)
class QuantityVerificationResult:
    """Result of verify_quantity_committed()'s bounded poll -- see that
    method's docstring. ``samples`` records every attempt (elapsed
    seconds since the poll started, whether a grid row was found that
    attempt, and the quantity value observed, if any) for timing
    diagnostics and regression assertions; never used for control flow
    itself, which is decided attempt-by-attempt as the poll runs."""

    matched: bool
    stop_reason: str  # "matched" | "timeout" | "wrong_context"
    expected: float
    observed: float | None
    attempts: int
    elapsed_s: float
    samples: list[tuple[float, bool, float | None]] = field(default_factory=list)


def _split_category_selector(code: str) -> tuple[str, str]:
    """Xactimate catalog codes are always a fixed 3-letter category
    prefix followed by a variable-length selector, e.g. "SFGGUTA" ->
    ("SFG", "GUTA") -- confirmed against every row observed live
    (SFG/GUTA, SFG/GUTA>, SFG/GUTC, SFG/GUTG, SFG/GUTHRA<, ...)."""
    code = code.strip()
    return code[:3], code[3:]


def _levenshtein_distance(a: str, b: str) -> int:
    """Plain edit distance, no external dependency -- used by
    ``_click_context_menu_item()`` to catch a stable OCR misread
    (Phase 4.5: "Delete" read as "betete") that a plain similarity
    ratio can't safely distinguish from a line that must never match
    (e.g. "undo delete line item"). See that method's docstring."""
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


class WindowsXactimateAdapter(XactimateAdapter):
    """Real Windows desktop adapter. See module docstring for the
    validated mechanism. ``supports_live_execution`` is a class
    default of False, overridable per-instance only by the pilot-gate
    process described in docs/xactimate-lookup.md -- never flip it in
    code without that evidence existing."""

    supports_live_execution = False

    def __init__(
        self,
        expected_project_name: str,
        *,
        evidence_dir: Path | None = None,
        dropdown_timeout_s: float = 5.0,
        window_finder=None,
    ) -> None:
        self.expected_project_name = expected_project_name
        self.evidence_dir = evidence_dir or Path.cwd() / "automation_evidence"
        self.dropdown_timeout_s = dropdown_timeout_s
        #: injectable for tests that want to fake window discovery
        #: without a real Xactimate process; production code leaves
        #: this None and uses _default_window_finder.
        self._window_finder = window_finder or self._default_window_finder

        self._main_hwnd: int | None = None
        self._last_dropdown_hwnd: int | None = None
        self._last_dropdown_rows: list[_RawDropdownRow] = []
        self._last_selected: DropdownResult | None = None
        self._last_selected_row_count_before: int | None = None
        self._current_query: str | None = None

    # ------------------------------------------------------------------
    # Lazy Windows-only imports
    # ------------------------------------------------------------------

    @staticmethod
    def _win32():
        import ctypes
        import ctypes.wintypes as wintypes

        return ctypes, wintypes

    @staticmethod
    def _win32gui():
        import win32gui

        return win32gui

    @staticmethod
    def _win32ui():
        import win32ui

        return win32ui

    @staticmethod
    def _uia():
        import comtypes.client

        comtypes.client.GetModule("UIAutomationCore.dll")
        from comtypes.gen import UIAutomationClient as UIA

        uia = comtypes.client.CreateObject(UIA.CUIAutomation, interface=UIA.IUIAutomation)
        return uia, UIA

    @staticmethod
    def _pytesseract():
        import pytesseract

        # A future config-driven install path belongs in
        # config/xactimate_windows_profile.yaml once this adapter is
        # promoted past the pilot gate; hardcoded here to match the
        # environment this was validated against (see docs).
        default_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
        if Path(default_cmd).exists():
            pytesseract.pytesseract.tesseract_cmd = default_cmd
        return pytesseract

    # ------------------------------------------------------------------
    # Window discovery
    # ------------------------------------------------------------------

    def _default_window_finder(self):
        """Enumerates top-level windows and classifies them. Returns
        (main_windows: list[(hwnd, title, rect)], popup_windows: list[(hwnd, title, rect)])
        where popup_windows are unnamed HwndWrapper windows (the results
        dropdown or, transiently, the "Loading" overlay) owned by the
        same class family as the main window."""
        win32gui = self._win32gui()
        ctypes, wintypes = self._win32()
        user32 = ctypes.windll.user32

        mains: list[tuple[int, str, tuple[int, int, int, int]]] = []
        popups: list[tuple[int, str, tuple[int, int, int, int]]] = []

        def cb(hwnd, _):
            try:
                cls = win32gui.GetClassName(hwnd)
            except Exception:
                return True
            if _APP_CLASS_MARKER not in cls:
                return True
            try:
                title = win32gui.GetWindowText(hwnd)
                visible = win32gui.IsWindowVisible(hwnd)
                rect = win32gui.GetWindowRect(hwnd)
            except Exception:
                return True
            if not visible or rect == (0, 0, 0, 0):
                return True
            if title and title != "Loading":
                mains.append((hwnd, title, rect))
            elif title != "Loading" and (rect[2] - rect[0]) > 50:
                popups.append((hwnd, title, rect))
            return True

        win32gui.EnumWindows(cb, None)
        return mains, popups

    def _find_main_window(self) -> tuple[int, str] | None:
        mains, _ = self._window_finder()
        if not mains:
            return None
        for hwnd, title, _rect in mains:
            if title.strip().lower() == self.expected_project_name.strip().lower():
                return hwnd, title
        # application is running but the active project doesn't match --
        # verify_application() and verify_project() must be able to
        # distinguish these two cases, so return the first candidate
        # anyway and let verify_project() do the name comparison.
        hwnd, title, _rect = mains[0]
        return hwnd, title

    def _find_dropdown_window(self) -> int | None:
        _, popups = self._window_finder()
        if not popups:
            return None
        return popups[0][0]

    # ------------------------------------------------------------------
    # Low-level input primitives -- the validated mechanism
    # ------------------------------------------------------------------

    def _get_client_origin(self, hwnd: int) -> tuple[int, int]:
        ctypes, wintypes = self._win32()
        user32 = ctypes.windll.user32
        pt = wintypes.POINT(0, 0)
        user32.ClientToScreen(hwnd, ctypes.byref(pt))
        return pt.x, pt.y

    def _force_foreground(self, hwnd: int) -> bool:
        """SetForegroundWindow alone is frequently denied by Windows'
        foreground-lock protection when called from a background
        automation process. Uses the documented AttachThreadInput
        workaround, falling back to a minimize/restore cycle -- both
        confirmed live (see docs/xactimate-lookup.md Phase 4.1/4.2)."""
        ctypes, wintypes = self._win32()
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        if user32.GetForegroundWindow() == hwnd:
            return True

        target_pid = wintypes.DWORD()
        target_tid = user32.GetWindowThreadProcessId(hwnd, ctypes.byref(target_pid))
        current_tid = kernel32.GetCurrentThreadId()

        attached = False
        if target_tid and target_tid != current_tid:
            attached = bool(user32.AttachThreadInput(current_tid, target_tid, True))

        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)

        if attached:
            user32.AttachThreadInput(current_tid, target_tid, False)

        time.sleep(0.3)
        if user32.GetForegroundWindow() != hwnd:
            user32.ShowWindow(hwnd, 6)  # SW_MINIMIZE
            time.sleep(0.15)
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            user32.SetForegroundWindow(hwnd)
            time.sleep(0.3)

        return user32.GetForegroundWindow() == hwnd

    def _click_client(self, hwnd: int, x: int, y: int) -> None:
        ctypes, _ = self._win32()
        user32 = ctypes.windll.user32
        ox, oy = self._get_client_origin(hwnd)
        user32.SetCursorPos(ox + x, oy + y)
        time.sleep(0.05)
        user32.mouse_event(0x0002, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTDOWN
        time.sleep(0.05)
        user32.mouse_event(0x0004, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTUP

    def _click_screen(self, x: int, y: int) -> None:
        """Click at absolute screen coordinates -- used only for a
        freshly-read UI Automation bounding rectangle, which is already
        in screen coordinates. Never used with a cached/guessed point."""
        ctypes, _ = self._win32()
        user32 = ctypes.windll.user32
        user32.SetCursorPos(x, y)
        time.sleep(0.05)
        user32.mouse_event(0x0002, 0, 0, 0, 0)
        time.sleep(0.05)
        user32.mouse_event(0x0004, 0, 0, 0, 0)

    def _type_keybdevent(self, text: str) -> None:
        """The validated trigger mechanism: SendInput-based typing
        (pywinauto's default) never triggers Xactimate's live-search
        binding; the legacy keybd_event API does, reliably (5/5 trials,
        see docs/xactimate-lookup.md Phase 4.2B)."""
        ctypes, _ = self._win32()
        user32 = ctypes.windll.user32
        KEYEVENTF_KEYUP = 0x0002
        for ch in text:
            vk_scan = user32.VkKeyScanW(ord(ch))
            vk = vk_scan & 0xFF
            need_shift = bool((vk_scan >> 8) & 1)
            if need_shift:
                user32.keybd_event(0x10, 0, 0, 0)
                time.sleep(0.01)
            user32.keybd_event(vk, 0, 0, 0)
            time.sleep(0.02)
            user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
            if need_shift:
                time.sleep(0.01)
                user32.keybd_event(0x10, 0, KEYEVENTF_KEYUP, 0)
            time.sleep(0.1)

    def _press_key(self, vk: int) -> None:
        ctypes, _ = self._win32()
        user32 = ctypes.windll.user32
        KEYEVENTF_KEYUP = 0x0002
        user32.keybd_event(vk, 0, 0, 0)
        time.sleep(0.05)
        user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)

    def _press_ctrl(self, vk: int) -> None:
        ctypes, _ = self._win32()
        user32 = ctypes.windll.user32
        KEYEVENTF_KEYUP = 0x0002
        VK_CONTROL = 0x11
        user32.keybd_event(VK_CONTROL, 0, 0, 0)
        time.sleep(0.02)
        user32.keybd_event(vk, 0, 0, 0)
        time.sleep(0.05)
        user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
        time.sleep(0.02)
        user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)

    def _select_all_and_delete(self) -> None:
        VK_A = 0x41
        VK_DELETE = 0x2E
        self._press_ctrl(VK_A)
        time.sleep(0.1)
        self._press_key(VK_DELETE)
        time.sleep(0.2)

    # ------------------------------------------------------------------
    # Screen capture
    # ------------------------------------------------------------------

    def _capture_client_image(self, hwnd: int):
        """PrintWindow against the MAIN window -- works fine for the
        static chrome (search box text, Quick Entry panel, grid), which
        is part of the main window's own rendered surface. Does NOT and
        cannot capture the results popup -- that is a different
        top-level window (see module docstring)."""
        from PIL import Image

        win32gui = self._win32gui()
        win32ui = self._win32ui()
        ctypes, wintypes = self._win32()
        user32 = ctypes.windll.user32

        wrect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(wrect))
        crect = wintypes.RECT()
        user32.GetClientRect(hwnd, ctypes.byref(crect))
        ox, oy = self._get_client_origin(hwnd)
        cl_rel = ox - wrect.left
        ct_rel = oy - wrect.top
        cw = crect.right - crect.left
        ch = crect.bottom - crect.top

        hwndDC = win32gui.GetWindowDC(hwnd)
        mfcDC = win32ui.CreateDCFromHandle(hwndDC)
        saveDC = mfcDC.CreateCompatibleDC()
        saveBitMap = win32ui.CreateBitmap()
        full_w = wrect.right - wrect.left
        full_h = wrect.bottom - wrect.top
        saveBitMap.CreateCompatibleBitmap(mfcDC, full_w, full_h)
        saveDC.SelectObject(saveBitMap)
        user32.PrintWindow(hwnd, saveDC.GetSafeHdc(), 2)  # PW_RENDERFULLCONTENT
        bmpinfo = saveBitMap.GetInfo()
        bmpstr = saveBitMap.GetBitmapBits(True)
        img = Image.frombuffer("RGB", (bmpinfo["bmWidth"], bmpinfo["bmHeight"]), bmpstr, "raw", "BGRX", 0, 1)
        win32gui.DeleteObject(saveBitMap.GetHandle())
        saveDC.DeleteDC()
        mfcDC.DeleteDC()
        win32gui.ReleaseDC(hwnd, hwndDC)

        return img.crop((cl_rel, ct_rel, cl_rel + cw, ct_rel + ch))

    # ------------------------------------------------------------------
    # OCR-based, self-locating field reading (the static chrome has no
    # UI Automation peers at all -- confirmed in Phase 4.1/4.2 -- so
    # this is the only viable strategy for read_populated_fields()).
    # ------------------------------------------------------------------

    def _ocr_text(self, image, psm: int = 7) -> str:
        pytesseract = self._pytesseract()
        return pytesseract.image_to_string(image, config=f"--psm {psm}").strip()

    @staticmethod
    def _normalize_inch_mark(text: str) -> str:
        """Tesseract live-observed to consistently misread a trailing
        double-quote (inch mark) as a degree sign at this crop size --
        e.g. 'up to 5\xb0' for the real 'up to 5"' (see
        docs/xactimate-lookup.md Phase 4.3). Xactimate descriptions in
        this catalog use the inch mark, never degrees, so a degree sign
        directly after a digit is corrected; any other occurrence is
        left alone rather than guessed at."""
        import re

        return re.sub(r"(?<=\d)\xb0", '"', text)

    def _locate_label(self, image, needle: str, prefer: str = "topmost") -> tuple[int, int, int, int] | None:
        """Finds the bounding box of a specific label text anywhere in
        the image, via word-level OCR data -- used to self-correct for
        scroll drift instead of trusting a cached absolute position.

        Two live-caught bugs fixed here (see docs/xactimate-lookup.md
        Phase 4.3): (1) the grid has its own "Cat" column header, a
        second real match for the "Cat:" needle -- Tesseract's default
        page-segmentation mode (PSM 3) intermittently failed to detect
        the Quick Entry panel's "Cat:" label at all in some captured
        frames (confirmed by testing PSM 3/4/6/11/12 against the same
        saved screenshot: only 11 and 12 found both occurrences), so a
        single-match implementation would sometimes silently anchor on
        the wrong one. Fixed by using PSM 11 (sparse text) AND an
        explicit `prefer` direction instead of trusting dict/OCR
        iteration order. (2) Even PSM 11 was later found to
        intermittently miss the Quick Entry "Cat:" label entirely (not
        a wrong match -- no match at all) when its value box is empty,
        which is why `_anchor_offset()` no longer anchors on it at all
        -- see that method's docstring."""
        pytesseract = self._pytesseract()
        from pytesseract import Output

        data = pytesseract.image_to_data(image, output_type=Output.DICT, config="--psm 11")
        needle_clean = needle.strip().lower().rstrip(":")
        matches = []
        for i, word in enumerate(data["text"]):
            if word.strip().lower().rstrip(":") == needle_clean:
                matches.append((data["top"][i], data["left"][i], data["width"][i], data["height"][i]))
        if not matches:
            return None
        matches.sort(key=lambda m: (m[0], m[1]), reverse=(prefer == "bottommost"))
        top, left, width, height = matches[0]
        return (left, top, left + width, top + height)

    def _anchor_offset(self, image) -> tuple[int, int] | None:
        """Locates the grid header's own "Cat" column label live and
        returns the (dx, dy) shift from its calibrated position in
        _ANCHORS -- applied to every other fixed anchor to correct for
        scroll drift. Returns None if the label can't be found (caller
        should fall back / stop).

        Originally anchored on the Quick Entry panel's "Cat:" label
        instead. Live testing found that label is intermittently
        undetectable by OCR *at all* (not a wrong match -- genuinely
        absent from the word list) when its value box is empty, which
        happens whenever nothing has been explicitly clicked into Quick
        Entry sync yet -- exactly the state `read_populated_fields()`
        is usually called in. The grid header's "Cat" column label, by
        contrast, was present in every sample checked (it's part of the
        grid itself, which must exist for there to be anything to
        anchor for). Preferring the *bottommost* "Cat" match is what
        selects the grid header over Quick Entry's when both are
        present. See docs/xactimate-lookup.md Phase 4.4."""
        found = self._locate_label(image, "Cat", prefer="bottommost")
        if found is None:
            return None
        calibrated = _ANCHORS["grid_header_cat_label"]
        dx = found[0] - calibrated[0]
        dy = found[1] - calibrated[1]
        return dx, dy

    def _capture_and_locate(self, hwnd: int, attempts: int = 6, delay_s: float = 0.6):
        """Captures a fresh screenshot and computes the anchor offset,
        retrying with a short delay if the anchor can't be found. Live
        testing found the anchor OCR occasionally misses on a single
        attempt even though a screenshot taken moments later (same
        underlying state, re-captured) succeeds -- a transient
        rendering/capture-timing issue, not a fundamentally undetectable
        state. Retrying is a standard, justified mitigation for exactly
        this class of flakiness rather than failing on the first miss.
        Returns (image, offset) -- offset is None only if every attempt
        failed. See docs/xactimate-lookup.md Phase 4.4."""
        image = None
        offset = None
        for attempt in range(attempts):
            image = self._capture_client_image(hwnd)
            offset = self._anchor_offset(image)
            if offset is not None:
                return image, offset
            if attempt < attempts - 1:
                time.sleep(delay_s)
        return image, None

    def _shifted_anchor(self, name: str, offset: tuple[int, int]) -> tuple[int, int, int, int]:
        l, t, r, b = _ANCHORS[name]
        dx, dy = offset
        return (l + dx, t + dy, r + dx, b + dy)

    def _count_grid_rows(self, image, offset: tuple[int, int]) -> int:
        """OCR's the '#' column beneath the grid header, counting
        distinct rows with numeric content -- used for before/after
        item-count mutation checks and to locate the most-recently-added
        row (assumed appended last, matching every row addition observed
        live).

        Serious, repeatedly-live-caught bug fixed here (see
        docs/xactimate-lookup.md Phase 4.4): the same column-gridline
        OCR bleed found elsewhere in this adapter (a stray "|" character)
        also affects the '#' column, producing text like "406 |" for a
        real, single-digit-only row number. The original `line.strip().
        isdigit()` check rejected any line with that trailing artifact
        outright, undercounting a real row as 0 -- which made
        cancel_current_item() silently return early ("nothing to
        clean up") on a grid that was NOT actually empty, reporting
        success without ever attempting a delete. This was caught only
        by repeatedly observing a "cleaned up" adapter call followed by
        the row still being visibly present on screen. Fixed by
        extracting the leading digit run from each line instead of
        requiring the whole line to be clean digits.

        Second, more serious bug (Phase 4.5): at 4+ rows, `--psm 6`
        (a "uniform block of text" assumption) misreads this narrow
        (~32px-wide) numeric column badly and non-randomly -- e.g. a
        real, clearly-legible "422" consistently read as "a2" or "ry",
        silently undercounting the grid by one or more rows. This
        directly caused a live wrong-row mutation: `enter_quantity()`
        computed the "last row" position from an undercounted total
        and entered a quantity into an existing, already-correct row
        instead of the newly-selected one, silently overwriting its
        value. `--psm 11` ("sparse text, no layout assumed") plus a 2x
        upscale reads every row correctly in the same live state where
        `--psm 6` failed -- swept scale 1x/2x/4x x five PSM modes
        before finding this combination. See
        docs/xactimate-lookup.md Phase 4.5."""
        header = self._shifted_anchor("grid_header", offset)
        col_l, col_r = _GRID_COLUMNS["number"]
        dx, dy = offset
        col_l, col_r = col_l + dx, col_r + dx
        # scan a generous band below the header for numeric row labels
        crop_box = (col_l, header[3], col_r, header[3] + 400)
        crop = image.crop(crop_box)
        crop = crop.resize((crop.width * 2, crop.height * 2))
        text = self._ocr_text(crop, psm=11)
        import re

        rows = [line for line in text.splitlines() if re.match(r"^\s*\d+", line)]
        return len(rows)

    # ------------------------------------------------------------------
    # XactimateAdapter contract
    # ------------------------------------------------------------------

    def verify_application(self) -> bool:
        try:
            found = self._find_main_window()
        except Exception:
            return False
        if found is None:
            return False
        self._main_hwnd = found[0]
        return True

    def verify_project(self) -> bool:
        try:
            found = self._find_main_window()
        except Exception:
            return False
        if found is None:
            return False
        hwnd, title = found
        self._main_hwnd = hwnd
        return title.strip().lower() == self.expected_project_name.strip().lower()

    def _ensure_main_window(self) -> int:
        if self._main_hwnd is None:
            found = self._find_main_window()
            if found is None:
                raise AdapterError("Xactimate main window not found.")
            self._main_hwnd = found[0]
        return self._main_hwnd

    def _reset_scroll_state(self) -> None:
        """Clicking the always-visible 'Items' tab (outside the
        scrollable content pane) re-selects the current tab and, per
        live testing, returns the content pane to its top scroll
        position -- the state _ANCHORS was calibrated against."""
        hwnd = self._ensure_main_window()
        l, t, r, b = _ANCHORS["items_tab"]
        self._click_client(hwnd, (l + r) // 2, (t + b) // 2)
        time.sleep(0.3)

    def focus_search(self) -> None:
        hwnd = self._ensure_main_window()
        if not self._force_foreground(hwnd):
            raise AdapterError("Could not bring Xactimate window to the foreground.")
        self._reset_scroll_state()
        l, t, r, b = _ANCHORS["search_box"]
        self._click_client(hwnd, (l + r) // 2, (t + b) // 2)
        time.sleep(0.2)

    def clear_search(self) -> None:
        self._select_all_and_delete()
        self._current_query = None
        self._last_dropdown_hwnd = None
        self._last_dropdown_rows = []

    def search_by_description(self, phrase: str) -> None:
        self._current_query = phrase
        self._type_keybdevent(phrase)

    def search_by_category_selector(self, category: str, selector: str) -> None:
        query = f"{category}{selector}"
        self._current_query = query
        self._type_keybdevent(query)

    def capture_dropdown(self):
        """Waits for the separate top-level popup window to appear,
        then does ONE raw UI Automation walk of it. Never trusts a
        cached popup handle from a previous search."""
        deadline = time.monotonic() + self.dropdown_timeout_s
        dropdown_hwnd = None
        while time.monotonic() < deadline:
            dropdown_hwnd = self._find_dropdown_window()
            if dropdown_hwnd is not None:
                break
            time.sleep(0.3)

        if dropdown_hwnd is None:
            raise PopupNotFoundError(f"No results popup appeared within {self.dropdown_timeout_s}s for query {self._current_query!r}.")

        self._last_dropdown_hwnd = dropdown_hwnd
        raw_rows = self._read_dropdown_rows(dropdown_hwnd)
        self._last_dropdown_rows = raw_rows
        return raw_rows

    def _read_dropdown_rows(self, dropdown_hwnd: int) -> list[_RawDropdownRow]:
        uia, UIA = self._uia()
        element = uia.ElementFromHandle(dropdown_hwnd)
        walker = uia.RawViewWalker
        scrollviewer = walker.GetFirstChildElement(element)
        if scrollviewer is None:
            return []

        rows: list[_RawDropdownRow] = []
        item = walker.GetFirstChildElement(scrollviewer)
        idx = 0
        while item:
            try:
                if item.CurrentControlType == UIA.UIA_ListItemControlTypeId:
                    texts: list[str] = []
                    child = walker.GetFirstChildElement(item)
                    while child:
                        try:
                            texts.append(child.CurrentName)
                        except Exception:
                            pass
                        try:
                            child = walker.GetNextSiblingElement(child)
                        except Exception:
                            break
                    if len(texts) >= 3:
                        rect = item.CurrentBoundingRectangle
                        rows.append(
                            _RawDropdownRow(
                                code_text=texts[0],
                                description_text=texts[1],
                                price_text=texts[2],
                                row_position=idx,
                                popup_hwnd=dropdown_hwnd,
                                rect_at_capture=(rect.left, rect.top, rect.right, rect.bottom),
                            )
                        )
                        idx += 1
            except Exception:
                pass
            try:
                item = walker.GetNextSiblingElement(item)
            except Exception:
                break
        return rows

    def parse_dropdown(self, raw) -> list[DropdownResult]:
        results = []
        for row in raw:
            category, selector = _split_category_selector(row.code_text)
            results.append(
                DropdownResult(
                    raw_text=f"{row.code_text} {row.description_text}",
                    row_position=row.row_position,
                    category=category,
                    selector=selector,
                    description=row.description_text,
                    item_number=None,
                    extraction_confidence=1.0,  # exact UI Automation text, not OCR
                )
            )
        return results

    def select_candidate(self, candidate: DropdownResult) -> None:
        """Never reuses the rect captured at parse time. Re-locates the
        popup, re-reads its rows fresh, verifies the candidate's text
        still matches, re-reads THAT row's live bounding rectangle, and
        only then clicks its center."""
        if self._last_dropdown_hwnd is None:
            raise PopupNotFoundError("select_candidate() called with no prior capture_dropdown().")

        win32gui = self._win32gui()
        try:
            still_open = win32gui.IsWindow(self._last_dropdown_hwnd)
        except Exception:
            still_open = False
        if not still_open:
            raise PopupNotFoundError("Results popup closed before selection could occur.")

        fresh_rows = self._read_dropdown_rows(self._last_dropdown_hwnd)
        match = None
        for row in fresh_rows:
            category, selector = _split_category_selector(row.code_text)
            if category == candidate.category and selector == candidate.selector:
                match = row
                break

        if match is None:
            raise StaleCandidateError(
                f"Candidate {candidate.category}/{candidate.selector} is no longer present in the "
                f"live popup -- refusing to click a stale/guessed position."
            )

        hwnd = self._ensure_main_window()
        before_img, offset = self._capture_and_locate(hwnd)
        self._last_selected_row_count_before = (
            self._count_grid_rows(before_img, offset) if offset is not None else None
        )

        # re-read the row's rectangle live, immediately before clicking --
        # never the rect captured during parse_dropdown().
        uia, UIA = self._uia()
        element = uia.ElementFromHandle(match.popup_hwnd)
        walker = uia.RawViewWalker
        scrollviewer = walker.GetFirstChildElement(element)
        target_elem = None
        item = walker.GetFirstChildElement(scrollviewer) if scrollviewer is not None else None
        while item:
            child = walker.GetFirstChildElement(item)
            code_text = child.CurrentName if child else None
            if code_text == match.code_text:
                target_elem = item
                break
            try:
                item = walker.GetNextSiblingElement(item)
            except Exception:
                break

        if target_elem is None:
            raise StaleCandidateError("Candidate row vanished between text verification and rectangle read.")

        rect = target_elem.CurrentBoundingRectangle
        cx = (rect.left + rect.right) // 2
        cy = (rect.top + rect.bottom) // 2
        self._click_screen(cx, cy)
        time.sleep(1.0)

        # Live-discovered: re-selecting a CAT/SEL that already exists in the
        # active group pops a real "Duplicate Item(s)" modal ("SFG GUTA
        # already exists in UTILITY_ROO2, Continue?", Yes/No) instead of
        # silently adding/updating anything. Per the adapter contract this
        # is always a hard stop -- never guessed through, never
        # auto-dismissed here. recover() is what presses Escape afterward.
        if self._unexpected_dialog_present():
            raise UnexpectedDialogError(
                f"Unexpected dialog appeared after selecting {candidate.category}/{candidate.selector} "
                f"(observed live: Xactimate's own 'Duplicate Item(s)' confirmation when the candidate "
                f"already exists in the active group)."
            )

        self._last_selected = candidate

    def _unexpected_dialog_present(self) -> bool:
        win32gui = self._win32gui()

        def cb(hwnd, acc):
            try:
                cls = win32gui.GetClassName(hwnd)
                title = win32gui.GetWindowText(hwnd)
                visible = win32gui.IsWindowVisible(hwnd)
            except Exception:
                return True
            if visible and _APP_CLASS_MARKER in cls and title not in ("", "Loading", self.expected_project_name):
                acc.append(hwnd)
            return True

        found: list[int] = []
        win32gui.EnumWindows(cb, found)
        return bool(found)

    def _last_row_geometry(self, image, offset: tuple[int, int]) -> tuple[int, int] | None:
        """Returns (row_count, last_row_top_y) or None if the grid is
        empty or couldn't be located. Anchored on grid_row_1, NOT the
        header's bottom edge -- live testing found a real ~9px gap
        between the header and the first data row that a header-bottom-
        relative calculation silently ignored, misaligning every crop
        by that amount (a real bug caught by comparing OCR output
        against the actual screenshot, not assumed away)."""
        row_count = self._count_grid_rows(image, offset)
        if row_count == 0:
            return None
        row_1 = self._shifted_anchor("grid_row_1", offset)
        last_row_top = row_1[1] + (row_count - 1) * _GRID_ROW_HEIGHT
        return row_count, last_row_top

    def read_populated_fields(self) -> PopulatedFields:
        """Reads the most-recently-added grid row's own cells directly
        -- NOT the "Quick Entry" panel. Live testing found Quick Entry
        only re-syncs to a row when that row is explicitly clicked
        (e.g. by enter_quantity()'s own click on the Quantity cell); it
        stays blank immediately after select_candidate(). The grid row,
        by contrast, is already fully legible with no extra click and
        therefore no extra risk -- this is a deliberate deviation from
        the preference order in the original build spec (accessible
        controls > keyboard nav > OCR crops > committed-row as a
        *secondary* source), justified by what was actually observed
        live: the grid row is the lower-risk primary source here, not
        a fallback. Neither the grid nor Quick Entry has UI Automation
        peers (Phase 4.1 finding, unchanged), so this is still OCR,
        self-locating via the same 'Cat:'-anchored offset used
        everywhere else in this adapter to stay robust to scroll drift."""
        #: OCR on a small cell crop is not perfectly deterministic --
        #: live testing caught real per-read noise (a stray cursor/border
        #: artifact producing "SFG |" instead of "SFG", "G" misread as
        #: "S" producing "SUTA" instead of "GUTA", on an otherwise-correct
        #: selection). Reads three independent fresh captures and takes
        #: the per-field majority vote (see docs/xactimate-lookup.md
        #: Phase 4.3) rather than trusting a single OCR pass -- this is
        #: the "multiple-read agreement" strategy the original build spec
        #: names explicitly. Any field where all three reads disagree is
        #: returned as-is from the first read; the orchestrator's own
        #: populated_fields_mismatch check is what actually catches a
        #: genuinely wrong selection, so this is a reliability
        #: improvement, not a substitute for that safety check.
        reads = [self._read_populated_fields_once() for _ in range(3)]

        def majority(values):
            counts: dict[str | None, int] = {}
            for v in values:
                counts[v] = counts.get(v, 0) + 1
            best = max(counts.items(), key=lambda kv: kv[1])
            return best[0]

        return PopulatedFields(
            category=majority([r.category for r in reads]),
            selector=majority([r.selector for r in reads]),
            description=majority([r.description for r in reads]),
            unit=majority([r.unit for r in reads]),
            action=majority([r.action for r in reads]),
            item_number=None,  # not visible anywhere in the observed UI -- honestly None, never guessed
        )

    def _read_populated_fields_once(self) -> PopulatedFields:
        hwnd = self._ensure_main_window()
        image, offset = self._capture_and_locate(hwnd)
        if offset is None:
            raise AdapterError("Could not locate the grid ('Cat:' anchor) to read populated fields.")

        geom = self._last_row_geometry(image, offset)
        if geom is None:
            raise AdapterError("No grid row found to read populated fields from -- was select_candidate() called?")
        _row_count, row_top = geom
        dx = offset[0]

        def crop_col(col_name):
            col_l, col_r = _GRID_COLUMNS[col_name]
            return image.crop((col_l + dx, row_top, col_r + dx, row_top + _GRID_ROW_HEIGHT))

        # category/selector text in a STATIC (non-highlighted) grid row is
        # small enough (~9px tall) that native-resolution OCR corrupts it
        # ("RFG"->"RFC", "ARMVN"->"ARMVI") -- not caught by Stage 1's
        # single-highlighted-row testing, where Xactimate renders this
        # text larger. Same 4x-upscale fix already applied to
        # activity/unit below. See docs/xactimate-lookup.md Phase 4.4
        # Stage 3.
        # psm=7 (single line) misreads "RFG" as "RFC" on this crop size --
        # psm=6 (single uniform block) reads it correctly at every scale
        # tested (1x-8x); see docs/xactimate-lookup.md Phase 4.4 Stage 3.
        cat_crop = crop_col("category")
        cat_crop = cat_crop.resize((cat_crop.width * 4, cat_crop.height * 4))
        cat = self._ocr_text(cat_crop, psm=6)
        sel_crop = crop_col("selector")
        sel_crop = sel_crop.resize((sel_crop.width * 4, sel_crop.height * 4))
        sel = self._ocr_text(sel_crop)
        act_crop = crop_col("activity")
        act_crop = act_crop.resize((act_crop.width * 4, act_crop.height * 4))
        act = self._ocr_text(act_crop)
        desc = self._normalize_inch_mark(self._ocr_text(crop_col("description"), psm=6))
        # Unit renders as an active combobox (with a dropdown-arrow glyph
        # immediately to its right, text shifted ~13px left) if the row
        # happens to be highlighted -- but the real orchestrator call
        # order is select_candidate() -> read_populated_fields() ->
        # enter_quantity(), so this method normally runs against the
        # STATIC (unhighlighted) row, not the highlighted one. The
        # column boundary below targets the static-state position
        # (confirmed via OCR word-position measurement); 4x upscaling
        # is kept regardless of state since small grid text is
        # unreliable at native resolution either way. See
        # docs/xactimate-lookup.md Phase 4.4 Stage 3.
        unit_crop = crop_col("unit")
        # 4x misread "SQ" as "$Q" (the "S" gets confused for a dollar
        # sign at that scale); 6x reads it correctly across every PSM
        # tested. See docs/xactimate-lookup.md Phase 4.4 Stage 3.
        unit_crop = unit_crop.resize((unit_crop.width * 6, unit_crop.height * 6))
        unit = self._ocr_text(unit_crop)

        return PopulatedFields(
            category=cat or None,
            selector=sel or None,
            description=desc or None,
            unit=unit or None,
            action=act or None,
            item_number=None,
        )

    def enter_quantity(self, quantity: float) -> None:
        hwnd = self._ensure_main_window()

        # Live-caught (Phase 4.5): a single-shot read of the grid
        # immediately after select_candidate()'s click can transiently
        # undercount rows -- reproduced live: a correctly-added third
        # row was reported as "row count did not increase" (raising
        # AdapterError) even though the row was visibly present and
        # correctly counted a moment later. Same class of post-
        # mutation render/OCR settle-timing gap as
        # verify_quantity_committed() below; bounded polling replaces
        # what was previously a single-shot check-and-raise. See
        # docs/xactimate-lookup.md Phase 4.5.
        start = time.time()
        geom = None
        while True:
            image, offset = self._capture_and_locate(hwnd, attempts=1, delay_s=0)
            if offset is not None:
                geom = self._last_row_geometry(image, offset)
                if geom is not None:
                    row_count, _ = geom
                    if self._last_selected_row_count_before is None or row_count > self._last_selected_row_count_before:
                        break
            if self._unexpected_dialog_present():
                raise AdapterError("enter_quantity(): an unexpected dialog appeared while waiting for the new grid row.")
            if time.time() - start >= 3.0:
                if offset is None:
                    raise AdapterError("Could not locate the grid to enter quantity.")
                if geom is None:
                    raise AdapterError("No grid row found to enter quantity into.")
                raise AdapterError(
                    f"Expected a new grid row after selection but row count did not increase "
                    f"within 3.0s (last observed row_count={geom[0]}, "
                    f"expected > {self._last_selected_row_count_before})."
                )
            time.sleep(0.3)

        row_count, last_row_top = geom
        col_l, col_r = _GRID_COLUMNS["quantity"]
        dx = offset[0]
        qx = (col_l + col_r) // 2 + dx
        qy = last_row_top + _GRID_ROW_HEIGHT // 2

        self._click_client(hwnd, qx, qy)
        time.sleep(0.2)
        self._select_all_and_delete()

        qty_text = f"{quantity:g}"
        self._type_keybdevent(qty_text)
        time.sleep(0.2)
        VK_TAB = 0x09
        self._press_key(VK_TAB)
        # Live-caught: the grid re-render/recalculation (RCV, group
        # subtotal, etc.) triggered by committing a quantity edit isn't
        # instantaneous -- a screenshot taken too soon after can land
        # mid-transition, causing anchor detection to intermittently fail
        # for a caller that reads the grid immediately afterward. 0.5s
        # was observed to be too short at least once; extended for
        # margin. See docs/xactimate-lookup.md Phase 4.4.
        time.sleep(1.0)

    def read_quantity(self) -> float | None:
        """Not part of the abstract XactimateAdapter contract -- an
        adapter-specific helper used by the quantity validation trials
        and available to callers that want an independent read-back
        without going through read_populated_fields()."""
        hwnd = self._ensure_main_window()
        image, offset = self._capture_and_locate(hwnd)
        if offset is None:
            return None
        geom = self._last_row_geometry(image, offset)
        if geom is None:
            return None
        _row_count, last_row_top = geom
        col_l, col_r = _GRID_COLUMNS["quantity"]
        dx = offset[0]
        crop = image.crop((col_l + dx, last_row_top, col_r + dx, last_row_top + _GRID_ROW_HEIGHT))
        # Live-caught (Phase 4.3): at native resolution Tesseract can drop
        # a decimal point entirely (visually present but sub-pixel at this
        # crop size) -- "2.5" read back as "25" with no punctuation at
        # all, not a misread character regex could fix. Upscaling 4x
        # before OCR is a standard mitigation for exactly this failure
        # mode.
        #
        # Live-caught (Phase 4.5): that same 4x upscale can blur a SHORT
        # value (a single digit, e.g. "7") into nothing -- reproduced
        # directly: native resolution read "7" correctly across every PSM
        # tried, but the 4x-upscaled crop returned an empty string with
        # the same PSM. Neither scale is reliable alone, so both are
        # tried: the decimal-preserving upscaled reading is used only
        # when it actually contains a decimal point the native reading is
        # missing (the specific failure upscaling exists to fix);
        # otherwise the native reading is preferred, falling back to
        # upscaled only if native produced nothing at all. See
        # docs/xactimate-lookup.md Phase 4.5.
        text_native = self._ocr_text(crop).replace(",", "").strip()
        crop_upscaled = crop.resize((crop.width * 4, crop.height * 4))
        text_upscaled = self._ocr_text(crop_upscaled).replace(",", "").strip()
        if "." in text_upscaled and "." not in text_native:
            text = text_upscaled
        else:
            text = text_native or text_upscaled
        # OCR on a cell crop occasionally picks up a stray border/highlight
        # artifact as a leading non-numeric character (live-observed: "> 10.5"
        # for a plain "10.5") -- extract the numeric substring rather than
        # requiring the whole string to already be a clean float.
        import re

        match = re.search(r"-?\d+(?:\.\d+)?", text)
        if match is None:
            return None
        try:
            return float(match.group())
        except ValueError:
            return None

    def commit_item(self) -> None:
        VK_S = 0x53
        self._press_ctrl(VK_S)
        time.sleep(1.5)

    def verify_quantity_committed(
        self, expected_quantity: float, timeout_s: float = 5.0, interval_s: float = 0.25
    ) -> QuantityVerificationResult:
        """Not part of the abstract contract -- bounded-polling
        replacement for a single-shot ``read_quantity()`` call used to
        confirm a quantity actually landed after a grid-mutating
        action (selection, quantity entry, or commit).

        Live investigation (Phase 4.5) found a single-shot read taken
        immediately after such an action intermittently returns `None`
        -- reproduced directly: a fresh CAT/SEL selection, quantity
        entry, and *immediate* `read_quantity()` call (no intervening
        delay beyond `enter_quantity()`'s own settle sleep) returned
        `None` on the first live trial, even though the value was
        correctly present in the grid a moment later and every attempt
        thereafter. This is not a fixed, predictable delay -- polling
        with a bounded budget is the correct fix, not a longer sleep
        (a longer fixed sleep just moves the same race to a different,
        still-unbounded worst case).

        Polls `read_quantity()` at `interval_s` and terminates on the
        first of: the observed value equals `expected_quantity`
        (success), `_unexpected_dialog_present()` is true (wrong
        context -- aborts immediately rather than continuing to poll
        past something that needs a human), or `timeout_s` elapses
        (failure). Every attempt's elapsed time, whether a grid row was
        located at all, and the observed value are recorded in the
        returned result's `.samples` for diagnostics. See
        docs/xactimate-lookup.md Phase 4.5."""
        start = time.time()
        samples: list[tuple[float, bool, float | None]] = []
        attempts = 0
        observed: float | None = None
        while True:
            attempts += 1
            elapsed = time.time() - start
            if self._unexpected_dialog_present():
                return QuantityVerificationResult(
                    matched=False, stop_reason="wrong_context", expected=expected_quantity,
                    observed=observed, attempts=attempts, elapsed_s=elapsed, samples=samples,
                )
            hwnd = self._ensure_main_window()
            image, offset = self._capture_and_locate(hwnd, attempts=1, delay_s=0)
            row_found = offset is not None and self._last_row_geometry(image, offset) is not None
            observed = self.read_quantity() if row_found else None
            samples.append((round(elapsed, 3), row_found, observed))
            if observed == expected_quantity:
                return QuantityVerificationResult(
                    matched=True, stop_reason="matched", expected=expected_quantity,
                    observed=observed, attempts=attempts, elapsed_s=elapsed, samples=samples,
                )
            if elapsed >= timeout_s:
                return QuantityVerificationResult(
                    matched=False, stop_reason="timeout", expected=expected_quantity,
                    observed=observed, attempts=attempts, elapsed_s=elapsed, samples=samples,
                )
            time.sleep(interval_s)

    def _click_context_menu_item(self, anchor_x: int, anchor_y: int, item_text: str, max_width: int = 500, max_height: int = 700) -> bool:
        """Locates and clicks a context-menu item by its literal text,
        via a full DESKTOP screenshot (a context menu is a separate
        top-level window -- invisible to the client-area PrintWindow
        capture used everywhere else in this file).

        Live-caught (Phase 4.4 Stage 3): a FIXED PIXEL OFFSET into this
        menu is fundamentally unreliable, not just imprecise --
        "Undo Delete Line Item" appears/disappears depending on
        whether there's undo history, shifting every item below it by
        one row (~23px) between calls in the SAME session (confirmed:
        the offset measured immediately after a delete didn't match
        the offset measured immediately after a save). OCR-locating
        the actual text at click time is the robust fix; matches the
        LINE whose full text equals item_text exactly (case-
        insensitive) so "Delete" doesn't false-match inside "Undo
        Delete Line Item". Returns False (never raises) if no
        matching line is found, so the caller can fail safely rather
        than click a guessed position. See docs/xactimate-lookup.md
        Phase 4.4.

        Live-caught (Phase 4.5): the search region originally only
        looked below-and-right of the click point, assuming that's
        always where Windows renders a context menu. It doesn't --
        Windows flips the menu upward when there isn't enough room
        below the cursor (observed live on a 1920x1080 screen, a
        different resolution than Phase 4.4's session, where a
        right-click low enough in the window pushed the menu entirely
        above the click point). The search region is now centered on
        the click point, covering both directions, clamped to the
        virtual screen so ImageGrab never receives a negative or
        off-screen bbox.

        That larger, both-directions region exposed a second issue:
        `--psm 6` ("assume a uniform block of text") merges/loses
        individual menu lines once the crop is big enough to also
        contain the busy background grid/panel behind the menu --
        live-reproduced: the standalone "Delete" line vanished
        entirely from its output on a crop where "Undo Delete Line
        Item" (two lines away) still came through correctly. `--psm 4`
        ("assume a single column of text of variable sizes") reliably
        isolates "Delete" as its own line on the same crop where
        `--psm 6` lost it.

        Live-caught (Phase 4.5): even with both fixes above, a single
        OCR pass can still misread the word itself -- reproduced live:
        "Delete" read as "betete" on this specific crop, caused by the
        menu's semi-transparent text blending with the busy background
        window content directly behind it at this exact pixel
        position. This is a STABLE misread, not per-attempt noise --
        confirmed by re-grabbing and re-OCRing the same live state
        three times and getting "betete" all three times, so retrying
        the capture alone does not fix it (retry is kept regardless,
        since a genuinely transient misread is a separate, real
        failure mode this file has hit elsewhere).
        The real fix: single-word menu lines within a small edit
        distance of the target are accepted as a match too, alongside
        an exact match. A plain fuzzy-ratio match was tried first and
        rejected -- "betete" vs. "delete" scores similarly low to
        "undo delete line item" vs. "delete" on difflib's ratio, so
        ratio alone can't safely distinguish the real misread from the
        line that must never match. Levenshtein edit distance does:
        "betete" is 2 edits from "delete", while every other real
        single-word item in this menu (including "select", the
        closest) is >= 3 edits away, and multi-word lines are excluded
        by the single-word restriction regardless of distance -- so
        "undo delete line item" can never match no matter how close
        any individual word is. See docs/xactimate-lookup.md Phase
        4.5."""
        from PIL import ImageGrab

        pytesseract = self._pytesseract()
        user32 = self._win32()[0].windll.user32
        screen_w = user32.GetSystemMetrics(0)
        screen_h = user32.GetSystemMetrics(1)
        left = max(0, anchor_x - max_width)
        top = max(0, anchor_y - max_height)
        right = min(screen_w, anchor_x + max_width)
        bottom = min(screen_h, anchor_y + max_height)
        crop_box = (left, top, right, bottom)
        target = item_text.strip().lower()

        for attempt in range(3):
            shot = ImageGrab.grab(bbox=crop_box)
            data = pytesseract.image_to_data(shot, config="--psm 4", output_type=pytesseract.Output.DICT)

            lines: dict[tuple[int, int, int], list[int]] = {}
            for i, text in enumerate(data["text"]):
                if not text.strip():
                    continue
                key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
                lines.setdefault(key, []).append(i)

            for indices in lines.values():
                line_text = " ".join(data["text"][i].strip() for i in indices).strip().lower()
                is_exact = line_text == target
                is_close_single_word = " " not in line_text and _levenshtein_distance(line_text, target) <= 2
                if not (is_exact or is_close_single_word):
                    continue
                lefts = [data["left"][i] for i in indices]
                rights = [data["left"][i] + data["width"][i] for i in indices]
                tops = [data["top"][i] for i in indices]
                bottoms = [data["top"][i] + data["height"][i] for i in indices]
                cx = left + (min(lefts) + max(rights)) // 2
                cy = top + (min(tops) + max(bottoms)) // 2
                self._click_screen(cx, cy)
                return True
            if attempt < 2:
                time.sleep(0.3)
        return False

    def cancel_current_item(self) -> None:
        """Not part of the abstract contract -- used by the
        non-destructive and assisted-selection trials to remove a
        just-selected row WITHOUT ever calling commit_item(). Live
        investigation found the Delete key alone does NOT remove a
        grid row (an earlier version of this method wrongly assumed it
        did, and its row-count check had a bug that let it silently
        report success without actually deleting anything -- see
        docs/xactimate-lookup.md Phase 4.3). The real mechanism is the
        row's right-click context menu's "Delete" item."""
        hwnd = self._ensure_main_window()
        image, offset = self._capture_and_locate(hwnd)
        if offset is None:
            raise AdapterError("Could not locate the grid to cancel the current item.")
        geom = self._last_row_geometry(image, offset)
        if geom is None:
            return
        row_count_before, last_row_top = geom

        col_l, col_r = _GRID_COLUMNS["description"]
        dx = offset[0]
        row_x = (col_l + col_r) // 2 + dx
        row_y = last_row_top + _GRID_ROW_HEIGHT // 2

        ctypes, _ = self._win32()
        user32 = ctypes.windll.user32
        ox, oy = self._get_client_origin(hwnd)
        screen_x, screen_y = ox + row_x, oy + row_y
        user32.SetCursorPos(screen_x, screen_y)
        time.sleep(0.1)
        user32.mouse_event(0x0008, 0, 0, 0, 0)  # MOUSEEVENTF_RIGHTDOWN
        time.sleep(0.05)
        user32.mouse_event(0x0010, 0, 0, 0, 0)  # MOUSEEVENTF_RIGHTUP
        # Live-caught: 0.4s was sometimes too short for the context menu
        # to be fully rendered and interactive before the follow-up
        # click, causing that click to land on the wrong thing (silently
        # missing "Delete" and hitting whatever was underneath instead)
        # -- manual, deliberately-paced testing at ~1s between right-click
        # and the follow-up click was reliable every time; this was not.
        # See docs/xactimate-lookup.md Phase 4.4.
        time.sleep(1.0)

        if not self._click_context_menu_item(screen_x, screen_y, "Delete"):
            # Best-effort: dismiss whatever menu is open rather than
            # leaving it hanging over the next call.
            self._press_key(0x1B)  # VK_ESCAPE
            raise AdapterError("cancel_current_item(): could not locate the 'Delete' context-menu item.")
        time.sleep(1.2)  # empirically: 0.5s was too short and produced a false-negative verification once

        # Live-caught false-positive bug (see docs/xactimate-lookup.md
        # Phase 4.4): if the click at _CONTEXT_MENU_DELETE_OFFSET misses
        # "Delete" and instead lands on something that opens a different
        # window (observed live: an unrelated "P.L. Categories" picker),
        # that window can visually obscure the grid row at the moment of
        # the post-click capture, making row_count_after read as lower
        # than it really is -- a false "success" that doesn't actually
        # delete anything. Checking for an unexpected window FIRST, and
        # treating its mere presence as a hard failure (not just closing
        # it and re-checking), catches this rather than trusting a
        # row-count read that could have been taken while occluded.
        if self._unexpected_dialog_present():
            self.close_transient_dialogs()
            raise AdapterError(
                "cancel_current_item(): an unexpected window appeared after the context-menu click "
                "(the click likely missed 'Delete') -- refusing to trust the row count until this is resolved."
            )

        image_after, offset_after = self._capture_and_locate(hwnd)
        row_count_after = self._count_grid_rows(image_after, offset_after) if offset_after is not None else None
        if row_count_after is None or row_count_after >= row_count_before:
            raise AdapterError(
                f"cancel_current_item(): row count did not decrease (before={row_count_before}, after={row_count_after})."
            )

    def capture_evidence(self) -> str:
        hwnd = self._ensure_main_window()
        image = self._capture_client_image(hwnd)
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d-%H%M%S")
        safe_query = "".join(c if c.isalnum() else "_" for c in (self._current_query or "none"))[:60]
        path = self.evidence_dir / f"{ts}_{safe_query}.png"
        image.save(path)
        return str(path)

    def close_transient_dialogs(self) -> bool:
        """Not part of the abstract contract -- checks for any owned
        window that isn't the main window, the results popup, or the
        transient 'Loading' overlay, and closes it with Escape if
        found. Returns True if a dialog was found and an Escape was
        sent, False if nothing needed closing. The one real dialog
        observed live (the "Duplicate Item(s)" Yes/No confirmation) was
        confirmed dismissable by clicking its "No" button directly;
        whether Escape maps to that same Cancel-equivalent choice on
        this specific dialog was not independently re-verified this
        session -- see docs/xactimate-lookup.md Phase 4.3 for the
        exact trial this is based on."""
        if not self._unexpected_dialog_present():
            return False
        VK_ESCAPE = 0x1B
        self._press_key(VK_ESCAPE)
        time.sleep(0.5)
        return True

    def recover(self) -> None:
        """Best-effort, never raises. Escape (safe per live testing --
        confirmed to close the results popup without side effects
        across every trial), clear transient state, re-verify the
        expected project is still active."""
        try:
            self.close_transient_dialogs()
        except Exception:
            pass
        try:
            VK_ESCAPE = 0x1B
            self._press_key(VK_ESCAPE)
            time.sleep(0.3)
        except Exception:
            pass
        self._last_dropdown_hwnd = None
        self._last_dropdown_rows = []
        self._last_selected = None
        self._current_query = None

    def get_adapter_diagnostics(self) -> dict:
        """Not part of the abstract contract -- a read-only status
        snapshot, analogous in spirit to the existing `automation
        diagnostics` CLI command (Phase 4.0), but adapter-scoped."""
        found = None
        try:
            found = self._find_main_window()
        except Exception:
            pass
        foreground = False
        if found is not None:
            ctypes, _ = self._win32()
            user32 = ctypes.windll.user32
            foreground = user32.GetForegroundWindow() == found[0]
        dropdown_hwnd = None
        try:
            dropdown_hwnd = self._find_dropdown_window()
        except Exception:
            pass
        return {
            "main_window_found": found is not None,
            "main_window_hwnd": found[0] if found else None,
            "main_window_title": found[1] if found else None,
            "project_matches": (found[1].strip().lower() == self.expected_project_name.strip().lower()) if found else False,
            "foreground": foreground,
            "dropdown_open": dropdown_hwnd is not None,
            "supports_live_execution": self.supports_live_execution,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
