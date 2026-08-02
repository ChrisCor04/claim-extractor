"""Layer 2: table/row reconstruction from the native-text line stream.

Xactimate-style carrier estimate exports render each line-item row's fields
on their own physical text line (description, then quantity+unit, then each
numeric/attribute field), rather than as a single wide table row. This
module classifies each text line into a token so that
``parsing/line_items.py`` can walk the stream and assemble rows without
each carrier adapter re-implementing tokenization.

The token *order* varies by carrier (State Farm puts CONDITION/DEP% after
DEPREC.; Travelers puts them before; USAA inserts an O&P column; Allstate
drops TAX entirely and merges AGE/LIFE + CONDITION onto one line). Carrier
adapters supply a small column schema (``parsing/line_items.py:
ColumnSchema``) that says how many "plain money" slots appear before the
optional trailing zone -- the tokenizer here stays carrier-agnostic.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from estimate_extractor.normalization.money import bracket_style, is_money_token
from estimate_extractor.normalization.text import parse_item_number_line

QTY_UNIT_RE = re.compile(r"^([\d,]+\.\d+)\s+([A-Za-z][A-Za-z .&]{0,30})\*?$")
AGE_LIFE_RE = re.compile(r"^(\d+)\s*/\s*(\d+|NA)\s*(?:yrs?\.?)?$", re.IGNORECASE)
AGE_LIFE_COMBO_RE = re.compile(
    r"^(?P<age_life>\d+\s*/\s*(?:\d+|NA)\s*(?:yrs?\.?)?)\s+(?P<condition>[A-Za-z][A-Za-z.]{0,20})$",
    re.IGNORECASE,
)
DEP_PERCENT_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*%(\s*\[[^\]]*\])?$")
DEP_PERCENT_NA_RE = re.compile(r"^(NA|N/A)$", re.IGNORECASE)

CONDITION_WORDS = frozenset(
    {
        "avg.",
        "avg",
        "average",
        "new",
        "poor",
        "good",
        "fair",
        "excellent",
        "replaced",
        "above avg.",
        "above average",
        "below avg.",
        "below average",
    }
)

HEADER_KEYWORDS = frozenset(
    {
        "description",
        "quantity",
        "unit",
        "unit price",
        "tax",
        "o&p",
        "rcv",
        "age/life",
        "cond.",
        "condition",
        "dep %",
        "dep. %",
        "deprec.",
        "depreciation",
        "acv",
    }
)

TOTALS_LABEL_RE = re.compile(
    r"^(Totals?:|Total:|Area Totals?:|Grand Total Areas:|Line Item Totals?:|Area Total:|"
    r"Coverage Totals?:)",
    re.IGNORECASE,
)


class TokenKind(str, Enum):
    ITEM_START = "item_start"
    QTY_UNIT = "qty_unit"
    AGE_LIFE = "age_life"
    AGE_LIFE_CONDITION_COMBO = "age_life_condition_combo"
    CONDITION = "condition"
    DEP_PERCENT = "dep_percent"
    MONEY_PLAIN = "money_plain"
    MONEY_PAREN = "money_paren"
    MONEY_ANGLE = "money_angle"
    TOTALS_LABEL = "totals_label"
    HEADER_KEYWORD = "header_keyword"
    BLANK = "blank"
    TEXT = "text"


@dataclass(frozen=True, slots=True)
class LineToken:
    kind: TokenKind
    raw: str
    # populated for ITEM_START
    item_number: int | None = None
    has_allowance_marker: bool = False
    description: str | None = None
    # populated for QTY_UNIT
    quantity_raw: str | None = None
    unit_raw: str | None = None
    # populated for AGE_LIFE_CONDITION_COMBO
    age_life_raw: str | None = None
    condition_raw: str | None = None


def classify_line(raw_line: str) -> LineToken:
    line = raw_line.strip()

    if not line:
        return LineToken(kind=TokenKind.BLANK, raw=raw_line)

    item = parse_item_number_line(line)
    if item is not None:
        star, num, desc = item
        return LineToken(
            kind=TokenKind.ITEM_START,
            raw=raw_line,
            item_number=num,
            has_allowance_marker=star,
            description=desc,
        )

    if TOTALS_LABEL_RE.match(line):
        return LineToken(kind=TokenKind.TOTALS_LABEL, raw=raw_line)

    if line.lower() in HEADER_KEYWORDS:
        return LineToken(kind=TokenKind.HEADER_KEYWORD, raw=raw_line)

    m = QTY_UNIT_RE.match(line)
    if m:
        return LineToken(
            kind=TokenKind.QTY_UNIT, raw=raw_line, quantity_raw=m.group(1), unit_raw=m.group(2).strip()
        )

    m = AGE_LIFE_COMBO_RE.match(line)
    if m and m.group("condition").lower() in CONDITION_WORDS:
        return LineToken(
            kind=TokenKind.AGE_LIFE_CONDITION_COMBO,
            raw=raw_line,
            age_life_raw=m.group("age_life"),
            condition_raw=m.group("condition"),
        )

    if AGE_LIFE_RE.match(line):
        return LineToken(kind=TokenKind.AGE_LIFE, raw=raw_line)

    if line.lower().rstrip(".") in {w.rstrip(".") for w in CONDITION_WORDS} or line.lower() in CONDITION_WORDS:
        return LineToken(kind=TokenKind.CONDITION, raw=raw_line)

    if DEP_PERCENT_RE.match(line) or DEP_PERCENT_NA_RE.match(line):
        return LineToken(kind=TokenKind.DEP_PERCENT, raw=raw_line)

    if is_money_token(line):
        style = bracket_style(line)
        if style == "paren":
            return LineToken(kind=TokenKind.MONEY_PAREN, raw=raw_line)
        if style == "angle":
            return LineToken(kind=TokenKind.MONEY_ANGLE, raw=raw_line)
        return LineToken(kind=TokenKind.MONEY_PLAIN, raw=raw_line)

    return LineToken(kind=TokenKind.TEXT, raw=raw_line)


def tokenize(lines: list[str]) -> list[LineToken]:
    return [classify_line(line) for line in lines]
