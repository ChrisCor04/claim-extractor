"""Offline benchmark utilities for :mod:`offline_catalog_mapper`."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .offline_catalog_mapper import OfflineCatalogMapper, REPOSITORY_ROOT

DEFAULT_BENCHMARK_PATH = REPOSITORY_ROOT / "fixtures" / "reference" / "offline_catalog_benchmark.json"


@dataclass(frozen=True, slots=True)
class BenchmarkCase:
    source_description: str
    category: str
    selector: str
    evidence: str


def load_benchmark_cases(path: Path | None = None) -> list[BenchmarkCase]:
    data = json.loads((path or DEFAULT_BENCHMARK_PATH).read_text(encoding="utf-8"))
    return [BenchmarkCase(**item) for item in data]


def run_benchmark(mapper: OfflineCatalogMapper, cases: list[BenchmarkCase] | None = None) -> dict[str, Any]:
    cases = cases or load_benchmark_cases()
    ranks: list[int | None] = []
    details = []
    resolved_correct = resolved_total = ambiguous = fallback = incorrect_high = exact_cases = exact_correct = 0
    for case in cases:
        result = mapper.map_line(case.source_description)
        expected = (case.category, case.selector)
        rank = next((candidate.rank for candidate in result.candidates if (candidate.category, candidate.selector) == expected), None)
        ranks.append(rank)
        top_correct = rank == 1
        exact = bool(result.candidates and result.candidates[0].components.get("exact"))
        exact_cases += int(exact)
        exact_correct += int(exact and top_correct)
        if result.resolution == "resolved":
            resolved_total += 1
            resolved_correct += int((result.category, result.selector) == expected)
            incorrect_high += int((result.category, result.selector) != expected)
        elif result.resolution == "ambiguous":
            ambiguous += 1
        else:
            fallback += 1
        details.append({
            "case": asdict(case), "expected_rank": rank, "result": result.to_dict(),
            "top_correct": top_correct,
        })
    total = len(cases)
    metric = lambda k: sum(rank is not None and rank <= k for rank in ranks) / total
    misses = []
    for detail in details:
        if detail["top_correct"]:
            continue
        case = detail["case"]
        result = detail["result"]
        expected_rank = detail["expected_rank"]
        top = result["candidates"][0]
        expected_candidate = next(
            (item for item in result["candidates"] if (item["category"], item["selector"]) == (case["category"], case["selector"])),
            None,
        )
        if top["components"]["exact"] and expected_candidate and expected_candidate["components"]["exact"]:
            pattern = "catalog_descriptions_too_similar"
            explanation = "multiple CAT/SEL records normalize to the same source text; lexical evidence cannot choose the trade identity"
        elif expected_rank is None:
            pattern = "lexical_retrieval_limitation"
            explanation = "the supported CAT/SEL did not enter the lexical top 10; local semantic retrieval is the likely next layer"
        elif len(result["normalized_source_description"].split()) <= 2:
            pattern = "insufficient_source_description"
            explanation = "the source has too few discriminating terms to separate nearby catalog records"
        else:
            pattern = "trade_or_activity_confusion"
            explanation = "the same component wording occurs across trade/activity variants, and the source lacks enough lexical evidence to disambiguate"
        enriched = dict(detail)
        enriched["failure_pattern"] = pattern
        enriched["explanation"] = explanation
        misses.append(enriched)

    # Select the most permissive score/margin pair with no benchmark-supported
    # incorrect automatic mappings. Exact ties still require a positive margin.
    observations = []
    for detail in details:
        result = detail["result"]
        observations.append((
            result["final_score"], result["margin"], detail["top_correct"],
            bool(result["candidates"][0]["components"]["exact"]),
        ))
    proposals = []
    for score_i in range(50, 101):
        score_floor = score_i / 100
        for margin_i in range(0, 31):
            margin_floor = margin_i / 100
            accepted = [o for o in observations if o[0] >= score_floor and o[1] >= margin_floor]
            if accepted and all(o[2] for o in accepted):
                proposals.append((len(accepted), -score_floor, -margin_floor, score_floor, margin_floor))
    _coverage, _neg_score, _neg_margin, proposed_score, proposed_margin = max(proposals)
    proposed_accepted = [o for o in observations if o[0] >= proposed_score and o[1] >= proposed_margin]
    return {
        "total_cases": total,
        "catalog_coverage": sum((case.category, case.selector) in mapper.catalog.by_identity for case in cases),
        "top_1_accuracy": metric(1), "top_3_accuracy": metric(3),
        "top_5_accuracy": metric(5), "top_10_accuracy": metric(10),
        "mean_reciprocal_rank": sum(1 / rank for rank in ranks if rank) / total,
        "exact_description_cases": exact_cases,
        "exact_description_accuracy": exact_correct / exact_cases if exact_cases else 0.0,
        "non_exact_description_cases": total - exact_cases,
        "non_exact_description_accuracy": (
            (sum(rank == 1 for rank in ranks) - exact_correct) / (total - exact_cases)
            if total != exact_cases else 0.0
        ),
        "auto_resolved": resolved_total,
        "auto_resolved_accuracy": resolved_correct / resolved_total if resolved_total else 0.0,
        "ambiguous": ambiguous, "bid_item_fallback": fallback,
        "incorrect_high_confidence": incorrect_high,
        "policy": asdict(mapper.policy), "misses": misses, "details": details,
        "proposed_policy": {
            "auto_score": proposed_score,
            "auto_margin": proposed_margin,
            "derivation": "maximum benchmark coverage among score/margin grid points with zero incorrect automatic mappings",
        },
        "proposed_auto_resolved": len(proposed_accepted),
        "proposed_auto_resolution_coverage": len(proposed_accepted) / total,
        "proposed_auto_resolved_accuracy": sum(o[2] for o in proposed_accepted) / len(proposed_accepted),
        "performance": mapper.measure_lookup_performance(case.source_description for case in cases),
    }


def write_benchmark_report(output_path: Path, mapper: OfflineCatalogMapper | None = None) -> dict[str, Any]:
    report = run_benchmark(mapper or OfflineCatalogMapper())
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report
