from __future__ import annotations

import pytest

from estimate_extractor.xactimate_lookup.window_normalization import centered_rect


def test_centered_rect_uses_monitor_work_area_not_virtual_desktop_origin():
    assert centered_rect((0, 0, 2560, 1393), 1920, 1023) == (320, 185, 1920, 1023)
    assert centered_rect((-1920, 0, 0, 1040), 1600, 900) == (-1760, 70, 1600, 900)


def test_centered_rect_fails_when_validated_geometry_cannot_fit():
    with pytest.raises(RuntimeError, match="does not fit"):
        centered_rect((0, 0, 1366, 728), 1920, 1023)
