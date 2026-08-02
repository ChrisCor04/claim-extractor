"""Logging setup. Redacts emails/phones/zips from every log record so PII
never lands in ordinary log output at INFO level or above (full raw text is
only ever written to local debug output files, see output/json_writer.py).
"""

from __future__ import annotations

import logging

from estimate_extractor.normalization.redact import redact_text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_text(record.msg)
        record.args = None if not record.args else tuple(
            redact_text(a) if isinstance(a, str) else a for a in record.args
        )
        return True


def configure_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    root.setLevel(level.upper())
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(RedactingFilter())
    root.handlers = [handler]
