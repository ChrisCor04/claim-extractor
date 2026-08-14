"""Phase 1/Phase 2 offline benchmark utilities."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .offline_catalog_mapper import OfflineCatalogMapper, REPOSITORY_ROOT, SourceLineContext

DEFAULT_BENCHMARK_PATH = REPOSITORY_ROOT / "fixtures" / "reference" / "offline_catalog_benchmark.json"
PHASE_1_BASELINE = {
    "benchmark_cases": 25, "top_1_accuracy": 0.92, "top_3_accuracy": 1.0,
    "top_5_accuracy": 1.0, "top_10_accuracy": 1.0,
    "mean_reciprocal_rank": 0.9533333333333333,
    "auto_resolution_coverage": 0.64, "auto_resolved_accuracy": 1.0,
    "incorrect_high_confidence": 0,
}


@dataclass(frozen=True, slots=True)
class BenchmarkCase:
    source_description: str
    category: str
    selector: str
    evidence: str
    section: str | None = None
    group: str | None = None
    quantity: float | None = None
    unit: str | None = None
    activity: str | None = None
    trade_hint: str | None = None

    def source_context(self) -> SourceLineContext:
        return SourceLineContext(
            description=self.source_description, section=self.section, group=self.group,
            quantity=self.quantity, unit=self.unit, activity=self.activity, trade_hint=self.trade_hint,
        )


def _activity(description: str) -> str | None:
    normalized = description.casefold()
    if "detach" in normalized and "reset" in normalized:
        return "detach_reset"
    if "tear off" in normalized or "haul and dispose" in normalized:
        return "tear_off"
    if normalized.startswith(("r&r ", "remove ", "replace ")):
        return "remove_replace"
    if "paint" in normalized or normalized.startswith("stain"):
        return "paint"
    return None


def load_benchmark_cases(path: Path | None = None) -> list[BenchmarkCase]:
    data = json.loads((path or DEFAULT_BENCHMARK_PATH).read_text(encoding="utf-8"))
    cases = []
    evidence_cache: dict[Path, dict[str, Any]] = {}
    for item in data:
        enriched = dict(item)
        evidence_file, _, evidence_key = item["evidence"].partition("#")
        evidence_path = REPOSITORY_ROOT / evidence_file
        if evidence_key and evidence_path.exists():
            source = evidence_cache.setdefault(evidence_path, json.loads(evidence_path.read_text(encoding="utf-8")))
            observed = source.get(evidence_key, {})
            evidence_identity = (
                observed.get("selected_candidate_category"), observed.get("selected_candidate_selector"),
            )
            if evidence_identity != (item["category"], item["selector"]):
                raise ValueError(
                    f"benchmark truth {item['category']}/{item['selector']} disagrees with "
                    f"{item['evidence']} ({evidence_identity[0]}/{evidence_identity[1]})"
                )
            enriched.setdefault("group", observed.get("group"))
            enriched.setdefault("quantity", observed.get("quantity"))
            observed_unit = observed.get("observed_unit")
            if observed_unit in {"EA", "LF", "SF", "SQ"}:
                enriched.setdefault("unit", observed_unit)
        enriched.setdefault("activity", _activity(item["source_description"]))
        cases.append(BenchmarkCase(**enriched))
    return cases


def _rank(candidates, expected: tuple[str, str]) -> int | None:
    return next((candidate.rank for candidate in candidates if (candidate.category, candidate.selector) == expected), None)


def _metrics(ranks: list[int | None]) -> dict[str, float]:
    total = len(ranks)
    return {
        "top_1_accuracy": sum(rank == 1 for rank in ranks) / total,
        "top_3_accuracy": sum(rank is not None and rank <= 3 for rank in ranks) / total,
        "top_5_accuracy": sum(rank is not None and rank <= 5 for rank in ranks) / total,
        "top_10_accuracy": sum(rank is not None and rank <= 10 for rank in ranks) / total,
        "mean_reciprocal_rank": sum(1 / rank for rank in ranks if rank) / total,
    }


def _breakdown(details: list[dict[str, Any]], predicate) -> dict[str, Any]:
    selected = [detail for detail in details if predicate(detail)]
    if not selected:
        return {"cases": 0}
    return {"cases": len(selected), **_metrics([detail["phase_2_rank"] for detail in selected])}


def _proposed_policy(details: list[dict[str, Any]]) -> dict[str, Any]:
    observations = [
        (detail["phase_2_result"]["final_score"], detail["phase_2_result"]["margin"], detail["phase_2_rank"] == 1)
        for detail in details
    ]
    proposals = []
    for score_i in range(77, 101):
        for margin_i in range(1, 31):
            score, margin = score_i / 100, margin_i / 100
            accepted = [item for item in observations if item[0] >= score and item[1] >= margin]
            if accepted and all(item[2] for item in accepted):
                proposals.append((len(accepted), -score, -margin, score, margin))
    count, _a, _b, score, margin = max(proposals)
    return {
        "auto_score": score, "auto_margin": margin, "auto_resolved": count,
        "auto_resolution_coverage": count / len(observations), "auto_resolved_accuracy": 1.0,
        "derivation": "maximum expanded-benchmark coverage with zero incorrect automatic mappings",
    }


def run_benchmark(mapper: OfflineCatalogMapper, cases: list[BenchmarkCase] | None = None) -> dict[str, Any]:
    cases = cases or load_benchmark_cases()
    details: list[dict[str, Any]] = []
    phase_1_ranks, phase_2_ranks = [], []
    resolved_total = resolved_correct = ambiguous = fallback = incorrect_high = 0
    for case in cases:
        expected = (case.category, case.selector)
        phase_1 = mapper.retrieve_phase_1(case.source_description)
        result = mapper.map_line(case.source_context())
        phase_1_rank = _rank(phase_1, expected)
        phase_2_rank = _rank(result.candidates, expected)
        phase_1_ranks.append(phase_1_rank)
        phase_2_ranks.append(phase_2_rank)
        correct = (result.category, result.selector) == expected
        if result.resolution == "resolved":
            resolved_total += 1
            resolved_correct += int(correct)
            incorrect_high += int(not correct)
        elif result.resolution == "ambiguous":
            ambiguous += 1
        else:
            fallback += 1
        duplicate_exact = sum(candidate.components.get("exact", 0) == 1 for candidate in phase_1) > 1
        details.append({
            "case": asdict(case), "expected_catalog_description": mapper.catalog.by_identity[expected].description,
            "phase_1_rank": phase_1_rank, "phase_2_rank": phase_2_rank,
            "improvement": None if phase_1_rank is None or phase_2_rank is None else phase_1_rank - phase_2_rank,
            "phase_1_candidates": [candidate.to_dict() for candidate in phase_1],
            "phase_2_result": result.to_dict(), "has_context": bool(case.group or case.section or case.trade_hint),
            "action_heavy": case.activity is not None, "duplicate_description": duplicate_exact,
        })
    proposed = _proposed_policy(details)
    return {
        "baseline_phase_1_published": PHASE_1_BASELINE,
        "total_cases": len(cases),
        "catalog_coverage": sum((case.category, case.selector) in mapper.catalog.by_identity for case in cases),
        "phase_1_on_expanded_benchmark": _metrics(phase_1_ranks),
        "phase_2": _metrics(phase_2_ranks),
        "auto_resolved": resolved_total,
        "auto_resolution_coverage": resolved_total / len(cases),
        "auto_resolved_accuracy": resolved_correct / resolved_total if resolved_total else 0.0,
        "ambiguous": ambiguous, "bid_item_fallback": fallback,
        "incorrect_high_confidence": incorrect_high,
        "proposed_policy": proposed,
        "breakdowns": {
            "exact_description": _breakdown(details, lambda d: bool(d["phase_2_result"]["candidates"][0]["components"]["exact"])),
            "non_exact_description": _breakdown(details, lambda d: not d["phase_2_result"]["candidates"][0]["components"]["exact"]),
            "with_context": _breakdown(details, lambda d: d["has_context"]),
            "without_context": {"cases": len(details), **_metrics(phase_1_ranks)},
            "action_heavy": _breakdown(details, lambda d: d["action_heavy"]),
            "duplicate_description": _breakdown(details, lambda d: d["duplicate_description"]),
        },
        "improvements": [d for d in details if (d["improvement"] or 0) > 0],
        "regressions": [d for d in details if (d["improvement"] or 0) < 0],
        "misses": [d for d in details if d["phase_2_rank"] != 1],
        "details": details,
        "performance": mapper.measure_lookup_performance(case.source_context() for case in cases),
    }


def write_benchmark_report(output_path: Path, mapper: OfflineCatalogMapper | None = None) -> dict[str, Any]:
    report = run_benchmark(mapper or OfflineCatalogMapper())
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report
