from __future__ import annotations

from estimate_extractor.selector_recommendation import candidate_generation
from estimate_extractor.selector_recommendation.models import RecommendationInput


def test_hinted_categories_from_trade(rules):
    item = RecommendationInput(line_item_id="x", trade="roofing")
    assert candidate_generation.hinted_categories(item, rules) == ["RFG"]


def test_hinted_categories_from_component_adds_to_trade_hints(rules):
    item = RecommendationInput(line_item_id="x", trade="roofing", component="composition_shingles")
    # trade hint (RFG) and component hint (RFG) both resolve to RFG -- deduplicated, order preserved.
    assert candidate_generation.hinted_categories(item, rules) == ["RFG"]


def test_hinted_categories_empty_when_no_hint_available(rules):
    item = RecommendationInput(line_item_id="x", trade="unknown_trade")
    assert candidate_generation.hinted_categories(item, rules) == []


def test_hinted_categories_missing_trade_does_not_crash(rules):
    item = RecommendationInput(line_item_id="x")
    assert candidate_generation.hinted_categories(item, rules) == []


def test_tokenize_lowercases_and_splits_on_non_alnum():
    assert candidate_generation.tokenize("Tear-off Composition Shingles, 3-tab!") == {
        "tear", "off", "composition", "shingles", "3", "tab"
    }


def test_tokenize_empty_text_returns_empty_set():
    assert candidate_generation.tokenize(None) == set()
    assert candidate_generation.tokenize("") == set()


def test_generate_candidate_pool_uses_category_hint(db_conn, rules):
    item = RecommendationInput(line_item_id="x", trade="roofing", original_description="Tear off composition shingles")
    pool, categories = candidate_generation.generate_candidate_pool(item, db_conn, rules)
    assert categories == ["RFG"]
    assert all(r.category == "RFG" for r in pool)
    assert not any(r.needs_review for r in pool)


def test_generate_candidate_pool_falls_back_to_full_pool_without_hint(db_conn, rules):
    item = RecommendationInput(line_item_id="x", trade="unknown_trade", original_description="Dumpster load debris")
    pool, categories = candidate_generation.generate_candidate_pool(item, db_conn, rules)
    assert categories == []
    assert {r.category for r in pool} == {"RFG", "PNT", "DOR", "DMO"}  # eligible pool, needs_review excluded


def test_generate_candidate_pool_include_uncertain_can_surface_needs_review(db_conn, rules):
    item = RecommendationInput(line_item_id="x", trade="roofing", original_description="roofing felt")
    pool, _categories = candidate_generation.generate_candidate_pool(item, db_conn, rules, include_uncertain=True)
    assert any(r.needs_review for r in pool)


def test_keyword_prefilter_ranks_by_token_overlap(rules):
    from estimate_extractor.selector_catalog.models import SelectorRecord

    def rec(selector, description):
        return SelectorRecord(
            category="RFG",
            selector=selector,
            description_original=description,
            description_normalized=description.lower(),
        )

    pool = [
        rec("A", "unrelated drywall texture"),
        rec("B", "tear off composition shingles"),
        rec("C", "composition shingle starter course"),
    ]
    item = RecommendationInput(line_item_id="x", original_description="tear off composition shingles 3 tab")
    filtered = candidate_generation._keyword_prefilter(item, pool, limit=2)
    assert [r.selector for r in filtered] == ["B", "C"]
