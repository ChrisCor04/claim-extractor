"""Deterministic action detection from a normalized description.

First matching rule wins, in the order rules appear in
config/normalization_rules.yaml (most specific first, e.g. "r&r" before the
generic "remove "). A match's confidence reflects how early/specific the
pattern was, not text similarity -- this is pattern matching, not fuzzy
scoring.
"""

from __future__ import annotations

from estimate_extractor.mapping.models import ActionType
from estimate_extractor.mapping.rules_config import ActionRule


def detect_action(description_lower: str, rules: tuple[ActionRule, ...]) -> tuple[ActionType, float]:
    for rule in rules:
        for pattern in rule.patterns:
            if pattern in description_lower:
                try:
                    action = ActionType(rule.action)
                except ValueError:
                    continue
                # Longer/more specific patterns (e.g. "r&r" vs "remove ")
                # get a small confidence boost.
                confidence = 0.95 if len(pattern) >= 6 else 0.85
                return action, confidence
    return ActionType.UNKNOWN, 0.3
