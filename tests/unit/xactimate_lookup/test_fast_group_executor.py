from __future__ import annotations

import json

import pytest

from estimate_extractor.xactimate_lookup.fast_group_executor import (
    FAST_KEY_HOLD_SECONDS, compile_executable_group_plan, execute_group_first_plan,
    exact_planned_group_rows, normalize_planned_group_identity, reconcile_complete_group_inventory,
    WindowsGroupBatchUI,
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
    def create_group(self, group): self.events.append(("create", group)); return "safe-create"
    def verify_all_groups_created(self, groups):
        self.events.append(("verify-all", tuple(groups)))
        if self.fail == "verify-all": raise RuntimeError("group missing")
        return "fresh-all"
    def select_group_lightweight(self, group): self.events.append(("select", group)); return "fresh-selection"
    def focus_quick_entry_cat(self): self.events.append(("focus",)); return "fresh-focus"
    def assert_batch_settled(self): self.events.append(("settled",))
    def type_text(self, value): self.events.append(("type", value))
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
    assert report["groups"][1]["bid_items"][0]["execution_status"] == "requires_biditem_sequence"
    assert FAST_KEY_HOLD_SECONDS == .005
    architectural = [event for event in ui.events if event[0] in {"create", "verify-all", "select", "type"}]
    assert architectural[:4] == [
        ("create", "A"), ("create", "B"), ("verify-all", ("A", "B")), ("select", "A"),
    ]
    assert architectural.index(("select", "B")) > architectural.index(("type", "IWS"))


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


def test_windows_creation_scopes_ensure_group_to_exact_planned_identity():
    class Adapter:
        def __init__(self):
            self.rows = ["TEST", "P4A_0814"]
            self.created = []

        def _find_unique_group_row(self, rows, requested):
            return 1  # reproduces the production fuzzy collision

        def ensure_group(self, requested):
            if self._find_unique_group_row(self.rows, requested) is None:
                self.created.append(requested)
                self.rows.append(requested)

    facade = object.__new__(WindowsGroupBatchUI)
    facade.adapter = Adapter()
    original = facade.adapter._find_unique_group_row
    facade.create_group("P4B_0814")
    assert facade.adapter.created == ["P4B_0814"]
    assert facade.adapter._find_unique_group_row == original
