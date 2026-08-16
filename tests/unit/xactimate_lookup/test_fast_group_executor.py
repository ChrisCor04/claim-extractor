from __future__ import annotations

import json

import pytest

from estimate_extractor.xactimate_lookup.fast_group_executor import (
    FAST_KEY_HOLD_SECONDS, compile_executable_group_plan, execute_group_first_plan,
    exact_planned_group_rows, normalize_planned_group_identity, reconcile_complete_group_inventory,
    GroupInventory, GroupInventoryEntry, WindowsGroupBatchUI,
)


def _shadow():
    def row(line_id, group, cat, sel, *, resolution="resolved", quantity=1):
        return {
            "line_item_id": line_id, "group": group, "original_description": line_id,
            "quantity": quantity, "unit": "EA", "source_pricing": {"unit_price": 2.5},
            "source_action": "remove_replace", "resolution": resolution,
            "category": cat if resolution == "resolved" else None,
            "selector": sel if resolution == "resolved" else None,
            "execution_category": cat, "execution_selector": sel,
            "catalog_description": line_id, "catalog_search_text": line_id,
            "score": .9, "margin": .4, "reason": "test", "top_candidates": [],
        }
    return {
        "schema_version": "phase3-shadow-quick-entry-plan-v2", "project": "TEST",
        "group_first_future_layout": [
            {"group": "A", "line_item_ids": ["a1", "a2"]},
            {"group": "B", "line_item_ids": ["b1", "b2"]},
        ],
        "items": [
            row("a1", "A", "RFG", "DRIP"), row("a2", "A", "RFG", "IWS"),
            row("b1", "B", "SDG", "VINYL"),
            row("b2", "B", "DOR", "BIDITM", resolution="ambiguous"),
        ],
    }


class UI:
    def __init__(self, fail=None): self.events, self.fail = [], fail; self.keyboard = self
    def verify_project_and_no_modal(self, project):
        self.events.append(("preflight", project))
        if self.fail == "preflight": raise RuntimeError("modal")
    def normalize_window(self): self.events.append(("normalize-window",)); return {"ok": True}
    def prepare_group_creation(self, groups): self.events.append(("inventory-initial", tuple(groups))); return "initial"
    def create_group(self, group):
        self.events.append(("create", group))
        return {"creation_state": "created", "verification_method": "bounded-new-row"}
    def verify_all_groups_created(self, groups):
        self.events.append(("verify-all", tuple(groups)))
        if self.fail == "verify-all": raise RuntimeError("group missing")
        return "fresh-all"
    def select_group_lightweight(self, group): self.events.append(("select", group)); return "fresh-selection"
    def focus_quick_entry_cat(self): self.events.append(("focus",)); return "fresh-focus"
    def assert_batch_settled(self): self.events.append(("settled",))
    def capture_group_evidence(self, group): self.events.append(("capture-group", group)); return f"{group}.png"
    def capture_final_evidence(self): self.events.append(("capture-final",)); return "final.png"
    def accept_expected_biditem_duplicate(self):
        self.events.append(("duplicate-poll",)); self.press_tab(); self.press_tab(); self.press_enter()
        return {"appearance_wait_seconds": .02, "acceptance_seconds": .02}
    def type_text(self, value): self.events.append(("type", value))
    def replace_text(self, value): self.events.append(("replace", value))
    def press_tab(self): self.events.append(("tab",))
    def press_enter(self): self.events.append(("enter",))


class Clock:
    def __init__(self): self.value = 0
    def __call__(self): self.value += .01; return self.value


def test_compiler_preserves_order_payload_diagnostics_and_biditem():
    plan = compile_executable_group_plan(_shadow())
    assert [group.group for group in plan.groups] == ["A", "B"]
    assert [item.line_item_id for group in plan.groups for item in group.items] == ["a1", "a2", "b1", "b2"]
    bid = plan.groups[1].bid_items[0]
    assert (bid.category, bid.selector, bid.execution_mode, bid.price) == ("DOR", "BIDITM", "requires_biditem_sequence", 2.5)
    assert json.loads(bid.mapper_diagnostics_json)["resolution"] == "ambiguous"


def test_compiler_rejects_missing_duplicate_or_ungrouped_rows():
    shadow = _shadow(); shadow["group_first_future_layout"][0]["line_item_ids"].append("a1")
    with pytest.raises(ValueError, match="missing or duplicate"): compile_executable_group_plan(shadow)
    shadow = _shadow(); shadow["group_first_future_layout"][1]["line_item_ids"].remove("b2")
    with pytest.raises(ValueError, match="ungrouped"): compile_executable_group_plan(shadow)


def test_all_groups_are_created_before_selection_and_hot_loop_is_keyboard_only():
    ui = UI()
    report = execute_group_first_plan(compile_executable_group_plan(_shadow()), ui, clock=Clock())
    assert ui.events.index(("create", "B")) < ui.events.index(("select", "A"))
    assert ui.events.index(("verify-all", ("A", "B"))) < ui.events.index(("select", "A"))
    first = ui.events.index(("type", "RFG"))
    assert ui.events[first:first + 7] == [
        ("type", "RFG"), ("type", "DRIP"), ("tab",), ("tab",), ("tab",), ("type", "1"), ("enter",),
    ]
    assert report["normal_item_count"] == 3 and report["bid_item_count"] == 1
    assert report["groups"][1]["bid_items"][0]["execution_status"] == "fast_biditem_executed"
    assert FAST_KEY_HOLD_SECONDS == .005
    architectural = [event for event in ui.events if event[0] in {"create", "verify-all", "select", "type"}]
    assert architectural[:4] == [
        ("create", "A"), ("create", "B"), ("verify-all", ("A", "B")), ("select", "A"),
    ]
    assert architectural.index(("select", "B")) > architectural.index(("type", "IWS"))
    assert ui.events.count(("inventory-initial", ("A", "B"))) == 1
    assert ui.events.count(("verify-all", ("A", "B"))) == 1


def test_preflight_failure_aborts_before_creation_or_input():
    ui = UI(fail="preflight")
    with pytest.raises(RuntimeError, match="modal"):
        execute_group_first_plan(compile_executable_group_plan(_shadow()), ui, clock=Clock())
    assert ui.events == [("preflight", "TEST")]


def test_missing_group_barrier_aborts_before_selection_or_input():
    ui = UI(fail="verify-all")
    with pytest.raises(RuntimeError, match="group missing"):
        execute_group_first_plan(compile_executable_group_plan(_shadow()), ui, clock=Clock())
    assert ("create", "A") in ui.events and ("create", "B") in ui.events
    assert not any(event[0] in {"select", "type"} for event in ui.events)


def test_no_mapper_or_catalog_is_accepted_by_live_executor_signature():
    # The compiled immutable plan and UI are the only live inputs.
    assert "mapper" not in execute_group_first_plan.__annotations__
    assert "catalog" not in execute_group_first_plan.__annotations__


def test_exact_planned_identity_tolerates_formatting_without_collapsing_names():
    assert normalize_planned_group_identity("P4A_0814") == "p4a0814"
    rows = ["TEST", "4 P4A.0814 = $88.27", "P4B 0814", "p4c_0814"]
    assert exact_planned_group_rows(rows, "P4A_0814") == [1]
    assert exact_planned_group_rows(rows, "P4B_0814") == [2]
    assert exact_planned_group_rows(rows, "P4C_0814") == [3]


def test_complete_inventory_reproduces_and_prevents_p4_name_collision():
    groups = ["P4A_0814", "P4B_0814", "P4C_0814"]
    with pytest.raises(RuntimeError, match="P4B_0814.*0 exact physical row"):
        reconcile_complete_group_inventory(["TEST", "P4A_0814"], groups)
    assert reconcile_complete_group_inventory(
        ["TEST", "P4A_0814", "P4B.0814", "P4C 0814"], groups,
    ) == {"P4A_0814": 1, "P4B_0814": 2, "P4C_0814": 3}


def test_complete_inventory_rejects_duplicate_rows_and_duplicate_planned_names():
    with pytest.raises(RuntimeError, match="2 exact physical row"):
        reconcile_complete_group_inventory(["P4A_0814", "P4A 0814"], ["P4A_0814"])
    with pytest.raises(RuntimeError, match="identities are empty or duplicate"):
        reconcile_complete_group_inventory(["P4A_0814"], ["P4A_0814", "p4a 0814"])


def test_population_routes_biditem_through_its_own_hot_loop():
    ui = UI()
    execute_group_first_plan(compile_executable_group_plan(_shadow()), ui, clock=Clock())
    bid = ui.events.index(("type", "DOR"))
    assert ui.events[bid:bid + 7] == [
        ("type", "DOR"), ("type", "BIDITM"), ("tab",),
        ("replace", "b2"), ("tab",), ("replace", "1"), ("enter",),
    ]


def test_biditem_then_multiple_normals_do_not_leak_navigation_mode():
    shadow = _shadow()
    shadow["group_first_future_layout"] = [{"group": "A", "line_item_ids": ["b2", "a1", "a2"]}]
    shadow["items"] = [shadow["items"][3], shadow["items"][0], shadow["items"][1]]
    for row in shadow["items"]: row["group"] = "A"
    ui = UI(); execute_group_first_plan(compile_executable_group_plan(shadow), ui, clock=Clock())
    start = ui.events.index(("type", "DOR"))
    assert ui.events[start:start + 14] == [
        ("type", "DOR"), ("type", "BIDITM"), ("tab",), ("replace", "b2"),
        ("tab",), ("replace", "1"), ("enter",),
        ("type", "RFG"), ("type", "DRIP"), ("tab",), ("tab",), ("tab",),
        ("type", "1"), ("enter",),
    ]
    assert ui.events[start + 14:start + 21] == [
        ("type", "RFG"), ("type", "IWS"), ("tab",), ("tab",), ("tab",),
        ("type", "1"), ("enter",),
    ]


def test_expected_biditem_duplicate_tracking_is_group_local_and_biditem_only():
    shadow = _shadow()
    bid_template = shadow["items"][3]
    bids = []
    for line_id, group in (("b1", "A"), ("b2", "A"), ("b3", "A"), ("b4", "B")):
        row = dict(bid_template); row.update(line_item_id=line_id, group=group, original_description=line_id)
        bids.append(row)
    normal1 = dict(shadow["items"][0]); normal1.update(line_item_id="n1", group="B")
    normal2 = dict(shadow["items"][0]); normal2.update(line_item_id="n2", group="B")
    shadow["items"] = [*bids, normal1, normal2]
    shadow["group_first_future_layout"] = [
        {"group": "A", "line_item_ids": ["b1", "b2", "b3"]},
        {"group": "B", "line_item_ids": ["b4", "n1", "n2"]},
    ]
    ui = UI(); report = execute_group_first_plan(compile_executable_group_plan(shadow), ui, clock=Clock())
    assert ui.events.count(("duplicate-poll",)) == 2
    assert len(report["groups"][0]["expected_biditem_duplicate_acceptances"]) == 2
    assert report["groups"][1]["expected_biditem_duplicate_acceptances"] == []
    second = [i for i, event in enumerate(ui.events) if event == ("type", "DOR")][1]
    assert ui.events[second:second + 11] == [
        ("type", "DOR"), ("type", "BIDITM"), ("tab",), ("replace", "b2"),
        ("tab",), ("replace", "1"), ("enter",), ("duplicate-poll",),
        ("tab",), ("tab",), ("enter",),
    ]
    # The first BIDITM in B and repeated ordinary RFG/DRIP rows never invoke it.
    b_select = ui.events.index(("select", "B"))
    assert ("duplicate-poll",) not in ui.events[b_select:]


def test_biditem_payload_preserves_source_and_nullable_price():
    bid = compile_executable_group_plan(_shadow()).groups[1].bid_items[0]
    assert (bid.category, bid.selector) == ("DOR", "BIDITM")
    assert bid.original_description == "b2" and bid.quantity == 1 and bid.unit == "EA" and bid.price == 2.5
    shadow = _shadow(); shadow["items"][3]["source_pricing"] = {}
    assert compile_executable_group_plan(shadow).groups[1].bid_items[0].price is None


def test_inventory_selection_uses_one_row_reread_not_full_tree_rescan():
    class Adapter:
        expected_project_name = "TEST"
        def verify_application(self): return True
        def verify_project(self): return True
        def _unexpected_dialog_present(self): return False
        def _find_dropdown_window(self): return None
        def _ensure_main_window(self): return 1
        def _force_foreground(self, hwnd): return True
        def _capture_client_image(self, hwnd): return object()
        def _locate_group_tree_header(self, image): return (10, 20, 30, 40)
        def _ocr_group_tree_row_text(self, image, header, index): return "P4B.0814"
        def _click_client(self, hwnd, *xy): self.clicked = xy
        def _group_tree_row_has_selection_boundary(self, image, header, index): return True
        def _anchor_offset(self, image): return (0, 0)
        def _items_search_pane_field(self, image): return (1, 1, 2, 2)
        def _win32gui(self):
            class W:
                @staticmethod
                def GetWindowRect(hwnd): return (0, 0, 100, 100)
            return W

    facade = object.__new__(WindowsGroupBatchUI)
    facade.adapter = Adapter()
    facade._inventory = GroupInventory(
        window_rect=(0, 0, 100, 100), header_rect=(10, 20, 30, 40),
        entries=(GroupInventoryEntry("p4b0814", "P4B_0814", 2, (50, 70)),),
    )
    assert facade.select_group_lightweight("P4B_0814").startswith("verified_inventory_row")
    assert facade.adapter.clicked == (50, 70)


def test_inventory_geometry_invalidation_prevents_stale_click():
    class Adapter:
        expected_project_name = "TEST"
        def verify_application(self): return True
        def verify_project(self): return True
        def _unexpected_dialog_present(self): return False
        def _find_dropdown_window(self): return None
        def _ensure_main_window(self): return 1
        def _force_foreground(self, hwnd): return True
        def _capture_client_image(self, hwnd): return object()
        def _locate_group_tree_header(self, image): return (11, 20, 30, 40)
        def _win32gui(self):
            class W:
                @staticmethod
                def GetWindowRect(hwnd): return (0, 0, 100, 100)
            return W
    facade = object.__new__(WindowsGroupBatchUI); facade.adapter = Adapter()
    facade._inventory = GroupInventory((0, 0, 100, 100), (10, 20, 30, 40), (
        GroupInventoryEntry("p4a0814", "P4A_0814", 1, (50, 50)),
    ))
    with pytest.raises(RuntimeError, match="geometry was invalidated"):
        facade.select_group_lightweight("P4A_0814")


def test_inventory_accepts_advisory_header_width_jitter_with_exact_origin():
    class Adapter:
        expected_project_name = "TEST"
        def verify_application(self): return True
        def verify_project(self): return True
        def _unexpected_dialog_present(self): return False
        def _find_dropdown_window(self): return None
        def _ensure_main_window(self): return 1
        def _force_foreground(self, hwnd): return True
        def _capture_client_image(self, hwnd): return object()
        def _locate_group_tree_header(self, image): return (270, 155, 301, 165)
        def _ocr_group_tree_row_text(self, image, header, index): return "ORBIT_ROOF_C"
        def _click_client(self, hwnd, *xy): pass
        def _group_tree_row_has_selection_boundary(self, image, header, index): return True
        def _anchor_offset(self, image): return (0, 0)
        def _items_search_pane_field(self, image): return (1, 1, 2, 2)
        def _win32gui(self):
            class W:
                @staticmethod
                def GetWindowRect(hwnd): return (0, 0, 1920, 1023)
            return W
    facade = object.__new__(WindowsGroupBatchUI); facade.adapter = Adapter()
    facade._inventory = GroupInventory((0, 0, 1920, 1023), (270, 155, 302, 165), (
        GroupInventoryEntry("orbitroofc", "ORBIT_ROOF_C", 1, (349, 206)),
    ))
    assert facade.select_group_lightweight("ORBIT_ROOF_C").startswith("verified_inventory_row")


def test_fresh_exact_group_row_cannot_satisfy_a_different_planned_group():
    class Adapter:
        def snapshot_group_names(self): return ["TEST", "P4A_0814"]
    facade = object.__new__(WindowsGroupBatchUI); facade.adapter = Adapter()
    assert facade._fresh_exact_group_row("P4A_0814") == (1, ["TEST", "P4A_0814"])
    assert facade._fresh_exact_group_row("P4B_0814") is None


# -- create_group(): fresh name-based reacquisition, no selection dependency --

class _FakeClock:
    """Advances instantly on sleep() -- lets bounded-deadline loops be
    tested without a real multi-second wait."""
    def __init__(self): self.now = 0.0
    def perf_counter(self): return self.now
    def sleep(self, seconds): self.now += seconds


class _CreateGroupAdapter:
    """Drives WindowsGroupBatchUI.create_group()'s full dialog-driven
    creation sequence against a controllable sequence of fresh
    snapshot_group_names() reads -- proving the post-dialog verification
    is a name-based reacquisition, not a selection-boundary/OCR read.
    Deliberately has no _group_tree_row_has_selection_boundary or
    _ocr_group_tree_row_text method at all: the new verification must
    never call either."""
    expected_project_name = "TEST"
    _GROUP_MENU_NEW_INDEX = 15

    def __init__(self, snapshot_sequence):
        self._snapshots = iter(snapshot_sequence)
        self._last_snapshot = None
        self._dialog_open = False
        self._click_count = 0
        self.evidence_dir = None

    def verify_application(self): return True
    def verify_project(self): return True
    def _unexpected_dialog_present(self): return False
    def _find_dropdown_window(self): return None
    def _ensure_main_window(self): return 1
    def _capture_client_image(self, hwnd): return object()
    def _locate_group_tree_header(self, image): return (10, 20, 30, 40)
    def _open_group_tree_context_menu(self, hwnd, header, row_index): return ["item"] * 20
    def _click_group_menu_item(self, items, index): self._dialog_open = True
    def _find_window_by_title(self, title):
        return 999 if (title == "New Group" and self._dialog_open) else None
    def _click_client(self, hwnd, *xy):
        self._click_count += 1
        if self._click_count == 2:  # the Attach/OK click closes the dialog
            self._dialog_open = False
    def _select_all_and_delete(self): pass
    def _type_keybdevent(self, text, char_interval_s=None): pass
    def snapshot_group_names(self):
        try:
            self._last_snapshot = next(self._snapshots)
        except StopIteration:
            pass  # keep returning the last value once the sequence is exhausted
        return self._last_snapshot


def _create_group_facade(adapter, evidence_dir):
    facade = object.__new__(WindowsGroupBatchUI)
    facade.adapter = adapter
    facade.calibration = type("Calibration", (), {"geometry": {
        "new_group_dialog_name_click": [1, 1], "new_group_dialog_attach_click": [2, 2],
    }})()
    facade._initial_rows = ["TEST"]
    facade._inventory = None
    adapter.evidence_dir = evidence_dir
    return facade


def test_creation_verification_tolerates_transient_incomplete_snapshot(tmp_path):
    adapter = _CreateGroupAdapter([
        ["TEST"],               # immediately after dialog closes -- not yet visible
        ["TEST"],               # still not settled
        ["TEST", "Exterior"],   # settled
    ])
    facade = _create_group_facade(adapter, tmp_path)
    detail = facade.create_group("Exterior")
    assert detail["creation_state"] == "created"
    assert detail["verification_method"] == "dialog_close_then_fresh_exact_name_reacquisition"
    assert detail["observed_row"] == 1
    assert detail["observed_display_name"] == "Exterior"
    assert detail["verification_attempts"] == 3


def test_creation_verification_never_depends_on_selection_boundary(tmp_path):
    # _CreateGroupAdapter deliberately has no selection-boundary or
    # per-row-OCR method at all -- if create_group() called either, this
    # would raise AttributeError instead of succeeding.
    adapter = _CreateGroupAdapter([["TEST", "Exterior"]])
    facade = _create_group_facade(adapter, tmp_path)
    detail = facade.create_group("Exterior")
    assert detail["creation_state"] == "created"


def test_creation_verification_tolerates_surrounding_ocr_noise(tmp_path):
    adapter = _CreateGroupAdapter([["TEST", "fej Exterior | Simila"]])
    facade = _create_group_facade(adapter, tmp_path)
    detail = facade.create_group("Exterior")
    assert detail["creation_state"] == "created"
    assert detail["observed_display_name"] == "fej Exterior | Simila"


def test_creation_verification_fails_closed_when_group_never_appears(tmp_path, monkeypatch):
    import estimate_extractor.xactimate_lookup.fast_group_executor as fge_module
    clock = _FakeClock()
    monkeypatch.setattr(fge_module.time, "perf_counter", clock.perf_counter)
    monkeypatch.setattr(fge_module.time, "sleep", clock.sleep)
    adapter = _CreateGroupAdapter([["TEST"]])  # never changes -- exhausts the bounded poll
    facade = _create_group_facade(adapter, tmp_path)
    with pytest.raises(RuntimeError, match="was not uniquely established"):
        facade.create_group("Exterior")


def test_creation_verification_fails_closed_when_a_different_group_appears(tmp_path, monkeypatch):
    import estimate_extractor.xactimate_lookup.fast_group_executor as fge_module
    clock = _FakeClock()
    monkeypatch.setattr(fge_module.time, "perf_counter", clock.perf_counter)
    monkeypatch.setattr(fge_module.time, "sleep", clock.sleep)
    adapter = _CreateGroupAdapter([["TEST", "Dwelling Roof"]])
    facade = _create_group_facade(adapter, tmp_path)
    with pytest.raises(RuntimeError, match="was not uniquely established"):
        facade.create_group("Exterior")


def test_creation_verification_fails_closed_on_ambiguous_match(tmp_path):
    adapter = _CreateGroupAdapter([["TEST", "Exterior", "New Exterior Zone"]])
    facade = _create_group_facade(adapter, tmp_path)
    with pytest.raises(RuntimeError, match="2 exact physical rows match"):
        facade.create_group("Exterior")


def test_creation_verification_failure_persists_diagnostic_evidence(tmp_path, monkeypatch):
    import estimate_extractor.xactimate_lookup.fast_group_executor as fge_module
    clock = _FakeClock()
    monkeypatch.setattr(fge_module.time, "perf_counter", clock.perf_counter)
    monkeypatch.setattr(fge_module.time, "sleep", clock.sleep)
    adapter = _CreateGroupAdapter([["TEST", "Dwelling Roof"]])
    facade = _create_group_facade(adapter, tmp_path)
    with pytest.raises(RuntimeError, match="was not uniquely established"):
        facade.create_group("Exterior")
    evidence_files = list(tmp_path.glob("group_creation_verification_failure_*.json"))
    assert len(evidence_files) == 1
    payload = json.loads(evidence_files[0].read_text())
    assert payload["requested_group"] == "Exterior"
    assert payload["exact_match_indices"] == []
    assert payload["final_inventory"] == ["TEST", "Dwelling Roof"]
    assert payload["verification_attempts"] >= 1
    assert payload["verification_elapsed_seconds"] >= 0


def test_diagnostic_capture_failure_never_masks_the_real_error(tmp_path, monkeypatch):
    import estimate_extractor.xactimate_lookup.fast_group_executor as fge_module
    clock = _FakeClock()
    monkeypatch.setattr(fge_module.time, "perf_counter", clock.perf_counter)
    monkeypatch.setattr(fge_module.time, "sleep", clock.sleep)
    adapter = _CreateGroupAdapter([["TEST"]])
    facade = _create_group_facade(adapter, tmp_path)
    adapter.evidence_dir = None  # forces the best-effort diagnostic capture itself to fail
    with pytest.raises(RuntimeError, match="was not uniquely established"):
        facade.create_group("Exterior")


def test_three_same_process_selections_reuse_inventory_without_complete_tree_ocr():
    names = {1: "ALPHA_ROOF_4B", 2: "BRAVO_SIDING_4B", 3: "CHARLIE_FENCE_4B"}

    class Adapter:
        expected_project_name = "TEST"
        def __init__(self): self.row_reads, self.clicks = [], []
        def verify_application(self): return True
        def verify_project(self): return True
        def _unexpected_dialog_present(self): return False
        def _find_dropdown_window(self): return None
        def _ensure_main_window(self): return 1
        def _force_foreground(self, hwnd): return True
        def _capture_client_image(self, hwnd): return object()
        def _locate_group_tree_header(self, image): return (10, 20, 30, 40)
        def _ocr_group_tree_row_text(self, image, header, index):
            self.row_reads.append(index); return names[index]
        def _click_client(self, hwnd, *xy): self.clicks.append(xy)
        def _group_tree_row_has_selection_boundary(self, image, header, index): return True
        def _anchor_offset(self, image): return (0, 0)
        def _items_search_pane_field(self, image): return (1, 1, 2, 2)
        def _win32gui(self):
            class W:
                @staticmethod
                def GetWindowRect(hwnd): return (0, 0, 100, 100)
            return W

    facade = object.__new__(WindowsGroupBatchUI); facade.adapter = Adapter()
    facade._inventory = GroupInventory((0, 0, 100, 100), (10, 20, 30, 40), tuple(
        GroupInventoryEntry(normalize_planned_group_identity(name), name, row, (50, 40 + row * 20))
        for row, name in names.items()
    ))
    original_inventory = facade._inventory
    for name in names.values():
        facade.select_group_lightweight(name)
        assert facade._inventory is original_inventory
    assert facade.adapter.row_reads == [1, 2, 3]
    assert facade.adapter.clicks == [(50, 60), (50, 80), (50, 100)]
