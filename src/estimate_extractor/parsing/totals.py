"""Summary-total row assembly ("Totals: X", "Area Totals: X", "Grand Total
Areas:", "Line Item Totals: X").

Most carriers print one money value per physical line (tax, RCV, deprec,
ACV in some order). Allstate is an exception: it sometimes prints the last
two values ("Deprec." and "ACV") on a single whitespace-separated line.
This module tolerates both by scanning each line for however many money
tokens it contains, rather than assuming exactly one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from estimate_extractor.normalization.money import is_money_token, parse_decimal
from estimate_extractor.parsing.line_items import ColumnSchema
from estimate_extractor.pdf.tables import TokenKind, classify_line

TOTALS_NAME_RE = re.compile(
    r"^(?:Totals?:|Total:|Area Totals?:|Grand Total Areas:|Line Item Totals?:|Coverage Totals?:)\s*(.*)$",
    re.IGNORECASE,
)


@dataclass(slots=True)
class SummaryTotalDraft:
    label: str
    name: str | None
    tax: Decimal | None
    replacement_cost_value: Decimal | None
    depreciation: Decimal | None
    actual_cash_value: Decimal | None


def _money_values_in_line(line: str) -> list[Decimal]:
    values: list[Decimal] = []
    for token in line.split():
        if is_money_token(token):
            v = parse_decimal(token)
            if v is not None:
                values.append(v)
    return values


def assemble_summary_total(
    lines: list[str], start_idx: int, schema: ColumnSchema
) -> tuple[SummaryTotalDraft, int]:
    label_line = lines[start_idx]
    m = TOTALS_NAME_RE.match(label_line.strip())
    name = m.group(1).strip() if m and m.group(1).strip() else None

    idx = start_idx + 1
    n = len(lines)
    collected: list[Decimal] = []
    # tax/O&P/RCV (whichever the schema has, minus unit_price) + deprec + ACV.
    expected_count = len([f for f in schema.core_fields if f != "unit_price"]) + 2

    while idx < n and len(collected) < expected_count:
        tok = classify_line(lines[idx])
        if tok.kind in (TokenKind.ITEM_START, TokenKind.TOTALS_LABEL, TokenKind.HEADER_KEYWORD):
            break
        if tok.kind == TokenKind.BLANK:
            idx += 1
            continue
        if tok.kind == TokenKind.QTY_UNIT:
            # A quantity+unit or measurement line ("0.00 SF Walls") means we
            # have wandered past the end of the totals row -- do not raid
            # its leading number as a stray money value.
            break
        values = _money_values_in_line(lines[idx])
        if not values:
            if tok.kind == TokenKind.TEXT:
                break
            idx += 1
            continue
        collected.extend(values)
        idx += 1

    tax: Decimal | None = None
    rcv: Decimal | None = None
    deprec: Decimal | None = None
    acv: Decimal | None = None

    if collected:
        acv = collected[-1]
        if len(collected) >= 2:
            deprec = collected[-2]
        remaining = collected[:-2]
        total_fields = [f for f in schema.core_fields if f != "unit_price"]
        if len(remaining) == len(total_fields):
            values_by_field = dict(zip(total_fields, remaining))
            tax = values_by_field.get("tax")
            rcv = values_by_field.get("replacement_cost_value")
        elif remaining:
            # Best effort: RCV is the most reliable of the remaining values
            # and is conventionally last among them.
            rcv = remaining[-1]
            if len(remaining) >= 2 and "tax" in total_fields:
                tax = remaining[-2]

    draft = SummaryTotalDraft(
        label=label_line.strip(),
        name=name,
        tax=tax,
        replacement_cost_value=rcv,
        depreciation=deprec,
        actual_cash_value=acv,
    )
    return draft, idx
