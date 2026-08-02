from __future__ import annotations

from estimate_extractor.adapters.base import BaseCarrierAdapter, CarrierProfile
from estimate_extractor.parsing.line_items import ColumnSchema

PROFILE = CarrierProfile(
    key="allstate",
    display_name="Allstate",
    detection_keywords=(
        "claims.allstate.com",
        "allstate indemnity company",
        "allstate property and casualty insurance company",
    ),
    # Allstate's line-item table has no TAX column: unit price and RCV only
    # before the AGE/LIFE + CONDITION + DEP% + DEPREC. trailing zone.
    column_schema=ColumnSchema(core_fields=("unit_price", "replacement_cost_value")),
)


class AllstateAdapter(BaseCarrierAdapter):
    def __init__(self) -> None:
        super().__init__(PROFILE)
