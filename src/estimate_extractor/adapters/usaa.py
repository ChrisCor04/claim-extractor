from __future__ import annotations

from estimate_extractor.adapters.base import BaseCarrierAdapter, CarrierProfile
from estimate_extractor.parsing.line_items import ColumnSchema

PROFILE = CarrierProfile(
    key="usaa",
    display_name="USAA",
    detection_keywords=(
        "usaa casualty insurance company",
        "claims.usaa.com",
        "usaa confidential",
    ),
    column_schema=ColumnSchema(
        core_fields=("unit_price", "tax", "overhead_and_profit", "replacement_cost_value")
    ),
)


class USAAAdapter(BaseCarrierAdapter):
    def __init__(self) -> None:
        super().__init__(PROFILE)
