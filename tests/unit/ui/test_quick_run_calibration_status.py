from __future__ import annotations

import inspect
import re
from types import SimpleNamespace

from estimate_extractor.ui.components import quick_run_panel
from estimate_extractor.ui.components.quick_run_panel import (
    _calibration_group_names_text, _calibration_partial_prompt, _calibration_setup_prompt,
    _saved_calibration_status,
)
from estimate_extractor.xactimate_lookup.xactimate_calibration import CALIBRATION_GROUP_NAMES


class ExistingPath:
    def exists(self): return True


class MissingPath:
    def exists(self): return False


def profile(*, state="ready_for_fast_execution", pitch_state="measured_confident", pitch=19.0):
    return SimpleNamespace(
        validation_state=state, client_width=1920, client_height=1023, dpi=96,
        window_title="TEST",
        geometry={"group_row_pitch_state": pitch_state, "group_row_height": pitch},
    )


def test_saved_ready_calibration_is_visible_without_running_calibration():
    calls = []
    result = _saved_calibration_status(loader=lambda: calls.append("load") or profile(), path=ExistingPath())
    assert calls == ["load"]
    assert result == {"status": "Ready", "client_size": "1920 × 1023", "dpi": 96,
                      "group_row_pitch_state": "measured_confident", "chosen_row_pitch": "19 px",
                      "project_name": "TEST"}


def test_saved_incomplete_calibration_reports_needs_calibration():
    result = _saved_calibration_status(
        loader=lambda: profile(state="valid_non_destructive_core_landmarks", pitch_state="unresolved", pitch=None),
        path=ExistingPath(),
    )
    assert result["status"] == "Needs calibration"
    assert result["group_row_pitch_state"] == "unresolved"
    assert result["chosen_row_pitch"] == "—"


def test_missing_profile_reports_not_calibrated_without_loading():
    result = _saved_calibration_status(loader=lambda: (_ for _ in ()).throw(AssertionError("must not load")),
                                       path=MissingPath())
    assert result["status"] == "Not calibrated"
    assert result["project_name"] == ""


def test_invalid_saved_profile_reports_needs_calibration():
    result = _saved_calibration_status(loader=lambda: (_ for _ in ()).throw(RuntimeError("bad schema")),
                                       path=ExistingPath())
    assert result["status"] == "Needs calibration"
    assert result["group_row_pitch_state"] == "invalid_profile"


# -- manual-setup UX: the three exact names, no checkbox, no geometry --

def test_calibration_group_names_text_lists_the_three_exact_sentinels():
    text = _calibration_group_names_text()
    assert text.splitlines() == list(CALIBRATION_GROUP_NAMES)


def test_normal_ui_no_longer_exposes_the_temporary_group_creation_checkbox():
    assert not hasattr(quick_run_panel, "CALIBRATION_CREATION_CHECKBOX_LABEL")
    assert not hasattr(quick_run_panel, "CALIBRATION_CREATION_CHECKBOX_HELP")


def test_calibration_button_calls_calibrate_xactimate_with_creation_disabled():
    source = inspect.getsource(quick_run_panel._render_fast_grouped_mode)
    assert "allow_interactive_group_rows=False" in source
    # The removed checkbox's old, ambiguous label/help text must be gone --
    # an unrelated plan-approval checkbox later in the same function is
    # fine and untouched, so this checks the specific removed wording,
    # not "no checkbox anywhere in this function".
    assert "Allow creation of temporary calibration groups" not in source
    assert "quick_fast_allow_interactive_calibration" not in source


def test_zero_of_three_prompt_names_all_three_groups_and_no_geometry():
    message = _calibration_setup_prompt(missing=list(CALIBRATION_GROUP_NAMES))
    for name in CALIBRATION_GROUP_NAMES:
        assert name in message
    # No pixel/DPI/pitch-style measurements -- never suggests entering or
    # reading off a geometry number. ("3" for "3 groups" is fine; a
    # multi-digit measurement-looking number is not.)
    import re
    assert not re.search(r"\d{2,}", message)  # no measurement-looking number
    assert "px" not in message.casefold() and "pixel" not in message.casefold()
    assert "pitch" not in message.casefold() and "dpi" not in message.casefold()
    # No implementation terminology / stack traces.
    assert "traceback" not in message.casefold() and "exception" not in message.casefold()


def test_partial_prompt_lists_present_and_missing_separately():
    message = _calibration_partial_prompt(present=["CAL_ROW_ALPHA"], missing=["CAL_ROW_BRAVO", "CAL_ROW_CHARLIE"])
    assert "CAL_ROW_ALPHA" in message
    assert "CAL_ROW_BRAVO" in message
    assert "CAL_ROW_CHARLIE" in message
    found_index = message.index("CAL_ROW_ALPHA")
    missing_index = message.index("CAL_ROW_BRAVO")
    assert found_index < missing_index  # present listed before missing, matching the requested layout
    assert not re.search(r"\d{2,}", message)


def test_prompts_never_hardcode_machine_specific_geometry():
    # Guards against exactly the values this task explicitly forbids
    # hardcoding into the UI.
    forbidden = ["1920", "1023", "96", "18px", "20px"]
    zero_of_three = _calibration_setup_prompt(missing=list(CALIBRATION_GROUP_NAMES))
    partial = _calibration_partial_prompt(present=["CAL_ROW_ALPHA"], missing=["CAL_ROW_BRAVO", "CAL_ROW_CHARLIE"])
    for text in (zero_of_three, partial):
        for value in forbidden:
            assert value not in text
