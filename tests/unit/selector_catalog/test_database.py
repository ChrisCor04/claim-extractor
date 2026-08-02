from __future__ import annotations

from estimate_extractor.selector_catalog import database
from estimate_extractor.selector_catalog.models import SelectorRecord, SourceReference


def _record(category, selector, description, needs_review=False):
    return SelectorRecord(
        category=category,
        selector=selector,
        description_original=description,
        description_normalized=description.lower(),
        needs_review=needs_review,
        review_reasons=["low_ocr_confidence"] if needs_review else [],
        ocr_confidence=0.9,
        source_images=[SourceReference(source_image="a.png", source_folder=category, source_sequence=1, ocr_confidence=0.9, row_index=0)],
    )


def test_create_database_creates_schema(tmp_path):
    conn = database.create_database(tmp_path / "test.db")
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='selectors'")
    assert cur.fetchone() is not None
    conn.close()


def test_replace_all_records_round_trips(tmp_path):
    conn = database.create_database(tmp_path / "test.db")
    records = [_record("RFG", "FLPIPE", "Flashing - pipe jack"), _record("ACT", "AV", "Acoustic ceiling tile")]
    database.replace_all_records(conn, records)
    loaded = database.load_all_records(conn)
    conn.close()
    assert len(loaded) == 2
    assert {r.key for r in loaded} == {("RFG", "FLPIPE"), ("ACT", "AV")}


def test_replace_all_records_clears_previous_state(tmp_path):
    conn = database.create_database(tmp_path / "test.db")
    database.replace_all_records(conn, [_record("RFG", "A", "First import")])
    database.replace_all_records(conn, [_record("RFG", "B", "Second import")])
    loaded = database.load_all_records(conn)
    conn.close()
    assert len(loaded) == 1
    assert loaded[0].selector == "B"


def test_search_exact_category_and_selector(tmp_path):
    conn = database.create_database(tmp_path / "test.db")
    database.replace_all_records(conn, [_record("RFG", "FLPIPE", "Flashing - pipe jack"), _record("ACT", "FLPIPE", "Unrelated")])
    results = database.search_records(conn, category="RFG", selector="FLPIPE")
    conn.close()
    assert len(results) == 1
    assert results[0].category == "RFG"


def test_search_selector_across_categories(tmp_path):
    conn = database.create_database(tmp_path / "test.db")
    database.replace_all_records(conn, [_record("RFG", "MN", "Roofing labor min"), _record("ACT", "MN", "Acoustical labor min")])
    results = database.search_records(conn, selector="MN")
    conn.close()
    assert len(results) == 2


def test_search_description_substring(tmp_path):
    conn = database.create_database(tmp_path / "test.db")
    database.replace_all_records(conn, [_record("RFG", "FLPIPE", "Flashing - pipe jack"), _record("RFG", "GSTOP", "Gravel stop")])
    results = database.search_records(conn, query="pipe jack")
    conn.close()
    assert len(results) == 1
    assert results[0].selector == "FLPIPE"


def test_search_needs_review_filter(tmp_path):
    conn = database.create_database(tmp_path / "test.db")
    database.replace_all_records(conn, [_record("RFG", "A", "Clear read"), _record("RFG", "B", "Fuzzy read", needs_review=True)])
    results = database.search_records(conn, needs_review=True)
    conn.close()
    assert len(results) == 1
    assert results[0].selector == "B"


def test_fuzzy_search_ranks_by_similarity_and_never_mutates_records():
    records = [_record("RFG", "FLPIPE", "Flashing - pipe jack"), _record("RFG", "GSTOP", "Gravel stop")]
    before = [r.to_dict() for r in records]
    results = database.fuzzy_search_records(records, "pipe jack flashing")
    assert results[0][0].selector == "FLPIPE"
    assert [r.to_dict() for r in records] == before


def test_fuzzy_search_respects_category_filter():
    records = [_record("RFG", "MN", "Roofing labor minimum"), _record("ACT", "MN", "Acoustical labor minimum")]
    results = database.fuzzy_search_records(records, "labor minimum", category="ACT")
    assert len(results) == 1
    assert results[0][0].category == "ACT"


def test_automation_eligible_records_excludes_needs_review():
    """A needs_review record (malformed selector, unit contamination,
    corroborated mismatch, low confidence, etc.) must never be returned as
    safe for automatic mapping -- this is the ONLY sanctioned filter for
    that purpose."""
    clean = _record("RFG", "FLPIPE", "Flashing - pipe jack", needs_review=False)
    flagged = _record("RFG", "stiR", "Something", needs_review=True)
    eligible = database.get_automation_eligible_records([clean, flagged])
    assert eligible == [clean]
    assert flagged not in eligible


def test_automation_eligible_records_empty_when_all_flagged():
    flagged = [_record("RFG", "A", "x", needs_review=True), _record("RFG", "B", "y", needs_review=True)]
    assert database.get_automation_eligible_records(flagged) == []
