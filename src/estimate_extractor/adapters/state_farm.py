from __future__ import annotations

from estimate_extractor.adapters.base import BaseCarrierAdapter, CarrierProfile
from estimate_extractor.parsing.line_items import ColumnSchema

PROFILE = CarrierProfile(
    key="state_farm",
    display_name="State Farm",
    detection_keywords=(
        "state farm claims",
        "statefarmfireclaims@statefarm.com",
        "state farm insurance",
        "state farm",
    ),
    column_schema=ColumnSchema(core_fields=("unit_price", "tax", "replacement_cost_value")),
)


class StateFarmAdapter(BaseCarrierAdapter):
    def __init__(self) -> None:
        super().__init__(PROFILE)
