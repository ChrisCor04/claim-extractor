from __future__ import annotations

import json

from estimate_extractor.xactimate_lookup.fast_grouped_service import (
    execute_saved_fast_grouped_run, prepare_fast_grouped_run, review_rows,
)


def _shadow():
    def item(line_id, cat, sel, resolution, description, quantity, price, source_order):
        return {
            "line_item_id": line_id, "source_order": source_order, "group": "Exterior",
            "original_description": description,
            "quantity": quantity, "unit": "EA", "source_pricing": {"unit_price": price} if price is not None else {},
            "source_action": None, "resolution": resolution,
            "category": cat if resolution == "resolved" else None,
            "selector": sel if resolution == "resolved" else None,
            "execution_category": cat, "execution_selector": sel,
            "catalog_description": description, "catalog_search_text": description,
            "score": .9, "margin": .4, "reason": "test", "top_candidates": [],
            "execution_state": "fast_normal_item_ready" if resolution == "resolved" else "review_required_with_bid_item_fallback",
        }
    return {
        "schema_version": "phase3-shadow-quick-entry-plan-v2", "project": "claim-slug",
        "summary": {"total_items": 2, "resolved": 1, "ambiguous": 1, "bid_item_fallback": 0,
                    "execution_bid_item_fallback": 1},
        "group_first_future_layout": [{"group": "Exterior", "line_item_ids": ["u1", "n1"]}],
        "items": [
            item("u1", "DOR", "BIDITM", "ambiguous", "Unmapped chair", 2, None, 1),
            item("n1", "RFG", "DRIP", "resolved", "Drip edge", 3, 3.75, 2),
        ],
    }


class UI:
    instances = []
    def __init__(self, project, evidence):
        self.events = []; self.keyboard = self; UI.instances.append(self)
    def verify_project_and_no_modal(self, project): self.events.append(("preflight", project))
    def normalize_window(self): self.events.append(("normalize-window",)); return {"ok": True}
    def prepare_group_creation(self, groups): self.events.append(("inventory", tuple(groups))); return "initial"
    def create_group(self, group): self.events.append(("create", group)); return {"creation_state": "created"}
    def verify_all_groups_created(self, groups): self.events.append(("barrier", tuple(groups))); return "complete"
    def select_group_lightweight(self, group): self.events.append(("select", group)); return "retained-map"
    def focus_quick_entry_cat(self): self.events.append(("focus",)); return "cat"
    def assert_batch_settled(self): self.events.append(("settled",))
    def capture_group_evidence(self, group): self.events.append(("capture", group)); return f"{group}.png"
    def capture_final_evidence(self): self.events.append(("final",)); return "final.png"
    def accept_expected_group_local_duplicate(self):
        self.events.append(("duplicate-poll",)); self.press_tab(); self.press_tab(); self.press_enter()
        return {"appearance_wait_seconds": .02, "acceptance_seconds": .02}
    def type_text(self, value): self.events.append(("type", value))
    def replace_text(self, value): self.events.append(("replace", value))
    def press_tab(self): self.events.append(("tab",))
    def press_enter(self): self.events.append(("enter",))


def test_prepare_produces_reviewable_all_executable_plan(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "estimate_extractor.xactimate_lookup.fast_grouped_service.build_shadow_plan",
        lambda project_dir: _shadow(),
    )
    prepared = prepare_fast_grouped_run(tmp_path)
    rows = review_rows(prepared.shadow_plan)
    assert len(rows) == 2 and all(row["CAT"] and row["SEL"] for row in rows)
    assert rows[0]["BIDITM"] is True and rows[0]["source_description"] == "Unmapped chair"
    assert rows[0]["source_pricing"] == {} and rows[1]["source_pricing"]["unit_price"] == 3.75
    assert prepared.json_path.exists() and prepared.report_path.exists()


def _paired_shadow(*, base_quantity):
    """A shadow plan whose sole group is an explicit remove/base source
    pair, plus one unrelated resolved item -- used to prove
    prepare_fast_grouped_run()/review_rows() handle a collapsing pair
    (fewer executable items than source rows) correctly."""
    return {
        "schema_version": "phase3-shadow-quick-entry-plan-v2", "project": "claim-slug",
        "summary": {"total_items": 3, "resolved": 3, "ambiguous": 0, "bid_item_fallback": 0,
                    "execution_bid_item_fallback": 0},
        "group_first_future_layout": [{"group": "Dwelling Roof", "line_item_ids": ["r1", "r2", "o1"]}],
        "items": [
            {
                "line_item_id": "r1", "source_order": 1, "group": "Dwelling Roof",
                "original_description": "Remove Additional charge for widget mounting",
                "quantity": 33.66, "unit": "SQ", "source_pricing": {}, "source_action": "remove",
                "resolution": "resolved", "category": "ABC", "selector": "WIDGET",
                "execution_category": "ABC", "execution_selector": "WIDGET",
                "catalog_description": "Additional charge for widget mounting",
                "catalog_search_text": "widget", "score": .9, "margin": .4, "reason": "test",
                "top_candidates": [], "execution_state": "fast_normal_item_ready",
            },
            {
                "line_item_id": "r2", "source_order": 2, "group": "Dwelling Roof",
                "original_description": "Additional charge for widget mounting",
                "quantity": base_quantity, "unit": "SQ", "source_pricing": {}, "source_action": "install",
                "resolution": "resolved", "category": "ABC", "selector": "WIDGET",
                "execution_category": "ABC", "execution_selector": "WIDGET",
                "catalog_description": "Additional charge for widget mounting",
                "catalog_search_text": "widget", "score": .9, "margin": .4, "reason": "test",
                "top_candidates": [], "execution_state": "fast_normal_item_ready",
            },
            {
                "line_item_id": "o1", "source_order": 3, "group": "Dwelling Roof",
                "original_description": "An unrelated roofing item",
                "quantity": 5.0, "unit": "SQ", "source_pricing": {}, "source_action": None,
                "resolution": "resolved", "category": "RFG", "selector": "OTHER",
                "execution_category": "RFG", "execution_selector": "OTHER",
                "catalog_description": "An unrelated roofing item",
                "catalog_search_text": "unrelated", "score": .9, "margin": .4, "reason": "test",
                "top_candidates": [], "execution_state": "fast_normal_item_ready",
            },
        ],
    }


def test_prepare_accepts_a_collapsing_pair_without_the_1to1_count_assertion_firing(monkeypatch, tmp_path):
    """Regression: prepare_fast_grouped_run() used to require executable
    item count == source row count, which a legitimate collapsed pair
    violates by design. It must validate coverage instead."""
    monkeypatch.setattr(
        "estimate_extractor.xactimate_lookup.fast_grouped_service.build_shadow_plan",
        lambda project_dir: _paired_shadow(base_quantity=33.66),
    )
    prepared = prepare_fast_grouped_run(tmp_path)
    assert len(prepared.shadow_plan["items"]) == 3  # source provenance untouched
    assert sum(len(g.items) for g in prepared.executable_plan.groups) == 2  # pair collapsed


def test_review_rows_shows_no_discrepancy_warning_when_quantities_agree():
    shadow = _paired_shadow(base_quantity=33.66)
    rows = review_rows(shadow)
    # Collapsing is still noted for review visibility, but never as a
    # quantity-discrepancy warning when the two source quantities agree.
    assert "differ" not in rows[0]["pairing_note"]
    assert "differ" not in rows[1]["pairing_note"]
    assert rows[2]["pairing_note"] == ""  # unrelated item untouched


def test_review_rows_surfaces_quantity_discrepancy_before_execution():
    shadow = _paired_shadow(base_quantity=35.67)
    rows = review_rows(shadow)
    note = rows[0]["pairing_note"]
    assert note == rows[1]["pairing_note"]  # both source rows carry the same note
    assert "Source quantities differ: 33.66 vs 35.67" in note
    assert "Xactimate will receive 33.66" in note
    assert rows[2]["pairing_note"] == ""


def test_saved_plan_live_boundary_dispatches_mixed_modes_in_source_order(tmp_path):
    path = tmp_path / "plan.json"; path.write_text(json.dumps(_shadow()), encoding="utf-8")
    report = execute_saved_fast_grouped_run(path, "TEST", tmp_path / "evidence", ui_factory=UI)
    events = UI.instances[-1].events
    assert events.index(("create", "Exterior")) < events.index(("barrier", ("Exterior",))) < events.index(("select", "Exterior"))
    start = events.index(("type", "DOR"))
    assert events[start:start + 14] == [
        ("type", "DOR"), ("type", "BIDITM"), ("tab",), ("replace", "Unmapped chair"),
        ("tab",), ("replace", "2"), ("enter",),
        ("type", "RFG"), ("type", "DRIP"), ("tab",), ("tab",), ("tab",),
        ("type", "3"), ("enter",),
    ]
    assert report["normal_item_count"] == 1 and report["bid_item_count"] == 1
    assert events.count(("capture", "Exterior")) == 1 and events[-1] == ("final",)


def test_app_originated_resolved_bid_bid_resolved_plan_reaches_calibrated_dispatch(tmp_path):
    shadow = _shadow(); normal = shadow["items"][1]; bid = shadow["items"][0]
    n1 = dict(normal); n1.update(line_item_id="n1", source_order=1)
    u1 = dict(bid); u1.update(line_item_id="u1", source_order=2, original_description="First fallback")
    u2 = dict(bid); u2.update(line_item_id="u2", source_order=3, original_description="Second fallback")
    n2 = dict(normal); n2.update(line_item_id="n2", source_order=4, selector="IWS", execution_selector="IWS")
    shadow["items"] = [n1, u1, u2, n2]
    shadow["group_first_future_layout"] = [{"group": "Exterior", "line_item_ids": ["n1", "u1", "u2", "n2"]}]
    path = tmp_path / "app-plan.json"; path.write_text(json.dumps(shadow), encoding="utf-8")
    report = execute_saved_fast_grouped_run(path, "TEST", tmp_path / "evidence", ui_factory=UI)
    events = UI.instances[-1].events
    typed = [event for event in events if event[0] in {"type", "replace", "duplicate-poll"}]
    assert typed == [
        ("type", "RFG"), ("type", "DRIP"), ("type", "3"),
        ("type", "DOR"), ("type", "BIDITM"), ("replace", "First fallback"), ("replace", "2"),
        ("type", "DOR"), ("type", "BIDITM"), ("replace", "Second fallback"), ("replace", "2"),
        ("duplicate-poll",),
        ("type", "RFG"), ("type", "IWS"), ("type", "3"),
    ]
    assert [item["line_item_id"] for item in report["groups"][0]["items"]] == ["n1", "u1", "u2", "n2"]
    assert report["groups"][0]["expected_duplicate_acceptances"][0]["line_item_id"] == "u2"


def test_conservative_app_path_remains_present_and_separate():
    source = open("src/estimate_extractor/ui/components/quick_run_panel.py", encoding="utf-8").read()
    assert "Conservative verified execution" in source
    assert "Fast Grouped Xactimate Entry (experimental)" in source
    assert "run_execution_plan(" in source
    assert "execute_saved_fast_grouped_run(" in source
