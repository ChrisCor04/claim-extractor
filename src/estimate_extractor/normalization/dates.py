"""Date normalization to ISO 8601.

Source documents use various U.S.-style formats: "6/4/2026 9:57 AM",
"4/27/2026", "10/10/2011 3:00 PM". We normalize to "YYYY-MM-DD" for
date-only values and "YYYY-MM-DDTHH:MM:SS" for values that include a time.
No timezone is assumed or attached since the source documents do not state
one explicitly.
"""

from __future__ import annotations

import re

from dateutil import parser as dateutil_parser

_HAS_TIME_RE = re.compile(r"\d{1,2}:\d{2}")


def normalize_date(raw: str | None) -> str | None:
    if raw is None:
        return None
    s = raw.strip()
    if not s or s.upper() in {"NA", "N/A"}:
        return None
    try:
        dt = dateutil_parser.parse(s, fuzzy=False)
    except (ValueError, OverflowError, TypeError):
        return None

    if _HAS_TIME_RE.search(s):
        return dt.strftime("%Y-%m-%dT%H:%M:%S")
    return dt.strftime("%Y-%m-%d")
