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
    XactimateCatalog,
    normalize_source_text,
)


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
    assert result.resolution == "ambiguous"
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
    assert report["total_cases"] >= 25
    assert report["catalog_coverage"] == report["total_cases"]
    assert report["top_1_accuracy"] >= 0.90
    assert report["top_3_accuracy"] == 1.0
    assert report["top_10_accuracy"] == 1.0
    assert report["proposed_auto_resolved_accuracy"] == 1.0
    assert report["proposed_auto_resolved"] > 0
    assert all("failure_pattern" in miss and "explanation" in miss for miss in report["misses"])


def test_lookup_performance_report_has_requested_fields(mapper):
    metrics = mapper.measure_lookup_performance(["Ice & water barrier", "Siding - vinyl"])
    assert metrics["catalog_load_seconds"] >= 0
    assert metrics["average_lookup_ms"] > 0
    assert metrics["median_lookup_ms"] > 0
    assert metrics["approximate_index_memory_mb"] > 0
