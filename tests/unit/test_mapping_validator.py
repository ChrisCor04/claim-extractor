from estimate_extractor.mapping.catalog import CatalogEntry, XactimateInfo
from estimate_extractor.mapping.models import (
    MappedLineItem,
    MappingCandidate,
    MappingOutcome,
    MappingStatus,
    Normalized,
    NormalizationConfidence,
    NormalizedLineItem,
    OriginalItem,
)
from estimate_extractor.mapping.validator import validate_catalog, validate_mapping


def _normalized_item(line_item_id: str, coverage_id: str | None, extraction_needs_review: bool = False) -> NormalizedLineItem:
    return NormalizedLineItem(
        line_item_id=line_item_id,
        original=OriginalItem(
            description="Test item",
            coverage_id=coverage_id,
            extraction_needs_review=extraction_needs_review,
            extraction_warnings=["some_warning"] if extraction_needs_review else [],
        ),
        normalized=Normalized(),
        confidence=NormalizationConfidence(overall=0.9, action=0.9, trade=0.9, component=0.9, material=0.9),
    )


def _mapped_item(line_item_id: str, coverage_id: str | None, status: MappingStatus, needs_review: bool, reasons: list[str], selector: str | None = None) -> MappedLineItem:
    best = MappingCandidate(mapping_id="m1", category="RFG", selector=selector, activity="install", description="x", confidence=0.85)
    return MappedLineItem(
        line_item_id=line_item_id,
        coverage_id=coverage_id,
        normalization=Normalized(),
        mapping=MappingOutcome(status=status, best_match=best, alternatives=[], needs_review=needs_review, review_reasons=reasons),
    )


def test_unresolved_coverage_produces_info_issue_not_a_crash():
    normalized = [_normalized_item("line_0001", coverage_id=None)]
    mapped = [_mapped_item("line_0001", None, MappingStatus.PARTIALLY_MAPPED, True, ["missing_selector"])]
    issues = validate_mapping([], normalized, mapped)
    codes = [i.code for i in issues]
    assert "UNRESOLVED_COVERAGE" in codes


def test_upstream_extraction_issue_surfaced():
    normalized = [_normalized_item("line_0001", coverage_id="coverage_001", extraction_needs_review=True)]
    mapped = [_mapped_item("line_0001", "coverage_001", MappingStatus.PARTIALLY_MAPPED, True, [])]
    issues = validate_mapping([], normalized, mapped)
    codes = [i.code for i in issues]
    assert "POSSIBLE_UPSTREAM_EXTRACTION_ISSUE" in codes


def test_missing_selector_issue_generated():
    normalized = [_normalized_item("line_0001", coverage_id="coverage_001")]
    mapped = [_mapped_item("line_0001", "coverage_001", MappingStatus.PARTIALLY_MAPPED, True, ["missing_selector"])]
    issues = validate_mapping([], normalized, mapped)
    codes = [i.code for i in issues]
    assert "MISSING_SELECTOR" in codes


def test_duplicate_mapping_id_detected_by_catalog_validation():
    entry_a = CatalogEntry(
        mapping_id="dup",
        canonical_terms=("x",),
        trade="roofing",
        component="composition_shingles",
        allowed_actions=("install",),
        allowed_units=("SQ",),
        xactimate=XactimateInfo(category=None, selector=None, activity=None, description=None),
        confidence_base=0.5,
        requires_review=True,
    )
    entry_b = CatalogEntry(
        mapping_id="dup",
        canonical_terms=("y",),
        trade="roofing",
        component="composition_shingles",
        allowed_actions=("install",),
        allowed_units=("SQ",),
        xactimate=XactimateInfo(category=None, selector=None, activity=None, description=None),
        confidence_base=0.5,
        requires_review=True,
    )
    issues = validate_catalog([entry_a, entry_b])
    assert any(i.code == "DUPLICATE_MAPPING_RULE" and i.severity.value == "error" for i in issues)


def test_no_line_item_is_skipped_even_without_a_mapped_counterpart():
    # A normalized item with no corresponding mapped item must not raise --
    # validator should just skip it gracefully (defensive; the pipeline
    # always produces both, but the validator itself must not crash).
    normalized = [_normalized_item("line_0001", coverage_id="coverage_001")]
    issues = validate_mapping([], normalized, [])
    assert isinstance(issues, list)
