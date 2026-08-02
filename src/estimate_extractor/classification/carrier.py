"""Carrier detection: scores each registered adapter against the document's
full text and picks the best match above the configured threshold, falling
back to the generic adapter otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass

from estimate_extractor.models.page import ParsedDocument


@dataclass(frozen=True, slots=True)
class CarrierMatch:
    key: str
    display_name: str
    confidence: float


def detect_carrier(document: ParsedDocument, adapters: dict, threshold: float) -> CarrierMatch:
    """``adapters`` maps carrier key -> CarrierAdapter instance. The
    "generic" key must be present as the fallback."""
    scores: list[CarrierMatch] = []
    for key, adapter in adapters.items():
        if key == "generic":
            continue
        score = adapter.detect(document)
        scores.append(CarrierMatch(key=key, display_name=adapter.display_name, confidence=score))

    scores.sort(key=lambda m: m.confidence, reverse=True)
    if scores and scores[0].confidence >= threshold:
        return scores[0]

    generic = adapters["generic"]
    return CarrierMatch(key="generic", display_name=generic.display_name, confidence=1.0 if not scores else scores[0].confidence)
