"""Unit-of-measure normalization."""

from __future__ import annotations

KNOWN_UNITS: frozenset[str] = frozenset(
    {"EA", "LF", "SF", "SY", "CF", "CY", "SQ", "HR", "DA", "RM", "LS", "MO", "WK", "MI", "GAL", "LB", "TON"}
)


def normalize_unit(raw: str | None) -> tuple[str | None, bool]:
    """Normalize a unit token to an uppercase code.

    Returns (normalized_unit, is_known). Unfamiliar units are preserved
    verbatim (uppercased) with is_known=False so callers can attach a
    warning instead of silently dropping the data.
    """
    if raw is None:
        return None, False
    s = raw.strip().upper()
    if not s:
        return None, False
    return s, s in KNOWN_UNITS
