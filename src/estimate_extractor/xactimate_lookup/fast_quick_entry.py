"""Isolated Phase 3 keyboard-only Quick Entry benchmark.

This module is deliberately not imported by the production runner. The CAT ->
SEL -> Tab x3 -> quantity -> Enter sequence is the specification here.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import mean, median
from typing import Protocol, Sequence


@dataclass(frozen=True, slots=True)
class FastEntryItem:
    category: str
    selector: str
    quantity: float
    source_line_id: str | None = None
    #: Tab presses between selector and quantity. 3 for the standard
    #: Cat->Sel->Act->Desc->Qty layout; 2 for a catalog identity with no
    #: traversable Act control (Cat->Sel->Desc->Qty).
    quantity_tab_count: int = 3


@dataclass(frozen=True, slots=True)
class FastBidItem:
    description: str
    quantity: float
    source_line_id: str | None = None


@dataclass(slots=True)
class FastEntryTiming:
    category: str
    selector: str
    quantity: float
    cat_start: float
    sel_entry: float
    quantity_entry: float
    submit: float
    ready_for_next: float
    total_item_seconds: float


class KeyboardIO(Protocol):
    def type_text(self, text: str) -> None: ...
    def replace_text(self, text: str) -> None: ...
    def press_tab(self) -> None: ...
    def press_enter(self) -> None: ...


class WindowsKeyboardIO:
    """Unpaced key events: no inherited search delays and no fixed sleeps."""

    def __init__(self, *, key_hold_seconds: float = 0.0) -> None:
        import ctypes
        self.user32 = ctypes.windll.user32
        self.key_hold_seconds = key_hold_seconds

    def type_text(self, text: str) -> None:
        key_up = 0x0002
        for character in text:
            scan = self.user32.VkKeyScanW(ord(character))
            vk, modifiers = scan & 0xFF, (scan >> 8) & 0xFF
            if modifiers & 1:
                self.user32.keybd_event(0x10, 0, 0, 0)
            self.user32.keybd_event(vk, 0, 0, 0)
            if self.key_hold_seconds:
                time.sleep(self.key_hold_seconds)
            self.user32.keybd_event(vk, 0, key_up, 0)
            if modifiers & 1:
                self.user32.keybd_event(0x10, 0, key_up, 0)

    def replace_text(self, text: str) -> None:
        """Replace the currently focused editor without inspecting its value."""
        key_up = 0x0002
        self.user32.keybd_event(0x11, 0, 0, 0)
        self.user32.keybd_event(0x41, 0, 0, 0)
        if self.key_hold_seconds:
            time.sleep(self.key_hold_seconds)
        self.user32.keybd_event(0x41, 0, key_up, 0)
        self.user32.keybd_event(0x11, 0, key_up, 0)
        self.user32.keybd_event(0x2E, 0, 0, 0)
        if self.key_hold_seconds:
            time.sleep(self.key_hold_seconds)
        self.user32.keybd_event(0x2E, 0, key_up, 0)
        self.type_text(text)

    def _press(self, vk: int) -> None:
        self.user32.keybd_event(vk, 0, 0, 0)
        if self.key_hold_seconds:
            time.sleep(self.key_hold_seconds)
        self.user32.keybd_event(vk, 0, 0x0002, 0)

    def press_tab(self) -> None:
        self._press(0x09)

    def press_enter(self) -> None:
        self._press(0x0D)


def execute_fast_items(
    keyboard: KeyboardIO, items: Sequence[FastEntryItem], *, clock=time.perf_counter,
    after_cat_seconds: float = 0.0, after_sel_seconds: float = 0.0, after_submit_seconds: float = 0.0,
) -> list[FastEntryTiming]:
    """Execute only the authoritative keyboard sequence, with no sleeps."""
    timings = []
    for item in items:
        started = clock()
        keyboard.type_text(item.category)
        if after_cat_seconds:
            time.sleep(after_cat_seconds)
        sel_entry = clock()
        keyboard.type_text(item.selector)
        if after_sel_seconds:
            time.sleep(after_sel_seconds)
        for _ in range(item.quantity_tab_count):
            keyboard.press_tab()
        quantity_entry = clock()
        keyboard.type_text(format(item.quantity, "g"))
        submit = clock()
        keyboard.press_enter()
        if after_submit_seconds:
            time.sleep(after_submit_seconds)
        ready = clock()
        timings.append(FastEntryTiming(
            category=item.category, selector=item.selector, quantity=item.quantity,
            cat_start=started, sel_entry=sel_entry, quantity_entry=quantity_entry,
            submit=submit, ready_for_next=ready, total_item_seconds=ready - started,
        ))
    return timings


def execute_fast_bid_item(
    keyboard: KeyboardIO, item: FastBidItem, *, clock=time.perf_counter,
) -> FastEntryTiming:
    """Execute one unresolved DOR/BIDITM item with no retained mode state."""
    started = clock()
    keyboard.type_text("DOR")
    sel_entry = clock()
    keyboard.type_text("BIDITM")
    keyboard.press_tab()  # BIDITM skips Act: selector -> Description.
    keyboard.replace_text(item.description)
    keyboard.press_tab()  # Description -> Quantity.
    quantity_entry = clock()
    keyboard.replace_text(format(item.quantity, "g"))
    submit = clock()
    keyboard.press_enter()
    ready = clock()
    return FastEntryTiming(
        category="DOR", selector="BIDITM", quantity=item.quantity,
        cat_start=started, sel_entry=sel_entry, quantity_entry=quantity_entry,
        submit=submit, ready_for_next=ready, total_item_seconds=ready - started,
    )


def summarize_timings(timings: Sequence[FastEntryTiming]) -> dict[str, float | int]:
    totals = [item.total_item_seconds for item in timings]
    return {
        "item_count": len(timings),
        "average_item_seconds": mean(totals) if totals else 0.0,
        "median_item_seconds": median(totals) if totals else 0.0,
        "minimum_item_seconds": min(totals) if totals else 0.0,
        "maximum_item_seconds": max(totals) if totals else 0.0,
    }


class WindowsFastQuickEntryBenchmark:
    """Coarse-preflight wrapper around the isolated keyboard primitive."""

    def __init__(self, *, expected_project: str, group: str, evidence_dir: Path) -> None:
        from .windows_adapter import WindowsXactimateAdapter
        self.expected_project = expected_project
        self.group = group
        self.evidence_dir = evidence_dir
        self.adapter = WindowsXactimateAdapter(expected_project, evidence_dir=evidence_dir)

    def preflight(self, *, create_group: bool = False) -> dict[str, bool | str]:
        if not self.adapter.verify_application() or not self.adapter.verify_project():
            raise RuntimeError("fast benchmark refused: expected Xactimate project is not positively verified")
        if self.adapter._unexpected_dialog_present() or self.adapter._find_dropdown_window() is not None:
            raise RuntimeError("fast benchmark refused: blocking dialog/dropdown is present")
        if create_group:
            self.adapter.ensure_group(self.group)
        self.adapter.select_group(self.group)
        if not self.adapter.verify_group(self.group, use_cache=False):
            raise RuntimeError("fast benchmark refused: intended group is not positively verified")
        focus = self.adapter._find_main_window()
        if focus is None:
            raise RuntimeError("fast benchmark refused: main window disappeared")
        hwnd, _title = focus
        image, offset = self.adapter._capture_and_locate(hwnd)
        if offset is None:
            raise RuntimeError("fast benchmark refused: Quick Entry/grid chrome could not be positively located")
        left, top, right, bottom = self.adapter._shifted_anchor("quick_entry_cat_value", offset)
        self.adapter._click_client(hwnd, (left + right) // 2, (top + bottom) // 2)
        if self.adapter._unexpected_dialog_present() or self.adapter._find_dropdown_window() is not None:
            raise RuntimeError("fast benchmark refused: dialog/dropdown appeared while establishing CAT focus")
        # Re-locate after the click: the validated CAT rectangle plus a
        # successful foreground click is the bounded positive focus proof.
        image2, offset2 = self.adapter._capture_and_locate(hwnd, attempts=1, delay_s=0)
        if offset2 is None or not self.adapter._force_foreground(hwnd):
            raise RuntimeError("fast benchmark refused: Quick Entry CAT focus could not be retained")
        return {"project_verified": True, "group_verified": True, "no_blocking_dialog": True, "cat_focus_established": True}

    def run(
        self, items: Sequence[FastEntryItem], *, create_group: bool = False,
        key_hold_seconds: float = 0.0, after_cat_seconds: float = 0.0,
        after_sel_seconds: float = 0.0, after_submit_seconds: float = 0.0,
    ) -> dict:
        if not 3 <= len(items) <= 5:
            raise ValueError("bounded fast benchmark requires 3 to 5 items")
        from .offline_catalog_mapper import XactimateCatalog
        catalog = XactimateCatalog.load()
        for item in items:
            if (item.category, item.selector) == ("DOR", "BIDITM"):
                raise ValueError("fast DOR/BIDITM is unsupported until its Description/Quantity/Price tab order is verified")
            if (item.category, item.selector) not in catalog.by_identity:
                raise ValueError(f"fast benchmark item is not in the authoritative catalog: {item.category}/{item.selector}")
        preflight = self.preflight(create_group=create_group)
        timings = execute_fast_items(
            WindowsKeyboardIO(key_hold_seconds=key_hold_seconds), items,
            after_cat_seconds=after_cat_seconds, after_sel_seconds=after_sel_seconds,
            after_submit_seconds=after_submit_seconds,
        )
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        report = {
            "mode": "experimental_fast_quick_entry_keyboard_only",
            "project": self.expected_project, "group": self.group,
            "sequence": "CAT -> SEL -> Tab x3 -> quantity -> Enter",
            "delays_seconds": {
                "key_hold": key_hold_seconds, "after_cat": after_cat_seconds,
                "after_sel": after_sel_seconds, "after_submit": after_submit_seconds,
            },
            "preflight": preflight,
            "items": [asdict(item) for item in timings],
            "summary": summarize_timings(timings),
            "per_item_ocr_or_row_reconciliation": False,
            "physical_submission_verification": "manual_visual_review_required",
        }
        (self.evidence_dir / "fast_quick_entry_benchmark.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        focus = self.adapter._find_main_window()
        if focus is not None:
            self.adapter._capture_client_image(focus[0]).save(self.evidence_dir / "post_batch.png")
        return report
