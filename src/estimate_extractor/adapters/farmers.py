from __future__ import annotations

from estimate_extractor.adapters.base import BaseCarrierAdapter, CarrierProfile
from estimate_extractor.parsing.line_items import ColumnSchema

PROFILE = CarrierProfile(
    key="farmers",
    display_name="Farmers",
    detection_keywords=(
        "myclaim@farmersinsurance.com",
        "mid-century insurance company",
        "farmers.com/claimstatus",
    ),
    column_schema=ColumnSchema(core_fields=("unit_price", "tax", "replacement_cost_value")),
)


class FarmersAdapter(BaseCarrierAdapter):
    def __init__(self) -> None:
        super().__init__(PROFILE)
