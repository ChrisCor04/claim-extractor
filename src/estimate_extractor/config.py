"""Configuration loading.

Config is plain YAML (see config.example.yaml). Money/percentage tolerances
are parsed as Decimal since they feed directly into reconciliation math.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True, slots=True)
class ExtractorConfig:
    version: str = "0.1.0"
    carrier_detection_threshold: float = 0.70
    generic_fallback_enabled: bool = True


@dataclass(frozen=True, slots=True)
class OCRConfig:
    enabled: bool = False
    minimum_native_text_characters: int = 50
    engine: str = "tesseract"
    dpi: int = 300


@dataclass(frozen=True, slots=True)
class ValidationConfig:
    money_absolute_tolerance: Decimal = Decimal("0.05")
    percentage_tolerance: Decimal = Decimal("0.001")
    low_confidence_threshold: float = 0.80
    fatal_confidence_threshold: float = 0.40


@dataclass(frozen=True, slots=True)
class OutputConfig:
    write_raw_text: bool = True
    write_debug_json: bool = True
    pretty_json: bool = True
    redact_debug_output: bool = False


@dataclass(frozen=True, slots=True)
class Config:
    extractor: ExtractorConfig = field(default_factory=ExtractorConfig)
    ocr: OCRConfig = field(default_factory=OCRConfig)
    validation: ValidationConfig = field(default_factory=ValidationConfig)
    output: OutputConfig = field(default_factory=OutputConfig)

    @staticmethod
    def default() -> "Config":
        return Config()

    @staticmethod
    def from_yaml(path: Path) -> "Config":
        raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return Config.from_dict(raw)

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> "Config":
        extractor_raw = raw.get("extractor", {}) or {}
        ocr_raw = raw.get("ocr", {}) or {}
        validation_raw = raw.get("validation", {}) or {}
        output_raw = raw.get("output", {}) or {}

        extractor = ExtractorConfig(
            version=extractor_raw.get("version", ExtractorConfig.version),
            carrier_detection_threshold=float(
                extractor_raw.get(
                    "carrier_detection_threshold",
                    ExtractorConfig.carrier_detection_threshold,
                )
            ),
            generic_fallback_enabled=bool(
                extractor_raw.get(
                    "generic_fallback_enabled", ExtractorConfig.generic_fallback_enabled
                )
            ),
        )
        ocr = OCRConfig(
            enabled=bool(ocr_raw.get("enabled", OCRConfig.enabled)),
            minimum_native_text_characters=int(
                ocr_raw.get(
                    "minimum_native_text_characters",
                    OCRConfig.minimum_native_text_characters,
                )
            ),
            engine=ocr_raw.get("engine", OCRConfig.engine),
            dpi=int(ocr_raw.get("dpi", OCRConfig.dpi)),
        )
        validation = ValidationConfig(
            money_absolute_tolerance=Decimal(
                str(
                    validation_raw.get(
                        "money_absolute_tolerance",
                        ValidationConfig.money_absolute_tolerance,
                    )
                )
            ),
            percentage_tolerance=Decimal(
                str(
                    validation_raw.get(
                        "percentage_tolerance", ValidationConfig.percentage_tolerance
                    )
                )
            ),
            low_confidence_threshold=float(
                validation_raw.get(
                    "low_confidence_threshold", ValidationConfig.low_confidence_threshold
                )
            ),
            fatal_confidence_threshold=float(
                validation_raw.get(
                    "fatal_confidence_threshold",
                    ValidationConfig.fatal_confidence_threshold,
                )
            ),
        )
        output = OutputConfig(
            write_raw_text=bool(output_raw.get("write_raw_text", OutputConfig.write_raw_text)),
            write_debug_json=bool(
                output_raw.get("write_debug_json", OutputConfig.write_debug_json)
            ),
            pretty_json=bool(output_raw.get("pretty_json", OutputConfig.pretty_json)),
            redact_debug_output=bool(
                output_raw.get("redact_debug_output", OutputConfig.redact_debug_output)
            ),
        )
        return Config(extractor=extractor, ocr=ocr, validation=validation, output=output)
