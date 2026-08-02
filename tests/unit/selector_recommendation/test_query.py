from __future__ import annotations

from estimate_extractor.selector_recommendation import query


def test_load_recommendation_pool_excludes_needs_review_by_default(db_conn):
    pool = query.load_recommendation_pool(db_conn)
    assert all(not r.needs_review for r in pool)
    assert not any(r.selector == "MALFORM" for r in pool)


def test_load_recommendation_pool_include_uncertain_returns_needs_review_rows(db_conn):
    pool = query.load_recommendation_pool(db_conn, include_uncertain=True)
    assert any(r.selector == "MALFORM" and r.needs_review for r in pool)


def test_load_pool_for_categories_is_scoped_and_deduped(db_conn):
    pool = query.load_pool_for_categories(db_conn, ["RFG", "PNT"])
    assert {r.category for r in pool} == {"RFG", "PNT"}
    assert len({r.key for r in pool}) == len(pool)


def test_load_pool_for_categories_excludes_needs_review_by_default(db_conn):
    pool = query.load_pool_for_categories(db_conn, ["RFG"])
    assert not any(r.needs_review for r in pool)


def test_manual_search_eligible_only_default(db_conn):
    results = query.manual_search(db_conn, text="roofing felt")
    assert all(not r.needs_review for r in results)


def test_manual_search_include_uncertain(db_conn):
    results = query.manual_search(db_conn, category="RFG", include_uncertain=True)
    assert any(r.needs_review for r in results)


def test_manual_search_fuzzy_ranks_by_similarity(db_conn):
    results = query.manual_search(db_conn, text="composition shingle tear off", fuzzy=True, limit=5)
    assert results
    assert results[0].selector == "ARMVN"
