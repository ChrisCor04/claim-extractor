"""Optional, offline-only rerankers for real catalog candidates."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, Sequence, TYPE_CHECKING

if TYPE_CHECKING:
    from .offline_catalog_mapper import CandidateScore, SourceLineContext, XactimateCatalog


class SemanticReranker(Protocol):
    """Return a semantic similarity for each supplied real candidate."""

    def score(self, source: "SourceLineContext", candidates: Sequence["CandidateScore"]) -> list[float]: ...


class CandidateChoiceReranker(Protocol):
    """A future LLM may return only an index into the supplied candidates."""

    def choose(self, source: "SourceLineContext", candidates: Sequence["CandidateScore"]) -> int | None: ...


def restricted_candidate_choice(
    reranker: CandidateChoiceReranker,
    source: "SourceLineContext",
    candidates: Sequence["CandidateScore"],
) -> "CandidateScore | None":
    choice = reranker.choose(source, candidates)
    if choice is None:
        return None
    if not isinstance(choice, int) or isinstance(choice, bool) or not 0 <= choice < len(candidates):
        raise ValueError("reranker must abstain or return an index into the supplied real candidate list")
    return candidates[choice]


@dataclass(slots=True)
class SentenceTransformerSemanticReranker:
    """Optional local sentence-transformer index with an immutable-source fingerprint.

    The model must already be installed locally. Catalog vectors are encoded once,
    then stored in a derived NPZ cache separate from the reference CSV.
    """

    catalog: "XactimateCatalog"
    model_path: str
    cache_path: Path
    _np: object = field(init=False, repr=False)
    _model: object = field(init=False, repr=False)
    _vectors: object = field(init=False, repr=False)
    _index: dict[tuple[str, str], int] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        try:
            import numpy as np
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:  # pragma: no cover - optional environment
            raise RuntimeError("local semantic reranking requires the optional 'semantic' dependencies") from exc
        self._np = np
        self._model = SentenceTransformer(self.model_path, local_files_only=True)
        fingerprint = hashlib.sha256(self.catalog.source_path.read_bytes()).hexdigest()
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        if self.cache_path.exists():
            cached = np.load(self.cache_path, allow_pickle=False)
            if str(cached["catalog_sha256"]) == fingerprint and len(cached["vectors"]) == len(self.catalog.records):
                self._vectors = cached["vectors"]
            else:
                self._vectors = self._build(fingerprint)
        else:
            self._vectors = self._build(fingerprint)
        self._index = {record.identity: index for index, record in enumerate(self.catalog.records)}

    def _build(self, fingerprint: str):
        vectors = self._model.encode(
            [record.description for record in self.catalog.records],
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        self._np.savez_compressed(self.cache_path, catalog_sha256=fingerprint, vectors=vectors)
        return vectors

    def score(self, source: "SourceLineContext", candidates: Sequence["CandidateScore"]) -> list[float]:
        query = " | ".join(filter(None, (source.description, source.section, source.group, source.trade_hint)))
        vector = self._model.encode([query], normalize_embeddings=True, show_progress_bar=False)[0]
        return [float(self._vectors[self._index[(item.category, item.selector)]] @ vector) for item in candidates]
