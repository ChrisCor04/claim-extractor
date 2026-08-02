from __future__ import annotations

from estimate_extractor.selector_catalog.deduplicator import merge_records
from estimate_extractor.selector_catalog.models import SelectorRecord, SourceReference


def _candidate(category, selector, description, *, source_image="a.png", sequence=1, confidence=0.9, truncated_reason=None):
    reasons = [truncated_reason] if truncated_reason else []
    return SelectorRecord(
        category=category,
        selector=selector,
        description_original=description,
        description_normalized=description.lower(),
        needs_review=bool(reasons),
        review_reasons=reasons,
        ocr_confidence=confidence,
        source_images=[
            SourceReference(source_image=source_image, source_folder=category, source_sequence=sequence, ocr_confidence=confidence, row_index=0)
        ],
    )


def test_single_candidate_passes_through_unmerged():
    result = merge_records([_candidate("RFG", "FLPIPE", "Flashing - pipe jack")])
    assert len(result.records) == 1
    assert result.records[0].source_images[0].source_image == "a.png"


def test_overlapping_screenshots_merge_identical_descriptions_and_retain_all_sources():
    candidates = [
        _candidate("RFG", "FLPIPE", "Flashing - pipe jack", source_image="a.png", sequence=1),
        _candidate("RFG", "FLPIPE", "Flashing - pipe jack", source_image="b.png", sequence=2),
    ]
    result = merge_records(candidates)
    assert len(result.records) == 1
    record = result.records[0]
    assert {s.source_image for s in record.source_images} == {"a.png", "b.png"}
    assert result.conflicts == []


def test_truncated_vs_complete_description_prefers_complete_not_flagged_as_conflict():
    candidates = [
        _candidate("RFG", "FCWGC", "Add for tear out wd flr glued down over concrete...", truncated_reason="description_truncated_in_screenshot"),
        _candidate("RFG", "FCWGC", "Add for tear out wd flr glued down over concrete subfloor"),
    ]
    result = merge_records(candidates)
    assert len(result.records) == 1
    assert result.records[0].description_original == "Add for tear out wd flr glued down over concrete subfloor"
    assert result.conflicts == []
    assert result.records[0].needs_review is False


def test_near_duplicate_ocr_noise_not_flagged_as_conflict():
    """Two OCR reads of the same on-screen text with a single misread
    character (a common OCR artifact) must not be treated as a genuine
    content conflict."""
    candidates = [
        _candidate("RFG", "FLPB6", "Flash parapet wall only - bitumen - over 3° up to 6"),
        _candidate("RFG", "FLPB6", "Flash parapet wall only - bitumen - over 3' up to 6"),
    ]
    result = merge_records(candidates)
    assert len(result.records) == 1
    assert result.conflicts == []


def test_genuinely_conflicting_descriptions_flagged_never_silently_dropped():
    candidates = [
        _candidate("RFG", "CUP>", "Cupola - Wood - Large"),
        _candidate("RFG", "CUP>", "Cupola - Copper - Large"),
    ]
    result = merge_records(candidates)
    assert len(result.records) == 1
    record = result.records[0]
    assert record.needs_review is True
    assert "conflicting_descriptions" in record.review_reasons
    assert len(result.conflicts) == 1
    assert set(result.conflicts[0][2]) == {"Cupola - Wood - Large", "Cupola - Copper - Large"}
    # The losing description is preserved for the reviewer, not discarded.
    assert "Cupola - Wood - Large" in record.conflicting_descriptions or "Cupola - Copper - Large" in record.conflicting_descriptions


def test_same_selector_different_category_never_merged():
    candidates = [
        _candidate("RFG", "MN", "Roofing labor minimum"),
        _candidate("ACT", "MN", "Acoustical labor minimum"),
    ]
    result = merge_records(candidates)
    assert len(result.records) == 2
    keys = {r.key for r in result.records}
    assert keys == {("RFG", "MN"), ("ACT", "MN")}


def test_empty_key_candidates_are_never_emitted_as_records():
    candidates = [
        _candidate("RFG", "", "Some description"),
        _candidate("", "FLPIPE", "Some description"),
        _candidate("RFG", "FLPIPE", "Flashing - pipe jack"),
    ]
    result = merge_records(candidates)
    assert len(result.records) == 1
    assert result.records[0].key == ("RFG", "FLPIPE")


def test_category_mismatch_propagates_to_merged_record():
    a = _candidate("ELE", "CRS", "Casing - Detach & reset")
    a.category_mismatch = True
    a.title_bar_category = "FNC"
    b = _candidate("ELE", "CRS", "Casing - Detach & reset", source_image="b.png")
    result = merge_records([a, b])
    assert result.records[0].category_mismatch is True
    assert result.records[0].title_bar_category == "FNC"
    assert "category_mismatch" in result.records[0].review_reasons


def test_provenance_preserved_across_three_way_merge():
    candidates = [
        _candidate("RFG", "GSTOP", "Gravel stop", source_image="a.png", sequence=1),
        _candidate("RFG", "GSTOP", "Gravel stop", source_image="b.png", sequence=2),
        _candidate("RFG", "GSTOP", "Gravel stop", source_image="c.png", sequence=3),
    ]
    result = merge_records(candidates)
    assert len(result.records[0].source_images) == 3
    assert {s.source_sequence for s in result.records[0].source_images} == {1, 2, 3}
