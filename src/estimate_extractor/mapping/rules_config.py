"""Loads config/normalization_rules.yaml into typed, immutable rule lists."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass(frozen=True, slots=True)
class ActionRule:
    patterns: tuple[str, ...]
    action: str


@dataclass(frozen=True, slots=True)
class TradeComponentRule:
    patterns: tuple[str, ...]
    trade: str | None
    component: str | None
    material: str | None


@dataclass(frozen=True, slots=True)
class AttributePattern:
    name: str
    regex: re.Pattern
    attribute: str
    value_type: str  # "bool" | "int" | "float" | "str"
    value_if_matched: object | None = None


@dataclass(frozen=True, slots=True)
class NormalizationRules:
    action_rules: tuple[ActionRule, ...] = field(default_factory=tuple)
    trade_component_rules: tuple[TradeComponentRule, ...] = field(default_factory=tuple)
    attribute_patterns: tuple[AttributePattern, ...] = field(default_factory=tuple)


def load_normalization_rules(path: Path) -> NormalizationRules:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    action_rules: list[ActionRule] = []
    trade_component_rules: list[TradeComponentRule] = []
    for entry in raw.get("rules", []):
        patterns = tuple(p.lower() for p in entry.get("patterns", []))
        if not patterns:
            continue
        if "action" in entry:
            action_rules.append(ActionRule(patterns=patterns, action=entry["action"]))
        if any(k in entry for k in ("trade", "component", "material")):
            trade_component_rules.append(
                TradeComponentRule(
                    patterns=patterns,
                    trade=entry.get("trade"),
                    component=entry.get("component"),
                    material=entry.get("material"),
                )
            )

    attribute_patterns: list[AttributePattern] = []
    for entry in raw.get("attribute_patterns", []):
        attribute_patterns.append(
            AttributePattern(
                name=entry["name"],
                regex=re.compile(entry["pattern"], re.IGNORECASE),
                attribute=entry["attribute"],
                value_type=entry.get("type", "str"),
                value_if_matched=entry.get("value_if_matched"),
            )
        )

    return NormalizationRules(
        action_rules=tuple(action_rules),
        trade_component_rules=tuple(trade_component_rules),
        attribute_patterns=tuple(attribute_patterns),
    )
