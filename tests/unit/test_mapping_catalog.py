from pathlib import Path

import pytest
import yaml

from estimate_extractor.mapping.catalog import DuplicateMappingIdError, load_catalog
from estimate_extractor.mapping.pipeline import DEFAULT_CONFIG_DIR


def test_default_catalog_loads_without_error():
    catalog = load_catalog(DEFAULT_CONFIG_DIR / "mapping_catalog.yaml")
    assert len(catalog) > 0
    for entry in catalog:
        assert entry.mapping_id
        assert entry.trade
        assert entry.component


def test_default_catalog_has_no_duplicate_mapping_ids():
    catalog = load_catalog(DEFAULT_CONFIG_DIR / "mapping_catalog.yaml")
    ids = [e.mapping_id for e in catalog]
    assert len(ids) == len(set(ids))


def test_duplicate_mapping_id_raises(tmp_path: Path):
    bad_catalog = [
        {
            "mapping_id": "dup",
            "canonical_terms": ["a"],
            "trade": "roofing",
            "component": "x",
            "allowed_actions": ["install"],
            "allowed_units": ["SQ"],
            "xactimate": {"category": None, "selector": None, "activity": None, "description": None},
            "confidence_base": 0.5,
            "requires_review": True,
        },
        {
            "mapping_id": "dup",
            "canonical_terms": ["b"],
            "trade": "roofing",
            "component": "y",
            "allowed_actions": ["install"],
            "allowed_units": ["SQ"],
            "xactimate": {"category": None, "selector": None, "activity": None, "description": None},
            "confidence_base": 0.5,
            "requires_review": True,
        },
    ]
    path = tmp_path / "bad_catalog.yaml"
    path.write_text(yaml.dump(bad_catalog))
    with pytest.raises(DuplicateMappingIdError):
        load_catalog(path)


def test_no_selector_is_populated_without_verified_source():
    # Per the build spec's "Xactimate data integrity" requirement: this
    # repository has no licensed Xactimate price list, so no catalog entry
    # may claim a verified selector.
    catalog = load_catalog(DEFAULT_CONFIG_DIR / "mapping_catalog.yaml")
    assert all(entry.xactimate.selector is None for entry in catalog)
