from estimate_extractor.mapping.models import TradeType
from estimate_extractor.mapping.pipeline import DEFAULT_CONFIG_DIR
from estimate_extractor.mapping.rules_config import load_normalization_rules
from estimate_extractor.mapping.trade_detector import detect_trade


def _rules():
    return load_normalization_rules(DEFAULT_CONFIG_DIR / "normalization_rules.yaml")


def test_gutter_detected_as_gutters_trade():
    trade, conf = detect_trade('r&r gutter - aluminum - up to 5"', _rules().trade_component_rules)
    assert trade == TradeType.GUTTERS
    assert conf > 0.5


def test_shingle_detected_as_roofing_trade():
    trade, _ = detect_trade("laminated - comp. shingle rfg. - w/out felt", _rules().trade_component_rules)
    assert trade == TradeType.ROOFING


def test_fence_detected_as_fencing_trade():
    trade, _ = detect_trade("stain - wood fence/gate", _rules().trade_component_rules)
    assert trade == TradeType.FENCING


def test_debris_detected_as_debris_removal_trade():
    trade, _ = detect_trade("haul debris - per pickup truck load", _rules().trade_component_rules)
    assert trade == TradeType.DEBRIS_REMOVAL


def test_unrecognized_text_returns_unknown_not_a_guess():
    trade, conf = detect_trade("completely unrecognizable gibberish xyz", _rules().trade_component_rules)
    assert trade == TradeType.UNKNOWN
    assert conf < 0.5
