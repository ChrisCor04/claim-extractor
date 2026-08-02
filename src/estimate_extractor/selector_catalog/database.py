"""SQLite persistence for the canonical selector catalog. Stdlib
``sqlite3`` only -- no new dependency, consistent with this repository's
minimal-dependency footprint.
"""

from __future__ import annotations

import json
import sqlite3
from difflib import SequenceMatcher
from pathlib import Path

from estimate_extractor.selector_catalog.models import SelectorRecord, SourceReference, normalize_description

SCHEMA = """
CREATE TABLE IF NOT EXISTS selectors (
    category TEXT NOT NULL,
    selector TEXT NOT NULL,
    description_original TEXT NOT NULL,
    description_normalized TEXT NOT NULL,
    needs_review INTEGER NOT NULL DEFAULT 0,
    review_reasons TEXT NOT NULL DEFAULT '[]',
    ocr_confidence REAL,
    title_bar_category TEXT,
    category_mismatch INTEGER NOT NULL DEFAULT 0,
    conflicting_descriptions TEXT NOT NULL DEFAULT '[]',
    primary_source_image TEXT,
    source_images_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (category, selector)
);

CREATE INDEX IF NOT EXISTS idx_selectors_category ON selectors(category);
CREATE INDEX IF NOT EXISTS idx_selectors_selector ON selectors(selector);
CREATE INDEX IF NOT EXISTS idx_selectors_description_normalized ON selectors(description_normalized);
CREATE INDEX IF NOT EXISTS idx_selectors_needs_review ON selectors(needs_review);
"""


def create_database(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def replace_all_records(conn: sqlite3.Connection, records: list[SelectorRecord]) -> None:
    """Full rebuild of the selectors table -- appropriate here since the
    catalog is always regenerated from the complete, deduplicated record
    set for a given import run (not incrementally patched row by row)."""
    conn.execute("DELETE FROM selectors")
    rows = [
        (
            r.category,
            r.selector,
            r.description_original,
            r.description_normalized,
            int(r.needs_review),
            json.dumps(r.review_reasons),
            r.ocr_confidence,
            r.title_bar_category,
            int(r.category_mismatch),
            json.dumps(r.conflicting_descriptions),
            r.primary_source_image,
            json.dumps([s.to_dict() for s in r.source_images]),
            r.created_at,
            r.updated_at,
        )
        for r in records
    ]
    conn.executemany(
        """
        INSERT INTO selectors (
            category, selector, description_original, description_normalized,
            needs_review, review_reasons, ocr_confidence, title_bar_category,
            category_mismatch, conflicting_descriptions, primary_source_image,
            source_images_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()


def _row_to_record(row: sqlite3.Row) -> SelectorRecord:
    return SelectorRecord(
        category=row["category"],
        selector=row["selector"],
        description_original=row["description_original"],
        description_normalized=row["description_normalized"],
        needs_review=bool(row["needs_review"]),
        review_reasons=json.loads(row["review_reasons"]),
        ocr_confidence=row["ocr_confidence"],
        title_bar_category=row["title_bar_category"],
        category_mismatch=bool(row["category_mismatch"]),
        conflicting_descriptions=json.loads(row["conflicting_descriptions"]),
        source_images=[SourceReference.from_dict(s) for s in json.loads(row["source_images_json"])],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def load_all_records(conn: sqlite3.Connection) -> list[SelectorRecord]:
    conn.row_factory = sqlite3.Row
    cur = conn.execute("SELECT * FROM selectors ORDER BY category, selector")
    return [_row_to_record(row) for row in cur.fetchall()]


def search_records(
    conn: sqlite3.Connection,
    query: str | None = None,
    category: str | None = None,
    selector: str | None = None,
    needs_review: bool | None = None,
) -> list[SelectorRecord]:
    """Exact CAT/SEL lookup (pass both), selector-across-categories lookup
    (pass only selector), description substring / normalized-token search
    (pass query), category-constrained search (pass category + query)."""
    conn.row_factory = sqlite3.Row
    clauses = []
    params: list = []
    if category:
        clauses.append("category = ?")
        params.append(category)
    if selector:
        clauses.append("selector = ?")
        params.append(selector)
    if needs_review is not None:
        clauses.append("needs_review = ?")
        params.append(int(needs_review))
    if query:
        clauses.append("(description_normalized LIKE ? OR selector LIKE ? OR category LIKE ?)")
        like = f"%{normalize_description(query)}%"
        params.extend([like, f"%{query.upper()}%", f"%{query.upper()}%"])
    sql = "SELECT * FROM selectors"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY category, selector"
    cur = conn.execute(sql, params)
    return [_row_to_record(row) for row in cur.fetchall()]


def fuzzy_search_records(
    records: list[SelectorRecord], query: str, category: str | None = None, limit: int = 20
) -> list[tuple[SelectorRecord, float]]:
    """Ranks records by description similarity to `query`. Read-only --
    never mutates source records (see build spec "must never modify
    source records")."""
    normalized_query = normalize_description(query)
    candidates = [r for r in records if category is None or r.category == category]
    scored = [
        (r, SequenceMatcher(None, normalized_query, r.description_normalized).ratio()) for r in candidates
    ]
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored[:limit]


def get_automation_eligible_records(records: list[SelectorRecord]) -> list[SelectorRecord]:
    """The ONLY sanctioned way to treat catalog records as safe for
    automatic mapping/automation: excludes every record with
    needs_review=True (malformed selector, probable unit contamination,
    corroborated category mismatch, low OCR confidence, or a truncated/
    conflicting description). This selector catalog is a reference/search
    tool for human reviewers, not an automatic mapping source -- see
    docs/selector-catalog.md -- so any future integration point that
    consumes master_selectors.db for automation purposes MUST filter
    through this function (or an equivalent needs_review check) rather
    than using search results directly."""
    return [r for r in records if not r.needs_review]
