"""Writer for extraction_report.json -- see estimate_extractor.output's
package docstring."""

from __future__ import annotations

import json
from pathlib import Path

from estimate_extractor.models.validation import ExtractionReport


def write_extraction_report(report: ExtractionReport, path: Path, pretty: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report.model_dump(mode="json"), indent=2 if pretty else None, default=str),
        encoding="utf-8",
    )
