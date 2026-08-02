"""Layer 2/4: line-item row assembly from the classified token stream.

Each carrier prints a line item's fields on their own physical text lines,
in this fixed shape:

    <item number>. <description>
    <quantity> <unit>
    <core money fields...>            (carrier-specific: see ColumnSchema)
    <trailing zone: any order/subset of AGE/LIFE, CONDITION, DEP%, DEPREC.>
    <ACV>                              (always last)
    <optional narrative notes...>

``assemble_line_item`` walks a page's content lines starting at a known
ITEM_START token and returns a carrier-agnostic draft plus how many lines
it consumed, so the caller (``state_machine.py``) can keep walking.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal

from estimate_extractor.models.canonical import DepreciationType
from estimate_extractor.normalization.money import classify_depreciation, parse_decimal, parse_percent
from estimate_extractor.normalization.text import detect_verb_flags, normalize_whitespace
from estimate_extractor.normalization.units import normalize_unit
from estimate_extractor.parsing.notes import classify_note_type, is_note_like_phrase
from estimate_extractor.parsing.sections import is_label_line, is_real_quantity_unit
from estimate_extractor.pdf.tables import LineToken, TokenKind, classify_line

# Standalone short phrases that are genuine notes even though they are
# short enough to otherwise look like a wrapped description fragment.
_SHORT_NOTE_EXCEPTIONS = ("rake only",)


@dataclass(frozen=True, slots=True)
class ColumnSchema:
    """Which plain-money fields appear, in order, immediately after
    quantity+unit and before the variable trailing zone. Field names must
    be one of: unit_price, tax, overhead_and_profit, replacement_cost_value.
    """

    core_fields: tuple[str, ...] = ("unit_price", "tax", "replacement_cost_value")


@dataclass(slots=True)
class NoteDraft:
    note_type: str
    text: str


@dataclass(slots=True)
class LineItemDraft:
    item_number: int | None
    has_allowance_marker: bool
    description: str
    description_reconstructed: bool
    quantity: Decimal | None
    unit_raw: str | None
    unit_of_measure: str | None
    unit_known: bool
    unit_price: Decimal | None
    tax: Decimal | None
    overhead_and_profit: Decimal | None
    replacement_cost_value: Decimal | None
    age: str | None
    life_expectancy: str | None
    condition: str | None
    depreciation_percent: Decimal | None
    depreciation_amount: Decimal | None
    depreciation_type: str
    actual_cash_value: Decimal | None
    notes: list[NoteDraft] = field(default_factory=list)
    raw_text: str = ""
    review_reasons: list[str] = field(default_factory=list)


def _split_age_life(age_life_raw: str | None) -> tuple[str | None, str | None]:
    if not age_life_raw:
        return None, None
    parts = age_life_raw.replace(" ", "").split("/")
    if len(parts) != 2:
        return age_life_raw, None
    age, life = parts[0], parts[1]
    life = life.upper().replace("YRS.", "").replace("YRS", "").replace("YR.", "").replace("YR", "")
    # age_life_raw looks like "2/25yrs" after space-strip; separate trailing unit text from life.
    m = re.match(r"^(\d+)(.*)$", life)
    if m:
        life = m.group(1)
    return age, life if life else None


_CATEGORY_HEADING_RE = re.compile(r"^\*{2,3}.+\*{2,3}$")


_DANGLING_ENDINGS = ("-", "w/", "&", "of", "for", "the", "and", "to", "per", "up")


def _is_description_wrap_fragment(text: str, description_so_far: str) -> bool:
    t = text.strip()
    if t.lower() in _SHORT_NOTE_EXCEPTIONS:
        return False
    if _CATEGORY_HEADING_RE.match(t):
        return False
    if len(t.split()) > 4:
        return False
    if is_label_line(t):
        # A capitalized, category-heading-shaped word ("Garage", "Windows",
        # "Plastic") is genuinely ambiguous by shape alone -- it's only
        # merged as a description tail when the description built so far
        # looks incomplete (ends on a dangling hyphen/connector, e.g.
        # "turtle type -"). A complete-looking description followed by such
        # a word is a new category heading, not a wrap (e.g. "R&R Window
        # screen, 1 - 9 SF" followed by "Garage").
        tail = description_so_far.rstrip()
        if not tail.endswith(_DANGLING_ENDINGS):
            return False
    return True


def assemble_line_item(
    lines: list[str], start_idx: int, schema: ColumnSchema
) -> tuple[LineItemDraft, int]:
    tok0 = classify_line(lines[start_idx])
    assert tok0.kind == TokenKind.ITEM_START
    description_parts = [tok0.description or ""]
    consumed_raw = [lines[start_idx]]
    idx = start_idx + 1
    n = len(lines)

    # Description may continue on subsequent plain-text lines before the
    # quantity line appears (rare, but seen when a long description wraps).
    while idx < n:
        tok = classify_line(lines[idx])
        if tok.kind == TokenKind.QTY_UNIT and is_real_quantity_unit(tok.unit_raw or ""):
            break
        if tok.kind == TokenKind.TEXT and tok.raw.strip():
            description_parts.append(tok.raw.strip())
            consumed_raw.append(lines[idx])
            idx += 1
            continue
        break

    description = normalize_whitespace(" ".join(p for p in description_parts if p))
    review_reasons: list[str] = []

    quantity: Decimal | None = None
    unit_raw: str | None = None
    unit_norm: str | None = None
    unit_known = False

    if idx < n and classify_line(lines[idx]).kind == TokenKind.QTY_UNIT:
        qty_tok = classify_line(lines[idx])
        quantity = parse_decimal(qty_tok.quantity_raw)
        unit_raw = qty_tok.unit_raw
        unit_norm, unit_known = normalize_unit(unit_raw)
        consumed_raw.append(lines[idx])
        idx += 1
    else:
        review_reasons.append("missing_quantity")

    core_values: dict[str, Decimal | None] = {}
    for field_name in schema.core_fields:
        if idx < n and classify_line(lines[idx]).kind in (
            TokenKind.MONEY_PLAIN,
            TokenKind.MONEY_PAREN,
            TokenKind.MONEY_ANGLE,
        ):
            core_values[field_name] = parse_decimal(lines[idx])
            consumed_raw.append(lines[idx])
            idx += 1
        else:
            core_values[field_name] = None
            review_reasons.append(f"missing_{field_name}")

    age_life_raw: str | None = None
    condition_raw: str | None = None
    dep_percent_raw: str | None = None
    deprec_raw: str | None = None
    acv_raw: str | None = None

    while idx < n:
        tok = classify_line(lines[idx])
        if tok.kind in (TokenKind.ITEM_START, TokenKind.TOTALS_LABEL, TokenKind.HEADER_KEYWORD):
            break
        if tok.kind == TokenKind.AGE_LIFE_CONDITION_COMBO:
            age_life_raw = tok.age_life_raw
            condition_raw = tok.condition_raw
            consumed_raw.append(lines[idx])
            idx += 1
            continue
        if tok.kind == TokenKind.AGE_LIFE:
            age_life_raw = tok.raw.strip()
            consumed_raw.append(lines[idx])
            idx += 1
            continue
        if tok.kind == TokenKind.CONDITION:
            condition_raw = tok.raw.strip()
            consumed_raw.append(lines[idx])
            idx += 1
            continue
        if tok.kind == TokenKind.DEP_PERCENT:
            dep_percent_raw = tok.raw.strip()
            consumed_raw.append(lines[idx])
            idx += 1
            continue
        if tok.kind in (TokenKind.MONEY_PAREN, TokenKind.MONEY_ANGLE) and deprec_raw is None:
            deprec_raw = tok.raw.strip()
            consumed_raw.append(lines[idx])
            idx += 1
            continue
        if tok.kind == TokenKind.MONEY_PLAIN and acv_raw is None:
            # ACV's position varies by carrier: Travelers/USAA print it
            # last; State Farm/Farmers print CONDITION and DEP% *after* it
            # (RCV, AGE/LIFE, DEPREC., ACV, CONDITION, DEP%). So finding
            # ACV does not end the trailing zone -- keep looping in case
            # more age/condition/dep% tokens follow; a second plain money
            # value (which would be unexpected) is what actually ends it.
            acv_raw = tok.raw.strip()
            consumed_raw.append(lines[idx])
            idx += 1
            continue
        break

    if acv_raw is None:
        review_reasons.append("missing_actual_cash_value")

    description_reconstructed = False
    if idx < n:
        tok = classify_line(lines[idx])
        if tok.kind == TokenKind.TEXT and _is_description_wrap_fragment(tok.raw, description):
            description = normalize_whitespace(f"{description} {tok.raw.strip()}")
            description_reconstructed = True
            review_reasons.append("possible_description_wrap")
            consumed_raw.append(lines[idx])
            idx += 1

    notes: list[NoteDraft] = []
    while idx < n:
        tok = classify_line(lines[idx])
        if tok.kind in (TokenKind.ITEM_START, TokenKind.TOTALS_LABEL, TokenKind.HEADER_KEYWORD):
            break
        if tok.kind == TokenKind.BLANK:
            idx += 1
            continue
        if tok.kind == TokenKind.TEXT:
            text = tok.raw.strip()
            if _CATEGORY_HEADING_RE.match(text):
                break
            if is_label_line(text) and not is_note_like_phrase(text) and len(text.split()) <= 3:
                break
            notes.append(NoteDraft(note_type=classify_note_type(text), text=text))
            consumed_raw.append(lines[idx])
            idx += 1
            continue
        break

    age, life = _split_age_life(age_life_raw)
    depreciation_percent = parse_percent(dep_percent_raw)
    rcv = core_values.get("replacement_cost_value")
    acv = parse_decimal(acv_raw)

    if deprec_raw is not None:
        depreciation_amount, depreciation_type = classify_depreciation(deprec_raw)
        if depreciation_type.value == "unknown":
            review_reasons.append("ambiguous_depreciation_notation")
    elif rcv is not None and acv is not None and rcv == acv:
        depreciation_amount, depreciation_type = Decimal("0.00"), DepreciationType.NONE
    else:
        depreciation_amount, depreciation_type = None, DepreciationType.UNKNOWN

    if not unit_known and unit_raw:
        review_reasons.append("unrecognized_unit")

    draft = LineItemDraft(
        item_number=tok0.item_number,
        has_allowance_marker=tok0.has_allowance_marker,
        description=description,
        description_reconstructed=description_reconstructed,
        quantity=quantity,
        unit_raw=unit_raw,
        unit_of_measure=unit_norm,
        unit_known=unit_known,
        unit_price=core_values.get("unit_price"),
        tax=core_values.get("tax"),
        overhead_and_profit=core_values.get("overhead_and_profit"),
        replacement_cost_value=rcv,
        age=age,
        life_expectancy=life,
        condition=condition_raw,
        depreciation_percent=depreciation_percent,
        depreciation_amount=depreciation_amount,
        depreciation_type=depreciation_type.value,
        actual_cash_value=acv,
        notes=notes,
        raw_text="\n".join(consumed_raw),
        review_reasons=review_reasons,
    )
    return draft, idx


def draft_flags(draft: LineItemDraft) -> dict[str, bool]:
    flags = detect_verb_flags(draft.description)
    if any(n.note_type == "code_upgrade_note" for n in draft.notes):
        flags["is_code_upgrade"] = True
    return flags


def compute_confidence(draft: LineItemDraft, schema: ColumnSchema) -> dict[str, float]:
    desc_conf = 0.8 if draft.description_reconstructed else (0.95 if draft.description else 0.2)
    qty_conf = 0.97 if draft.quantity is not None else 0.3
    if draft.unit_of_measure and draft.unit_known:
        unit_conf = 0.95
    elif draft.unit_of_measure:
        unit_conf = 0.7
    else:
        unit_conf = 0.3
    price_conf = 0.95 if draft.unit_price is not None else 0.3
    if "tax" in schema.core_fields:
        tax_conf = 0.95 if draft.tax is not None else 0.4
    else:
        tax_conf = 0.9  # confidently absent: this carrier's layout has no tax column
    rcv_conf = 0.95 if draft.replacement_cost_value is not None else 0.3
    dep_conf = 0.5 if draft.depreciation_type == "unknown" else 0.95
    acv_conf = 0.95 if draft.actual_cash_value is not None else 0.3
    overall = min(desc_conf, qty_conf, unit_conf, price_conf, tax_conf, rcv_conf, dep_conf, acv_conf)
    return {
        "overall": overall,
        "description": desc_conf,
        "quantity": qty_conf,
        "unit_of_measure": unit_conf,
        "unit_price": price_conf,
        "tax": tax_conf,
        "replacement_cost_value": rcv_conf,
        "depreciation": dep_conf,
        "actual_cash_value": acv_conf,
    }
