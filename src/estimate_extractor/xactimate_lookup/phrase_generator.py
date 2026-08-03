"""Deterministic, explainable, configurable search-phrase generation.

Builds a phrase from fixed ordered buckets -- component, material, size,
style/grade, action -- rather than lightly editing the original sentence.
See config/xactimate_lookup_phrase_rules.yaml for the full rationale and
docs/xactimate-lookup.md for worked examples. Every kept/dropped term is
recorded on the returned PhraseResult so the choice is always auditable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from estimate_extractor.xactimate_lookup.models import PhraseResult

DEFAULT_PHRASE_RULES_PATH = Path(__file__).resolve().parents[3] / "config" / "xactimate_lookup_phrase_rules.yaml"

# Numeric/dimension patterns -- kept in code rather than YAML for regex
# reliability; documented in the YAML file for reviewers. Order matters:
# more specific ("up to N", ranges) before the generic "N <unit>" catch-all.
# `_NUM` accepts a plain integer, a decimal ("1.5"), or a fraction ("1/2")
# -- fractional dimensions are common and real (e.g. "1/2\" drywall",
# confirmed against real Phase 3.6 catalog entries like '1/2° drywall -
# hung, taped...').
_NUM = r"\d+(?:[./]\d+)?"
_SIZE_PATTERNS = [
    re.compile(rf"\bup to {_NUM}\b"),
    re.compile(rf"\b{_NUM}\s*(?:-|to)\s*{_NUM}\b"),
    # No trailing \b here: a quote/apostrophe unit symbol is non-word, so
    # \b would require a word character immediately after it -- which
    # fails at end-of-string or before another symbol (e.g. '1/2"' at the
    # end of a description), silently dropping exactly the fraction-inch
    # sizes this pattern exists to catch.
    re.compile(rf"\b{_NUM}\s*(?:in(?:ch(?:es)?)?\b|ft\b|feet\b|foot\b|sf\b|sq\.?\s*ft\b|lf\b|sq\b|sy\b|\"|')"),
]
_TRAILING_UNIT_WORD = re.compile(r"\s*(?:in(?:ch(?:es)?)?|ft|feet|foot|sf|sq\.?\s*ft|lf|sq|sy|\"|')\s*$")
_RANGE_SPACING = re.compile(r"\s*(-|to)\s*")


@dataclass(frozen=True, slots=True)
class PhraseRules:
    filler_phrases: tuple[str, ...]
    component_keywords: tuple[tuple[str, str], ...]
    material_keywords: tuple[tuple[str, str], ...]
    style_keywords: tuple[tuple[str, str], ...]
    leading_style_keywords: tuple[tuple[str, str], ...]
    modifier_keywords: tuple[tuple[str, str], ...]
    action_search_terms: dict[str, str | None]
    max_phrase_terms: int


def load_phrase_rules(path: Path = DEFAULT_PHRASE_RULES_PATH) -> PhraseRules:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return PhraseRules(
        filler_phrases=tuple(p.lower() for p in (raw.get("filler_phrases") or [])),
        component_keywords=tuple((k.lower(), v) for k, v in (raw.get("component_keywords") or [])),
        material_keywords=tuple((k.lower(), v) for k, v in (raw.get("material_keywords") or [])),
        style_keywords=tuple((k.lower(), v) for k, v in (raw.get("style_keywords") or [])),
        leading_style_keywords=tuple((k.lower(), v) for k, v in (raw.get("leading_style_keywords") or [])),
        modifier_keywords=tuple((k.lower(), v) for k, v in (raw.get("modifier_keywords") or [])),
        action_search_terms=dict(raw.get("action_search_terms") or {}),
        max_phrase_terms=int(raw.get("max_phrase_terms", 6)),
    )


def _strip_filler_phrases(text: str, filler_phrases: tuple[str, ...]) -> str:
    for phrase in filler_phrases:
        text = text.replace(phrase, " ")
    return text


def extract_size_term(text: str) -> str | None:
    """Returns a canonical size term (unit word dropped, numeric/qualifier
    kept) or None. Always runs against the raw description text -- size
    information is rarely present in Phase 2's normalized fields."""
    for index, pattern in enumerate(_SIZE_PATTERNS):
        match = pattern.search(text)
        if not match:
            continue
        term = _TRAILING_UNIT_WORD.sub("", match.group(0)).strip()
        if index == 1:
            # Only the "N to/- M" range pattern gets its separator
            # canonicalized to "1-9" -- not the "up to N" pattern, whose
            # "to" is part of the phrase, not a range separator (see
            # _dedupe_preserve_order, which deliberately preserves
            # digit-digit hyphens intact).
            term = _RANGE_SPACING.sub("-", term)
        return term
    return None


def _word_boundary_search(keyword: str, text: str) -> bool:
    # Word-boundary match, not plain substring -- otherwise a material
    # keyword like "laminate" would incorrectly fire inside "laminated"
    # (a distinct, far more common roofing STYLE word; see
    # docs/xactimate-lookup.md "Phrase-generator word-boundary fix").
    return re.search(r"\b" + re.escape(keyword) + r"\b", text) is not None


def extract_style_term(text: str, style_keywords: tuple[tuple[str, str], ...]) -> str | None:
    for keyword, canonical in style_keywords:
        if _word_boundary_search(keyword, text):
            return canonical
    return None


def _resolve_bucket_term(text: str, keywords: tuple[tuple[str, str], ...], normalized_value: str | None) -> str | None:
    for keyword, canonical in keywords:
        if _word_boundary_search(keyword, text):
            return canonical
    if normalized_value and normalized_value != "unknown":
        normalized_text = normalized_value.lower().replace("_", " ")
        for keyword, canonical in keywords:
            if _word_boundary_search(keyword, normalized_text):
                return canonical
        return normalized_text
    return None


# A hyphen NOT flanked by digits on both sides is a word separator for
# dedup purposes (so a hyphenated remnant like "3-tab" is recognized as
# the same words as a space-separated "3 tab" from another bucket,
# instead of appearing twice under two different spellings). A hyphen
# BETWEEN two digits (a genuine numeric range like "1-9") is preserved
# intact -- splitting it would turn a meaningful range into two
# unrelated-looking numbers.
_WORD_HYPHEN = re.compile(r"(?<!\d)-|-(?!\d)")


def _dedupe_preserve_order(terms: list[str]) -> list[str]:
    seen_words: set[str] = set()
    result: list[str] = []
    for term in terms:
        normalized = _WORD_HYPHEN.sub(" ", term)
        words = [w for w in normalized.split() if w and w not in seen_words]
        if not words:
            continue
        seen_words.update(words)
        result.append(" ".join(words))
    return result


def generate_search_phrase(
    original_description: str,
    normalized_component: str | None,
    normalized_material: str | None,
    normalized_action: str | None,
    rules: PhraseRules,
) -> PhraseResult:
    if not original_description:
        return PhraseResult(phrase="", terms=[], reasons=[], dropped=[])

    text = original_description.lower()
    text = _strip_filler_phrases(text, rules.filler_phrases)

    leading_style_term = extract_style_term(text, rules.leading_style_keywords)
    modifier_term = extract_style_term(text, rules.modifier_keywords)
    component_term = _resolve_bucket_term(text, rules.component_keywords, normalized_component)
    material_term = _resolve_bucket_term(text, rules.material_keywords, normalized_material)
    size_term = extract_size_term(text)
    style_term = extract_style_term(text, rules.style_keywords)
    action_term = rules.action_search_terms.get(normalized_action or "") if normalized_action else None

    reasons: list[str] = []
    dropped: list[str] = []
    # Type-qualifying style words (e.g. "laminated", "3-tab") and
    # modifiers (e.g. "wet") lead a real Xactimate description
    # ("Laminated - comp. shingle rfg.") -- confirmed against the real
    # Phase 3.6 catalog; see docs/xactimate-lookup.md "Phrase-generator
    # ordering fix". Grade words (standard/high/premium grade) trail, as
    # observed in real entries like "Carpet - High grade".
    ordered: list[tuple[str, str | None]] = [
        ("leading_style", leading_style_term),
        ("modifier", modifier_term),
        ("component", component_term),
        ("material", material_term),
        ("size", size_term),
        ("grade", style_term),
        ("action", action_term),
    ]
    terms: list[str] = []
    for bucket, term in ordered:
        if term:
            terms.append(term)
            reasons.append(f"{bucket}: kept {term!r}")
        else:
            dropped.append(bucket)

    terms = _dedupe_preserve_order(terms)[: rules.max_phrase_terms]

    if not terms:
        # No bucket produced anything (component/material unrecognized,
        # no size/style/action wording) -- fall back to a generic
        # stopword-stripped version of the description rather than
        # handing the orchestrator an empty search phrase.
        terms = _fallback_terms(text, rules.max_phrase_terms)
        if terms:
            reasons.append("fallback: no structured terms found, used cleaned description")

    phrase = " ".join(terms)
    return PhraseResult(phrase=phrase, terms=terms, reasons=reasons, dropped=dropped)


_GENERIC_STOPWORDS = frozenset(
    {"the", "a", "an", "and", "or", "of", "for", "with", "per", "only", "no", "including", "incl", "-", "&"}
)
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _fallback_terms(text: str, max_terms: int) -> list[str]:
    tokens = [t for t in _TOKEN_RE.findall(text) if t not in _GENERIC_STOPWORDS and len(t) > 1]
    seen: set[str] = set()
    result: list[str] = []
    for token in tokens:
        if token in seen:
            continue
        seen.add(token)
        result.append(token)
        if len(result) >= max_terms:
            break
    return result
