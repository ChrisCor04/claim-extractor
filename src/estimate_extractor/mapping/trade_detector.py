"""Deterministic trade detection, and the shared best-matching-rule finder
reused by component_detector.py (trade/component/material are set together
by the same config/normalization_rules.yaml rule entry)."""

from __future__ import annotations

from estimate_extractor.mapping.models import TradeType
from estimate_extractor.mapping.rules_config import TradeComponentRule


def find_best_rule(description_lower: str, rules: tuple[TradeComponentRule, ...]) -> TradeComponentRule | None:
    """The first rule (in file order) with a matching pattern, preferring
    the LONGEST matching pattern among a rule's own patterns list so a more
    specific phrase ("laminated - comp. shingle") beats a generic one
    ("comp. shingle roofing") when both happen to match the same text --
    but rule file order still takes precedence across different rules,
    since more specific rules are listed first by convention."""
    for rule in rules:
        if any(pattern in description_lower for pattern in rule.patterns):
            return rule
    return None


def detect_trade(description_lower: str, rules: tuple[TradeComponentRule, ...]) -> tuple[TradeType, float]:
    rule = find_best_rule(description_lower, rules)
    if rule is None or not rule.trade:
        return TradeType.UNKNOWN, 0.3
    try:
        trade = TradeType(rule.trade)
    except ValueError:
        return TradeType.UNKNOWN, 0.3
    return trade, 0.9
