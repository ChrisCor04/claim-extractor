from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from estimate_extractor.xactimate_lookup.offline_catalog_benchmark import run_benchmark
from estimate_extractor.xactimate_lookup.offline_catalog_mapper import (
    DEFAULT_CATALOG_PATH,
    CatalogIntegrityError,
    OfflineCatalogMapper,
    ResolutionPolicy,
    SourceLineContext,
    XactimateCatalog,
    normalize_source_text,
)
from estimate_extractor.xactimate_lookup.offline_catalog_rerankers import restricted_candidate_choice


@pytest.fixture(scope="module")
def mapper() -> OfflineCatalogMapper:
    return OfflineCatalogMapper()


def test_authoritative_catalog_integrity_and_fallback_metadata(mapper):
    assert len(mapper.catalog.records) == 12_806
    assert len(mapper.catalog.by_identity) == 12_806
    fallback = mapper.catalog.by_identity[("DOR", "BIDITM")]
    assert fallback.description == "Doors (Bid Item)"
    assert fallback.item_id == "10810"


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("Ice & water barrier", ("RFG", "IWS")),
        ("ICE & WATER BARRIER", ("RFG", "IWS")),
        ("Ice-water barrier", ("RFG", "IWS")),
        ("Drip-edge", ("RFG", "DRIP")),
        ("3 tab - 25 yr. comp. shingle roofing - incl. felt", ("RFG", "240")),
        ("Remove Ice & water barrier", ("RFG", "IWS")),
        ("Replace Ice & water barrier", ("RFG", "IWS")),
        ("R&R Ice & water barrier", ("RFG", "IWS")),
    ],
)
def test_normalization_and_irrelevant_carrier_wording(mapper, source, expected):
    top = mapper.retrieve(source)[0]
    assert (top.category, top.selector) == expected


def test_detach_reset_semantics_are_preserved(mapper):
    assert normalize_source_text("Detach & Reset Gutter / downspout - aluminum - up to 5\"").startswith("detach reset")
    result = mapper.map_line("Detach & Reset Gutter / downspout - aluminum - up to 5\"")
    assert result.resolution == "resolved"
    assert (result.category, result.selector) == ("SFG", "GUTRS")
    assert any((c.category, c.selector) == ("SFG", "GUTRS") for c in result.candidates)


def test_short_and_competing_descriptions_abstain(mapper):
    assert mapper.map_line("House wrap (air/moisture barrier)").resolution == "ambiguous"
    short = mapper.map_line("flashing")
    assert short.resolution != "resolved"
    assert len(short.candidates) == 10


def test_candidate_ordering_is_deterministic_and_catalog_backed(mapper):
    first = mapper.retrieve("vinyl siding")
    second = mapper.retrieve("vinyl siding")
    assert first == second
    for candidate in first:
        record = mapper.catalog.by_identity[(candidate.category, candidate.selector)]
        assert candidate.catalog_description == record.description
        assert candidate.item_id == record.item_id
        assert candidate.price_list == record.price_list
        assert (candidate.category, candidate.selector) != ("DOR", "BIDITM")


def test_resolved_identity_and_metadata_are_authoritative(mapper):
    result = mapper.map_line("Siding - vinyl")
    assert result.resolution == "resolved"
    record = mapper.catalog.by_identity[(result.category, result.selector)]
    assert (result.category, result.selector) == ("SDG", "VINYL")
    assert (result.catalog_description, result.item_id, result.price_list) == (
        record.description, record.item_id, record.price_list,
    )


def test_unresolved_uses_policy_fallback_and_preserves_source_fields(mapper):
    pricing = {"unit_price": 12.34, "total": 37.02}
    result = mapper.map_line("zxqv orbital unicorn retrofit", quantity=3, unit="EA", pricing=pricing)
    assert result.resolution == "bid_item_fallback"
    assert (result.category, result.selector) == ("DOR", "BIDITM")
    assert result.source_description == "zxqv orbital unicorn retrofit"
    assert (result.source_quantity, result.source_unit, result.source_pricing) == (3, "EA", pricing)
    assert result.reason == "no normal catalog candidate passed the ambiguity floor"
    assert result.fallback_catalog_description == "Doors (Bid Item)"
    assert result.fallback_item_id == "10810"
    assert len(result.candidates) == 10
    assert all((c.category, c.selector) != ("DOR", "BIDITM") for c in result.candidates)


def test_fallback_policy_does_not_fabricate_missing_metadata(mapper):
    catalog = XactimateCatalog(
        [record for record in mapper.catalog.records if record.identity != ("DOR", "BIDITM")],
        source_path=mapper.catalog.source_path,
        load_seconds=0,
    )
    result = OfflineCatalogMapper(catalog, policy=ResolutionPolicy(1.1, 1.1, 1.1)).map_line("unknown")
    assert (result.category, result.selector) == ("DOR", "BIDITM")
    assert result.fallback_catalog_description is None
    assert result.fallback_item_id is None


def test_catalog_path_is_independent_of_working_directory(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    catalog = XactimateCatalog.load()
    assert catalog.source_path == DEFAULT_CATALOG_PATH.resolve()
    assert len(catalog.records) == 12_806


def test_reference_csv_is_never_modified(mapper):
    before = (DEFAULT_CATALOG_PATH.stat().st_mtime_ns, hashlib.sha256(DEFAULT_CATALOG_PATH.read_bytes()).hexdigest())
    mapper.map_line("Ridge cap")
    after = (DEFAULT_CATALOG_PATH.stat().st_mtime_ns, hashlib.sha256(DEFAULT_CATALOG_PATH.read_bytes()).hexdigest())
    assert after == before


@pytest.mark.parametrize(
    "contents",
    [
        "CAT,SEL,Description\nRFG,IWS,Ice & water barrier\n",
        "PriceList,CAT,SEL,Description,ItemId\nP,,IWS,Ice & water barrier,1\n",
        "PriceList,CAT,SEL,Description,ItemId\nP,RFG,,Ice & water barrier,1\n",
        "PriceList,CAT,SEL,Description,ItemId\nP,RFG,IWS,,1\n",
        "PriceList,CAT,SEL,Description,ItemId\nP,RFG,IWS,One,1\nP,RFG,IWS,Two,2\n",
    ],
)
def test_malformed_catalog_fails_clearly(tmp_path: Path, contents: str):
    path = tmp_path / "catalog.csv"
    path.write_text(contents, encoding="utf-8")
    with pytest.raises(CatalogIntegrityError):
        XactimateCatalog.load(path)


def test_evidence_benchmark_retrieval_and_policy(mapper):
    report = run_benchmark(mapper)
    assert report["total_cases"] >= 50
    assert report["catalog_coverage"] == report["total_cases"]
    assert report["phase_2"]["top_1_accuracy"] >= report["phase_1_on_expanded_benchmark"]["top_1_accuracy"]
    assert report["phase_2"]["top_10_accuracy"] == 1.0
    assert report["proposed_policy"]["auto_resolved_accuracy"] == 1.0
    assert report["proposed_policy"]["auto_resolved"] > 0
    assert report["incorrect_high_confidence"] == 0
    assert report["breakdowns"]["with_context"]["cases"] > 0
    assert report["breakdowns"]["without_context"]["cases"] == report["total_cases"]


def test_lookup_performance_report_has_requested_fields(mapper):
    metrics = mapper.measure_lookup_performance(["Ice & water barrier", "Siding - vinyl"])
    assert metrics["catalog_load_seconds"] >= 0
    assert metrics["average_lookup_ms"] > 0
    assert metrics["median_lookup_ms"] > 0
    assert metrics["approximate_index_memory_mb"] > 0


def test_trade_context_resolves_identical_house_wrap_descriptions(mapper):
    no_context = mapper.map_line("House wrap (air/moisture barrier)")
    siding = mapper.map_line(SourceLineContext(
        "House wrap (air/moisture barrier)", group="Exterior surfaces", unit="SF",
    ))
    insulation = mapper.retrieve(SourceLineContext(
        "House wrap (air/moisture barrier)", group="Attic insulation", unit="SF",
    ))
    assert no_context.resolution == "ambiguous"
    assert (siding.category, siding.selector) == ("SDG", "HWRAP")
    assert (insulation[0].category, insulation[0].selector) == ("INS", "HWRAP")
    assert siding.candidates[0].components["trade_category_context"] == 1.0


def test_detach_reset_action_resolves_gutter_variant_without_answer_lookup(mapper):
    result = mapper.map_line(SourceLineContext(
        'Detach & Reset Gutter / downspout - aluminum - up to 5"',
        group="Main Roof", unit="LF", activity="detach_reset",
    ))
    assert (result.candidates[0].category, result.candidates[0].selector) == ("SFG", "GUTRS")
    assert result.candidates[0].components["action_activity"] == 1.0
    assert result.candidates[0].components["unit_compatibility"] == 1.0
    assert result.activity_resolution == "catalog_action_supported"


def test_action_bearing_removal_row_and_base_material_remain_distinct(mapper):
    tear_off = mapper.retrieve(SourceLineContext(
        "Tear off, haul and dispose of comp. shingles - Laminated",
        group="Dwelling Roof", activity="tear_off", unit="SQ",
    ))
    base = mapper.retrieve(SourceLineContext(
        "Laminated - comp. shingle rfg. - w/out felt", group="Dwelling Roof", unit="SQ",
    ))
    assert (tear_off[0].category, tear_off[0].selector) == ("RFG", "ARMV>")
    assert (base[0].category, base[0].selector) == ("RFG", "300S")


def test_generic_remove_replace_is_preserved_as_external_activity(mapper):
    result = mapper.map_line(SourceLineContext(
        "R&R Siding - vinyl", group="Exterior surfaces", activity="remove_replace",
    ))
    assert (result.candidates[0].category, result.candidates[0].selector) == ("SDG", "VINYL")
    assert result.source_activity == "remove_replace"
    assert result.activity_resolution == "external_activity"


def test_misleading_group_cannot_override_strong_description(mapper):
    result = mapper.retrieve(SourceLineContext("Ice & water barrier", group="Fence", unit="EA"))
    assert (result[0].category, result[0].selector) == ("RFG", "IWS")


def test_unit_is_soft_and_missing_or_wrong_unit_does_not_regress(mapper):
    identities = []
    for unit in (None, "LF", "EA", "noisy-unit"):
        top = mapper.retrieve(SourceLineContext("Siding - vinyl", group="Exterior surfaces", unit=unit))[0]
        identities.append((top.category, top.selector))
    assert identities == [("SDG", "VINYL")] * 4


def test_structured_context_preserves_fallback_payload(mapper):
    source = SourceLineContext(
        "zxqv orbital unicorn retrofit", group="Exterior", quantity=2.5,
        unit="EA", activity="install", pricing={"total": 99.0},
    )
    result = mapper.map_line(source)
    assert result.resolution == "bid_item_fallback"
    assert (result.category, result.selector) == ("DOR", "BIDITM")
    assert (result.source_quantity, result.source_unit, result.source_pricing) == (2.5, "EA", {"total": 99.0})


class _UnavailableSemanticReranker:
    def score(self, source, candidates):
        raise RuntimeError("optional model unavailable")


class _DeterministicSemanticReranker:
    def score(self, source, candidates):
        return [1.0 if (candidate.category, candidate.selector) == ("SDG", "HWRAP") else 0.0 for candidate in candidates]


def test_optional_embedding_unavailable_preserves_lexical_behavior(mapper):
    fallback = OfflineCatalogMapper(mapper.catalog, semantic_reranker=_UnavailableSemanticReranker())
    assert fallback.retrieve("Ice & water barrier") == mapper.retrieve("Ice & water barrier")


def test_semantic_reranker_can_only_rerank_real_lexical_candidates(mapper):
    semantic = OfflineCatalogMapper(mapper.catalog, semantic_reranker=_DeterministicSemanticReranker())
    candidates = semantic.retrieve("House wrap (air/moisture barrier)")
    assert (candidates[0].category, candidates[0].selector) == ("SDG", "HWRAP")
    assert candidates[0].components["semantic_applied"] == 1.0
    assert all((c.category, c.selector) in mapper.catalog.by_identity for c in candidates)


def test_future_choice_reranker_is_restricted_to_supplied_candidates(mapper):
    candidates = mapper.retrieve("Drip edge", top_k=5)

    class ChooseSecond:
        def choose(self, source, supplied):
            return 1

    class InventOutsideList:
        def choose(self, source, supplied):
            return len(supplied)

    source = SourceLineContext("Drip edge")
    assert restricted_candidate_choice(ChooseSecond(), source, candidates) is candidates[1]
    with pytest.raises(ValueError):
        restricted_candidate_choice(InventOutsideList(), source, candidates)
