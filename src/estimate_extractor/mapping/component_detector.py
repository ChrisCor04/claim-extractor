"""Deterministic component + material detection, reusing the same
trade/component/material rule match as trade_detector.py (see
find_best_rule) since one config rule sets all three fields together."""

from __future__ import annotations

from estimate_extractor.mapping.rules_config import TradeComponentRule
from estimate_extractor.mapping.trade_detector import find_best_rule


def detect_component(
    description_lower: str, rules: tuple[TradeComponentRule, ...]
) -> tuple[str, str | None, float]:
    rule = find_best_rule(description_lower, rules)
    if rule is None or not rule.component:
        return "unknown", None, 0.3
    return rule.component, rule.material, 0.9
