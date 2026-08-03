"""Deterministic item-signature computation.

The signature identifies "this kind of line item" for the internal
lookup registry's exact-match fast path -- built only from trade,
component, material, a canonicalized size key, action, and unit.
**Never** from price, quantity magnitude, or any price-list identifier
(see build spec "Prices and price-list names must not be used for
identity or matching.").
"""

from __future__ import annotations

from estimate_extractor.xactimate_lookup.phrase_generator import PhraseRules, extract_size_term, extract_style_term


def compute_size_key(original_description: str) -> str | None:
    return extract_size_term(original_description.lower()) if original_description else None


def compute_grade_key(original_description: str, rules: PhraseRules) -> str | None:
    """Checks BOTH the leading-style (type-qualifier: "3-tab", "laminated")
    and trailing-style (grade: "high grade") keyword lists -- the split
    between the two only affects search-phrase word ORDER
    (phrase_generator.py); for identity/conflict-detection purposes a
    "3-tab" item and a "laminated" item must still resolve to different
    grade_key values, or two genuinely different items would collapse
    onto the same item signature."""
    if not original_description:
        return None
    text = original_description.lower()
    return extract_style_term(text, rules.leading_style_keywords) or extract_style_term(text, rules.style_keywords)


def compute_normalized_description(
    trade: str | None, component: str | None, material: str | None, action: str | None
) -> str:
    """A readable, deterministic rendering of the item's normalized
    (structured) fields -- distinct from both ``original_description``
    (verbatim carrier text) and the concise ``search_phrase`` (bucketed,
    size/style-aware, meant for a Xactimate search box). Stored on the
    registry record so a reviewer can see what the system understood
    about an item without re-deriving it from raw fields."""
    parts = []
    if component and component != "unknown":
        parts.append(component.replace("_", " "))
    if material:
        parts.append(material)
    if action and action != "unknown":
        parts.append(action.replace("_", " "))
    if not parts and trade and trade != "unknown":
        parts.append(trade.replace("_", " "))
    return " ".join(parts).strip()


def compute_item_signature(
    trade: str | None,
    component: str | None,
    material: str | None,
    action: str | None,
    unit: str | None,
    original_description: str,
    rules: PhraseRules,
) -> str:
    """A stable, human-readable (not hashed -- see repo convention of
    favoring transparency over opaque identifiers) key. Two line items
    with the same trade/component/material/size/action/unit produce the
    same signature regardless of wording differences elsewhere in the
    description, punctuation, or price."""
    size_key = compute_size_key(original_description) or "-"
    grade_key = compute_grade_key(original_description, rules) or "-"
    parts = [
        (trade or "unknown").strip().lower(),
        (component or "unknown").strip().lower(),
        (material or "-").strip().lower(),
        size_key,
        grade_key,
        (action or "unknown").strip().lower(),
        (unit or "-").strip().upper(),
    ]
    return "|".join(parts)
