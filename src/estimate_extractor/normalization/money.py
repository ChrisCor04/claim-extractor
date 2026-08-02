"""Decimal-safe parsing of money, percentage, and depreciation notation.

Xactimate-style estimate exports use accounting notation for money that is
being subtracted or is a special kind of depreciation:

  * ``$14,582.40``          -> plain positive amount
  * ``(9,981.19)``          -> parenthesized: recoverable depreciation, or a
                                "less this amount" line in a summary
  * ``<409.84>``            -> angle-bracketed: nonrecoverable depreciation
  * ``NA`` / ``N/A`` / ``-``-> unknown / not applicable -> None

All parsing here returns ``Decimal`` (never ``float``) so callers can do
exact reconciliation math. Conversion to a JSON-safe ``float`` happens only
at model-construction time via :func:`to_json_number`.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from estimate_extractor.models.canonical import DepreciationType

_NA_TOKENS = {"na", "n/a", "-", "--", "none", ""}

_MONEY_CHARS_RE = re.compile(r"[^0-9.\-]")


def _strip_wrapping(raw: str) -> tuple[str, str]:
    """Return (inner_text, bracket_style) where bracket_style is one of
    'paren', 'angle', 'plain'."""
    s = raw.strip()
    if s.startswith("(") and s.endswith(")"):
        return s[1:-1].strip(), "paren"
    if s.startswith("<") and s.endswith(">"):
        return s[1:-1].strip(), "angle"
    return s, "plain"


def bracket_style(raw: str) -> str:
    _, style = _strip_wrapping(raw)
    return style


def parse_decimal(raw: str | None) -> Decimal | None:
    """Parse a single numeric token into a non-negative Decimal magnitude.

    Returns None for NA/blank/unparseable tokens. Never returns a negative
    number: callers that need sign/bracket semantics should consult
    :func:`bracket_style` separately.
    """
    if raw is None:
        return None
    s = raw.strip()
    if s.lower() in _NA_TOKENS:
        return None
    inner, _style = _strip_wrapping(s)
    inner = inner.rstrip("*").strip()
    inner = _MONEY_CHARS_RE.sub("", inner.replace(",", ""))
    if inner in ("", "-", "."):
        return None
    try:
        value = Decimal(inner)
    except InvalidOperation:
        return None
    return abs(value)


def parse_percent(raw: str | None) -> Decimal | None:
    """Parse a percentage token (e.g. '23.33%', '10.0%', 'NA') to a Decimal
    percentage value (23.33), not a fraction."""
    if raw is None:
        return None
    s = raw.strip()
    if s.lower() in _NA_TOKENS:
        return None
    s = s.rstrip("%").strip()
    # Strip Xactimate depreciation-method tags like "33.3% [%]" or "[M]".
    s = re.sub(r"\[[^\]]*\]", "", s).strip()
    return parse_decimal(s)


def classify_depreciation(
    raw: str | None,
) -> tuple[Decimal | None, DepreciationType]:
    """Classify a depreciation token per the interpretation rules:

      * ``(123.45)``            -> recoverable
      * ``<123.45>``             -> nonrecoverable
      * ``0.00`` / ``(0.00)``    -> none
      * plain positive, no bracket -> unknown (ambiguous; needs review)
      * absent / NA               -> (None, UNKNOWN)
    """
    if raw is None:
        return None, DepreciationType.UNKNOWN
    s = raw.strip()
    if s.lower() in _NA_TOKENS:
        return None, DepreciationType.UNKNOWN

    magnitude = parse_decimal(s)
    if magnitude is None:
        return None, DepreciationType.UNKNOWN

    if magnitude == 0:
        return magnitude, DepreciationType.NONE

    style = bracket_style(s)
    if style == "paren":
        return magnitude, DepreciationType.RECOVERABLE
    if style == "angle":
        return magnitude, DepreciationType.NONRECOVERABLE
    return magnitude, DepreciationType.UNKNOWN


def to_json_number(value: Decimal | None) -> float | None:
    """Convert a Decimal to a JSON-safe float at serialization time."""
    if value is None:
        return None
    return float(value)


def is_money_token(raw: str) -> bool:
    """True if the token looks like a money/decimal value (possibly
    wrapped in parens/angle-brackets, possibly with a trailing '*' allowance
    marker), false otherwise. Used by the row tokenizer to distinguish
    numeric fields from label/note text."""
    s = raw.strip().rstrip("*").strip()
    inner, _style = _strip_wrapping(s)
    inner = inner.replace(",", "").replace("$", "").strip()
    if inner in ("", "-"):
        return False
    return bool(re.fullmatch(r"-?\d+(\.\d+)?", inner))
