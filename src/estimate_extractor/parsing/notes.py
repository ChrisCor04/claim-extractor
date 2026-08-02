"""Narrative-note classification and attachment.

Notes are the free-text lines that trail a line item (ITEL/Material Metrix
pricing explanations, waste calculations, roof options, building-code
citations, "Rake Only", etc.). They are attached to whichever line item's
row they were consumed after by the row assembler in ``line_items.py``;
this module only classifies note *type* and flags an obvious attachment
ambiguity for the validation layer.
"""

from __future__ import annotations

from estimate_extractor.normalization.text import looks_like_code_upgrade_note

_TYPE_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("pricing_note", ("itel", "material metrix", "priced by", "material supplier", "supply warehouse", "allowance of $")),
    ("code_note", ("building code", "r903.", "r905.", "code r9", "per code")),
    ("waste_note", ("auto calculated waste", "options:", "bundle rounding")),
    ("measurement_note", ("accounted for", "allotted to", "rake only")),
]


def classify_note_type(text: str) -> str:
    t = text.lower()
    if looks_like_code_upgrade_note(text):
        return "code_upgrade_note"
    for note_type, keywords in _TYPE_KEYWORDS:
        if any(k in t for k in keywords):
            return note_type
    return "general_note"


# Lexical lead-ins that mark a short line as a genuine narrative note even
# though its length alone would otherwise make it look like a bare
# category/section label (e.g. "Windows", "Fence"). Used by the row
# assembler to decide whether a short trailing line should stay attached to
# the current line item as a note or be handed back to the state machine as
# a possible new section/category header.
_NOTE_LEAD_INS = (
    "accounted for",
    "allotted to",
    "starter",
    "orig.",
    "component",
    "auto calculated",
    "options:",
    "this ",
    "the ",
    "note:",
    "per code",
)
_EXACT_NOTE_PHRASES = ("rake only",)


def is_note_like_phrase(text: str) -> bool:
    t = text.strip().lower()
    if t in _EXACT_NOTE_PHRASES:
        return True
    return any(t.startswith(lead_in) for lead_in in _NOTE_LEAD_INS)
