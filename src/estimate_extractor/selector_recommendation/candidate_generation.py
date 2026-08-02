"""Staged, bounded candidate generation: trade/component category hints ->
keyword/token pre-filter -> (compatibility + fuzzy ranking happen
downstream in scoring.py/ranker.py). Deliberately never runs expensive
fuzzy text similarity over the full eligible pool -- see build spec "Do
not search all 10,088 records with expensive fuzzy logic for every item
if a smaller category-constrained pool is available."
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import yaml

from estimate_extractor.selector_catalog.models import SelectorRecord, normalize_description
from estimate_extractor.selector_recommendation import query
from estimate_extractor.selector_recommendation.models import RecommendationInput

DEFAULT_RULES_PATH = Path(__file__).resolve().parents[3] / "config" / "selector_recommendation_rules.yaml"

_TOKEN_RE = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True, slots=True)
class RecommendationRules:
    trade_category_hints: dict[str, tuple[str, ...]]
    component_category_hints: dict[str, tuple[str, ...]]
    grade_vocabulary: tuple[str, ...]
    action_vocabulary: dict[str, tuple[str, ...]]
    distinction_keyword_groups: tuple[tuple[str, ...], ...]


def load_recommendation_rules(path: Path = DEFAULT_RULES_PATH) -> RecommendationRules:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    trade_hints = {k.lower(): tuple(v or ()) for k, v in (raw.get("trade_category_hints") or {}).items()}
    component_hints = {k.lower(): tuple(v or ()) for k, v in (raw.get("component_category_hints") or {}).items()}
    action_vocab = {k: tuple(v or ()) for k, v in (raw.get("action_vocabulary") or {}).items()}
    return RecommendationRules(
        trade_category_hints=trade_hints,
        component_category_hints=component_hints,
        grade_vocabulary=tuple(raw.get("grade_vocabulary") or ()),
        action_vocabulary=action_vocab,
        distinction_keyword_groups=tuple(tuple(g) for g in (raw.get("distinction_keyword_groups") or ())),
    )


def tokenize(text: str | None) -> set[str]:
    if not text:
        return set()
    return set(_TOKEN_RE.findall(text.lower()))


def hinted_categories(item: RecommendationInput, rules: RecommendationRules) -> list[str]:
    """Category hints from trade first, then component -- order preserved,
    duplicates dropped. Returns an empty list when neither trade nor
    component yields a confident hint (candidate_generation then falls
    back to an unconstrained-but-bounded pool)."""
    categories: list[str] = []
    trade = (item.trade or "").lower()
    for cat in rules.trade_category_hints.get(trade, ()):
        if cat not in categories:
            categories.append(cat)
    component = (item.component or "").lower().replace(" ", "_")
    for cat in rules.component_category_hints.get(component, ()):
        if cat not in categories:
            categories.append(cat)
    return categories


def _item_token_pool(item: RecommendationInput) -> set[str]:
    tokens: set[str] = set()
    tokens |= tokenize(item.normalized_description)
    tokens |= tokenize(item.original_description)
    tokens |= tokenize(item.component)
    tokens |= tokenize(item.material)
    for value in item.attributes.values():
        if isinstance(value, str):
            tokens |= tokenize(value)
    return tokens


def _keyword_prefilter(item: RecommendationInput, pool: list[SelectorRecord], limit: int) -> list[SelectorRecord]:
    """Cheap token-overlap pre-filter -- ranks by shared-token count with
    the source item's description/component/material text, then truncates
    to `limit`. This bounds the pool BEFORE any fuzzy scoring runs, per
    the build spec's staged-approach requirement. Records with zero token
    overlap are kept only if the pool is already small (<= limit), since a
    genuinely relevant item can still be found by category hint alone with
    completely different wording."""
    item_tokens = _item_token_pool(item)
    if not item_tokens or len(pool) <= limit:
        return pool

    scored = []
    for record in pool:
        record_tokens = tokenize(record.description_normalized)
        overlap = len(item_tokens & record_tokens)
        scored.append((overlap, record))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [record for _overlap, record in scored[:limit]]


def generate_candidate_pool(
    item: RecommendationInput,
    conn: sqlite3.Connection,
    rules: RecommendationRules,
    *,
    max_category_pool: int = 400,
    prefilter_limit: int = 200,
    include_uncertain: bool = False,
) -> tuple[list[SelectorRecord], list[str]]:
    """Returns (bounded candidate pool, hinted categories used). Stage 1:
    category hints narrow the pool via SQL. Stage 2 (no hint available):
    the full eligible pool is loaded once and cheaply token-filtered --
    still bounded, never full-table fuzzy-scored. Stage 3 (always):
    keyword/token pre-filter caps what downstream compatibility/scoring
    has to evaluate."""
    categories = hinted_categories(item, rules)
    if categories:
        pool = query.load_pool_for_categories(conn, categories, include_uncertain=include_uncertain)
        if len(pool) > max_category_pool * max(len(categories), 1):
            pool = _keyword_prefilter(item, pool, max_category_pool * max(len(categories), 1))
    else:
        pool = query.load_recommendation_pool(conn, include_uncertain=include_uncertain)

    bounded = _keyword_prefilter(item, pool, prefilter_limit)
    return bounded, categories
