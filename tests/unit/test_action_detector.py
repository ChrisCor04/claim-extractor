from estimate_extractor.mapping.action_detector import detect_action
from estimate_extractor.mapping.models import ActionType
from estimate_extractor.mapping.rules_config import load_normalization_rules
from estimate_extractor.mapping.pipeline import DEFAULT_CONFIG_DIR


def _rules():
    return load_normalization_rules(DEFAULT_CONFIG_DIR / "normalization_rules.yaml")


def test_r_and_r_detected_as_remove_and_replace():
    action, conf = detect_action('r&r gutter - aluminum - up to 5"', _rules().action_rules)
    assert action == ActionType.REMOVE_AND_REPLACE
    assert conf > 0.5


def test_detach_and_reset_detected():
    action, _ = detect_action("detach & reset exhaust cap - through roof", _rules().action_rules)
    assert action == ActionType.DETACH_AND_RESET


def test_tear_off_detected_as_remove():
    action, _ = detect_action("tear off, haul and dispose of comp. shingles", _rules().action_rules)
    assert action == ActionType.REMOVE


def test_pressure_wash_detected():
    action, _ = detect_action("clean with pressure/chemical spray", _rules().action_rules)
    assert action == ActionType.PRESSURE_WASH


def test_stain_and_finish_detected():
    action, _ = detect_action("stain & finish pergola", _rules().action_rules)
    assert action == ActionType.STAIN


def test_haul_debris_detected():
    action, _ = detect_action("haul debris - per pickup truck load", _rules().action_rules)
    assert action == ActionType.HAUL


def test_labor_minimum_detected():
    action, _ = detect_action("drywall labor minimum", _rules().action_rules)
    assert action == ActionType.LABOR_MINIMUM


def test_no_verb_returns_unknown_not_a_guess():
    # No action rule matches a plain material description with no verb --
    # must stay "unknown" rather than guessing "install".
    action, conf = detect_action("laminated - comp. shingle rfg. - w/out felt", _rules().action_rules)
    assert action == ActionType.UNKNOWN
    assert conf < 0.5


def test_action_detection_is_deterministic():
    rules = _rules()
    results = {detect_action('r&r gutter - aluminum - up to 5"', rules.action_rules) for _ in range(5)}
    assert len(results) == 1
