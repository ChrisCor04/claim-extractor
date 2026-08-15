from __future__ import annotations

import pytest

from estimate_extractor.xactimate_lookup.fast_quick_entry import (
    FastBidItem, FastEntryItem, WindowsFastQuickEntryBenchmark,
    execute_fast_bid_item, execute_fast_items, summarize_timings,
)


class RecordingKeyboard:
    def __init__(self):
        self.events = []

    def type_text(self, text): self.events.append(("type", text))
    def replace_text(self, text): self.events.append(("replace", text))
    def press_tab(self): self.events.append(("tab",))
    def press_enter(self): self.events.append(("enter",))


class Clock:
    def __init__(self): self.value = 0.0
    def __call__(self):
        self.value += 0.01
        return self.value


def test_exact_authoritative_sequence_has_three_tabs_and_no_delay_calls():
    keyboard = RecordingKeyboard()
    timing = execute_fast_items(keyboard, [FastEntryItem("RFG", "DRIP", 12.5)], clock=Clock())
    assert keyboard.events == [
        ("type", "RFG"), ("type", "DRIP"),
        ("tab",), ("tab",), ("tab",),
        ("type", "12.5"), ("enter",),
    ]
    assert timing[0].cat_start < timing[0].sel_entry < timing[0].quantity_entry < timing[0].submit < timing[0].ready_for_next


def test_multiple_items_execute_back_to_back_and_report_timings():
    keyboard = RecordingKeyboard()
    items = [FastEntryItem("RFG", "IWS", 1), FastEntryItem("SDG", "VINYL", 2)]
    timings = execute_fast_items(keyboard, items, clock=Clock())
    assert keyboard.events[6:8] == [("enter",), ("type", "SDG")]
    summary = summarize_timings(timings)
    assert summary["item_count"] == 2
    assert summary["average_item_seconds"] > 0
    assert summary["median_item_seconds"] > 0


def test_biditem_uses_calibrated_unresolved_only_sequence():
    keyboard = RecordingKeyboard()
    execute_fast_bid_item(
        keyboard, FastBidItem("Chair - Pillow / Pad - Standard grade", 1.75), clock=Clock(),
    )
    assert keyboard.events == [
        ("type", "DOR"), ("type", "BIDITM"), ("tab",),
        ("replace", "Chair - Pillow / Pad - Standard grade"),
        ("tab",), ("replace", "1.75"), ("enter",),
    ]


def test_normal_sequence_never_replaces_description():
    keyboard = RecordingKeyboard()
    execute_fast_items(keyboard, [FastEntryItem("RFG", "DRIP", 7.25)], clock=Clock())
    assert keyboard.events == [
        ("type", "RFG"), ("type", "DRIP"),
        ("tab",), ("tab",), ("tab",), ("type", "7.25"), ("enter",),
    ]
    assert not any(event[0] == "replace" for event in keyboard.events)


def test_fast_item_requires_real_normal_identity_shape():
    item = FastEntryItem("DOR", "BIDITM", 1)
    assert item.selector == "BIDITM"  # data model can preserve it
    # Live BIDITM execution is deliberately not exposed as a special sequence.
    assert not hasattr(item, "description_tab_count")


def test_live_wrapper_rejects_bid_item_before_preflight():
    runner = object.__new__(WindowsFastQuickEntryBenchmark)
    with pytest.raises(ValueError, match="fast DOR/BIDITM is unsupported"):
        runner.run([FastEntryItem("DOR", "BIDITM", 1)] * 3)
