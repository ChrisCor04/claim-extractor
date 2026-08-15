from __future__ import annotations

from pathlib import Path

from estimate_extractor.xactimate_lookup.offline_catalog_mapper import OfflineCatalogMapper
from estimate_extractor.xactimate_lookup.shadow_quick_entry_plan import build_shadow_plan, render_shadow_report


PROJECT = Path(__file__).resolve().parents[3] / "projects" / "odom-insurance-v2"


def test_real_estimate_builds_complete_group_first_shadow_plan():
    mapper = OfflineCatalogMapper()
    plan = build_shadow_plan(PROJECT, mapper)
    assert plan["project"] == "odom-insurance-v2"
    assert plan["summary"]["total_items"] == 35
    assert sum(plan["summary"][key] for key in ("resolved", "ambiguous", "bid_item_fallback")) == 35
    assert {item["line_item_id"] for item in plan["items"]} == {f"line_{index:04d}" for index in range(1, 36)}
    assert sum(len(group["line_item_ids"]) for group in plan["group_first_future_layout"]) == 35
    for item in plan["items"]:
        assert item["group"] and item["original_description"]
        assert item["catalog_search_text"]
        assert "unit_price" in item["source_pricing"]
        if item["resolution"] == "resolved":
            assert (item["category"], item["selector"]) in mapper.catalog.by_identity
            assert item["execution_state"] == "fast_normal_item_ready"
            assert (item["execution_category"], item["execution_selector"]) == (item["category"], item["selector"])
        elif item["resolution"] == "ambiguous":
            assert item["category"] is None and item["selector"] is None
            assert len(item["top_candidates"]) == 10
            assert (item["execution_category"], item["execution_selector"]) == ("DOR", "BIDITM")
            assert item["execution_description"] == item["original_description"]
        else:
            assert (item["category"], item["selector"]) == ("DOR", "BIDITM")
            assert item["execution_state"] == "fast_bid_item_ready"
            assert (item["execution_category"], item["execution_selector"]) == ("DOR", "BIDITM")
            assert item["quantity"] is not None and item["unit"]

    chair = next(item for item in plan["items"] if item["original_description"] == "Chair - Pillow / Pad - Standard grade")
    assert chair["resolution"] == "ambiguous"
    assert (chair["execution_category"], chair["execution_selector"]) == ("DOR", "BIDITM")
    flashing = next(item for item in plan["items"] if item["original_description"] == "R&R Flashing - pipe jack")
    assert flashing["catalog_search_text"] == "flashing pipe jack"
    assert flashing["source_action"] == "remove_replace"


def test_shadow_report_is_human_readable_and_mentions_fallback_policy():
    report = render_shadow_report(build_shadow_plan(PROJECT, OfflineCatalogMapper()))
    assert "Phase 3 shadow Quick Entry plan" in report
    assert "DOR/BIDITM fallback" in report
    assert "Live execution: disabled" in report
    assert "Main Roof" in report
