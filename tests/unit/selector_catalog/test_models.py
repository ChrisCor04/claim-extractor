from __future__ import annotations

from estimate_extractor.selector_catalog.models import (
    ScreenshotManifestEntry,
    SelectorRecord,
    SourceReference,
    is_selector_plausible,
    is_truncated,
    normalize_description,
    probable_unit_contamination,
)


def test_normalize_description_lowercases_and_collapses_whitespace():
    assert normalize_description("  Flashing  -  pipe   jack ") == "flashing - pipe jack"


def test_normalize_description_never_mutates_input():
    original = "Flashing - Pipe Jack"
    normalize_description(original)
    assert original == "Flashing - Pipe Jack"


def test_normalize_description_normalizes_unicode_punctuation():
    assert normalize_description("Flashing – pipe jack…") == "flashing - pipe jack..."
    assert normalize_description("5’ stud") == "5' stud"


def test_normalize_description_collapses_repeated_punctuation():
    assert normalize_description("Something....") == "something..."
    assert normalize_description("Something----else") == "something-else"


def test_is_truncated_detects_ellipsis_variants():
    assert is_truncated("Add for tear out wd flr glued down...") is True
    assert is_truncated("Add for tear out wd flr glued down..") is True
    assert is_truncated("Add for tear out wd flr glued down…") is True
    assert is_truncated("Flashing - pipe jack") is False


def test_selector_record_key_is_category_selector_tuple():
    record = SelectorRecord(
        category="RFG", selector="FLPIPE", description_original="Flashing - pipe jack", description_normalized="flashing - pipe jack"
    )
    assert record.key == ("RFG", "FLPIPE")


def test_selector_record_primary_source_image_is_first_source():
    record = SelectorRecord(
        category="RFG",
        selector="FLPIPE",
        description_original="Flashing - pipe jack",
        description_normalized="flashing - pipe jack",
        source_images=[
            SourceReference(source_image="a.png", source_folder="RFG", source_sequence=1, ocr_confidence=0.9, row_index=0),
            SourceReference(source_image="b.png", source_folder="RFG", source_sequence=2, ocr_confidence=0.8, row_index=3),
        ],
    )
    assert record.primary_source_image == "a.png"


def test_selector_record_round_trips_through_dict():
    record = SelectorRecord(
        category="RFG",
        selector="FLPIPE",
        description_original="Flashing - pipe jack",
        description_normalized="flashing - pipe jack",
        needs_review=True,
        review_reasons=["low_ocr_confidence"],
        ocr_confidence=0.7,
        title_bar_category="RFG",
        category_mismatch=False,
        conflicting_descriptions=["Flashing - pipe jack v2"],
        source_images=[SourceReference(source_image="a.png", source_folder="RFG", source_sequence=1, ocr_confidence=0.7, row_index=0)],
    )
    restored = SelectorRecord.from_dict(record.to_dict())
    assert restored.to_dict() == record.to_dict()


def test_screenshot_manifest_entry_round_trips_through_dict():
    entry = ScreenshotManifestEntry(
        relative_path="Screenshots_By_CAT/RFG/RFG_001_x.png",
        folder_category="RFG",
        sequence=1,
        status="processed",
        rows_extracted=42,
        title_bar_category="RFG",
        category_mismatch=False,
        processed_at="2026-01-01T00:00:00+00:00",
    )
    restored = ScreenshotManifestEntry.from_dict(entry.to_dict())
    assert restored.to_dict() == entry.to_dict()


# --- probable_unit_contamination (QA cleanup pass) --------------------------


def test_probable_unit_contamination_detects_trailing_unit_token():
    assert probable_unit_contamination("Anchor - twist in ground type EA") == "EA"


def test_probable_unit_contamination_case_insensitive():
    assert probable_unit_contamination("Anchor - twist in ground type ea") == "EA"


def test_probable_unit_contamination_none_when_no_trailing_unit_token():
    assert probable_unit_contamination("Flashing - pipe jack") is None


def test_probable_unit_contamination_none_for_empty_description():
    assert probable_unit_contamination("") is None
    assert probable_unit_contamination(None) is None  # type: ignore[arg-type]


def test_probable_unit_contamination_does_not_flag_unit_like_word_mid_sentence():
    # "SF" appears, but not as the LAST token -- not a contamination candidate.
    assert probable_unit_contamination("180 SF of coverage total") is None


def test_probable_unit_contamination_flags_legitimate_looking_text_too():
    """This is a *candidate* heuristic, not proof -- real, legitimate
    descriptions like '...over 180 SF' are expected to be flagged as
    candidates here (and then cleared by coordinate re-verification, see
    test_cleanup.py), not silently ignored by the heuristic itself."""
    assert probable_unit_contamination("Sunroom / Garden Room kit - over 180 SF") == "SF"


# --- is_selector_plausible (QA cleanup pass) ---------------------------------


def test_is_selector_plausible_true_for_real_shaped_codes():
    for selector in ("FLPIPE", "GCR240H", "ST", "ST+", "ST++", "SG2", "IPO1>", "ISO3<", "SH5/8"):
        assert is_selector_plausible(selector) is True, selector


def test_is_selector_plausible_false_for_lowercase_contamination():
    for selector in ("amrv", "oRiIocm", "stiR+", "Bites", "pps"):
        assert is_selector_plausible(selector) is False, selector


def test_is_selector_plausible_true_for_empty_selector():
    # Empty selectors are handled separately (review-queue routing for
    # missing values) -- this function only judges shape.
    assert is_selector_plausible("") is True
