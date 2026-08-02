"""Text cleanup helpers shared across parsing modules."""

from __future__ import annotations

import re

_WS_RE = re.compile(r"\s+")

# Matches a line-item numbering prefix such as "1.  ", "* 1.  ", "18. ".
# Capped at 3 digits (max 999): real item counts never get remotely close
# to that, and the cap avoids matching a 4-digit year that lands alone on
# its own line when a note sentence wraps mid-date, e.g. "...on 24 Mar\n2026.
# This is an average retail price...".
ITEM_NUMBER_RE = re.compile(r"^(?P<star>\*\s*)?(?P<num>\d{1,3})\.\s+(?P<desc>\S.*)$")


def normalize_whitespace(text: str) -> str:
    return _WS_RE.sub(" ", text).strip()


def parse_item_number_line(line: str) -> tuple[bool, int, str] | None:
    """If ``line`` starts a new numbered estimate line item, return
    (has_allowance_marker, item_number, remaining_description_text).
    Otherwise return None."""
    m = ITEM_NUMBER_RE.match(line.strip())
    if not m:
        return None
    return bool(m.group("star")), int(m.group("num")), m.group("desc").strip()


def detect_verb_flags(description: str) -> dict[str, bool]:
    """Infer remove/replace/detach-reset/labor-minimum flags from the
    leading verb phrase of a line-item description. Conservative: only sets
    a flag when the description explicitly uses that Xactimate verb
    convention; does not guess for plain material/labor descriptions."""
    d = description.strip()
    dl = d.lower()
    flags = {
        "is_remove_only": False,
        "is_replace_only": False,
        "is_remove_and_replace": False,
        "is_detach_and_reset": False,
        "is_labor_minimum": False,
        "is_code_upgrade": False,
    }
    if dl.startswith("r&r ") or dl.startswith("r & r "):
        flags["is_remove_and_replace"] = True
    elif dl.startswith("detach & reset") or dl.startswith("detach and reset") or dl.startswith("d&r "):
        flags["is_detach_and_reset"] = True
    elif dl.startswith("remove ") or dl.startswith("tear off") or dl.startswith("haul "):
        flags["is_remove_only"] = True
    elif dl.startswith("install ") or dl.startswith("replace "):
        flags["is_replace_only"] = True

    if "labor minimum" in dl:
        flags["is_labor_minimum"] = True

    return flags


CODE_UPGRADE_PHRASES = (
    "required by current building code",
    "required by current building codes",
    "expands the scope of repairs",
    "code upgrade",
    "did not previously exist",
)


def looks_like_code_upgrade_note(note_text: str) -> bool:
    t = note_text.lower()
    return any(phrase in t for phrase in CODE_UPGRADE_PHRASES)
