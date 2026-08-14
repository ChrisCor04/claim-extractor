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

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CATALOG_PATH = REPOSITORY_ROOT / "fixtures" / "reference" / "xactimate_catalog_unique.csv"
REQUIRED_COLUMNS = ("PriceList", "CAT", "SEL", "Description", "ItemId")
FALLBACK_CAT = "DOR"
FALLBACK_SEL = "BIDITM"

_WORD_RE = re.compile(r"[a-z0-9]+")
_LEADING_ACTION_RE = re.compile(
    r"^(?:remove\s+and\s+replace|remove\s*&\s*replace|r\s*&\s*r|remove|replace|install)\s+"
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


class CatalogIntegrityError(ValueError):
    pass


def normalize_catalog_text(value: str) -> str:
    text = value.casefold().replace("&", " and ")
    text = re.sub(r"\br\s*[/&]\s*r\b", "remove and replace", text)
    text = re.sub(r"\bdetach\s*(?:and|/)\s*reset\b", "detach reset", text)
    text = re.sub(r"\bw\s*/\s*out\b", "without", text)
    text = re.sub(r"\bw\s*/\s*o\b", "without", text)
    words = _WORD_RE.findall(text)
    words = [_ABBREVIATIONS.get(word, word) for word in words]
    # A conservative plural fold: do not damage short codes/words.
    words = [word[:-1] if len(word) > 4 and word.endswith("s") and not word.endswith("ss") else word for word in words]
    return " ".join(words)


def normalize_source_text(value: str) -> str:
    normalized = normalize_catalog_text(value)
    # Ordinary carrier activity prefixes frequently describe the source-line
    # operation while CAT/SEL identifies the component. Detach/reset is kept:
    # the catalog itself contains distinct detach/reset selectors.
    return _LEADING_ACTION_RE.sub("", normalized).strip()


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
    auto_score: float = 0.78
    auto_margin: float = 0.055
    ambiguous_score: float = 0.48


class OfflineCatalogMapper:
    def __init__(self, catalog: XactimateCatalog | None = None, *, policy: ResolutionPolicy | None = None) -> None:
        self.catalog = catalog or XactimateCatalog.load()
        self.policy = policy or ResolutionPolicy()
        self.fallback_record = self.catalog.by_identity.get((FALLBACK_CAT, FALLBACK_SEL))

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

    def retrieve(self, source_description: str, *, top_k: int = 10) -> list[CandidateScore]:
        if top_k < 1:
            raise ValueError("top_k must be positive")
        query = normalize_source_text(source_description)
        query_tokens = set(query.split())
        scored = []
        for index in self._candidate_indices(query_tokens):
            record = self.catalog.records[index]
            # DOR/BIDITM is a policy fallback, never a retrieval winner.
            if record.identity == (FALLBACK_CAT, FALLBACK_SEL):
                continue
            score, components = self._score(query, record)
            scored.append((score, record.category, record.selector, record, components))
        scored.sort(key=lambda item: (-item[0], item[1], item[2], item[3].description))
        return [CandidateScore(
            rank=rank, category=record.category, selector=record.selector,
            catalog_description=record.description, item_id=record.item_id,
            price_list=record.price_list, final_score=round(score, 6),
            components={key: round(value, 6) for key, value in components.items()},
        ) for rank, (score, _cat, _sel, record, components) in enumerate(scored[:top_k], start=1)]

    def map_line(
        self, source_description: str, *, quantity: float | None = None,
        unit: str | None = None, pricing: dict[str, Any] | None = None,
    ) -> MappingResult:
        candidates = self.retrieve(source_description, top_k=10)
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
        return MappingResult(
            source_description=source_description,
            normalized_source_description=normalize_source_text(source_description),
            resolution=resolution,
            category=(chosen.category if chosen else FALLBACK_CAT if resolution == "bid_item_fallback" else None),
            selector=(chosen.selector if chosen else FALLBACK_SEL if resolution == "bid_item_fallback" else None),
            catalog_description=chosen.catalog_description if chosen else None,
            item_id=chosen.item_id if chosen else None,
            price_list=chosen.price_list if chosen else None,
            final_score=top.final_score, top_2_score=second_score, margin=round(margin, 6),
            reason=reason, candidates=candidates, source_quantity=quantity, source_unit=unit,
            source_pricing=dict(pricing or {}),
            fallback_catalog_description=fallback.description if fallback else None,
            fallback_item_id=fallback.item_id if fallback else None,
            fallback_price_list=fallback.price_list if fallback else None,
        )

    def measure_lookup_performance(self, descriptions: Iterable[str]) -> dict[str, float]:
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
