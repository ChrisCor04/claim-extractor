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
    def __init__(self, fail=None, fail_select_for=None):
        self.events, self.fail, self.fail_select_for = [], fail, fail_select_for
        self.keyboard = self
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
    def select_group_lightweight(self, group):
        self.events.append(("select", group))
        if group == self.fail_select_for:
            raise RuntimeError("fast group selection refused: fresh selection boundary is absent")
        return "fresh-selection"
    def focus_quick_entry_cat(self): self.events.append(("focus",)); return "fresh-focus"
    def assert_batch_settled(self): self.events.append(("settled",))
    def capture_group_evidence(self, group): self.events.append(("capture-group", group)); return f"{group}.png"
    def capture_final_evidence(self): self.events.append(("capture-final",)); return "final.png"
    def accept_expected_group_local_duplicate(self):
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


def _tab_count_shadow(entries):
    """entries: sequence of (line_id, cat, sel, catalog_description)."""
    items = []
    for line_id, cat, sel, catalog_description in entries:
        items.append({
            "line_item_id": line_id, "group": "A", "original_description": line_id,
            "quantity": 1, "unit": "EA", "source_pricing": {"unit_price": 2.5},
            "source_action": "remove", "resolution": "resolved" if (cat, sel) != ("DOR", "BIDITM") else "bid_item_fallback",
            "category": cat if (cat, sel) != ("DOR", "BIDITM") else None,
            "selector": sel if (cat, sel) != ("DOR", "BIDITM") else None,
            "execution_category": cat, "execution_selector": sel,
            "catalog_description": catalog_description, "catalog_search_text": line_id,
            "score": .9, "margin": .4, "reason": "test", "top_candidates": [],
        })
    return {
        "schema_version": "phase3-shadow-quick-entry-plan-v2", "project": "TEST",
        "group_first_future_layout": [{"group": "A", "line_item_ids": [e[0] for e in entries]}],
        "items": items,
    }


@pytest.mark.parametrize("line_id,cat,sel,catalog_description,expected_tab_count", [
    ("armv", "RFG", "ARMV>", "Tear off, haul and dispose of comp. shingles - Laminated", 2),
    ("armv_case", "RFG", "ARMV>", "  TEAR OFF, haul and dispose of comp. shingles - Laminated  ", 2),
    ("tearout_prefix", "TCR", "TRIM", "Tear out trim and bag for disposal", 2),
    ("tearout_embedded", "DMO", "TREE", "Tree - tear out and disposal - 12\" to 24\" diameter", 2),
    ("tearout_case", "WTR", "SF", "  TEAR OUT SUBFLOOR & BAG FOR DISPOSAL  ", 2),
    ("demolish", "DMO", "H", "Demolish/remove home (1001 sf - 2000 sf)", 2),
    ("remove_asbestos", "HMR", "ASBRMM", "Remove asbestos floor mastic (no haul off)", 2),
    ("remove_vapor_barrier", "WTR", "CSV", "Remove polyethylene vapor barrier", 2),
    ("scrape_off", "HMR", "ACARMV", "Scrape off asbestos acoustic (popcorn) texture-no haul off", 2),
    ("haul_debris", "DMO", "PU", "Haul debris - per pickup truck load - including dump fees", 2),
    ("abatement_without", "HMR", "ABTNR", "Abatement without third party review", 2),
    ("abatement_with", "HMR", "ABTR", "Abatement with third party review", 2),
    ("s300", "RFG", "300S", "Laminated - comp. shingle rfg. - w/out felt", 3),
    ("rjack", "PNT", "RJACK", "Prime & paint roof jack", 3),
    ("ventcp5", "HVC", "VENTCP5", "Furnace vent - rain cap and storm collar, 5\"", 3),
    ("detach_reset", "RFG", "PAVCRS", "Power attic vent cover only - Detach & reset", 3),
    ("additional_charge", "RFG", "STEEP", "Additional charge for steep roof - 7/12 to 9/12 slope", 3),
    ("prime_paint", "PNT", "VENT", "Prime & paint roof vent", 3),
    ("strip_paint", "PNT", "DORST", "Strip paint/finish from door, 3-0 or smaller (per side)", 3),
    ("scrape_prep", "PNT", "SCRP", "Scrape {V} & prep for paint", 3),
])
def test_quantity_tab_count_driven_by_catalog_description(
    line_id, cat, sel, catalog_description, expected_tab_count,
):
    shadow = _tab_count_shadow([(line_id, cat, sel, catalog_description)])
    plan = compile_executable_group_plan(shadow)
    item = plan.groups[0].items[0]
    assert item.quantity_tab_count == expected_tab_count


def test_quantity_tab_count_ignores_source_action_cat_sel_and_quantity():
    """Same catalog_description ("Tear off...") but everything else varied
    -- the rule must key exclusively on catalog_description."""
    shadow = _tab_count_shadow([("x1", "SDG", "ASBRMV", "Tear off asbestos siding (no haul off)")])
    shadow["items"][0]["source_action"] = "detach_reset"
    shadow["items"][0]["quantity"] = 999
    plan = compile_executable_group_plan(shadow)
    assert plan.groups[0].items[0].quantity_tab_count == 2


def test_biditem_quantity_tab_count_is_irrelevant_and_untouched():
    """DOR/BIDITM stays on its own dedicated execute_fast_bid_item() path,
    which never reads quantity_tab_count -- compiling it must not raise and
    must not affect is_bid/execution_mode."""
    shadow = _tab_count_shadow([("bid1", "DOR", "BIDITM", None)])
    plan = compile_executable_group_plan(shadow)
    item = plan.groups[0].items[0]
    assert item.execution_mode == "requires_biditem_sequence"
    assert item.quantity_tab_count == 3  # unused default; never consulted for bid items


def _remove_base_pair_with_distinct_catalog_description(line_prefix, cat, sel, base_description, qty_remove, qty_base):
    """Builds a two-row, same-group remove/base pair shaped exactly like
    real persisted data: original_description carries the source "Remove
    X" / "X" wording the collapse predicate keys on, while
    catalog_description is the SAME neutral resolved-catalog text for
    both rows (confirmed against real project data for RFG/HIGH and
    RFG/STEEP -- Xactimate's catalog has no separate "Remove" variant;
    activity is carried by Act alone). Exercises the exact divergence
    between the two description fields that quantity_tab_count must not
    confuse."""
    remove_id, base_id = f"{line_prefix}_remove", f"{line_prefix}_base"
    return {
        "schema_version": "phase3-shadow-quick-entry-plan-v2", "project": "TEST",
        "group_first_future_layout": [{"group": "A", "line_item_ids": [remove_id, base_id]}],
        "items": [
            {
                "line_item_id": remove_id, "group": "A",
                "original_description": f"Remove {base_description}",
                "quantity": qty_remove, "unit": "SQ", "source_pricing": {"unit_price": 2.5},
                "source_action": "remove", "resolution": "resolved",
                "category": cat, "selector": sel, "execution_category": cat, "execution_selector": sel,
                "catalog_description": base_description, "catalog_search_text": base_description,
                "score": .9, "margin": .4, "reason": "test", "top_candidates": [],
            },
            {
                "line_item_id": base_id, "group": "A", "original_description": base_description,
                "quantity": qty_base, "unit": "SQ", "source_pricing": {"unit_price": 2.5},
                "source_action": "install", "resolution": "resolved",
                "category": cat, "selector": sel, "execution_category": cat, "execution_selector": sel,
                "catalog_description": base_description, "catalog_search_text": base_description,
                "score": .9, "margin": .4, "reason": "test", "top_candidates": [],
            },
        ],
    }


@pytest.mark.parametrize("line_prefix,cat,sel,base_description", [
    ("high", "RFG", "HIGH", "Additional charge for high roof (2 stories or greater)"),
    ("steep", "RFG", "STEEP", "Additional charge for steep roof - 7/12 to 9/12 slope"),
])
def test_remove_base_pair_source_wording_never_triggers_no_act(line_prefix, cat, sel, base_description):
    """The source ("Remove Additional charge...") and resolved catalog
    (bare "Additional charge...") descriptions genuinely diverge here,
    exactly as in real persisted data. The collapse must still occur
    exactly as before, and the resulting single physical item must stay
    at quantity_tab_count = 3 -- proving the word "Remove" in
    original_description never participates in NO_ACT classification."""
    shadow = _remove_base_pair_with_distinct_catalog_description(
        line_prefix, cat, sel, base_description, qty_remove=33.66, qty_base=35.67,
    )
    plan = compile_executable_group_plan(shadow)
    items = plan.groups[0].items
    assert len(items) == 1  # collapse still occurs exactly as before
    item = items[0]
    assert item.collapse_reason == "paired_remove_base_same_identity"
    assert item.category == cat and item.selector == sel
    assert item.quantity == 33.66  # the remove row's own quantity, unchanged
    assert item.quantity_tab_count == 3  # driven by catalog_description alone, not "Remove ..." source wording


def _pair_shadow(group_items):
    """group_items: sequence of (group_name, [(line_id, description, cat, sel, qty, resolution), ...])."""
    items, layout = [], []
    for group_name, entries in group_items:
        ids = []
        for line_id, description, cat, sel, qty, resolution in entries:
            items.append({
                "line_item_id": line_id, "group": group_name, "original_description": description,
                "quantity": qty, "unit": "EA", "source_pricing": {"unit_price": 2.5},
                "source_action": "remove_replace", "resolution": resolution,
                "category": cat if resolution == "resolved" else None,
                "selector": sel if resolution == "resolved" else None,
                "execution_category": cat, "execution_selector": sel,
                "catalog_description": description, "catalog_search_text": description,
                "score": .9, "margin": .4, "reason": "test", "top_candidates": [],
            })
            ids.append(line_id)
        layout.append({"group": group_name, "line_item_ids": ids})
    return {
        "schema_version": "phase3-shadow-quick-entry-plan-v2", "project": "TEST",
        "group_first_future_layout": layout, "items": items,
    }


def test_remove_base_pair_same_quantity_collapses_with_no_discrepancy():
    shadow = _pair_shadow([("A", [
        ("a1", "Remove Additional charge for widget mounting", "ABC", "WIDGET", 9.36, "resolved"),
        ("a2", "Additional charge for widget mounting", "ABC", "WIDGET", 9.36, "resolved"),
    ])])
    plan = compile_executable_group_plan(shadow)
    items = plan.groups[0].items
    assert len(items) == 1
    assert (items[0].category, items[0].selector, items[0].quantity) == ("ABC", "WIDGET", 9.36)
    assert items[0].collapse_reason == "paired_remove_base_same_identity"
    assert items[0].source_quantities == (9.36, 9.36)
    assert items[0].quantity_disagreement is False
    assert items[0].human_review_required is False


def test_remove_base_pair_with_different_quantity_still_collapses_and_flags_review():
    """Revised rule: quantity is not part of the pair predicate. Two
    genuinely different measurements (a remove vs. an install quantity)
    still collapse to one physical submission, using the remove row's own
    quantity -- never added, averaged, or maximized -- but the disagreement
    must be visible for human review, never silently absorbed."""
    shadow = _pair_shadow([("A", [
        ("a1", "Remove Additional charge for widget mounting", "ABC", "WIDGET", 33.66, "resolved"),
        ("a2", "Additional charge for widget mounting", "ABC", "WIDGET", 35.67, "resolved"),
    ])])
    plan = compile_executable_group_plan(shadow)
    items = plan.groups[0].items
    assert len(items) == 1
    item = items[0]
    assert item.collapse_reason == "paired_remove_base_same_identity"
    assert item.quantity == 33.66  # the remove (first) row's own quantity -- not 35.67, not summed/averaged
    assert item.source_line_item_ids == ("a1", "a2")
    assert item.source_quantities == (33.66, 35.67)  # both originals retained
    assert item.quantity_disagreement is True
    assert item.human_review_required is True


def test_remove_base_pair_with_different_category_does_not_collapse():
    shadow = _pair_shadow([("A", [
        ("a1", "Remove Additional charge for widget mounting", "ABC", "WIDGET", 9.36, "resolved"),
        ("a2", "Additional charge for widget mounting", "XYZ", "WIDGET", 9.36, "resolved"),
    ])])
    plan = compile_executable_group_plan(shadow)
    items = plan.groups[0].items
    assert len(items) == 2
    assert all(item.collapse_reason is None for item in items)


def test_remove_base_pair_with_different_selector_does_not_collapse():
    shadow = _pair_shadow([("A", [
        ("a1", "Remove Additional charge for widget mounting", "ABC", "WIDGET", 9.36, "resolved"),
        ("a2", "Additional charge for widget mounting", "ABC", "GADGET", 9.36, "resolved"),
    ])])
    plan = compile_executable_group_plan(shadow)
    items = plan.groups[0].items
    assert len(items) == 2
    assert all(item.collapse_reason is None for item in items)


def test_same_cat_sel_qty_with_unrelated_descriptions_does_not_collapse():
    shadow = _pair_shadow([("A", [
        ("a1", "Completely unrelated first item", "ABC", "WIDGET", 9.36, "resolved"),
        ("a2", "A totally different second item", "ABC", "WIDGET", 9.36, "resolved"),
    ])])
    plan = compile_executable_group_plan(shadow)
    items = plan.groups[0].items
    assert len(items) == 2
    assert all(item.collapse_reason is None for item in items)


def test_matching_remove_base_pair_across_different_groups_does_not_collapse():
    shadow = _pair_shadow([
        ("A", [("a1", "Remove Additional charge for widget mounting", "ABC", "WIDGET", 9.36, "resolved")]),
        ("B", [("b1", "Additional charge for widget mounting", "ABC", "WIDGET", 9.36, "resolved")]),
    ])
    plan = compile_executable_group_plan(shadow)
    assert len(plan.groups[0].items) == 1 and plan.groups[0].items[0].collapse_reason is None
    assert len(plan.groups[1].items) == 1 and plan.groups[1].items[0].collapse_reason is None


def test_reverse_order_base_then_remove_does_not_collapse_without_proven_evidence():
    """No real extracted plan has ever shown a base row followed later by
    its own 'Remove' row -- only forward order (remove first) is proven, so
    reverse order must fail closed rather than being guessed at."""
    shadow = _pair_shadow([("A", [
        ("a1", "Additional charge for widget mounting", "ABC", "WIDGET", 9.36, "resolved"),
        ("a2", "Remove Additional charge for widget mounting", "ABC", "WIDGET", 9.36, "resolved"),
    ])])
    plan = compile_executable_group_plan(shadow)
    items = plan.groups[0].items
    assert len(items) == 2
    assert all(item.collapse_reason is None for item in items)


@pytest.mark.parametrize(
    "cat, sel, base_description",
    [
        ("ABC", "WIDGET1", "Additional charge for widget A"),
        ("XYZ", "WIDGET2", "Additional charge for widget B"),
    ],
)
def test_generic_remove_base_shapes_collapse_without_hardcoded_vocabulary(cat, sel, base_description):
    """Two independent synthetic vocabularies, neither resembling any real
    trade code or claim description, prove the predicate is structural."""
    shadow = _pair_shadow([("A", [
        ("a1", f"Remove {base_description}", cat, sel, 9.36, "resolved"),
        ("a2", base_description, cat, sel, 9.36, "resolved"),
    ])])
    plan = compile_executable_group_plan(shadow)
    items = plan.groups[0].items
    assert len(items) == 1
    assert (items[0].category, items[0].selector) == (cat, sel)
    assert items[0].collapse_reason == "paired_remove_base_same_identity"


def test_unrelated_items_around_a_collapsing_pair_preserve_order_and_are_untouched():
    shadow = _pair_shadow([("A", [
        ("a0", "An unrelated first item", "QRS", "FIRST", 3.0, "resolved"),
        ("a1", "Remove Additional charge for widget mounting", "ABC", "WIDGET", 9.36, "resolved"),
        ("a2", "Additional charge for widget mounting", "ABC", "WIDGET", 9.36, "resolved"),
        ("a3", "An unrelated last item", "QRS", "LAST", 4.0, "resolved"),
    ])])
    plan = compile_executable_group_plan(shadow)
    items = plan.groups[0].items
    assert [item.line_item_id for item in items] == ["a0", "a1", "a3"]
    assert [item.collapse_reason for item in items] == [None, "paired_remove_base_same_identity", None]
    assert items[1].source_line_item_ids == ("a1", "a2")


def test_collapsed_pair_preserves_provenance_from_both_source_rows():
    shadow = _pair_shadow([("A", [
        ("a1", "Remove Additional charge for widget mounting", "ABC", "WIDGET", 9.36, "resolved"),
        ("a2", "Additional charge for widget mounting", "ABC", "WIDGET", 9.36, "resolved"),
    ])])
    plan = compile_executable_group_plan(shadow)
    item = plan.groups[0].items[0]
    assert item.source_line_item_ids == ("a1", "a2")
    assert item.source_descriptions == (
        "Remove Additional charge for widget mounting", "Additional charge for widget mounting",
    )
    assert item.collapse_reason == "paired_remove_base_same_identity"


def test_ordinary_uncollapsed_item_still_carries_single_element_provenance():
    plan = compile_executable_group_plan(_shadow())
    item = plan.groups[0].items[0]
    assert item.source_line_item_ids == (item.line_item_id,)
    assert item.source_descriptions == (item.original_description,)
    assert item.collapse_reason is None


def test_collapsed_pair_reduces_physical_submissions_in_the_hot_loop():
    """The executor must never see two items for a collapsed pair -- one
    keyboard submission only, proving the collapse happens before
    execution, not as a skip inside the hot loop."""
    shadow = _pair_shadow([("A", [
        ("a1", "Remove Additional charge for widget mounting", "ABC", "WIDGET", 9.36, "resolved"),
        ("a2", "Additional charge for widget mounting", "ABC", "WIDGET", 9.36, "resolved"),
    ])])
    plan = compile_executable_group_plan(shadow)
    assert plan.groups[0].items[0].quantity == 9.36  # not doubled (18.72) or summed

    ui = UI(); report = execute_group_first_plan(plan, ui, clock=Clock())
    typed = [event for event in ui.events if event[0] == "type"]
    assert typed == [("type", "ABC"), ("type", "WIDGET"), ("type", "9.36")]
    assert ("duplicate-poll",) not in ui.events
    assert report["normal_item_count"] == 1
    assert len(report["groups"][0]["items"]) == 1


def test_app_originated_source_row_count_preserved_while_executable_count_shrinks():
    """The shadow plan's own source rows (used for review/audit) are never
    mutated by compilation -- only the compiled executable plan shrinks."""
    shadow = _pair_shadow([("A", [
        ("a1", "Remove Additional charge for widget mounting", "ABC", "WIDGET", 9.36, "resolved"),
        ("a2", "Additional charge for widget mounting", "ABC", "WIDGET", 9.36, "resolved"),
        ("a3", "An unrelated item", "QRS", "OTHER", 1.0, "resolved"),
    ])])
    source_row_count = len(shadow["items"])
    plan = compile_executable_group_plan(shadow)
    assert len(shadow["items"]) == source_row_count == 3  # untouched
    assert sum(len(group.items) for group in plan.groups) == 2  # pair collapsed to one


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


def test_focus_click_skipped_experimental_pause_replaces_it(monkeypatch):
    """Isolation proof for the focus_quick_entry_cat()-skip experiment:
    selection still happens exactly once per group, focus_quick_entry_cat()
    is never called, a bare 0.2s pause takes its place immediately after
    selection and before that group's first keystroke, and the item
    keyboard/duplicate sequence and report shape are unchanged from the
    non-experimental baseline proven above."""
    import estimate_extractor.xactimate_lookup.fast_group_executor as fge_module
    sleep_calls: list[float] = []
    monkeypatch.setattr(fge_module.time, "sleep", lambda seconds: sleep_calls.append(seconds))

    ui = UI()
    report = execute_group_first_plan(compile_executable_group_plan(_shadow()), ui, clock=Clock())

    # Selection still occurs exactly once per group.
    assert ui.events.count(("select", "A")) == 1
    assert ui.events.count(("select", "B")) == 1

    # focus_quick_entry_cat() is never called.
    assert ("focus",) not in ui.events

    # The 0.2s pause replaces it, once per group, immediately after that
    # group's selection and before its first keystroke -- nothing else
    # (no click, no capture, no OCR) happens in between.
    assert sleep_calls == [0.2, 0.2]
    select_a, select_b = ui.events.index(("select", "A")), ui.events.index(("select", "B"))
    first_type_a = ui.events.index(("type", "RFG"))
    first_type_b = ui.events.index(("type", "SDG"))
    assert select_a < first_type_a and select_b < first_type_b
    # No UI event (click, capture, OCR) intervenes between selection and
    # the first keystroke -- the only thing standing between them is the
    # (non-UI-event) 0.2s sleep asserted above.
    assert ui.events[select_a + 1] == ("type", "RFG")
    assert ui.events[select_b + 1] == ("type", "SDG")

    # Item keyboard sequence and order unchanged from the non-experimental baseline.
    first = ui.events.index(("type", "RFG"))
    assert ui.events[first:first + 7] == [
        ("type", "RFG"), ("type", "DRIP"), ("tab",), ("tab",), ("tab",), ("type", "1"), ("enter",),
    ]

    # Report structure preserved: focus fields carry the explicit sentinel,
    # never disguised as real click or pause timing.
    for group_report in report["groups"]:
        assert group_report["focus_method"] == "skipped_after_verified_group_transition"
        assert group_report["focus_seconds"] == 0.0
    assert report["quick_entry_focus"]["total_seconds"] == 0.0
    assert report["normal_item_count"] == 3 and report["bid_item_count"] == 1


def test_no_act_item_reaches_the_real_hot_loop_with_two_tabs():
    """End-to-end: compile_executable_group_plan() -> execute_group_first_plan()
    -> the real (non-experimental) execute_fast_items() call site. A
    Tear-off item's keyboard sequence has exactly 2 tabs; an ordinary item
    immediately after it in the same group still gets 3."""
    shadow = _tab_count_shadow([
        ("armv", "RFG", "ARMV>", "Tear off, haul and dispose of comp. shingles - Laminated"),
        ("s300", "RFG", "300S", "Laminated - comp. shingle rfg. - w/out felt"),
    ])
    ui = UI()
    execute_group_first_plan(compile_executable_group_plan(shadow), ui, clock=Clock())
    first = ui.events.index(("type", "RFG"))
    assert ui.events[first:first + 6] == [
        ("type", "RFG"), ("type", "ARMV>"), ("tab",), ("tab",), ("type", "1"), ("enter",),
    ]
    second = ui.events.index(("type", "RFG"), first + 1)
    assert ui.events[second:second + 7] == [
        ("type", "RFG"), ("type", "300S"), ("tab",), ("tab",), ("tab",), ("type", "1"), ("enter",),
    ]


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


def test_expected_group_local_duplicate_tracking_covers_biditem_and_resolved_items():
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
    # Group A: three identical BIDITM entries -> 2nd and 3rd are duplicates.
    assert len(report["groups"][0]["expected_duplicate_acceptances"]) == 2
    # Group B: one BIDITM (never repeated within B) plus two identical
    # resolved RFG/DRIP entries -> only the second resolved entry is a
    # duplicate. Resolved duplicates now use the SAME acceptance path as
    # BIDITM's, so overall count is 2 (group A) + 1 (group B) = 3.
    assert len(report["groups"][1]["expected_duplicate_acceptances"]) == 1
    assert report["groups"][1]["expected_duplicate_acceptances"][0]["line_item_id"] == "n2"
    assert ui.events.count(("duplicate-poll",)) == 3

    second_bid = [i for i, event in enumerate(ui.events) if event == ("type", "DOR")][1]
    assert ui.events[second_bid:second_bid + 11] == [
        ("type", "DOR"), ("type", "BIDITM"), ("tab",), ("replace", "b2"),
        ("tab",), ("replace", "1"), ("enter",), ("duplicate-poll",),
        ("tab",), ("tab",), ("enter",),
    ]
    # The lone BIDITM in B has no earlier BIDITM in B, so it is not a duplicate.
    b_select = ui.events.index(("select", "B"))
    first_b_bid = ui.events.index(("type", "DOR"), b_select)
    assert ui.events[first_b_bid:first_b_bid + 8] == [
        ("type", "DOR"), ("type", "BIDITM"), ("tab",), ("replace", "b4"),
        ("tab",), ("replace", "1"), ("enter",), ("type", "RFG"),
    ]
    # The second resolved RFG/DRIP in B: normal commit, then the identical
    # Tab/Tab/Enter acceptance sequence BIDITM's duplicate uses.
    second_normal = [i for i, event in enumerate(ui.events) if event == ("type", "RFG")][1]
    assert ui.events[second_normal:second_normal + 8] == [
        ("type", "RFG"), ("type", "DRIP"), ("tab",), ("tab",), ("tab",),
        ("type", "1"), ("enter",), ("duplicate-poll",),
    ]


def _identity_shadow(group_items):
    """group_items: sequence of (group_name, [(line_id, cat, sel, resolution), ...])."""
    items, layout = [], []
    for group_name, entries in group_items:
        ids = []
        for line_id, cat, sel, resolution in entries:
            items.append({
                "line_item_id": line_id, "group": group_name, "original_description": line_id,
                "quantity": 1, "unit": "EA", "source_pricing": {"unit_price": 2.5},
                "source_action": "remove_replace", "resolution": resolution,
                "category": cat if resolution == "resolved" else None,
                "selector": sel if resolution == "resolved" else None,
                "execution_category": cat, "execution_selector": sel,
                "catalog_description": line_id, "catalog_search_text": line_id,
                "score": .9, "margin": .4, "reason": "test", "top_candidates": [],
            })
            ids.append(line_id)
        layout.append({"group": group_name, "line_item_ids": ids})
    return {
        "schema_version": "phase3-shadow-quick-entry-plan-v2", "project": "TEST",
        "group_first_future_layout": layout, "items": items,
    }


def _run_identities(group_items):
    ui = UI()
    report = execute_group_first_plan(
        compile_executable_group_plan(_identity_shadow(group_items)), ui, clock=Clock(),
    )
    return ui, report


def test_single_resolved_item_has_no_duplicate_acceptance():
    ui, report = _run_identities([("A", [("a1", "RFG", "DRIP", "resolved")])])
    assert ui.events.count(("duplicate-poll",)) == 0
    assert report["groups"][0]["expected_duplicate_acceptances"] == []


def test_two_identical_resolved_items_duplicate_only_on_second():
    ui, report = _run_identities([("A", [
        ("a1", "RFG", "DRIP", "resolved"), ("a2", "RFG", "DRIP", "resolved"),
    ])])
    assert ui.events.count(("duplicate-poll",)) == 1
    accepted = report["groups"][0]["expected_duplicate_acceptances"]
    assert [item["line_item_id"] for item in accepted] == ["a2"]


def test_three_identical_resolved_items_duplicate_on_second_and_third():
    ui, report = _run_identities([("A", [
        ("a1", "RFG", "DRIP", "resolved"), ("a2", "RFG", "DRIP", "resolved"), ("a3", "RFG", "DRIP", "resolved"),
    ])])
    assert ui.events.count(("duplicate-poll",)) == 2
    accepted = report["groups"][0]["expected_duplicate_acceptances"]
    assert [item["line_item_id"] for item in accepted] == ["a2", "a3"]


def test_same_resolved_identity_in_different_groups_is_not_a_duplicate():
    ui, report = _run_identities([
        ("A", [("a1", "RFG", "DRIP", "resolved")]),
        ("B", [("b1", "RFG", "DRIP", "resolved")]),
    ])
    assert ui.events.count(("duplicate-poll",)) == 0
    assert report["groups"][0]["expected_duplicate_acceptances"] == []
    assert report["groups"][1]["expected_duplicate_acceptances"] == []


def test_two_biditm_items_duplicate_only_on_second():
    ui, report = _run_identities([("A", [
        ("a1", "DOR", "BIDITM", "ambiguous"), ("a2", "DOR", "BIDITM", "ambiguous"),
    ])])
    assert ui.events.count(("duplicate-poll",)) == 1
    accepted = report["groups"][0]["expected_duplicate_acceptances"]
    assert [item["line_item_id"] for item in accepted] == ["a2"]


def test_resolved_then_biditm_then_repeated_resolved_only_flags_the_repeat():
    ui, report = _run_identities([("A", [
        ("a1", "RFG", "DRIP", "resolved"), ("a2", "DOR", "BIDITM", "ambiguous"), ("a3", "RFG", "DRIP", "resolved"),
    ])])
    assert ui.events.count(("duplicate-poll",)) == 1
    accepted = report["groups"][0]["expected_duplicate_acceptances"]
    assert [item["line_item_id"] for item in accepted] == ["a3"]


def test_two_different_resolved_identities_never_duplicate():
    ui, report = _run_identities([("A", [
        ("a1", "RFG", "DRIP", "resolved"), ("a2", "RFG", "IWS", "resolved"),
    ])])
    assert ui.events.count(("duplicate-poll",)) == 0
    assert report["groups"][0]["expected_duplicate_acceptances"] == []


def test_biditm_then_resolved_then_repeated_biditm_only_flags_the_repeat():
    ui, report = _run_identities([("A", [
        ("a1", "DOR", "BIDITM", "ambiguous"), ("a2", "RFG", "DRIP", "resolved"), ("a3", "DOR", "BIDITM", "ambiguous"),
    ])])
    assert ui.events.count(("duplicate-poll",)) == 1
    accepted = report["groups"][0]["expected_duplicate_acceptances"]
    assert [item["line_item_id"] for item in accepted] == ["a3"]


def test_duplicate_tracking_preserves_source_order():
    ui, report = _run_identities([("A", [
        ("a1", "RFG", "DRIP", "resolved"), ("a2", "DOR", "BIDITM", "ambiguous"), ("a3", "RFG", "DRIP", "resolved"),
    ])])
    assert [item["line_item_id"] for item in report["groups"][0]["items"]] == ["a1", "a2", "a3"]


def test_duplicate_tracking_resets_independently_per_group():
    ui, report = _run_identities([
        ("A", [("a1", "RFG", "DRIP", "resolved"), ("a2", "RFG", "DRIP", "resolved")]),
        ("B", [("b1", "RFG", "DRIP", "resolved"), ("b2", "RFG", "DRIP", "resolved")]),
    ])
    assert len(report["groups"][0]["expected_duplicate_acceptances"]) == 1
    assert len(report["groups"][1]["expected_duplicate_acceptances"]) == 1
    assert ui.events.count(("duplicate-poll",)) == 2


def test_resolved_duplicate_identity_ignores_description_and_quantity():
    shadow = _identity_shadow([("A", [
        ("a1", "RFG", "DRIP", "resolved"), ("a2", "RFG", "DRIP", "resolved"),
    ])])
    shadow["items"][0]["original_description"] = "First description"
    shadow["items"][0]["quantity"] = 2
    shadow["items"][1]["original_description"] = "Completely different text"
    shadow["items"][1]["quantity"] = 99
    ui = UI()
    report = execute_group_first_plan(compile_executable_group_plan(shadow), ui, clock=Clock())
    assert ui.events.count(("duplicate-poll",)) == 1
    accepted = report["groups"][0]["expected_duplicate_acceptances"]
    assert [item["line_item_id"] for item in accepted] == ["a2"]


def test_missing_duplicate_dialog_when_expected_fails_closed():
    """A predicted duplicate that Xactimate does not actually confirm must
    surface as a hard failure, never be silently swallowed."""
    class FailingDuplicateUI(UI):
        def accept_expected_group_local_duplicate(self):
            self.events.append(("duplicate-poll",))
            raise RuntimeError(
                "expected repeated group-local item identity did not present Duplicate Item(s) within 100 ms"
            )
    shadow = _identity_shadow([("A", [
        ("a1", "RFG", "DRIP", "resolved"), ("a2", "RFG", "DRIP", "resolved"),
    ])])
    ui = FailingDuplicateUI()
    with pytest.raises(RuntimeError, match="did not present Duplicate Item"):
        execute_group_first_plan(compile_executable_group_plan(shadow), ui, clock=Clock())


def test_unexpected_dialog_on_first_occurrence_is_not_blindly_accepted():
    """A first (non-repeat) occurrence never predicts a duplicate, so the
    hot loop must never call the duplicate-acceptance primitive for it --
    an unexpectedly-blocking dialog is left for the existing
    assert_batch_settled() safety net to fail closed on, not silently
    accepted as if it had been predicted."""
    class UnexpectedDialogUI(UI):
        def __init__(self):
            super().__init__()
            self.duplicate_poll_calls = 0
        def accept_expected_group_local_duplicate(self):
            self.duplicate_poll_calls += 1
            self.events.append(("duplicate-poll",))
            return {"appearance_wait_seconds": .02, "acceptance_seconds": .02}
        def assert_batch_settled(self):
            self.events.append(("settled",))
            raise RuntimeError("fast group batch stopped: blocking dialog/dropdown detected after batch")
    shadow = _identity_shadow([("A", [("a1", "RFG", "DRIP", "resolved")])])
    ui = UnexpectedDialogUI()
    with pytest.raises(RuntimeError, match="blocking dialog/dropdown detected after batch"):
        execute_group_first_plan(compile_executable_group_plan(shadow), ui, clock=Clock())
    assert ui.duplicate_poll_calls == 0


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


def test_select_group_lightweight_succeeds_with_a_realistically_displaced_selection_boundary():
    """End-to-end regression for the live 'Exterior' failure: the click
    lands on the correct cached row and the row IS genuinely selected, but
    the rendered border sits a pixel off the OCR-derived nominal edge.
    Uses the REAL windows_adapter selection-boundary detector (not a
    return-True stub) against a real drawn image, an arbitrary group name,
    and an arbitrary row index -- proving the generalized tolerance fix,
    not anything Exterior- or row-1-specific."""
    from PIL import Image, ImageDraw

    from estimate_extractor.xactimate_lookup.windows_adapter import WindowsXactimateAdapter

    real = WindowsXactimateAdapter(expected_project_name="TEST", window_finder=lambda: ([], []))
    header = (270, 155, 301, 165)
    group = "SomeArbitraryGroup"
    row_index = 3

    image = Image.new("RGB", (700, 400), "white")
    nominal_top = real._group_tree_row_crop_top(header[1], row_index)
    dy = 1  # the live-observed displacement
    top_y = nominal_top + dy
    bottom_y = nominal_top + dy + real._GROUP_TREE_ROW_CROP_HEIGHT - 2
    draw = ImageDraw.Draw(image)
    draw.line((header[0] - 4, top_y, 600, top_y), fill=(125, 162, 206), width=1)
    draw.line((header[0] - 4, bottom_y, 600, bottom_y), fill=(125, 162, 206), width=1)

    class Adapter:
        expected_project_name = "TEST"
        def verify_application(self): return True
        def verify_project(self): return True
        def _unexpected_dialog_present(self): return False
        def _find_dropdown_window(self): return None
        def _ensure_main_window(self): return 1
        def _force_foreground(self, hwnd): return True
        def _capture_client_image(self, hwnd): return image
        def _locate_group_tree_header(self, img): return header
        def _ocr_group_tree_row_text(self, img, hdr, index): return group
        def _click_client(self, hwnd, *xy): self.clicked = xy
        def _group_tree_row_has_selection_boundary(self, img, hdr, index):
            return WindowsXactimateAdapter._group_tree_row_has_selection_boundary(real, img, hdr, index)
        def _anchor_offset(self, img): return (0, 0)
        def _items_search_pane_field(self, img): return (1, 1, 2, 2)
        def _win32gui(self):
            class W:
                @staticmethod
                def GetWindowRect(hwnd): return (0, 0, 100, 100)
            return W

    facade = object.__new__(WindowsGroupBatchUI)
    facade.adapter = Adapter()
    row_center = real._group_tree_row_xy(header, row_index)
    facade._inventory = GroupInventory(
        window_rect=(0, 0, 100, 100), header_rect=header,
        entries=(GroupInventoryEntry(normalize_planned_group_identity(group), group, row_index, row_center),),
    )

    result = facade.select_group_lightweight(group)

    assert result.startswith("verified_inventory_row")
    assert facade.adapter.clicked == row_center


def test_select_group_lightweight_still_fails_when_no_boundary_is_present_at_all(monkeypatch):
    """Sanity counterpart: an undisplaced, genuinely absent boundary must
    still refuse -- the tolerance widens WHERE a boundary may be found, it
    does not make a missing boundary pass. Fake time so the bounded settle
    loop's timeout is exercised without a real multi-second wait."""
    import estimate_extractor.xactimate_lookup.fast_group_executor as fge_module
    clock = _FakeClock()
    monkeypatch.setattr(fge_module.time, "perf_counter", clock.perf_counter)
    monkeypatch.setattr(fge_module.time, "sleep", clock.sleep)

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
        def _group_tree_row_has_selection_boundary(self, image, header, index): return False
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
    with pytest.raises(RuntimeError, match="fresh selection boundary is absent"):
        facade.select_group_lightweight("P4B_0814")


def test_selection_boundary_failure_persists_diagnostic_evidence(tmp_path, monkeypatch):
    import estimate_extractor.xactimate_lookup.fast_group_executor as fge_module
    clock = _FakeClock()
    monkeypatch.setattr(fge_module.time, "perf_counter", clock.perf_counter)
    monkeypatch.setattr(fge_module.time, "sleep", clock.sleep)

    class Adapter:
        expected_project_name = "TEST"
        evidence_dir = tmp_path
        def verify_application(self): return True
        def verify_project(self): return True
        def _unexpected_dialog_present(self): return False
        def _find_dropdown_window(self): return None
        def _ensure_main_window(self): return 1
        def _force_foreground(self, hwnd): return True
        def _capture_client_image(self, hwnd): return object()
        def _locate_group_tree_header(self, image): return (10, 20, 30, 40)
        def _ocr_group_tree_row_text(self, image, header, index): return "Exterior"
        def _click_client(self, hwnd, *xy): pass
        def _group_tree_row_has_selection_boundary(self, image, header, index): return False
        def snapshot_group_names(self): return ["TEST", "Exterior"]
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
        entries=(GroupInventoryEntry("exterior", "Exterior", 1, (50, 70)),),
    )
    with pytest.raises(RuntimeError, match="fresh selection boundary is absent"):
        facade.select_group_lightweight("Exterior")

    evidence_files = list(tmp_path.glob("group_selection_verification_failure_*.json"))
    assert len(evidence_files) == 1
    payload = json.loads(evidence_files[0].read_text())
    assert payload["requested_group"] == "Exterior"
    assert payload["failure_stage"] == "selection_boundary_absent"
    assert payload["physical_row"] == 1
    assert payload["reread_text"] == "Exterior"
    assert payload["has_selection_boundary"] is False
    assert payload["anchor_offset"] == [0, 0]
    assert payload["items_search_pane_field"] == [1, 1, 2, 2]
    assert payload["verification_attempts"] >= 1
    assert payload["elapsed_settle_seconds"] >= 0


def test_selection_diagnostic_capture_failure_never_masks_the_real_error(monkeypatch):
    """The diagnostic write itself must be fully best-effort: an adapter
    with no evidence_dir at all (e.g. every pre-existing lightweight test
    fake in this file) must still raise the ORIGINAL, unmodified error."""
    import estimate_extractor.xactimate_lookup.fast_group_executor as fge_module
    clock = _FakeClock()
    monkeypatch.setattr(fge_module.time, "perf_counter", clock.perf_counter)
    monkeypatch.setattr(fge_module.time, "sleep", clock.sleep)

    class Adapter:
        expected_project_name = "TEST"
        # deliberately no evidence_dir attribute
        def verify_application(self): return True
        def verify_project(self): return True
        def _unexpected_dialog_present(self): return False
        def _find_dropdown_window(self): return None
        def _ensure_main_window(self): return 1
        def _force_foreground(self, hwnd): return True
        def _capture_client_image(self, hwnd): return object()
        def _locate_group_tree_header(self, image): return (10, 20, 30, 40)
        def _ocr_group_tree_row_text(self, image, header, index): return "P4B.0814"
        def _click_client(self, hwnd, *xy): pass
        def _group_tree_row_has_selection_boundary(self, image, header, index): return False
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
    with pytest.raises(RuntimeError, match="fresh selection boundary is absent"):
        facade.select_group_lightweight("P4B_0814")


# -- select_group_lightweight(): bounded post-click settling loop --------

def _settle_adapter(boundary_sequence):
    """Adapter whose _group_tree_row_has_selection_boundary() result comes
    from a controllable sequence, one entry consumed per fresh capture --
    proves the settling loop captures a genuinely fresh frame each attempt
    rather than re-checking a stale one. Exhausting the sequence keeps
    returning False (never settles)."""
    class Adapter:
        expected_project_name = "TEST"
        def __init__(self):
            self.capture_count = 0
            self.boundary_calls = 0
            self._sequence = iter(boundary_sequence)
        def verify_application(self): return True
        def verify_project(self): return True
        def _unexpected_dialog_present(self): return False
        def _find_dropdown_window(self): return None
        def _ensure_main_window(self): return 1
        def _force_foreground(self, hwnd): return True
        def _capture_client_image(self, hwnd):
            self.capture_count += 1
            return object()
        def _locate_group_tree_header(self, image): return (10, 20, 30, 40)
        def _ocr_group_tree_row_text(self, image, header, index): return "P4B.0814"
        def _click_client(self, hwnd, *xy): self.clicked = xy
        def _group_tree_row_has_selection_boundary(self, image, header, index):
            self.boundary_calls += 1
            try:
                return next(self._sequence)
            except StopIteration:
                return False
        def _anchor_offset(self, image): return (0, 0)
        def _items_search_pane_field(self, image): return (1, 1, 2, 2)
        def _win32gui(self):
            class W:
                @staticmethod
                def GetWindowRect(hwnd): return (0, 0, 100, 100)
            return W
    return Adapter()


def _settle_facade(adapter):
    facade = object.__new__(WindowsGroupBatchUI)
    facade.adapter = adapter
    facade._inventory = GroupInventory(
        window_rect=(0, 0, 100, 100), header_rect=(10, 20, 30, 40),
        entries=(GroupInventoryEntry("p4b0814", "P4B_0814", 2, (50, 70)),),
    )
    return facade


def _fake_clock_installed(monkeypatch):
    import estimate_extractor.xactimate_lookup.fast_group_executor as fge_module
    clock = _FakeClock()
    monkeypatch.setattr(fge_module.time, "perf_counter", clock.perf_counter)
    monkeypatch.setattr(fge_module.time, "sleep", clock.sleep)
    return clock


def test_settling_loop_succeeds_immediately_without_unnecessary_retries(monkeypatch):
    clock = _fake_clock_installed(monkeypatch)
    adapter = _settle_adapter([True])
    facade = _settle_facade(adapter)

    result = facade.select_group_lightweight("P4B_0814")

    assert result.startswith("verified_inventory_row")
    assert adapter.boundary_calls == 1
    assert clock.now == 0.0  # first attempt already succeeded -- never slept


def test_settling_loop_succeeds_after_one_transient_miss(monkeypatch):
    _fake_clock_installed(monkeypatch)
    adapter = _settle_adapter([False, True])
    facade = _settle_facade(adapter)

    result = facade.select_group_lightweight("P4B_0814")

    assert result.startswith("verified_inventory_row")
    assert adapter.boundary_calls == 2
    # 1 pre-click capture (header/OCR reread) + 2 settle-loop captures --
    # proves each settle attempt uses a genuinely fresh frame, not a
    # cached/reused one.
    assert adapter.capture_count == 3


def test_settling_loop_succeeds_after_multiple_transient_misses(monkeypatch):
    _fake_clock_installed(monkeypatch)
    adapter = _settle_adapter([False, False, False, True])
    facade = _settle_facade(adapter)

    result = facade.select_group_lightweight("P4B_0814")

    assert result.startswith("verified_inventory_row")
    assert adapter.boundary_calls == 4


def test_settling_loop_times_out_and_fails_closed_with_existing_refusal(monkeypatch):
    clock = _fake_clock_installed(monkeypatch)
    adapter = _settle_adapter([])  # never returns True
    facade = _settle_facade(adapter)

    with pytest.raises(RuntimeError, match="fresh selection boundary is absent"):
        facade.select_group_lightweight("P4B_0814")
    assert adapter.boundary_calls > 1  # genuinely retried, not a single immediate check
    assert clock.now >= facade._SELECTION_BOUNDARY_SETTLE_TIMEOUT_S


def test_blocking_ui_appearing_during_settle_fails_immediately_not_at_timeout(monkeypatch):
    clock = _fake_clock_installed(monkeypatch)

    class Adapter:
        expected_project_name = "TEST"
        def __init__(self): self.attempts = 0
        def verify_application(self): return True
        def verify_project(self): return True
        def _unexpected_dialog_present(self): return self.attempts >= 2  # appears on the 2nd check
        def _find_dropdown_window(self): return None
        def _ensure_main_window(self): return 1
        def _force_foreground(self, hwnd): return True
        def _capture_client_image(self, hwnd):
            self.attempts += 1
            return object()
        def _locate_group_tree_header(self, image): return (10, 20, 30, 40)
        def _ocr_group_tree_row_text(self, image, header, index): return "P4B.0814"
        def _click_client(self, hwnd, *xy): pass
        def _group_tree_row_has_selection_boundary(self, image, header, index): return False
        def _win32gui(self):
            class W:
                @staticmethod
                def GetWindowRect(hwnd): return (0, 0, 100, 100)
            return W

    adapter = Adapter()
    facade = _settle_facade(adapter)

    with pytest.raises(RuntimeError, match="blocking UI appeared"):
        facade.select_group_lightweight("P4B_0814")
    assert adapter.attempts == 2  # stopped as soon as the dialog was seen
    assert clock.now < facade._SELECTION_BOUNDARY_SETTLE_TIMEOUT_S  # never reached the full bound


def test_ocr_identity_mismatch_fails_before_any_click():
    class Adapter:
        expected_project_name = "TEST"
        def __init__(self): self.click_called = False
        def verify_application(self): return True
        def verify_project(self): return True
        def _unexpected_dialog_present(self): return False
        def _find_dropdown_window(self): return None
        def _ensure_main_window(self): return 1
        def _force_foreground(self, hwnd): return True
        def _capture_client_image(self, hwnd): return object()
        def _locate_group_tree_header(self, image): return (10, 20, 30, 40)
        def _ocr_group_tree_row_text(self, image, header, index): return "SomethingElse"
        def _click_client(self, hwnd, *xy): self.click_called = True
        def _win32gui(self):
            class W:
                @staticmethod
                def GetWindowRect(hwnd): return (0, 0, 100, 100)
            return W

    adapter = Adapter()
    facade = _settle_facade(adapter)

    with pytest.raises(RuntimeError, match="failed exact independent name reread"):
        facade.select_group_lightweight("P4B_0814")
    assert adapter.click_called is False


def test_post_selection_context_check_still_runs_after_settled_boundary(monkeypatch):
    """A settled (True) boundary must not shortcut the existing post-
    selection Items/grid context verification -- success requires both."""
    _fake_clock_installed(monkeypatch)
    adapter = _settle_adapter([True])
    adapter._anchor_offset = lambda image: None
    facade = _settle_facade(adapter)

    with pytest.raises(RuntimeError, match="selected Items/grid context is not established"):
        facade.select_group_lightweight("P4B_0814")


# -- create_group(): trust-based creation, no per-group identity proof --
#
# Product decision: group identity is safety-critical only immediately
# before that group's own line items are entered -- select_group_
# lightweight() (completely unchanged) already, independently proves it
# there. Requiring create_group() to ALSO re-prove identity right after
# creation proved repeatedly, live-caught fragile: exact OCR of an
# arbitrary new name ("Ext_Surfaces" stably misread as "Ext_Surtaces"),
# and even a structural physical-delta/hierarchy replacement of that OCR
# check still ended up depending on calibration's Subtotal column header
# ("Subtotal header fallback found 0 bounded candidate(s)") in a narrower
# window where the group tree itself remained perfectly readable. Neither
# failure mode ever reflected an actual creation problem -- the group was
# always physically created correctly. create_group() now only confirms
# the New Group dialog itself opened, accepted the typed name, and
# closed, with nothing blocking left behind.

class _FakeClock:
    """Advances instantly on sleep() -- lets bounded-deadline loops be
    tested without a real multi-second wait."""
    def __init__(self): self.now = 0.0
    def perf_counter(self): return self.now
    def sleep(self, seconds): self.now += seconds


class _MinimalCreateGroupAdapter:
    """Drives WindowsGroupBatchUI.create_group()'s full dialog-driven
    creation sequence. Deliberately has no snapshot_group_names,
    _snapshot_group_names_from_image, _ocr_group_tree_row_text,
    _group_tree_row_indent_x, _locate_label, _ocr_data, or selection-
    boundary method at all: create_group() must never call any of them --
    it only confirms the New Group dialog opened, accepted the name, and
    closed, with nothing blocking left behind."""
    expected_project_name = "TEST"
    _GROUP_MENU_NEW_INDEX = 15

    def __init__(self, *, dialog_appears=True, dialog_closes=True):
        self._dialog_open = False
        self._click_count = 0
        self.evidence_dir = None
        self._dialog_appears = dialog_appears
        self._dialog_closes = dialog_closes

    def verify_application(self): return True
    def verify_project(self): return True
    def _unexpected_dialog_present(self): return False
    def _find_dropdown_window(self): return None
    def _ensure_main_window(self): return 1
    def _capture_client_image(self, hwnd): return object()
    def _locate_group_tree_header(self, image): return (10, 20, 30, 40)
    def _open_group_tree_context_menu(self, hwnd, header, row_index): return ["item"] * 20
    def _click_group_menu_item(self, items, index):
        if self._dialog_appears:
            self._dialog_open = True
    def _find_window_by_title(self, title):
        return 999 if (title == "New Group" and self._dialog_open) else None
    def _click_client(self, hwnd, *xy):
        self._click_count += 1
        if self._click_count == 2 and self._dialog_closes:  # the Attach/OK click closes the dialog
            self._dialog_open = False
    def _select_all_and_delete(self): pass
    def _type_keybdevent(self, text, char_interval_s=None): pass


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


def test_creation_succeeds_when_dialog_submits_and_closes(tmp_path):
    adapter = _MinimalCreateGroupAdapter()
    facade = _create_group_facade(adapter, tmp_path)

    detail = facade.create_group("Ext_Surfaces")

    assert detail["creation_state"] == "created"
    assert detail["verification_method"] == "new_group_dialog_submitted_and_closed"
    assert adapter._click_count == 2  # name-field click + attach click, nothing more


def test_creation_ignores_ocr_of_the_new_row_entirely(tmp_path):
    """_MinimalCreateGroupAdapter has no snapshot_group_names,
    _snapshot_group_names_from_image, _ocr_group_tree_row_text, or any
    other row-reading method at all -- if create_group() tried to read
    the new row's text for any reason, this would raise AttributeError
    instead of succeeding. The requested name is deliberately one already
    proven to OCR incorrectly in this exact layout ("Ext_Surfaces" ->
    "Ext_Surtaces" live) -- irrelevant here, since nothing ever reads it."""
    adapter = _MinimalCreateGroupAdapter()
    facade = _create_group_facade(adapter, tmp_path)

    detail = facade.create_group("Ext_Surfaces")

    assert detail["creation_state"] == "created"


def test_creation_ignores_subtotal_header_availability(monkeypatch, tmp_path):
    """Direct proof, not just an absent-method inference: even if
    xactimate_calibration's Subtotal-dependent helpers WOULD raise
    (unavailable Subtotal column, as live-caught in a narrower window),
    they must never be reached by creation at all."""
    import estimate_extractor.xactimate_lookup.xactimate_calibration as calibration_module

    def _must_not_be_called(*_args, **_kwargs):
        raise AssertionError("create_group() must never call calibration's Subtotal-dependent inventory")
    monkeypatch.setattr(calibration_module, "_group_column_inventory", _must_not_be_called)
    monkeypatch.setattr(calibration_module, "_locate_group_column_headers", _must_not_be_called)

    adapter = _MinimalCreateGroupAdapter()
    facade = _create_group_facade(adapter, tmp_path)

    detail = facade.create_group("Ext_Surfaces")

    assert detail["creation_state"] == "created"


def test_creation_fails_closed_when_dialog_does_not_close(monkeypatch, tmp_path):
    clock = _fake_clock_installed(monkeypatch)
    adapter = _MinimalCreateGroupAdapter(dialog_closes=False)
    facade = _create_group_facade(adapter, tmp_path)

    with pytest.raises(RuntimeError, match="New Group dialog did not close"):
        facade.create_group("Ext_Surfaces")
    assert clock.now > 0  # genuinely waited within the bound, not a single immediate check


def test_creation_fails_closed_when_dialog_never_appears(monkeypatch, tmp_path):
    clock = _fake_clock_installed(monkeypatch)
    adapter = _MinimalCreateGroupAdapter(dialog_appears=False)
    facade = _create_group_facade(adapter, tmp_path)

    with pytest.raises(RuntimeError, match="New Group dialog did not appear"):
        facade.create_group("Ext_Surfaces")


def test_creation_refuses_and_creates_nothing_when_target_already_present(tmp_path):
    adapter = _MinimalCreateGroupAdapter()
    facade = _create_group_facade(adapter, tmp_path)
    facade._initial_rows = ["TEST", "Ext_Surfaces"]

    detail = facade.create_group("Ext_Surfaces")

    assert detail == {"creation_state": "already_present_exact", "verification_method": "initial_exact_inventory"}
    assert adapter._click_count == 0  # no dialog interaction attempted at all


def test_creation_fails_closed_when_blocking_ui_remains_after_creation(tmp_path):
    adapter = _MinimalCreateGroupAdapter()
    # A blocking dialog appears only once the New Group dialog itself has
    # already closed (2nd click) -- proves this is detected after
    # creation, not confused with the New Group dialog itself.
    adapter._unexpected_dialog_present = lambda: adapter._click_count >= 2
    facade = _create_group_facade(adapter, tmp_path)

    with pytest.raises(RuntimeError, match="blocking UI appeared after creation"):
        facade.create_group("Ext_Surfaces")


# -- integration: creation is trust-based, selection remains the sole,
# unchanged identity gate before any item entry --

def test_selection_failure_prevents_any_items_from_entering_the_unverified_group():
    """Creation is now trust-based (no per-group verification) -- proves
    identity is still safety-critical exactly where it always was:
    immediately before item entry, via select_group_lightweight()
    (unchanged). Group A's items must be typed (A's selection succeeds);
    group B's items must NEVER be typed, because B's selection fails."""
    ui = UI(fail_select_for="B")
    with pytest.raises(RuntimeError, match="fresh selection boundary is absent"):
        execute_group_first_plan(compile_executable_group_plan(_shadow()), ui, clock=Clock())

    assert ("create", "A") in ui.events and ("create", "B") in ui.events  # creation is trust-based, both requested
    a_select = ui.events.index(("select", "A"))
    b_select = ui.events.index(("select", "B"))
    assert any(event[0] == "type" for event in ui.events[a_select:b_select])  # A's items were entered
    assert not any(event[0] == "type" for event in ui.events[b_select:])  # zero items entered for B


def test_creation_relaxed_but_selection_still_required_before_each_groups_items():
    """End-to-end proof of the new architecture: creation is trust-based,
    but each group's identity is still independently, freshly established
    by select_group_lightweight() (completely unchanged) immediately
    before its own items are entered -- create all groups, verify the
    complete set, select group A, enter its items, select group B, enter
    its items."""
    ui = UI()
    report = execute_group_first_plan(compile_executable_group_plan(_shadow()), ui, clock=Clock())

    assert ui.events.index(("create", "A")) < ui.events.index(("create", "B")) < ui.events.index(("verify-all", ("A", "B")))
    assert ui.events.index(("verify-all", ("A", "B"))) < ui.events.index(("select", "A"))
    a_select = ui.events.index(("select", "A"))
    b_select = ui.events.index(("select", "B"))
    assert a_select < b_select
    assert any(event[0] == "type" for event in ui.events[a_select:b_select])
    assert any(event[0] == "type" for event in ui.events[b_select:])
    assert report["normal_item_count"] == 3 and report["bid_item_count"] == 1


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
