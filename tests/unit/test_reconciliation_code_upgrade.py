"""Regression test: code-upgrade line items must be excluded from
calculated_line_item_total (and summed separately as
paid_when_incurred_total) without being removed from canonical.line_items.

Confirmed against two independent real fixtures (Wei Tang, Odom): in both,
the exact dollar gap between summing every line item and the document's own
reported total is precisely the sum of that document's code-upgrade-flagged
items' RCV.
"""

from __future__ import annotations

from estimate_extractor.config import ValidationConfig
from estimate_extractor.models.canonical import (
    CanonicalEstimate,
    DocumentMeta,
    ExtractionStatus,
    LineItem,
    LineItemConfidence,
    LineItemFlags,
    LineItemSource,
    SummaryTotal,
    SummaryType,
    ValidationState,
)
from estimate_extractor.validation.arithmetic import compute_reconciliation


def _line_item(line_item_id: str, rcv: float, *, is_code_upgrade: bool = False) -> LineItem:
    return LineItem(
        line_item_id=line_item_id,
        description="Test item",
        description_normalized_whitespace="Test item",
        replacement_cost_value=rcv,
        flags=LineItemFlags(is_code_upgrade=is_code_upgrade),
        source=LineItemSource(page_start=1, page_end=1, raw_text="raw"),
        confidence=LineItemConfidence(
            overall=0.9,
            description=0.9,
            quantity=0.9,
            unit_of_measure=0.9,
            unit_price=0.9,
            tax=0.9,
            replacement_cost_value=0.9,
            depreciation=0.9,
            actual_cash_value=0.9,
        ),
    )


def _canonical(line_items: list[LineItem], reported_total: float) -> CanonicalEstimate:
    document = DocumentMeta(
        source_filename="test.pdf",
        source_sha256="deadbeef",
        carrier_detected="Test",
        carrier_confidence=0.99,
        page_count=1,
        extraction_status=ExtractionStatus.SUCCESS,
        extractor_version="0.1.0",
    )
    summary_total = SummaryTotal(
        summary_id="summary_001",
        summary_type=SummaryType.LINE_ITEM_TOTAL,
        label="Line Item Totals: TEST",
        replacement_cost_value=reported_total,
    )
    return CanonicalEstimate(
        document=document,
        line_items=line_items,
        summary_totals=[summary_total],
        validation_state=ValidationState(status=ExtractionStatus.SUCCESS),
    )


def test_code_upgrade_items_excluded_from_calculated_total_but_reported_separately():
    line_items = [
        _line_item("line_0001", 1000.0),
        _line_item("line_0002", 500.0),
        _line_item("line_0003", 141.72, is_code_upgrade=True),
    ]
    canonical = _canonical(line_items, reported_total=1500.0)
    config = ValidationConfig()

    reported, calculated, paid_when_incurred, difference, ok, note = compute_reconciliation(canonical, config)

    assert reported == 1500.0
    assert calculated == 1500.0  # excludes the 141.72 code-upgrade item
    assert paid_when_incurred == 141.72
    assert ok is True
    assert difference == 0.0
    assert "code-upgrade" in note.lower()

    # The code-upgrade item is still present in canonical.line_items --
    # only excluded from the reconciliation sum, never removed from output.
    assert len(canonical.line_items) == 3
    assert any(li.line_item_id == "line_0003" for li in canonical.line_items)


def test_no_code_upgrade_items_behaves_as_before():
    line_items = [_line_item("line_0001", 1000.0), _line_item("line_0002", 500.0)]
    canonical = _canonical(line_items, reported_total=1500.0)
    config = ValidationConfig()

    reported, calculated, paid_when_incurred, difference, ok, note = compute_reconciliation(canonical, config)

    assert calculated == 1500.0
    assert paid_when_incurred is None
    assert ok is True


def test_multiple_code_upgrade_items_sum_correctly():
    # Mirrors the real Odom fixture: 5 code-upgrade items summing to an
    # exact, previously-unexplained gap.
    line_items = [
        _line_item("line_0001", 20000.0),
        _line_item("line_0002", 859.97, is_code_upgrade=True),
        _line_item("line_0003", 784.10, is_code_upgrade=True),
        _line_item("line_0004", 1286.18, is_code_upgrade=True),
        _line_item("line_0005", 1208.53, is_code_upgrade=True),
        _line_item("line_0006", 941.28, is_code_upgrade=True),
    ]
    canonical = _canonical(line_items, reported_total=20000.0)
    config = ValidationConfig()

    reported, calculated, paid_when_incurred, difference, ok, note = compute_reconciliation(canonical, config)

    assert calculated == 20000.0
    assert round(paid_when_incurred, 2) == 5080.06
    assert ok is True
    assert len(canonical.line_items) == 6
