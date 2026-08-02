from estimate_extractor.mapping.component_detector import detect_component
from estimate_extractor.mapping.pipeline import DEFAULT_CONFIG_DIR
from estimate_extractor.mapping.rules_config import load_normalization_rules


def _rules():
    return load_normalization_rules(DEFAULT_CONFIG_DIR / "normalization_rules.yaml")


def test_laminated_shingle_component_and_material():
    component, material, conf = detect_component(
        "laminated - comp. shingle rfg. - w/out felt", _rules().trade_component_rules
    )
    assert component == "composition_shingles"
    assert material == "laminated composition shingles"
    assert conf > 0.5


def test_ridge_cap_component_no_material():
    component, material, _ = detect_component(
        "hip / ridge cap - standard profile - composition shingles", _rules().trade_component_rules
    )
    assert component == "ridge_cap"


def test_gutter_component_and_material():
    component, material, _ = detect_component('r&r gutter - aluminum - up to 5"', _rules().trade_component_rules)
    assert component == "gutter"
    assert material == "aluminum"


def test_unrecognized_component_is_unknown_not_a_guess():
    component, material, conf = detect_component("completely unrecognizable gibberish xyz", _rules().trade_component_rules)
    assert component == "unknown"
    assert material is None
    assert conf < 0.5
