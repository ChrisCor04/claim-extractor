"""Read-only access into the Phase 3.6 canonical selector catalog
(master_selectors.db). Every function here defaults to eligible-only
(``needs_review = 0``) -- see build spec "By default, candidate
generation must query only records where needs_review = 0." Passing
``include_uncertain=True`` is the one sanctioned way to see
needs_review=1 rows, and callers are responsible for visibly labeling
them (see selector_recommendation.explanations).

This module never writes to the database -- see build spec "Do not
modify the source selector database during this phase."
"""

from __future__ import annotations

import sqlite3

from estimate_extractor.selector_catalog import database
from estimate_extractor.selector_catalog.models import SelectorRecord


def load_recommendation_pool(conn: sqlite3.Connection, include_uncertain: bool = False) -> list[SelectorRecord]:
    """Loads the full candidate pool once per recommendation run (not per
    line item) so the staged in-memory filtering in candidate_generation.py
    never re-queries the database per item. 10-13k lightweight dataclass
    rows is inexpensive to hold in memory; the expense the build spec
    warns against is per-item fuzzy scoring, which this staged design
    avoids by category/keyword-filtering first (see candidate_generation.py)."""
    needs_review_filter = None if include_uncertain else False
    return database.search_records(conn, needs_review=needs_review_filter)


def load_pool_for_categories(
    conn: sqlite3.Connection, categories: list[str], include_uncertain: bool = False
) -> list[SelectorRecord]:
    """Category-constrained load -- used when a small, confident category
    hint set is available, avoiding loading the full eligible pool."""
    needs_review_filter = None if include_uncertain else False
    results: list[SelectorRecord] = []
    seen_keys: set[tuple[str, str]] = set()
    for category in categories:
        for record in database.search_records(conn, category=category, needs_review=needs_review_filter):
            if record.key in seen_keys:
                continue
            seen_keys.add(record.key)
            results.append(record)
    return results


def manual_search(
    conn: sqlite3.Connection,
    text: str | None = None,
    category: str | None = None,
    selector: str | None = None,
    include_uncertain: bool = False,
    fuzzy: bool = False,
    limit: int = 25,
) -> list[SelectorRecord]:
    """Backs the manual selector browser (CLI ``selectors search`` and the
    UI's manual search widget) -- eligible-only by default, with the same
    explicit include_uncertain toggle as ranked candidate generation."""
    needs_review_filter = None if include_uncertain else False
    if fuzzy:
        pool = database.search_records(conn, category=category, needs_review=needs_review_filter)
        scored = database.fuzzy_search_records(pool, text or "", category=category, limit=limit)
        return [record for record, _score in scored]
    return database.search_records(
        conn, query=text, category=category, selector=selector, needs_review=needs_review_filter
    )[:limit]
