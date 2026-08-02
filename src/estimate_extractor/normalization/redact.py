"""PII redaction for logs and (optionally) debug output.

Per the privacy requirements: ordinary logs must never contain full claim
numbers, emails, phone numbers, or addresses at INFO level. Full raw text
is only ever written to local debug output, and only when the operator has
not additionally passed --redact-debug-output.
"""

from __future__ import annotations

import re

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE_RE = re.compile(r"\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}")
_ZIP_RE = re.compile(r"\b\d{5}(?:-\d{4})?\b")


def redact_text(text: str) -> str:
    text = _EMAIL_RE.sub("[REDACTED_EMAIL]", text)
    text = _PHONE_RE.sub("[REDACTED_PHONE]", text)
    text = _ZIP_RE.sub("[REDACTED_ZIP]", text)
    return text
