from __future__ import annotations

from estimate_extractor.adapters.base import BaseCarrierAdapter, CarrierProfile
from estimate_extractor.parsing.line_items import ColumnSchema

PROFILE = CarrierProfile(
    key="travelers",
    display_name="Travelers",
    detection_keywords=(
        "travelers.com",
        "the travelers indemnity company",
        "the standard fire insurance company",
    ),
    column_schema=ColumnSchema(core_fields=("unit_price", "tax", "replacement_cost_value")),
)


class TravelersAdapter(BaseCarrierAdapter):
    def __init__(self) -> None:
        super().__init__(PROFILE)
