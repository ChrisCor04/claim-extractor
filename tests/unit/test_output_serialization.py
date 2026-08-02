import json

import pytest
from pydantic import ValidationError

from estimate_extractor.models.canonical import (
    CanonicalEstimate,
    DocumentMeta,
    ExtractionStatus,
    LineItem,
    LineItemConfidence,
    LineItemSource,
    ValidationState,
)
from estimate_extractor.parsing.state_machine import IdFactory


def _minimal_document_meta() -> DocumentMeta:
    return DocumentMeta(
        source_filename="test.pdf",
        source_sha256="deadbeef",
        carrier_detected="State Farm",
        carrier_confidence=0.99,
        page_count=1,
        extraction_status=ExtractionStatus.SUCCESS,
        extractor_version="0.1.0",
    )


def test_money_fields_serialize_as_json_numbers_not_strings():
    line_item = LineItem(
        line_item_id="line_0001",
        description="Test item",
        description_normalized_whitespace="Test item",
        quantity=33.66,
        unit_price=68.75,
        replacement_cost_value=2314.13,
        actual_cash_value=2314.13,
        source=LineItemSource(page_start=1, page_end=1, raw_text="raw"),
        confidence=LineItemConfidence(
            overall=0.95,
            description=0.95,
            quantity=0.95,
            unit_of_measure=0.95,
            unit_price=0.95,
            tax=0.95,
            replacement_cost_value=0.95,
            depreciation=0.95,
            actual_cash_value=0.95,
        ),
    )
    canonical = CanonicalEstimate(
        document=_minimal_document_meta(),
        line_items=[line_item],
        validation_state=ValidationState(status=ExtractionStatus.SUCCESS),
    )
    dumped = canonical.model_dump(mode="json")
    raw_json = json.dumps(dumped)
    reparsed = json.loads(raw_json)
    item = reparsed["line_items"][0]
    assert isinstance(item["replacement_cost_value"], float)
    assert item["replacement_cost_value"] == 2314.13
    assert isinstance(item["quantity"], float)


def test_confidence_out_of_bounds_rejected():
    with pytest.raises(ValidationError):
        LineItemConfidence(
            overall=1.5,  # invalid: > 1.0
            description=0.9,
            quantity=0.9,
            unit_of_measure=0.9,
            unit_price=0.9,
            tax=0.9,
            replacement_cost_value=0.9,
            depreciation=0.9,
            actual_cash_value=0.9,
        )


def test_unknown_field_rejected_strict_schema():
    with pytest.raises(ValidationError):
        DocumentMeta(
            source_filename="test.pdf",
            source_sha256="deadbeef",
            carrier_detected="State Farm",
            carrier_confidence=0.99,
            page_count=1,
            extraction_status=ExtractionStatus.SUCCESS,
            extractor_version="0.1.0",
            some_unexpected_field="oops",
        )


def test_id_factory_deterministic_document_order():
    factory = IdFactory()
    assert factory.next("coverage") == "coverage_001"
    assert factory.next("coverage") == "coverage_002"
    assert factory.next("area") == "area_001"
    assert factory.next_line_item() == "line_0001"
    assert factory.next_line_item() == "line_0002"
    assert factory.next("section") == "section_001"
