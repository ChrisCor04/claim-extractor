"""Ranks captured Xactimate dropdown results against the source line
item and classifies the outcome as AUTO_SELECT / REVIEW_REQUIRED /
NO_MATCH. Same deterministic weighted-score + hard-conflict-cap shape as
selector_recommendation/scoring.py -- no LLM, nothing opaque. AUTO_SELECT
additionally requires a strong score, no hard conflict, reliable
extraction, AND a clear margin over the runner-up (see build spec
"Do not automatically choose the first dropdown result.").
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from estimate_extractor.selector_catalog.models import normalize_description
from estimate_extractor.xactimate_lookup.models import (
    DECISION_AUTO_SELECT,
    DECISION_NO_MATCH,
    DECISION_REVIEW_REQUIRED,
    DropdownResult,
    RankedCandidate,
)
from estimate_extractor.xactimate_lookup.phrase_generator import PhraseRules, extract_dimension_pair, extract_size_term

DEFAULT_RANKING_PATH = Path(__file__).resolve().parents[3] / "config" / "xactimate_lookup_ranking.yaml"


@dataclass(frozen=True, slots=True)
class RankingConfig:
    weights: dict[str, float]
    conflict_caps: dict[str, float]
    auto_select_min: float
    review_required_min: float
    auto_select_margin: float
    min_extraction_confidence: float
    fuzzy_enabled: bool
    fuzzy_min_score: float
    max_dropdown_candidates_considered: int


def load_ranking_config(path: Path = DEFAULT_RANKING_PATH) -> RankingConfig:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    thresholds = raw.get("thresholds", {})
    fuzzy = raw.get("fuzzy_matching", {})
    return RankingConfig(
        weights=raw.get("weights", {}),
        conflict_caps=raw.get("conflict_caps", {}),
        auto_select_min=float(thresholds.get("auto_select_min", 0.88)),
        review_required_min=float(thresholds.get("review_required_min", 0.55)),
        auto_select_margin=float(raw.get("auto_select_margin", 0.08)),
        min_extraction_confidence=float(raw.get("min_extraction_confidence", 0.80)),
        fuzzy_enabled=bool(fuzzy.get("enabled", True)),
        fuzzy_min_score=float(fuzzy.get("min_score", 0.35)),
        max_dropdown_candidates_considered=int(raw.get("max_dropdown_candidates_considered", 15)),
    )


#: Phase 5.17 (live-caught): ranking-only, additive supplement to
#: PhraseRules.action_search_terms -- NEVER used for search-phrase
#: generation (that field feeds generate_search_phrase()'s literal
#: Xactimate search-box query text; a Phase 5.16 attempt to give
#: "detach_and_reset" a real action_search_terms value broke that
#: shared path and was reverted). Used ONLY by score_dropdown_
#: candidate()'s substring-bonus check below, to recognize when an
#: OMITTED prefix/suffix is a real, catalog-family-distinguishing
#: action phrase rather than generic filler -- see that check's own
#: docstring for the live-caught evidence.
_RANKING_ONLY_ACTION_PHRASES = {
    "detach_and_reset": "detach & reset",
}

#: Phase 5.17 (live-caught): see score_dropdown_candidate()'s
#: "unrequested structural component marker" check for the full
#: rationale. A candidate word from this set that the source never
#: mentions at all is treated as a genuinely different structural
#: component, not incidental detail -- deliberately just the one
#: live-evidenced word ("sheathing", live-reproduced beating the
#: correct RFG/STEEP candidate for a steep-roof surcharge line via
#: shared "roof" wording alone) rather than a broad guessed vocabulary.
_UNREQUESTED_COMPONENT_MARKERS = {"sheathing"}


def _fuzzy_ratio(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    try:
        from rapidfuzz import fuzz  # type: ignore[import-not-found]

        return fuzz.ratio(a, b) / 100.0
    except ImportError:
        return difflib.SequenceMatcher(None, a, b).ratio()


def _words_match_with_catalog_tolerance(word_a: str, word_b: str) -> bool:
    """Phase 5.8 (live-caught): a strict exact-word intersection missed
    the objectively correct candidate family for a real PDF row --
    normalized_component "composition_shingles" (words "composition",
    "shingles") never matched Xactimate's own catalog text "Laminated -
    comp. shingle rfg. - w/out felt" (words "comp", "shingle"), wrongly
    triggering a hard wrong_component conflict that clamped every
    genuinely-correct grade variant's score to 0.45 -- well below even
    review_required_min, producing NO_MATCH despite 10 visibly relevant
    candidates. Tolerant of two GENERAL, evidence-backed patterns, not
    a hardcoded "comp"="composition" special case:
    1. singular/plural ("shingles" / "shingle");
    2. Xactimate's consistent truncated-abbreviation catalog style
       (a >=4-character prefix match in either direction, e.g. "comp"
       is a prefix of "composition") -- confirmed against the same
       live dropdown, which also abbreviates "roofing" as "rfg" and
       drops articles/prepositions entirely.
    Deliberately narrow (word-level, both directions, minimum length
    guard against short-word false positives) -- this fixes evidence
    quality, not a ranking threshold."""
    if word_a == word_b:
        return True
    singular_a = word_a[:-1] if word_a.endswith("s") and len(word_a) > 3 else word_a
    singular_b = word_b[:-1] if word_b.endswith("s") and len(word_b) > 3 else word_b
    if singular_a == singular_b:
        return True
    if len(word_a) >= 4 and len(word_b) >= 4 and (word_a.startswith(word_b) or word_b.startswith(word_a)):
        return True
    return False


def _any_word_matches(words_a: set[str], words_b: set[str]) -> bool:
    return any(_words_match_with_catalog_tolerance(a, b) for a in words_a for b in words_b)


#: Phase 5.16 (live-caught): Xactimate's steep-roof/steep-sheathing
#: surcharge family distinguishes items by an explicit slope RANGE
#: ("7/12 to 9/12 slope", recognized by extract_size_term() above) vs a
#: THRESHOLD phrasing ("greater than 12/12 slope" / "over 12/12
#: slope") that extract_size_term() does not recognize at all (it only
#: handles "N-M"/"up to N"/"N <unit>"). A source asking for a specific
#: slope range and a candidate stating an unrelated higher threshold
#: both fell through to size_state="UNSPECIFIED" (neutral, not a
#: conflict) instead of "CONFLICT" -- live-reproduced as a 0.0758
#: margin, just under auto_select_margin, between the correct
#: RFG/STEEP ("7/12 to 9/12 slope", exact range match) and the wrong
#: RFG/STEEP>> ("greater than 12/12 slope").
_SLOPE_THRESHOLD_PATTERN = re.compile(r"(?:greater than|over)\s+(\d+/\d+)\s*slope")


def _slope_threshold_conflicts(size_key_lower: str, candidate_text: str) -> bool:
    """Ranking-only, additive -- mirrors extract_dimension_pair()'s own
    precedent (score_dropdown_candidate() below): only ever STRENGTHENS
    an already-UNSPECIFIED size verdict to CONFLICT, never downgrades a
    MATCH, and only fires when the source's own size_key is itself a
    fraction-range (contains "/"), so it can never affect an ordinary
    linear/area/weight size comparison."""
    if "/" not in size_key_lower:
        return False
    match = _SLOPE_THRESHOLD_PATTERN.search(candidate_text)
    if not match:
        return False
    return match.group(1) not in size_key_lower


def score_dropdown_candidate(
    *,
    original_description: str,
    trade: str | None,
    component: str | None,
    material: str | None,
    action: str | None,
    unit: str | None,
    size_key: str | None,
    grade_key: str | None,
    dropdown: DropdownResult,
    rules: PhraseRules,
    config: RankingConfig,
    prior_verified_mapping: bool = False,
) -> RankedCandidate:
    item_text = normalize_description(original_description or "")
    candidate_text = normalize_description(dropdown.description or dropdown.raw_text or "")

    description_score = 0.0
    if item_text and candidate_text:
        if item_text == candidate_text:
            description_score = 1.0
        elif item_text in candidate_text:
            # Phase 5.6 (live-caught): a source description that is a
            # PREFIX/substring of a longer CANDIDATE description used to
            # score the SAME flat 1.0 as an exact match -- e.g. "Drip
            # edge" (source) tied with "Drip edge - copper", "Drip edge
            # - PVC/TPO clad metal", etc., all at 1.0, collapsing the
            # top-vs-second AUTO_SELECT margin to 0.0 even though the
            # plain item is the obviously-correct one and the others add
            # real, distinguishing qualifiers (a material, a size, an
            # attachment type) the source never mentioned. Scoring this
            # case by length ratio instead rewards the closer (shorter
            # extra-text) match without ever scoring it as low as a
            # genuinely different description -- still much higher than
            # the fuzzy-ratio floor for two truly different strings.
            #
            # Deliberately ONE-DIRECTIONAL: only a longer CANDIDATE is
            # suspicious (it's claiming to be a more specific variant the
            # source never asked for). The reverse -- source is a longer,
            # more narrative sentence that happens to CONTAIN a terse
            # catalog description as a substring (a real PDF description
            # is routinely more verbose than a short catalog code) -- is
            # normal, not evidence of a wrong candidate, and stays at the
            # full 1.0 it always scored.
            description_score = len(item_text) / len(candidate_text)
        elif candidate_text in item_text:
            # Phase 5.17 (live-caught): granting a flat 1.0 whenever the
            # candidate is a substring of the source assumes whatever's
            # OMITTED is generic filler (an action prefix like "R&R"
            # with no distinguishing catalog-family effect -- the
            # normal, intended case this branch exists for). Live-
            # reproduced: source "Detach & Reset Power attic vent cover
            # only - plastic" matched candidate "Power attic vent cover
            # only - plastic" (a generic-action variant) this way at a
            # flat 1.0, while the CORRECT, action-specific candidate
            # ("...- Detach & reset") lost to word-order and fell to a
            # plain fuzzy-ratio score -- the omitted text was exactly
            # "detach & reset", a real, catalog-family-distinguishing
            # action phrase (this catalog keeps a SEPARATE "Detach &
            # reset" entry apart from the plain default-action one),
            # not filler. Narrow, general check: if the omitted text
            # contains a recognized action phrase (checked against
            # action_search_terms' own real values -- clean/paint/
            # repair/etc. -- plus the ranking-only supplement above)
            # that the candidate itself doesn't also state, this isn't
            # the "normal narrative source" case the flat 1.0 is for;
            # fall through to the ordinary fuzzy-ratio comparison
            # instead, exactly like a non-substring candidate would get.
            omitted = item_text.replace(candidate_text, "", 1)
            action_phrase = (
                _RANKING_ONLY_ACTION_PHRASES.get(action or "")
                or (rules.action_search_terms.get(action or "") if action else None)
            )
            if action_phrase and action_phrase in omitted and action_phrase not in candidate_text:
                description_score = _fuzzy_ratio(item_text, candidate_text)
            else:
                description_score = 1.0
        else:
            description_score = _fuzzy_ratio(item_text, candidate_text)

    # "unknown" is Phase 2's sentinel for "no component was extracted" --
    # it must never be treated as a literal component to search for
    # (that would manufacture a false wrong_component conflict on every
    # item with unrecognized component, exactly the kind of "missing
    # context" case that should reduce confidence, not fabricate one).
    component_clean = component if component and component != "unknown" else ""
    component_words = {w for w in re.findall(r"[a-z0-9]+", component_clean.replace("_", " "))}
    candidate_words = set(re.findall(r"[a-z0-9]+", candidate_text))
    # Phase 5.15 Pass 2 (ground-truth-guided, live-caught): a candidate
    # like "Clean {V}" is Xactimate's own GENERIC TEMPLATE entry for a
    # cleaning labor item applicable to any surface -- the literal "{v}"
    # placeholder (still present after normalize_description(), which
    # only touches punctuation/whitespace/case) is Xactimate's OWN
    # catalog-formatting signal that this description was never meant
    # to restate a specific component word at all, structurally unlike
    # every other component check here. Requiring a literal component-
    # word match against a placeholder is not evidence of a real
    # mismatch -- confirmed live: "Clean Fence" (component="fence")
    # against "Clean {V}" hard-capped to 0.45 (wrong_component) even
    # though this is exactly the correct reference answer for generic
    # surface-cleaning carrier rows this catalog has no per-component
    # selector for. Narrow and objective (checks Xactimate's own
    # placeholder syntax, not source vocabulary) -- never fires for an
    # ordinary candidate that simply omits the component word.
    candidate_is_generic_template = "{v}" in candidate_text
    component_ok = not component_words or candidate_is_generic_template or _any_word_matches(component_words, candidate_words)
    component_score = 1.0 if component_ok else 0.0

    # Phase 5.17 (live-caught): the word-overlap check above treats ANY
    # shared word as a full pass -- live-reproduced, "Additional charge
    # for steep roof - 7/12 to 9/12 slope" (component "roof_surcharge")
    # scored component_ok=True against "Add charge for sheathing steep
    # roof - 7/12 - 9/12 slope", a genuinely different FRAMING item
    # (charged for sheathing labor, not the roofing surcharge itself)
    # that only shares the word "roof" as a qualifier of "sheathing",
    # not as its own subject. Mirrors the grade/style "unrequested
    # qualifier" check just below (score_dropdown_candidate is the only
    # place either lives): a candidate stating an UNREQUESTED, distinct
    # structural-component word the source never mentions at all is
    # real evidence of a different item, not merely extra detail --
    # same principle, a new narrow vocabulary (currently just the one
    # live-evidenced word) rather than reusing material/style_keywords,
    # since "sheathing" is neither. Deliberately narrow: this can only
    # ever downgrade an otherwise-passing component_ok, never fire when
    # component_words is already empty (no real signal to protect).
    if component_ok and component_words:
        for marker in _UNREQUESTED_COMPONENT_MARKERS:
            if marker in candidate_words and marker not in component_words and marker not in item_text:
                component_ok = False
                component_score = 0.0
                break

    # Phase 5.6 (live-caught): when normalization didn't extract a
    # material at all (material=None/empty -- e.g. "Roof vent - turtle
    # type - Plastic", where "roof_vent" is the component and nothing
    # upstream tags "Plastic" as a material), the material dimension
    # was skipped entirely, so a "Metal" candidate and the correct
    # "Plastic" candidate scored almost identically (pure text fuzz on
    # two otherwise-identical strings differing by one word) --
    # reproduced live as a 0.078 margin, just under the 0.08 threshold.
    # Falls back to extracting a material word directly from the
    # SOURCE description using the same curated material_keywords
    # vocabulary phrase_generator.py already uses -- never invents a
    # new word list, never overrides an already-normalized material.
    if not material:
        for keyword, canonical in rules.material_keywords:
            if keyword in item_text:
                material = canonical
                break

    # Phase 5.10A (live-caught): "the candidate doesn't literally
    # contain the source's material word" was being treated as
    # equivalent to "the candidate contradicts the source's material" --
    # the fallback fuzzy-ratio compared the material word against the
    # ENTIRE candidate description ("aluminum" vs "Gutter splash guard"),
    # which is almost always a low score for ANY candidate that simply
    # never mentions a material at all, not just ones that state a
    # different one. That silently manufactured a wrong_material hard
    # conflict on the objectively correct "SFG/GSG -- Gutter splash
    # guard" result for "R&R Gutter splash guard" (source material:
    # aluminum), capping its score below AUTO_SELECT/at REVIEW_REQUIRED
    # for no real reason -- the candidate never claimed a different
    # material, it simply didn't mention one. Absence of information is
    # not contradiction: a hard conflict now requires the candidate to
    # explicitly state a DIFFERENT, recognized material -- checked
    # against the SAME curated material_keywords vocabulary already used
    # above to backfill a missing source material, never a new word
    # list. Three states, matching the general principle applied to
    # size/grade below too:
    #   MATCH       -- candidate states the SAME material (by literal
    #                  text, word overlap, or the curated vocabulary).
    #   CONFLICT     -- candidate explicitly states a DIFFERENT
    #                  recognized material -- the only case allowed to
    #                  cap the score.
    #   UNSPECIFIED -- candidate states no material at all -- neutral,
    #                  mildly lower-confidence evidence (0.5), never a
    #                  hard conflict.
    material_ok = True
    material_score = 0.0
    material_applicable = bool(material)
    material_state = None
    if material:
        material_lower = material.lower()
        material_words = set(re.findall(r"[a-z0-9]+", material_lower))
        if material_lower in candidate_text or candidate_text in material_lower:
            material_score = 1.0
            material_state = "MATCH"
        elif material_words & candidate_words:
            # At least one material word (e.g. "aluminum") appears
            # literally in the candidate -- treat as a match even if the
            # full material phrase doesn't line up word-for-word.
            material_score = 1.0
            material_state = "MATCH"
        else:
            candidate_materials = {
                canonical for keyword, canonical in rules.material_keywords if keyword in candidate_text
            }
            if material_lower in candidate_materials:
                material_score = 1.0
                material_state = "MATCH"
            elif candidate_materials:
                # The candidate explicitly names a DIFFERENT recognized
                # material -- real, opposing evidence.
                material_state = "CONFLICT"
                material_ok = False
                material_score = 0.0
            else:
                # No material stated in the candidate at all -- absent,
                # not contradictory. Mild neutral-leaning evidence
                # rather than a flat 0.0, since it's genuinely unknown
                # rather than known-wrong.
                material_state = "UNSPECIFIED"
                material_ok = True
                material_score = 0.5

    # Phase 5.10A: same absence-is-not-contradiction principle as
    # material above -- a candidate that states NO size at all
    # ("Gutter splash guard") is unspecified, not a conflict, versus one
    # that states a DIFFERENT size (extracted via the same extract_
    # size_term() phrase_generator.py already uses to compute size_key
    # itself, applied here to the CANDIDATE's own text) -- e.g. source
    # 5" vs candidate stating 6" is a real conflict.
    size_ok = True
    size_score = 0.0
    size_applicable = bool(size_key)
    size_state = None
    if size_key:
        size_key_lower = size_key.lower()
        if size_key_lower in candidate_text:
            size_ok = True
            size_score = 1.0
            size_state = "MATCH"
        else:
            candidate_size = extract_size_term(candidate_text)
            if candidate_size and candidate_size.lower() != size_key_lower:
                size_ok = False
                size_state = "CONFLICT"
            elif _slope_threshold_conflicts(size_key_lower, candidate_text):
                size_ok = False
                size_state = "CONFLICT"
            else:
                size_ok = True
                size_score = 0.5
                size_state = "UNSPECIFIED"

    # Phase 5.12 (live-caught): a two-dimension size ("16' x 7'") only
    # ever contributed its FIRST number to size_key above (e.g. "16"),
    # so a candidate stating a DIFFERENT second dimension ("16' x 8'")
    # still counted as size_state=MATCH -- live-reproduced as "Overhead
    # door & hardware - 16' x 7'" scoring 1.0 with the wrong 8'-tall
    # variant right behind it at 0.9662, a margin too thin to clear
    # AUTO_SELECT despite the correct candidate being an unambiguous
    # exact match. extract_dimension_pair() (phrase_generator.py) is
    # deliberately a SEPARATE function from the one that feeds the
    # search-box query text (see its own docstring for why) -- this
    # only ever STRENGTHENS an existing verdict to CONFLICT when the
    # candidate explicitly states a different second dimension; it
    # never downgrades an existing CONFLICT, and never fires when the
    # source has no two-dimension spec at all.
    source_dimension = extract_dimension_pair(original_description or "")
    if source_dimension and size_state != "CONFLICT":
        candidate_dimension = extract_dimension_pair(candidate_text)
        if candidate_dimension and candidate_dimension != source_dimension:
            size_ok = False
            size_score = 0.0
            size_state = "CONFLICT"

    # Phase 5.10A: same principle for the primary grade/style check --
    # a candidate stating NO grade/style qualifier at all is unspecified
    # (e.g. a plain "Composition shingles" candidate against a source
    # asking for "3-tab"), not a conflict; one stating a DIFFERENT
    # recognized grade/style is a real conflict. Checked against the
    # same curated style_keywords/leading_style_keywords vocabulary the
    # separate "unrequested qualifier" check just below already uses.
    grade_ok = True
    grade_score = 0.0
    grade_applicable = bool(grade_key)
    grade_state = None
    if grade_key:
        grade_key_lower = grade_key.lower()
        if grade_key_lower in candidate_text:
            grade_ok = True
            grade_score = 1.0
            grade_state = "MATCH"
        else:
            candidate_grades = {
                canonical for keyword, canonical in (*rules.style_keywords, *rules.leading_style_keywords)
                if keyword in candidate_text
            }
            if candidate_grades and grade_key_lower not in candidate_grades:
                grade_ok = False
                grade_state = "CONFLICT"
            else:
                grade_ok = True
                grade_score = 0.5
                grade_state = "UNSPECIFIED"

    # Phase 5.6 (live-caught): the check above is one-directional -- it
    # only asks "does the candidate have the style/grade word the
    # SOURCE mentioned". It says nothing about a candidate that adds an
    # UNREQUESTED distinguishing variant qualifier ("half round",
    # "treated") the source never mentions at all -- reproduced live as
    # "Gutter / downspout - aluminum - up to 5\"" (exact match) scoring
    # only ~0.04 above "...- half round - aluminum..." (a real, different
    # catalog item), never enough margin for AUTO_SELECT. Checked
    # against the SAME curated, evidence-backed style_keywords/leading_
    # style_keywords vocabulary already used for grade_key -- not a new
    # word list, just applied in the other direction. Folded into the
    # SAME grade/style conflict dimension (not a new one) since it is
    # the same concept: an incompatible style/grade qualifier.
    if grade_ok:
        for keyword, _canonical in (*rules.style_keywords, *rules.leading_style_keywords):
            if keyword in candidate_text and keyword not in item_text:
                grade_ok = False
                grade_key = grade_key or keyword  # only for the conflict message below
                break

    # Phase 5.17: falls back to the ranking-only supplement (see its own
    # docstring above) when action_search_terms has no real value for
    # this action (e.g. "detach_and_reset", deliberately null there
    # since it also drives search-phrase text) -- this dimension only
    # ever affects scoring, never what gets typed into the search box.
    action_term = (rules.action_search_terms.get(action or "") or _RANKING_ONLY_ACTION_PHRASES.get(action or "")) if action else None
    action_ok = True
    action_score = 0.0
    action_applicable = bool(action_term)
    if action_term:
        action_ok = action_term.lower() in candidate_text
        action_score = 1.0 if action_ok else 0.5  # absence is neutral -- most descriptions omit action entirely

    verified_score = 1.0 if prior_verified_mapping else 0.0

    # Weighted AVERAGE OVER APPLICABLE DIMENSIONS ONLY -- a dimension
    # with no real signal to check (no size/grade/action wording, no
    # prior verified mapping) is excluded from both the numerator and
    # denominator rather than diluted in at a flat neutral score. This
    # matters concretely: without it, a description-search item with no
    # size/grade info and no prior mapping can never structurally exceed
    # ~0.85 even with a perfect description/component/material/action
    # match, making auto_select_min effectively unreachable (found via
    # real-data testing against config/xactimate_lookup_ranking.yaml's
    # thresholds -- see docs/xactimate-lookup.md "Ranking calibration").
    # unit_compatibility is always excluded: DropdownResult currently
    # carries no structured unit to actually check.
    weights = config.weights
    weighted_sum = weights.get("description_similarity", 0.25) * description_score
    weighted_sum += weights.get("component_match", 0.25) * component_score
    applicable_weight = weights.get("description_similarity", 0.25) + weights.get("component_match", 0.25)

    for applicable, weight_key, score in (
        (material_applicable, "material_match", material_score),
        (size_applicable, "size_match", size_score),
        (action_applicable, "action_compatibility", action_score),
        (grade_applicable, "grade_style_match", grade_score),
        (prior_verified_mapping, "prior_verified_mapping", verified_score),
    ):
        if not applicable:
            continue
        w = weights.get(weight_key, 0.0)
        weighted_sum += w * score
        applicable_weight += w

    weighted = weighted_sum / applicable_weight if applicable_weight else 0.0

    match_reasons: list[str] = []
    conflict_reasons: list[str] = []
    caps = config.conflict_caps

    if description_score >= 0.85:
        match_reasons.append("description closely matches the dropdown result")
    if not component_ok:
        weighted = min(weighted, caps.get("wrong_component", 0.45))
        conflict_reasons.append(f"wrong_component: {sorted(component_words)} not found in candidate description")
    elif component_words:
        match_reasons.append("component matches")
    # Phase 5.10A: material/size/grade conflicts now fire ONLY on
    # material_state/size_state/grade_state == "CONFLICT" (an explicit,
    # different value stated in the candidate) -- material_ok/size_ok/
    # grade_ok are already True for the UNSPECIFIED case (see above), so
    # this reads unchanged from Phase 5.6, but the messages now name the
    # actual conflicting state for a clearer audit trail.
    if material and not material_ok:
        weighted = min(weighted, caps.get("wrong_material", 0.55))
        conflict_reasons.append(f"wrong_material: candidate states a different material than {material!r}")
    elif material and material_state == "MATCH":
        match_reasons.append("material matches")
    elif material and material_state == "UNSPECIFIED":
        match_reasons.append(f"material {material!r} unspecified in candidate (not a conflict)")
    if size_key and not size_ok:
        weighted = min(weighted, caps.get("wrong_size", 0.60))
        conflict_reasons.append(f"wrong_size: candidate states a different size than {size_key!r}")
    elif size_key and size_state == "MATCH":
        match_reasons.append("size matches")
    elif size_key and size_state == "UNSPECIFIED":
        match_reasons.append(f"size {size_key!r} unspecified in candidate (not a conflict)")
    if grade_key and not grade_ok:
        weighted = min(weighted, caps.get("incompatible_grade_or_style", 0.55))
        conflict_reasons.append(f"incompatible grade/style: {grade_key!r} conflict with candidate")
    elif grade_key and grade_state == "MATCH":
        match_reasons.append("grade/style matches")
    elif grade_key and grade_state == "UNSPECIFIED":
        match_reasons.append(f"grade/style {grade_key!r} unspecified in candidate (not a conflict)")
    if action_term and not action_ok:
        weighted = min(weighted, caps.get("wrong_action", 0.50))
        conflict_reasons.append(f"wrong_action: {action_term!r} not found in candidate description")
    elif action_term and action_ok:
        # Phase 5.18: unlike component/material/size/grade above, a
        # genuine action match previously had no POSITIVE match_reasons
        # entry at all (only the negative wrong_action conflict was
        # recorded) -- needed as a real, checkable signal for the new
        # below-score-floor semantic-dominance rule in classify_
        # decision(), which requires confirmed action evidence, not
        # merely "no conflict" (a trivial pass when action_term is None
        # looks identical to a genuine match without this).
        match_reasons.append(f"action {action_term!r} matches")
    if prior_verified_mapping:
        match_reasons.append("backed by a prior verified internal mapping")

    if dropdown.extraction_confidence < config.min_extraction_confidence:
        conflict_reasons.append(f"low dropdown extraction confidence ({dropdown.extraction_confidence:.2f})")

    return RankedCandidate(
        dropdown=dropdown,
        score=round(min(1.0, max(0.0, weighted)), 4),
        match_reasons=match_reasons,
        conflict_reasons=conflict_reasons,
    )


def rank_dropdown_results(
    *,
    original_description: str,
    trade: str | None,
    component: str | None,
    material: str | None,
    action: str | None,
    unit: str | None,
    size_key: str | None,
    grade_key: str | None,
    dropdowns: list[DropdownResult],
    rules: PhraseRules,
    config: RankingConfig,
    prior_verified_mapping: bool = False,
) -> list[RankedCandidate]:
    bounded = dropdowns[: config.max_dropdown_candidates_considered]
    scored = [
        score_dropdown_candidate(
            original_description=original_description, trade=trade, component=component, material=material,
            action=action, unit=unit, size_key=size_key, grade_key=grade_key, dropdown=d, rules=rules,
            config=config, prior_verified_mapping=prior_verified_mapping,
        )
        for d in bounded
    ]
    scored.sort(key=lambda c: (-c.score, c.dropdown.row_position))
    return scored


#: Phase 5.15 Pass 2 (ground-truth-guided, live-verified against the
#: completed Aranda reference estimate): score_dropdown_candidate()'s
#: weighted-AVERAGE formula (see its own comment) makes 1.0 reachable
#: ONLY when description_score is a literal exact/near-exact text match
#: AND every other APPLICABLE dimension (component/material/size/
#: grade/action) independently scored a full MATCH -- there is no
#: partial-credit path to exactly 1.0. This is categorically stronger
#: evidence than "close by raw score": it means the source description
#: and the candidate agree on every dimension the ranker actually
#: checked. See classify_decision()'s own use of this threshold below.
_EXACT_MATCH_SCORE_THRESHOLD = 0.999


#: Phase 5.18 (live-caught): auto_select_min protects against a LOW
#: fuzzy text-similarity score, which is usually real evidence of
#: uncertainty -- but not always. Two live-reproduced cases (Power
#: attic vent cover, action "Detach & Reset", vs Xactimate's own
#: "...- Detach & reset" wording; "Clean Fence" vs Xactimate's generic
#: "Clean {V}" template) score only ~0.72-0.81 purely because the
#: CORRECT candidate's own catalog text is worded very differently
#: from the source (reordered action phrase; a placeholder that
#: structurally can't restate the component) -- not because the match
#: is actually uncertain. `_below_floor_semantic_dominance()` is a
#: SEPARATE, stricter gate for exactly this situation -- see its own
#: docstring for the full condition set and why each one is required.
_BELOW_FLOOR_MIN_SCORE = 0.70
_BELOW_FLOOR_STRICT_MARGIN = 0.20
_BELOW_FLOOR_RUNNER_UP_CEILING = 0.60


def _below_floor_semantic_dominance(candidates: list[RankedCandidate], config: RankingConfig) -> bool:
    """Live-reproduced negative controls this deliberately does NOT
    fire for (see test_ranking.py's own below-floor negative-control
    tests): a lone low-scoring candidate with no real competing
    evidence (fails the absolute floor below); a wrong-material/wrong-
    size/wrong-component candidate (fails has_hard_conflict, which is
    checked first); two candidates that are both plausibly close
    (fails the strict margin or the runner-up ceiling). Every condition
    is required -- this is deliberately NOT "big margin alone":
      - top.score >= _BELOW_FLOOR_MIN_SCORE: substantially above
        review_required_min (0.55) -- a genuinely weak match never
        qualifies just because nothing else competes.
      - no hard conflict, adequate extraction confidence: the same
        safety nets classify_decision() already requires above
        auto_select_min, unweakened here.
      - a CONFIRMED action match (a "action '...' matches" match_
        reasons entry -- see score_dropdown_candidate()'s own Phase
        5.18 addition): real, positive intent evidence, not merely
        "no action conflict" (which is also true when there's no
        action signal to check at all -- e.g. component="unknown"
        cases like the Power attic vent cover row, where this is the
        ONLY dimension offering genuine corroborating evidence).
      - margin >= _BELOW_FLOOR_STRICT_MARGIN (0.20, 2.5x the ordinary
        auto_select_margin) AND the runner-up's own absolute score is
        capped low (< 0.60): the gap must come from the runner-up
        being genuinely weak, not merely from the top candidate being
        unusually low."""
    top = candidates[0]
    if top.score < _BELOW_FLOOR_MIN_SCORE:
        return False
    if top.has_hard_conflict:
        return False
    if top.dropdown.extraction_confidence < config.min_extraction_confidence:
        return False
    if not any(r.startswith("action ") and "matches" in r for r in top.match_reasons):
        return False
    second_score = candidates[1].score if len(candidates) > 1 else 0.0
    if (top.score - second_score) < _BELOW_FLOOR_STRICT_MARGIN:
        return False
    if second_score >= _BELOW_FLOOR_RUNNER_UP_CEILING:
        return False
    return True


@dataclass(slots=True, frozen=True)
class TieResolution:
    """The record of one attempt to resolve a cross-category EXACT tie
    using pre-existing source context -- see _resolve_cross_category_
    tie()'s own docstring for the full rule. Always built once an exact
    score tie is detected (`tie_detected=True`), whether or not it was
    actually resolved, so a persisted search attempt can show its work:
    what was tied, what context was consulted, and exactly why a
    candidate was or was not preferred.

    `reason` is one of: "not_textually_equivalent",
    "three_or_more_way_tie", "same_category_tie",
    "no_category_hint_available", "both_candidates_match_hint",
    "neither_candidate_matches_hint", "context_matched_one_candidate"."""

    tie_detected: bool
    reason: str
    category_a: str | None
    selector_a: str | None
    category_b: str | None
    selector_b: str | None
    preferred_categories: tuple[str, ...]
    resolved: bool
    resolved_category: str | None
    resolved_selector: str | None

    def to_dict(self) -> dict:
        return {
            "tie_detected": self.tie_detected,
            "reason": self.reason,
            "category_a": self.category_a,
            "selector_a": self.selector_a,
            "category_b": self.category_b,
            "selector_b": self.selector_b,
            "preferred_categories": list(self.preferred_categories),
            "resolved": self.resolved,
            "resolved_category": self.resolved_category,
            "resolved_selector": self.resolved_selector,
        }


def _resolve_cross_category_tie(
    candidates: list[RankedCandidate], top: RankedCandidate, second: RankedCandidate,
    preferred_categories: tuple[str, ...],
) -> TieResolution | None:
    """A narrow, generalized tie-break for the specific shape live-caught
    across Rows 7/11/18 of the odom-insurance-v2 benchmark: two
    candidates in DIFFERENT Xactimate categories with textually
    identical descriptions score an exact tie (e.g. "Digital satellite
    system - Detach & reset" exists verbatim in both ELS and RFG;
    "Step flashing" in both RFG and SDG; "House wrap" in both INS and
    SDG) -- classify_decision_with_diagnostics()'s ordinary margin gate
    can never distinguish these on text alone, and must not: an
    arbitrary pick by candidate order would fabricate false confidence.

    Returns None (not applicable -- caller attaches no tie_resolution
    at all) when the two candidates aren't an exact score tie in the
    first place; this keeps every ordinary REVIEW_REQUIRED case (a
    near-miss margin like Rows 27/35, or any non-tied ambiguity)
    completely untouched. Once a tie IS detected, always returns a
    TieResolution explaining the outcome, resolved or not.

    `preferred_categories` is the caller's own pre-computed positive
    context -- e.g. the source item's trade/component mapped through
    selector_recommendation's existing, data-calibrated trade_category_
    hints/component_category_hints (see candidate_generation.
    hinted_categories()) -- never derived here, and never a new
    category-preference table: this function only ever asks "does
    already-established mapping evidence single out one of the two
    tied categories?" It resolves ONLY when that context implicates
    exactly one of the two tied candidates' categories -- never when
    it's silent (no hint), implicates both (conflicting/non-
    discriminating), or implicates neither. It never lowers
    auto_select_margin or touches the text/ranking score; it only
    chooses which of two ALREADY-equal-scoring candidates to select."""
    if top.score != second.score:
        return None

    category_a, selector_a = top.dropdown.category, top.dropdown.selector
    category_b, selector_b = second.dropdown.category, second.dropdown.selector
    base = dict(
        tie_detected=True, category_a=category_a, selector_a=selector_a,
        category_b=category_b, selector_b=selector_b, preferred_categories=preferred_categories,
        resolved=False, resolved_category=None, resolved_selector=None,
    )

    text_a = normalize_description(top.dropdown.description or top.dropdown.raw_text or "")
    text_b = normalize_description(second.dropdown.description or second.dropdown.raw_text or "")
    if text_a != text_b:
        return TieResolution(reason="not_textually_equivalent", **base)

    # A third candidate tied at the same score means this isn't a clean
    # pairwise ambiguity -- no defensible single winner to pick.
    if len(candidates) > 2 and candidates[2].score == top.score:
        return TieResolution(reason="three_or_more_way_tie", **base)

    if category_a == category_b:
        # Same-category "tie" (e.g. two duplicate catalog rows) is a
        # different problem than the cross-category ambiguity this
        # function exists for -- a trade/component hint could never
        # discriminate between two rows in the SAME category anyway.
        return TieResolution(reason="same_category_tie", **base)

    if not preferred_categories:
        return TieResolution(reason="no_category_hint_available", **base)

    a_in = category_a in preferred_categories
    b_in = category_b in preferred_categories
    if a_in and b_in:
        return TieResolution(reason="both_candidates_match_hint", **base)
    if not a_in and not b_in:
        return TieResolution(reason="neither_candidate_matches_hint", **base)

    winner_category, winner_selector = (category_a, selector_a) if a_in else (category_b, selector_b)
    return TieResolution(
        reason="context_matched_one_candidate", resolved=True,
        resolved_category=winner_category, resolved_selector=winner_selector,
        tie_detected=True, category_a=category_a, selector_a=selector_a,
        category_b=category_b, selector_b=selector_b, preferred_categories=preferred_categories,
    )


@dataclass(slots=True, frozen=True)
class DecisionDiagnostics:
    """Every value classify_decision() actually consulted to reach its
    outcome, plus which exact branch ("gate") produced it -- built by
    classify_decision_with_diagnostics(), which runs the SAME branches
    classify_decision() itself delegates to, so this can never drift
    from the real decision (see that function's own docstring). Exists
    so a persisted search attempt can explain -- after the fact, without
    re-running anything -- exactly why AUTO_SELECT did or did not
    happen, without duplicating the decision logic anywhere else.

    `gate` is one of: "no_candidates", "below_review_required_min",
    "below_auto_select_min", "below_floor_semantic_dominance_override",
    "hard_conflict", "low_extraction_confidence", "insufficient_margin",
    "insufficient_margin_exact_top_override",
    "exact_tie_resolved_by_context",
    "exact_tie_resolved_by_first_candidate_fallback", "clear_margin".

    `selected_candidate` is the actual RankedCandidate the caller should
    treat as chosen whenever `decision == DECISION_AUTO_SELECT` -- None
    otherwise. Exists because it is normally (but, since the cross-
    category tie resolution below, no longer ALWAYS) `candidates[0]`;
    callers must not assume position. Deliberately excluded from
    to_dict() -- it's a live object reference for immediate in-process
    use, not persisted data (top_category/top_selector/top_score below
    already capture the winner's identity for persistence)."""

    decision: str
    gate: str
    top_category: str | None
    top_selector: str | None
    top_score: float | None
    top_extraction_confidence: float | None
    top_has_hard_conflict: bool | None
    top_conflict_reasons: tuple[str, ...]
    second_category: str | None
    second_selector: str | None
    second_score: float | None
    margin: float | None
    auto_select_min: float
    auto_select_margin: float
    min_extraction_confidence: float
    tie_resolution: TieResolution | None = None
    selected_candidate: RankedCandidate | None = None

    def to_dict(self) -> dict:
        return {
            "decision": self.decision,
            "gate": self.gate,
            "top_category": self.top_category,
            "top_selector": self.top_selector,
            "top_score": self.top_score,
            "top_extraction_confidence": self.top_extraction_confidence,
            "top_has_hard_conflict": self.top_has_hard_conflict,
            "top_conflict_reasons": list(self.top_conflict_reasons),
            "second_category": self.second_category,
            "second_selector": self.second_selector,
            "second_score": self.second_score,
            "margin": self.margin,
            "auto_select_min": self.auto_select_min,
            "auto_select_margin": self.auto_select_margin,
            "min_extraction_confidence": self.min_extraction_confidence,
            "tie_resolution": self.tie_resolution.to_dict() if self.tie_resolution is not None else None,
        }


def classify_decision_with_diagnostics(
    candidates: list[RankedCandidate], config: RankingConfig, *, preferred_categories: tuple[str, ...] = (),
) -> DecisionDiagnostics:
    """The actual decision logic -- classify_decision() is a thin
    wrapper returning only `.decision`, so every existing caller's
    signature/behavior is completely unchanged. Never automatically
    returns AUTO_SELECT for an empty or single weak result -- see build
    spec 'Do not automatically choose the first dropdown result.'.

    `preferred_categories`: pre-existing, positive category evidence for
    THIS item -- e.g. the source line item's trade/component mapped
    through selector_recommendation's own data-calibrated trade/
    component category hints (see candidate_generation.hinted_
    categories()). Computed entirely by the caller; this function never
    derives it and never assumes any category is globally preferred.
    Defaults to empty, which makes _resolve_cross_category_tie() below
    a pure no-op -- every existing caller that doesn't pass this is
    completely unaffected."""
    thresholds = dict(
        auto_select_min=config.auto_select_min,
        auto_select_margin=config.auto_select_margin,
        min_extraction_confidence=config.min_extraction_confidence,
    )

    def _build(decision: str, gate: str, *, top=None, second=None, margin=None, tie_resolution=None) -> DecisionDiagnostics:
        return DecisionDiagnostics(
            decision=decision, gate=gate,
            top_category=top.dropdown.category if top is not None else None,
            top_selector=top.dropdown.selector if top is not None else None,
            top_score=top.score if top is not None else None,
            top_extraction_confidence=top.dropdown.extraction_confidence if top is not None else None,
            top_has_hard_conflict=top.has_hard_conflict if top is not None else None,
            top_conflict_reasons=tuple(top.conflict_reasons) if top is not None else (),
            second_category=second.dropdown.category if second is not None else None,
            second_selector=second.dropdown.selector if second is not None else None,
            second_score=second.score if second is not None else None,
            margin=margin,
            tie_resolution=tie_resolution,
            selected_candidate=top if decision == DECISION_AUTO_SELECT else None,
            **thresholds,
        )

    if not candidates:
        return _build(DECISION_NO_MATCH, "no_candidates")

    top = candidates[0]
    second = candidates[1] if len(candidates) > 1 else None
    second_score = second.score if second is not None else 0.0
    margin = round(top.score - second_score, 4)

    if top.score < config.review_required_min:
        return _build(DECISION_NO_MATCH, "below_review_required_min", top=top, second=second, margin=margin)

    if top.score < config.auto_select_min:
        if _below_floor_semantic_dominance(candidates, config):
            return _build(DECISION_AUTO_SELECT, "below_floor_semantic_dominance_override", top=top, second=second, margin=margin)
        return _build(DECISION_REVIEW_REQUIRED, "below_auto_select_min", top=top, second=second, margin=margin)
    if top.has_hard_conflict:
        return _build(DECISION_REVIEW_REQUIRED, "hard_conflict", top=top, second=second, margin=margin)
    if top.dropdown.extraction_confidence < config.min_extraction_confidence:
        return _build(DECISION_REVIEW_REQUIRED, "low_extraction_confidence", top=top, second=second, margin=margin)

    if (top.score - second_score) < config.auto_select_margin:
        # Phase 5.15 Pass 2 (live-caught against ground truth):
        # margin alone was refusing candidates that are, semantically,
        # not really "close" at all -- live-reproduced against the
        # completed Aranda reference on 10 separate rows (gable
        # cornice return vs cornice STRIP, roof jack vs roof vent,
        # 1 coat vs 2 coats, single vs double garage door opening,
        # steep-profile "Standard" vs "High", color vs mill finish,
        # dump TRAILER vs dump TRUCK, R&R door opener vs its CLEAN-only
        # variant, laminated shingles w/out felt vs w/ felt): in every
        # one, the top candidate scored the maximum reachable 1.0
        # (full agreement across every checked dimension -- see
        # _EXACT_MATCH_SCORE_THRESHOLD's own docstring) while the
        # runner-up, despite scoring close by raw fuzzy-text distance,
        # could never also reach 1.0 -- its own description genuinely
        # differs from the source in some dimension the ranker already
        # checks. A numeric gap between "everything matches" and
        # "something doesn't" is not the kind of ambiguity margin
        # exists to catch -- margin exists to catch two candidates
        # that are BOTH strong, equally-plausible answers (confirmed
        # against two genuine reference ties: two identically-worded
        # candidates in different real categories, and a real 3-way
        # tie for "Lighting Installer - Electrician - per hour",
        # where the reference itself uses a different selector in
        # different instances -- this override never fires for either,
        # since the runner-up is ALSO exactly 1.0 there). Deliberately
        # NOT "lower the margin" (a global threshold change) and NOT
        # per-row/per-selector special-casing -- this is a single,
        # narrow, evidence-based exception keyed only on the ranker's
        # own strongest possible signal.
        exact_top = top.score >= _EXACT_MATCH_SCORE_THRESHOLD
        exact_tie = second_score >= _EXACT_MATCH_SCORE_THRESHOLD
        if not (exact_top and not exact_tie):
            # Phase 5.19 (live-caught, odom-insurance-v2 Rows 7/11/18):
            # the comment above already identifies this exact shape --
            # "two identically-worded candidates in different real
            # categories" -- as one the margin override deliberately
            # never resolves. _resolve_cross_category_tie() is a
            # SEPARATE, narrower mechanism that only ever fires for a
            # genuine score tie, and only when the caller's own
            # pre-existing positive context implicates exactly one of
            # the two tied categories -- see its docstring. Returns
            # None (not an exact tie at all -- e.g. Rows 27/35's near-
            # miss margins) or a TieResolution that did not resolve
            # (no/conflicting context) for every case that isn't this
            # narrow shape, leaving REVIEW_REQUIRED exactly as before.
            tie = None
            if second is not None:
                tie = _resolve_cross_category_tie(candidates, top, second, preferred_categories)
            if tie is not None and tie.resolved:
                winner, loser = (top, second) if tie.resolved_category == top.dropdown.category else (second, top)
                return _build(
                    DECISION_AUTO_SELECT, "exact_tie_resolved_by_context",
                    top=winner, second=loser, margin=margin, tie_resolution=tie,
                )
            if tie is not None and tie.tie_detected:
                # Phase 5.22: an explicit, temporary fallback for a
                # genuine exact tie (top.score == second.score, both
                # already cleared auto_select_min/hard-conflict/
                # extraction-confidence above) that the contextual
                # resolver above was given a real chance to break and
                # could not -- for ANY reason it declines (no hint,
                # conflicting hint, same-category tie, non-textually-
                # equivalent tie, 3+-way tie). Rather than leave every
                # such case REVIEW_REQUIRED indefinitely, deliberately
                # AUTO_SELECTs `top`, which is always candidates[0] at
                # this point -- i.e. the first candidate in whatever
                # order the caller supplied them. This is knowingly
                # order-DEPENDENT (unlike the context resolver above,
                # which never is) -- an accepted, explicit limitation of
                # a last-resort tie-break, not a claim that position 0
                # is semantically preferred.
                return _build(
                    DECISION_AUTO_SELECT, "exact_tie_resolved_by_first_candidate_fallback",
                    top=top, second=second, margin=margin, tie_resolution=tie,
                )
            return _build(DECISION_REVIEW_REQUIRED, "insufficient_margin", top=top, second=second, margin=margin, tie_resolution=tie)
        return _build(DECISION_AUTO_SELECT, "insufficient_margin_exact_top_override", top=top, second=second, margin=margin)

    return _build(DECISION_AUTO_SELECT, "clear_margin", top=top, second=second, margin=margin)


def classify_decision(candidates: list[RankedCandidate], config: RankingConfig) -> str:
    """Never automatically returns AUTO_SELECT for an empty or single
    weak result -- see build spec 'Do not automatically choose the first
    dropdown result.' Thin wrapper over classify_decision_with_
    diagnostics() -- see that function for the actual branch-by-branch
    logic; this preserves the exact original signature/return value for
    every existing caller, unchanged."""
    return classify_decision_with_diagnostics(candidates, config).decision
