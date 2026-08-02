"""Orchestrates action/trade/component/material/attribute detection into a
single Normalized result. Never modifies the original extracted facts
(quantity, unit, description text as stored in OriginalItem) -- it only
*derives* a normalized view alongside them.
"""

from __future__ import annotations

import re

from estimate_extractor.mapping.action_detector import detect_action
from estimate_extractor.mapping.component_detector import detect_component
from estimate_extractor.mapping.models import Normalized, NormalizationConfidence
from estimate_extractor.mapping.rules_config import NormalizationRules
from estimate_extractor.mapping.trade_detector import detect_trade

_WS_RE = re.compile(r"\s+")


def clean_description(text: str) -> str:
    """Lowercase + collapse whitespace for rule matching. Deliberately does
    NOT strip digits, quotes, hyphens, slashes, or punctuation -- dimensions
    ('16\\' x 7\\''), fractions ('1/2"'), and qualifiers ('up to 5"') must
    survive for both rule matching and attribute extraction."""
    return _WS_RE.sub(" ", text).strip().lower()


def _coerce(raw: str, value_type: str):
    if value_type == "int":
        try:
            return int(float(raw))
        except ValueError:
            return None
    if value_type == "float":
        try:
            return float(raw)
        except ValueError:
            return None
    if value_type == "bool":
        return bool(raw)
    return raw


def extract_attributes(description_lower: str, rules: NormalizationRules) -> dict[str, object]:
    attributes: dict[str, object] = {}
    for pattern in rules.attribute_patterns:
        match = pattern.regex.search(description_lower)
        if not match:
            continue
        if pattern.value_if_matched is not None:
            attributes[pattern.attribute] = pattern.value_if_matched
        elif match.groups():
            attributes[pattern.attribute] = _coerce(match.group(1), pattern.value_type)
    return attributes


def normalize_line_item(
    description: str,
    quantity: float | None,
    unit_of_measure: str | None,
    rules: NormalizationRules,
) -> tuple[Normalized, NormalizationConfidence, bool, list[str]]:
    description_lower = clean_description(description)

    action, action_conf = detect_action(description_lower, rules.action_rules)
    trade, trade_conf = detect_trade(description_lower, rules.trade_component_rules)
    component, material, component_conf = detect_component(description_lower, rules.trade_component_rules)
    material_conf = component_conf if material else (0.5 if component != "unknown" else 0.3)

    attributes = extract_attributes(description_lower, rules)

    normalized = Normalized(
        action=action,
        trade=trade,
        component=component,
        material=material,
        attributes=attributes,
        quantity=quantity,
        unit_of_measure=unit_of_measure,
    )

    overall = min(action_conf, trade_conf, component_conf)
    confidence = NormalizationConfidence(
        overall=overall,
        action=action_conf,
        trade=trade_conf,
        component=component_conf,
        material=material_conf,
    )

    review_reasons: list[str] = []
    if action.value == "unknown":
        review_reasons.append("action_not_recognized")
    if trade.value == "unknown":
        review_reasons.append("trade_not_recognized")
    if component == "unknown":
        review_reasons.append("component_not_recognized")
    needs_review = bool(review_reasons) or overall < 0.6

    return normalized, confidence, needs_review, review_reasons
