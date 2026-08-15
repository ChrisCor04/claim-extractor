"""Deterministic, read-only mapping against the repository Xactimate catalog.

This module is intentionally disconnected from live execution.  It never opens
Xactimate and never mutates the authoritative catalog.
"""

from __future__ import annotations

import csv
import math
import re
import sys
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from statistics import median
from typing import Any, Iterable

from .offline_catalog_rerankers import SemanticReranker

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CATALOG_PATH = REPOSITORY_ROOT / "fixtures" / "reference" / "xactimate_catalog_unique.csv"
REQUIRED_COLUMNS = ("PriceList", "CAT", "SEL", "Description", "ItemId")
FALLBACK_CAT = "DOR"
FALLBACK_SEL = "BIDITM"

_WORD_RE = re.compile(r"[a-z0-9]+")
_LEADING_ACTION_PATTERNS = (
    (re.compile(r"^(?:remove\s+and\s+replace|remove\s+replace|r\s+and\s+r)\b\s*"), "remove_replace"),
    (re.compile(r"^detach\s+reset\b\s*"), "detach_reset"),
    (re.compile(r"^remove\b\s*"), "remove"),
    (re.compile(r"^replace\b\s*"), "replace"),
    (re.compile(r"^install\b\s*"), "install"),
)
_ABBREVIATIONS = {
    "alum": "aluminum", "comp": "composition", "incl": "including",
    "w/o": "without", "wout": "without", "yr": "year", "yrs": "year",
    "lb": "pound", "lbs": "pound", "ft": "foot", "sf": "square foot",
}
_LOW_INFORMATION = frozenset({
    "a", "an", "and", "the", "of", "for", "to", "with", "per", "each",
    "existing", "material", "labor", "remove", "replace", "install", "including",
    "up", "only", "standard", "type",
})

_TRADE_CONTEXT = {
    "RFG": {"roof", "roofing", "shingle", "ridge", "shed"},
    "SDG": {"siding", "exterior surface", "ext surface", "cladding"},
    "SFG": {"gutter", "downspout", "soffit", "fascia"},
    "INS": {"insulation", "thermal", "attic insulation"},
    "FEN": {"fence", "fencing", "gate"},
    "DOR": {"door", "garage door", "overhead door"},
    "WDR": {"window", "window screen"},
    "DMO": {"debris", "debris removal", "demolition"},
    "PNT": {"paint", "painting", "stain"},
    "HVC": {"hvac", "air conditioning", "furnace"},
    "ELS": {"satellite", "electrical", "digital satellite"},
    "CLN": {"clean", "cleaning"},
    "AWN": {"awning", "patio cover"},
}

_ACTION_PHRASES = {
    "detach_reset": ("detach reset",),
    "tear_off": ("tear off", "haul dispose", "remove dispose"),
    "paint": ("paint", "prime paint", "stain"),
    "material_only": ("material only", "material source"),
    "labor_only": ("labor only",),
}


class CatalogIntegrityError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SourceLineContext:
    description: str
    section: str | None = None
    group: str | None = None
    quantity: float | None = None
    unit: str | None = None
    activity: str | None = None
    trade_hint: str | None = None
    pricing: dict[str, Any] = field(default_factory=dict)

    @property
    def context_text(self) -> str:
        return normalize_catalog_text(" ".join(filter(None, (self.section, self.group, self.trade_hint))))


def normalize_catalog_text(value: str) -> str:
    text = value.casefold()
    text = re.sub(r"\br\s*[/&]\s*r\b", "remove and replace", text)
    text = text.replace("&", " and ")
    text = re.sub(r"\bdetach\s*(?:and|/)\s*reset\b", "detach reset", text)
    text = re.sub(r"\bw\s*/\s*out\b", "without", text)
    text = re.sub(r"\bw\s*/\s*o\b", "without", text)
    words = _WORD_RE.findall(text)
    words = [_ABBREVIATIONS.get(word, word) for word in words]
    # A conservative plural fold: do not damage short codes/words.
    words = [word[:-1] if len(word) > 4 and word.endswith("s") and not word.endswith("ss") else word for word in words]
    return " ".join(words)


@dataclass(frozen=True, slots=True)
class SourceNormalization:
    original_description: str
    catalog_search_text: str
    action: str | None


def _canonical_action(value: str | None) -> str | None:
    normalized = normalize_catalog_text(value or "").replace(" ", "_")
    aliases = {
        "remove_and_replace": "remove_replace", "r_and_r": "remove_replace",
        "detach_and_reset": "detach_reset",
    }
    return aliases.get(normalized, normalized or None)


def parse_source_normalization(value: str, *, explicit_activity: str | None = None) -> SourceNormalization:
    """Return immutable source text, canonical action, and lookup-only text."""
    normalized = normalize_catalog_text(value)
    inferred = None
    search_text = normalized
    for pattern, action in _LEADING_ACTION_PATTERNS:
        match = pattern.match(normalized)
        if match:
            inferred = action
            search_text = normalized[match.end():].strip()
            break
    return SourceNormalization(
        original_description=value, catalog_search_text=search_text or normalized,
        action=_canonical_action(explicit_activity) or inferred,
    )


def normalize_source_text(value: str) -> str:
    return parse_source_normalization(value).catalog_search_text


@dataclass(frozen=True, slots=True)
class CatalogRecord:
    price_list: str
    category: str
    selector: str
    description: str
    item_id: str
    normalized_description: str
    tokens: tuple[str, ...]

    @property
    def identity(self) -> tuple[str, str]:
        return self.category, self.selector


@dataclass(frozen=True, slots=True)
class CandidateScore:
    rank: int
    category: str
    selector: str
    catalog_description: str
    item_id: str
    price_list: str
    final_score: float
    components: dict[str, float]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class MappingResult:
    source_description: str
    normalized_source_description: str
    catalog_search_text: str
    resolution: str
    category: str | None
    selector: str | None
    catalog_description: str | None
    item_id: str | None
    price_list: str | None
    final_score: float
    top_2_score: float
    margin: float
    reason: str
    candidates: list[CandidateScore] = field(default_factory=list)
    source_quantity: float | None = None
    source_unit: str | None = None
    source_pricing: dict[str, Any] = field(default_factory=dict)
    source_activity: str | None = None
    activity_resolution: str = "unspecified"
    fallback_catalog_description: str | None = None
    fallback_item_id: str | None = None
    fallback_price_list: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["candidates"] = [candidate.to_dict() for candidate in self.candidates]
        return data


class XactimateCatalog:
    def __init__(self, records: Iterable[CatalogRecord], *, source_path: Path, load_seconds: float) -> None:
        self.records = tuple(records)
        self.source_path = source_path
        self.load_seconds = load_seconds
        self.by_identity = {record.identity: record for record in self.records}
        self._postings: dict[str, set[int]] = defaultdict(set)
        document_frequency: Counter[str] = Counter()
        for index, record in enumerate(self.records):
            unique = set(record.tokens)
            document_frequency.update(unique)
            for token in unique:
                self._postings[token].add(index)
        size = len(self.records)
        self.idf = {token: math.log((size + 1) / (count + 1)) + 1 for token, count in document_frequency.items()}

    @classmethod
    def load(cls, path: Path | None = None) -> "XactimateCatalog":
        source = (path or DEFAULT_CATALOG_PATH).resolve()
        started = time.perf_counter()
        with source.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames is None or any(column not in reader.fieldnames for column in REQUIRED_COLUMNS):
                raise CatalogIntegrityError(
                    f"catalog must contain columns {REQUIRED_COLUMNS!r}; found {reader.fieldnames!r}"
                )
            records: list[CatalogRecord] = []
            seen: dict[tuple[str, str], tuple[str, str]] = {}
            for line_number, row in enumerate(reader, start=2):
                values = {column: (row.get(column) or "").strip() for column in REQUIRED_COLUMNS}
                missing = [column for column in ("CAT", "SEL", "Description") if not values[column]]
                if missing:
                    raise CatalogIntegrityError(f"catalog row {line_number} has blank required values: {missing}")
                identity = (values["CAT"], values["SEL"])
                metadata = (values["Description"], values["ItemId"])
                if identity in seen:
                    raise CatalogIntegrityError(
                        f"duplicate CAT/SEL {identity!r} at row {line_number}; prior metadata={seen[identity]!r}"
                    )
                seen[identity] = metadata
                normalized = normalize_catalog_text(values["Description"])
                records.append(CatalogRecord(
                    price_list=values["PriceList"], category=identity[0], selector=identity[1],
                    description=values["Description"], item_id=values["ItemId"],
                    normalized_description=normalized, tokens=tuple(normalized.split()),
                ))
        if not records:
            raise CatalogIntegrityError("catalog contains no records")
        return cls(records, source_path=source, load_seconds=time.perf_counter() - started)


@dataclass(frozen=True, slots=True)
class ResolutionPolicy:
    auto_score: float = 0.77
    auto_margin: float = 0.01
    ambiguous_score: float = 0.48


class OfflineCatalogMapper:
    def __init__(
        self, catalog: XactimateCatalog | None = None, *, policy: ResolutionPolicy | None = None,
        semantic_reranker: SemanticReranker | None = None,
    ) -> None:
        self.catalog = catalog or XactimateCatalog.load()
        self.policy = policy or ResolutionPolicy()
        self.fallback_record = self.catalog.by_identity.get((FALLBACK_CAT, FALLBACK_SEL))
        self.semantic_reranker = semantic_reranker

    def _candidate_indices(self, query_tokens: set[str]) -> set[int]:
        indices: set[int] = set()
        for token in query_tokens:
            indices.update(self.catalog._postings.get(token, ()))
        return indices or set(range(len(self.catalog.records)))

    def _score(self, query: str, record: CatalogRecord) -> tuple[float, dict[str, float]]:
        query_tokens = query.split()
        query_set, candidate_set = set(query_tokens), set(record.tokens)
        shared = query_set & candidate_set
        all_tokens = query_set | candidate_set
        weighted_shared = sum(self.catalog.idf.get(token, 1.0) for token in shared)
        weighted_union = sum(self.catalog.idf.get(token, 1.0) for token in all_tokens) or 1.0
        token_overlap = weighted_shared / weighted_union
        important = query_set - _LOW_INFORMATION
        important_overlap = len(important & candidate_set) / len(important) if important else token_overlap
        fuzzy = SequenceMatcher(None, query, record.normalized_description).ratio()
        unordered = SequenceMatcher(None, " ".join(sorted(query_tokens)), " ".join(sorted(record.tokens))).ratio()
        containment = min(len(query), len(record.normalized_description)) / max(len(query), len(record.normalized_description), 1) \
            if query in record.normalized_description or record.normalized_description in query else 0.0
        exact = float(query == record.normalized_description)
        final = 1.0 if exact else (
            0.34 * token_overlap + 0.25 * important_overlap + 0.21 * fuzzy
            + 0.12 * unordered + 0.08 * containment
        )
        return final, {
            "exact": exact, "weighted_token_overlap": token_overlap,
            "important_token_recall": important_overlap, "fuzzy_similarity": fuzzy,
            "unordered_similarity": unordered, "containment": containment,
        }

    @staticmethod
    def _coerce_source(source: str | SourceLineContext) -> SourceLineContext:
        return source if isinstance(source, SourceLineContext) else SourceLineContext(description=source)

    def _trade_context_score(
        self, context: str, description_categories: set[str], record: CatalogRecord,
    ) -> float:
        if record.category in description_categories:
            return 1.0
        if description_categories:
            return -0.2
        if not context:
            return 0.0
        supported = _TRADE_CONTEXT.get(record.category, set())
        if any(phrase in context for phrase in supported):
            return 1.0
        # A misleading label is only a small negative signal; description remains dominant.
        if any(phrase in context for phrases in _TRADE_CONTEXT.values() for phrase in phrases):
            return -0.2
        return 0.0

    def _action_score(self, source_action: str, record: CatalogRecord) -> float:
        candidate = record.normalized_description
        scores = []
        for phrases in _ACTION_PHRASES.values():
            source_has = any(phrase in source_action for phrase in phrases)
            candidate_has = any(phrase in candidate for phrase in phrases)
            if source_has:
                scores.append(1.0 if candidate_has else -0.15)
        # Plain R&R/remove/replace normally belongs to activity, not CAT/SEL.
        return max(scores, default=0.0)

    @staticmethod
    def _unit_score(unit: str, record: CatalogRecord) -> float:
        if not unit:
            return 0.0
        description = record.normalized_description
        if unit in {"lf", "linear foot", "foot"} and any(word in description for word in ("gutter", "downspout", "ridge", "flashing")):
            return 1.0
        if unit in {"sf", "square foot"} and any(word in description for word in ("siding", "wrap", "panel", "screen")):
            return 1.0
        if unit in {"sq", "square"} and any(word in description for word in ("roof", "shingle", "felt")):
            return 1.0
        if unit in {"ea", "each"} and any(word in description for word in ("vent", "cap", "door", "screen")):
            return 0.5
        return 0.0

    def _retrieve(
        self, source_description: str | SourceLineContext, *, top_k: int = 10,
        apply_phase_2_context: bool = True,
    ) -> list[CandidateScore]:
        if top_k < 1:
            raise ValueError("top_k must be positive")
        source = self._coerce_source(source_description)
        normalization = parse_source_normalization(
            source.description, explicit_activity=source.activity,
        )
        query = normalization.catalog_search_text
        query_tokens = set(query.split())
        context = source.context_text
        description_categories = {
            category for category, phrases in _TRADE_CONTEXT.items()
            if any(phrase in query for phrase in phrases)
        }
        source_action = normalize_catalog_text(" ".join(filter(None, (normalization.action, source.description))))
        unit = normalize_catalog_text(source.unit or "")
        scored = []
        for index in self._candidate_indices(query_tokens):
            record = self.catalog.records[index]
            # DOR/BIDITM is a policy fallback, never a retrieval winner.
            if record.identity == (FALLBACK_CAT, FALLBACK_SEL):
                continue
            lexical_score, components = self._score(query, record)
            trade_score = self._trade_context_score(context, description_categories, record) if apply_phase_2_context else 0.0
            action_score = self._action_score(source_action, record) if apply_phase_2_context else 0.0
            unit_score = self._unit_score(unit, record) if apply_phase_2_context else 0.0
            # Context can break a strong lexical tie, but only nudges weak
            # matches. Explicit action-bearing variants (detach/reset,
            # tear-off, paint) receive more weight because that language is
            # part of the catalog identity rather than generic R&R activity.
            context_weight = 0.08 if lexical_score >= 0.85 else 0.02
            # Explicit catalog-bearing activities (detach/reset, tear-off,
            # paint, material/labor only) remain a distinct retrieval signal
            # after being removed from the material-description query. Generic
            # remove/replace is deliberately absent from _ACTION_PHRASES and
            # therefore receives no catalog-identity boost.
            score = max(
                0.0, lexical_score + context_weight * trade_score + 0.55 * action_score + 0.015 * unit_score,
            )
            components.update({
                "lexical_score": lexical_score,
                "phrase_score": max(components["exact"], components["containment"]),
                "trade_category_context": trade_score,
                "action_activity": action_score,
                "unit_compatibility": unit_score,
                "semantic_similarity": 0.0,
                "semantic_applied": 0.0,
            })
            scored.append((score, record.category, record.selector, record, components))
        scored.sort(key=lambda item: (-item[0], item[1], item[2], item[3].description))
        candidates = [CandidateScore(
            rank=rank, category=record.category, selector=record.selector,
            catalog_description=record.description, item_id=record.item_id,
            price_list=record.price_list, final_score=round(score, 6),
            components={key: round(value, 6) for key, value in components.items()},
        ) for rank, (score, _cat, _sel, record, components) in enumerate(scored[:max(top_k, 25)], start=1)]
        if self.semantic_reranker is not None and apply_phase_2_context:
            try:
                semantic_scores = self.semantic_reranker.score(source, candidates)
                if len(semantic_scores) != len(candidates):
                    raise ValueError("semantic reranker returned the wrong number of scores")
                reranked = []
                for candidate, semantic in zip(candidates, semantic_scores, strict=True):
                    components = dict(candidate.components)
                    components["semantic_similarity"] = round(float(semantic), 6)
                    components["semantic_applied"] = 1.0
                    combined = 0.82 * candidate.final_score + 0.18 * max(0.0, min(1.0, float(semantic)))
                    reranked.append((combined, candidate, components))
                reranked.sort(key=lambda item: (-item[0], item[1].category, item[1].selector))
                candidates = [CandidateScore(
                    rank=rank, category=item.category, selector=item.selector,
                    catalog_description=item.catalog_description, item_id=item.item_id,
                    price_list=item.price_list, final_score=round(score, 6), components=components,
                ) for rank, (score, item, components) in enumerate(reranked, start=1)]
            except (RuntimeError, ValueError, OSError):
                # Optional semantics are advisory. Deterministic retrieval remains usable.
                pass
        return candidates[:top_k]

    def retrieve(self, source_description: str | SourceLineContext, *, top_k: int = 10) -> list[CandidateScore]:
        return self._retrieve(source_description, top_k=top_k, apply_phase_2_context=True)

    def retrieve_phase_1(self, source_description: str, *, top_k: int = 10) -> list[CandidateScore]:
        """Reproduce the context-free Phase 1 lexical baseline for evaluation."""
        return self._retrieve(source_description, top_k=top_k, apply_phase_2_context=False)

    def map_line(
        self, source_description: str | SourceLineContext, *, quantity: float | None = None,
        unit: str | None = None, pricing: dict[str, Any] | None = None,
    ) -> MappingResult:
        source = self._coerce_source(source_description)
        if not isinstance(source_description, SourceLineContext):
            source = SourceLineContext(
                description=source.description, quantity=quantity, unit=unit, pricing=dict(pricing or {}),
            )
        normalization = parse_source_normalization(
            source.description, explicit_activity=source.activity,
        )
        if source.activity != normalization.action:
            source = SourceLineContext(
                description=source.description, section=source.section, group=source.group,
                quantity=source.quantity, unit=source.unit, activity=normalization.action,
                trade_hint=source.trade_hint, pricing=dict(source.pricing),
            )
        candidates = self.retrieve(source, top_k=10)
        top = candidates[0]
        second_score = candidates[1].final_score if len(candidates) > 1 else 0.0
        margin = top.final_score - second_score
        exact = bool(top.components.get("exact"))
        unique_exact = exact and second_score < 1.0
        strong = unique_exact or (top.final_score >= self.policy.auto_score and margin >= self.policy.auto_margin)
        if strong:
            resolution, chosen, reason = "resolved", top, "unique exact description" if unique_exact else "score and margin passed"
        elif top.final_score >= self.policy.ambiguous_score:
            resolution, chosen, reason = "ambiguous", None, "top candidate did not pass auto-resolution score/margin"
        else:
            resolution, chosen, reason = "bid_item_fallback", None, "no normal catalog candidate passed the ambiguity floor"
        fallback = self.fallback_record if resolution == "bid_item_fallback" else None
        if source.activity == "remove_replace":
            activity_resolution = "external_activity"
        elif source.activity and top.components.get("action_activity") == 1.0 and strong:
            activity_resolution = "catalog_action_supported"
        elif source.activity:
            activity_resolution = "ambiguous"
        else:
            activity_resolution = "unspecified"
        return MappingResult(
            source_description=source.description,
            normalized_source_description=normalization.catalog_search_text,
            catalog_search_text=normalization.catalog_search_text,
            resolution=resolution,
            category=(chosen.category if chosen else FALLBACK_CAT if resolution == "bid_item_fallback" else None),
            selector=(chosen.selector if chosen else FALLBACK_SEL if resolution == "bid_item_fallback" else None),
            catalog_description=chosen.catalog_description if chosen else None,
            item_id=chosen.item_id if chosen else None,
            price_list=chosen.price_list if chosen else None,
            final_score=top.final_score, top_2_score=second_score, margin=round(margin, 6),
            reason=reason, candidates=candidates, source_quantity=source.quantity, source_unit=source.unit,
            source_pricing=dict(source.pricing),
            source_activity=source.activity, activity_resolution=activity_resolution,
            fallback_catalog_description=fallback.description if fallback else None,
            fallback_item_id=fallback.item_id if fallback else None,
            fallback_price_list=fallback.price_list if fallback else None,
        )

    def measure_lookup_performance(self, descriptions: Iterable[str | SourceLineContext]) -> dict[str, float]:
        timings = []
        for description in descriptions:
            started = time.perf_counter()
            self.retrieve(description)
            timings.append(time.perf_counter() - started)
        return {
            "catalog_load_seconds": self.catalog.load_seconds,
            "average_lookup_ms": 1000 * sum(timings) / len(timings) if timings else 0.0,
            "median_lookup_ms": 1000 * median(timings) if timings else 0.0,
            "approximate_index_memory_mb": (
                sys.getsizeof(self.catalog.records)
                + sum(sys.getsizeof(record) + sys.getsizeof(record.normalized_description) + sys.getsizeof(record.tokens)
                      for record in self.catalog.records)
                + sys.getsizeof(self.catalog._postings)
                + sum(sys.getsizeof(token) + sys.getsizeof(indices) for token, indices in self.catalog._postings.items())
            ) / (1024 * 1024),
        }
