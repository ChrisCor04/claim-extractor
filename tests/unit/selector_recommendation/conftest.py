from __future__ import annotations

import sqlite3

import pytest

from estimate_extractor.selector_catalog import database
from estimate_extractor.selector_catalog.models import SelectorRecord, SourceReference, normalize_description
from estimate_extractor.selector_recommendation.candidate_generation import RecommendationRules


def make_record(
    category: str,
    selector: str,
    description: str,
    *,
    needs_review: bool = False,
    review_reasons: list[str] | None = None,
    ocr_confidence: float | None = 0.95,
    source_image: str | None = "a.png",
) -> SelectorRecord:
    source_images = (
        [SourceReference(source_image=source_image, source_folder=category, source_sequence=1, ocr_confidence=ocr_confidence, row_index=0)]
        if source_image
        else []
    )
    return SelectorRecord(
        category=category,
        selector=selector,
        description_original=description,
        description_normalized=normalize_description(description),
        needs_review=needs_review,
        review_reasons=review_reasons or (["low_ocr_confidence"] if needs_review else []),
        ocr_confidence=ocr_confidence,
        source_images=source_images,
    )


SAMPLE_RECORDS = [
    make_record("RFG", "ARMVN", "Tear off composition shingles - 3 tab (no haul off)"),
    make_record("RFG", "FELT3O", "Roofing felt - 30 lb."),
    make_record("RFG", "MALFORM", "roofing felt - garbled ocr text", needs_review=True, ocr_confidence=0.4),
    make_record("PNT", "PWASH", "Clean with pressure/chemical spray"),
    make_record("DOR", "OHDOOR", "Overhead door - steel"),
    make_record("DMO", "DUMP", "Dumpster load - Approx. 20 yards, 4 tons of debris"),
]


@pytest.fixture
def db_conn(tmp_path) -> sqlite3.Connection:
    conn = database.create_database(tmp_path / "test.db")
    database.replace_all_records(conn, SAMPLE_RECORDS)
    yield conn
    conn.close()


@pytest.fixture
def rules() -> RecommendationRules:
    return RecommendationRules(
        trade_category_hints={"roofing": ("RFG",), "painting": ("PNT",), "doors": ("DOR",), "demolition": ("DMO",)},
        component_category_hints={"composition_shingles": ("RFG",)},
        grade_vocabulary=("standard grade", "high grade", "premium grade"),
        action_vocabulary={
            "remove": ("remove", "tear off"),
            "install": ("install",),
            "clean": ("clean",),
            "pressure_wash": ("pressure wash", "pressure/chemical spray"),
        },
        distinction_keyword_groups=(
            ("remove", "replace", "detach and reset"),
            ("metal", "wood", "tile", "vinyl", "concrete"),
        ),
    )
