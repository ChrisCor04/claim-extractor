"""Fallback adapter used when no registered carrier crosses the detection
confidence threshold. Assumes the common Xactimate-style column layout
(unit price, tax, RCV, then the AGE/LIFE/CONDITION/DEP%/DEPREC. trailing
zone, then ACV) shared by State Farm, Travelers, and Farmers.
"""

from __future__ import annotations

from estimate_extractor.adapters.base import BaseCarrierAdapter, CarrierProfile
from estimate_extractor.parsing.line_items import ColumnSchema

PROFILE = CarrierProfile(
    key="generic",
    display_name="Generic (Xactimate-style)",
    detection_keywords=(),
    column_schema=ColumnSchema(core_fields=("unit_price", "tax", "replacement_cost_value")),
)


class GenericXactimateStyleAdapter(BaseCarrierAdapter):
    def __init__(self) -> None:
        super().__init__(PROFILE)

    def detect(self, document) -> float:
        return 0.0  # never wins detection on its own; only used as the fallback
