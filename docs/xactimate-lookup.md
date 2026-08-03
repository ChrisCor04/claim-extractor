# Description-first Xactimate lookup workflow (Phase 3.8)

## Purpose and non-goals

Phases 3.6/3.7 gave ClaimXtract a searchable selector catalog and a
ranked recommendation layer, but both still assumed a reviewer already
had *some* CAT/SEL candidate to look at. Phase 3.8 removes that
assumption: for each normalized line item, it decides whether a trusted
internal mapping already exists (fast CAT/SEL path) or whether the
system needs to generate a description search phrase, rank the resulting
dropdown candidates, and ask a human to resolve it -- then remembers that
resolution for next time.

This phase does **not** build live Xactimate desktop automation. It
builds the recommendation/ranking/registry machinery and a working manual
workflow, plus a clean adapter boundary a future phase can implement
against a real desktop-automation backend.

## Investigation findings

- Phase 3.6's `SelectorRecord` has no unit field and Xactimate
  descriptions almost never contain action verbs (`remove`/`replace`/
  `R&R`) as literal text -- action is expressed via a separate Activity
  symbol (see `config/xactimate_activity_symbols.yaml`). Confirmed
  directly against the real catalog: `RFG/ARMVN` = "Tear off composition
  shingles", `PNT/FENST` = "Stain - wood fence/gate" -- action words that
  *do* appear are a small, specific set (tear off, clean, paint, stain,
  seal, prime, inspect), not the generic remove/replace/install/detach/
  reset vocabulary. This directly shaped both the search-phrase generator
  (drop compound actions by default) and the ranking model (action
  absence is neutral, not a conflict).
- Phase 3.7's `RecommendationInput` already carries every field this
  phase needs (trade/component/material/action/unit/quantity) -- reused
  directly rather than duplicated (`xactimate_lookup.models` re-exports
  it).
- Phase 3.5's verified catalog (`verified_xactimate_catalog.yaml`) is
  itself a form of "trusted mapping" (compatibility-matched, human-
  verified). It is checked as a second trusted source, after the new
  registry, before falling back to description search.
- All approvals present in the local `projects/` fixtures are synthetic
  (`reviewer: "benchmark-run"`, notes like "simulated verification") --
  the same finding Phase 3.7 already documented. `lookup stats` reuses
  Phase 3.7's `real_ground_truth()` (promoted from private to public) to
  exclude them, so top-1/top-3 report `N/A` rather than a fabricated
  number.

## Final architecture

```
src/estimate_extractor/xactimate_lookup/
    models.py            LookupPlan, LookupOutcome, DropdownResult, RankedCandidate,
                          InternalMappingRecord, PopulatedFields, decision/status constants
    signature.py          deterministic item signature (trade/component/material/size/grade/action/unit -- never price)
    phrase_generator.py   concise search-phrase generation (bucketed, configurable, explainable)
    registry.py            SQLite persistence for the internal lookup registry
    ranking.py              dropdown-candidate scoring + AUTO_SELECT/REVIEW_REQUIRED/NO_MATCH
    adapter.py               XactimateAdapter ABC + FakeXactimateAdapter (no real adapter exists yet)
    orchestrator.py          plan -> search -> capture -> rank -> decide -> (dry-run or commit)
    service.py                 CLI/UI-facing orchestration: planning, recording, disabling, stats
```

Config: `config/xactimate_lookup_phrase_rules.yaml` (filler phrases,
component/material/style keyword tables, action-term allowlist),
`config/xactimate_lookup_ranking.yaml` (weights, conflict caps,
auto-select thresholds/margin).

Storage: `config/internal_lookup_registry.db` (SQLite, same
create-on-demand / backup-before-write pattern as Phase 3.6/3.5), audit
trail at `config/internal_lookup_events.json` (this registry is
cross-project data, so its audit log lives beside it in `config/`, not
inside any one project's `review/` directory).

## Lookup decision flow

```
for each normalized line item:
    signature = compute_item_signature(trade, component, material, action, unit, description)
                 -- never price or price-list identifiers

    trusted = registry.find_reusable_mapping(signature)   # exact-signature, status=approved only
           or a Phase 3.5 human-verified catalog match (compatibility-matched)

    if trusted:
        plan = search "CAT SEL" (confirms the mapping is still findable)
    else:
        phrase = generate_search_phrase(description, component, material, action)
        plan = search by phrase

    dropdowns = adapter.capture_dropdown_results()   # real or Fake
    candidates = rank(dropdowns)                      # weighted score + hard conflict caps
    decision = AUTO_SELECT | REVIEW_REQUIRED | NO_MATCH

    if decision == AUTO_SELECT and not dry_run:
        select -> read_populated_fields() -> verify match -> enter_quantity -> commit -> capture_evidence
    else:
        stop for review (see safety-stop rules)

    after human approval (record_resolution):
        apply CAT/SEL/description to the line item via review_service (audited)
        optionally save/update the registry mapping for future reuse
```

## Safety-stop rules

`orchestrator.execute_plan()` stops (never commits) when:

| Condition | `stop_reason` |
|---|---|
| Adapter can't verify the active Xactimate context | `xactimate_context_unverified` |
| No dropdown results returned | `no_results` |
| Dropdown capture raises `AdapterError` | `dropdown_extraction_failed` |
| Top candidate's score/margin/extraction-confidence insufficient | `ambiguous_candidates` |
| Top candidate has a hard conflict (wrong component/material/size/action/grade) | `hard_conflict` |
| Post-selection field read-back disagrees with the selected candidate | `populated_fields_mismatch` |
| Quantity is missing or `<= 0` | `unit_or_quantity_invalid` |

`AUTO_SELECT` additionally requires: score >= `auto_select_min` (0.88),
no hard conflict, `extraction_confidence >= min_extraction_confidence`
(0.80), **and** a `>= auto_select_margin` (0.08) lead over the runner-up
-- "Do not automatically choose the first dropdown result" is enforced by
construction (candidates are re-sorted by score, tie-broken deterministically
by row position, never assumed to already be in the right order).

`dry_run=True` (the CLI/UI default) runs the entire pipeline but never
calls `select_result` / `enter_quantity` / `commit_item`, regardless of
decision -- verified directly: `commit_item` is never invoked in any
dry-run test or the real-fixture dry-run demonstration below.

## Search phrase generation

Built from fixed ordered buckets (component, material, size, style/
grade, action) rather than lightly editing the original sentence --
deterministic, explainable (every kept/dropped term has a reason), and
fully reviewer-configurable via YAML.

```
"Remove and replace aluminum gutter/downspout up to 5 inches"
    -> "gutter aluminum up to 5"
    (component: gutter, material: aluminum, size: up to 5;
     style/action buckets empty -- compound action dropped by design)

"Tear off composition shingles - 3 tab (no haul off)"
    -> "3 tab composition shingles"      (see Phase 4.0 addendum: leading-style
                                           ordering fix changed this from
                                           "composition shingles 3 tab")

"R&R Window screen, 1 - 9 SF"
    -> "window 1-9"                     (numeric range preserved intact)

"Clean with pressure/chemical spray - siding"
    -> "siding pressure wash"           (a kept, meaningful action)

"Ice & water barrier"                   (no component/material keyword match)
    -> "ice water barrier"              (generic stopword-stripped fallback -- never empty)
```

Two real bugs were caught and fixed via direct testing against real line
items from `projects/*/mapping/normalized_estimate.json` before this was
considered done (not merely against the one worked example in the spec):

1. **Word-boundary collision**: a material keyword `"laminate"` matched
   as a plain substring inside `"laminated"` (a distinct, far more common
   roofing *style* word), producing corrupted phrases like `"shingles
   laminate laminated"`. Fixed by switching every keyword lookup
   (component/material/style) to `\bword\b` regex matching.
2. **Hyphen/dedup interaction**: naively treating every hyphen as a word
   separator (needed so `"3-tab"` and `"3 tab"` dedupe as the same term)
   also shredded genuine numeric ranges like `"1-9"` into `"1 9"`. Fixed
   by only treating a hyphen as a separator when it is *not* between two
   digits, and by canonicalizing range spacing (`"1 - 9"` / `"1 to 9"` ->
   `"1-9"`) at extraction time.

## Candidate ranking

Weighted-average scoring over **applicable** dimensions only (description
similarity, component match, material match, size match, action
compatibility, grade/style match, prior-verified-mapping bonus) -- a
dimension with no real signal to check (no size/grade wording, no prior
mapping) is excluded from the average entirely rather than diluted in at
a flat neutral score. This was a real calibration bug caught via testing:
the original design flat-averaged in neutral 0.5 defaults for every
inapplicable dimension, which capped the maximum achievable score at
~0.85 even for a textbook-perfect match (exact description + component +
material + action, no size/grade/prior-mapping signal available) --
making `auto_select_min` (0.88) structurally unreachable for the most
common real case. Fixed by renormalizing over only the dimensions that
were actually evaluated; reverified the same case then scored 1.0.

Hard conflicts (wrong component/material/size/action/grade) cap the
final score regardless of raw weighted total, mirroring
`mapping/scorer.py` and `selector_recommendation/scoring.py`'s existing
conflict-override pattern.

## Verified-catalog priority

Lookup order, exactly as specified:

1. internal registry exact-signature match (`status=approved`)
2. Phase 3.5 human-verified catalog compatibility match
3. description search -> rank -> human resolution
4. resolution saved (applied to the item + optionally the registry)
5. registry entry reused for future items with the same signature

Both trusted sources are checked before falling back to description
search; a registry hit and a verified-catalog hit are never conflated
(the verified-catalog case is represented with a `verified_catalog:`-
prefixed transient mapping_id, never persisted to the registry table).

## Manual workflow (usable today, no automation required)

Mapping Review -> select a line item -> "Xactimate Lookup" section:
shows the trusted mapping (if any) with an Apply button, or the
generated search phrase in a copyable code block with its reasoning, plus
a form to record what the reviewer found in their own Xactimate session
(CAT, SEL, description, unit, action, item number, evidence reference,
reason -- reason is mandatory and audited). "Apply this result" always
writes through `review_service` (never a direct file write); "also save
as a reusable mapping" is a separate checkbox, mirroring Phase 3.7's
Apply vs. Save-as-reusable-rule split.

Verified end-to-end with Streamlit's `AppTest` harness against a
disposable copy of a real project: filled in the form, clicked submit,
confirmed no exception, confirmed the target line item's category/
selector were updated through the real audited path, and confirmed a new
registry mapping was created.

## Files added

```
config/xactimate_lookup_phrase_rules.yaml
config/xactimate_lookup_ranking.yaml
src/estimate_extractor/xactimate_lookup/__init__.py
src/estimate_extractor/xactimate_lookup/models.py
src/estimate_extractor/xactimate_lookup/signature.py
src/estimate_extractor/xactimate_lookup/phrase_generator.py
src/estimate_extractor/xactimate_lookup/registry.py
src/estimate_extractor/xactimate_lookup/ranking.py
src/estimate_extractor/xactimate_lookup/adapter.py
src/estimate_extractor/xactimate_lookup/orchestrator.py
src/estimate_extractor/xactimate_lookup/service.py
src/estimate_extractor/ui/components/xactimate_lookup_panel.py
tests/unit/xactimate_lookup/ (conftest + 8 test files)
tests/integration/test_xactimate_lookup.py
docs/xactimate-lookup.md
```

## Files modified

- `src/estimate_extractor/cli.py` -- new `lookup` command group
  (`phrase`, `plan`, `record`, `list-mappings`, `disable`, `stats`,
  `dry-run`); `selectors search` gained the same eligible-only-default +
  `--include-uncertain` pattern already used elsewhere.
- `src/estimate_extractor/selector_recommendation/service.py` -- renamed
  `_real_ground_truth` to public `real_ground_truth` so this phase could
  reuse it without reaching into a private function (no behavior change).
- `src/estimate_extractor/ui/components/mapping_table.py` -- renders the
  new lookup panel below the Phase 3.7 recommendation panel.

## Known limitations

- No real `XactimateAdapter` implementation exists yet -- only
  `FakeXactimateAdapter`. Live desktop execution requires a future phase
  to implement the ABC against a real automation backend.
- Unit compatibility in ranking is a soft textual signal (Phase 3.6
  stores no structured unit); `DropdownResult` likewise carries no
  structured unit today.
- `review-required rate` / `no-match rate` / `description searches
  resolved` require dropdown data from an actual (or scripted) source --
  `lookup stats` reports them as `N/A` rather than fabricate them from
  planning alone; `lookup dry-run` (or the benchmark below) is how to get
  real numbers.

---

## Phase 4.0 addendum: adapter refinement, safety hardening, automation CLI

Phase 4.0 extended this same package in place -- no parallel system was
built. Every requirement in that build spec (registry fields, phrase
generation, ranking, lookup priority, adapter boundary, dry-run,
learning) was already implemented by Phase 3.8; the genuine gaps were an
adapter vocabulary mismatch, two unimplemented safety checks, a registry
schema gap, and richer CLI/UI presentation.

### Adapter interface refinement

`XactimateAdapter` methods were renamed/split to the more granular
vocabulary this phase specified, and a capability guard + recovery hook
were added:

| Before (3.8) | After (4.0) |
|---|---|
| `verify_active_context()` | `verify_application()` + `verify_project()` (separately checkable) |
| `focus_search_box()` / `clear_search_box()` | `focus_search()` / `clear_search()` |
| `enter_text(text)` | `search_by_description(phrase)` / `search_by_category_selector(cat, sel)` |
| `wait_for_dropdown_results()` + `capture_dropdown_results()` | `capture_dropdown()` (raw) + `parse_dropdown(raw)` (structured) |
| `select_result(result)` | `select_candidate(candidate)` |
| *(none)* | `recover()` -- best-effort return to a safe state after any stop/error |
| *(none)* | `supports_live_execution: bool` class attribute, default `False` |

`FakeXactimateAdapter.supports_live_execution` stays `False`; a real
adapter must explicitly set it `True`. `orchestrator.execute_plan()`
refuses `dry_run=False` against any adapter that doesn't declare it --
verified by a dedicated test that asserts the adapter is never even
touched (`adapter.log.calls == []`) before the refusal.

### New safety checks (real gaps, not just naming)

Two were genuinely unimplemented, found by reading `orchestrator.py`
directly rather than assuming: **`populated.unit` was read from the
adapter but never compared to the source item's unit.** Fixed --
`STOP_REASON_UNIT_MISMATCH` fires when both are present and disagree
(case-insensitive), and is skipped entirely when either side has no unit
signal (never a false block). Also added: `STOP_REASON_UNSUPPORTED_ADAPTER`
(see above) and `STOP_REASON_UNEXPECTED_DIALOG` (a new
`UnexpectedDialogError` adapter exception, always a hard stop, always
followed by `adapter.recover()`).

### Registry schema additions

Two columns added to `internal_mappings` (additive, no migration needed
-- no production data exists yet): `normalized_description` (a readable
rendering of trade/component/material/action, computed by
`signature.compute_normalized_description()`, distinct from both the
verbatim `source_description` and the bucketed `search_phrase`) and
`xactimate_activity_raw` (the raw observed Activity-column symbol/text,
copied verbatim like Phase 3.5's `activity_raw`/`activity_interpretation`
split -- never inferred, distinct from Phase 2's semantic `action`).

### Search-phrase generator: three more real bugs found via the spec's own new examples

Testing this phase's two new worked examples (not just the one already
validated in Phase 3.8) found three additional real bugs:

1. **`"Remove wet drywall 1/2\""` produced `"drywall"`** -- missing both
   the modifier "wet" and the size "1/2". Fixed by adding a
   `modifier_keywords` bucket (seeded with "wet", confirmed real and
   recurring in the WTR category: *"Remove wet ceiling tile & drywall and
   bag - Cat 3"*) and fixing the size regex to accept fractions
   (`\d+(?:[./]\d+)?`, not just integers/decimals) -- confirmed real:
   *"1/2° drywall - hung, taped..."*.
2. **The fraction-size fix immediately hit a second bug**: the unit-symbol
   size pattern's trailing `\b` could never match after a quote/apostrophe
   character (a non-word char followed by end-of-string is never a word
   boundary), silently dropping every symbol-unit size at the end of a
   description. Fixed by moving `\b` onto only the word-based unit
   alternatives.
3. **`"Replace laminated composition shingles"` produced `"composition
   shingles laminated"`**, not the expected `"laminated composition
   shingles"`. Investigated against the real Phase 3.6 catalog: type-
   qualifying style words genuinely **lead** real descriptions
   (*"Laminated - comp. shingle rfg."*, *"3 tab - 25 yr. - comp. shingle
   roofing"*), while grade words genuinely **trail** (*"Carpet - High
   grade"*, *"Tile floor covering - Premium grade"*). Fixed by splitting
   `style_keywords` into `leading_style_keywords` (3-tab, laminated --
   emitted before component) and `style_keywords` (grade -- emitted in
   its existing trailing position).
4. **Fixing #3 silently broke signature identity**: `signature.
   compute_grade_key()` only checked the (now grade-only) trailing list,
   so a "3-tab" item and a "laminated" item -- genuinely different
   products -- collapsed onto the same `grade_key` and therefore the same
   item signature. Caught by re-deriving both signatures immediately after
   the split and finding them equal. Fixed by having `compute_grade_key()`
   check both lists; a dedicated regression test (`test_signature_
   distinguishes_leading_style_words`) now guards this permanently.

All four fixes were re-verified against the full real 218-item corpus
(all 7 local projects) after landing: zero empty phrases, zero duplicate
words, both this phase's new worked examples and Phase 3.8's original
five examples all still exact-match.

### CLI additions

```
python -m estimate_extractor lookup registry [--active-only/--all] [--category CAT]
python -m estimate_extractor automation plan projects/<project>/mapping/mapped_estimate.json
python -m estimate_extractor automation dry-run projects/<project>/mapping/mapped_estimate.json [--dropdown-script FILE.json]
python -m estimate_extractor automation diagnostics
```

`lookup registry` is a full-detail per-mapping browser (signature,
normalized description, raw activity, evidence, timestamps) -- `lookup
list-mappings` (Phase 3.8) stays as the terse one-line-per-mapping view;
neither duplicates the other's presentation depth. `automation plan`
never touches an adapter (pure description of what a run would do);
`automation dry-run` reuses the exact same `dry_run_for_project()`/
`orchestrator.execute_plan()` machinery as `lookup dry-run`, just with
per-candidate score/reason detail in the output. `automation
diagnostics` is read-only and never attempts to launch or connect to
Xactimate -- with no real adapter configured, it says so explicitly.

### UI additions

The lookup panel now always shows (regardless of trusted vs.
description-search path): a **Lookup method** badge, **Automation
readiness** signal, cached search phrase, cached CAT/SEL, and a
**Ranking explanation** (for a trusted mapping: its usage/success/
rejection counts and who approved it; for description search: an
explicit note that no live session is connected, so nothing can be
ranked automatically yet). The manual capture form gained a raw-activity-
symbol field feeding the new `xactimate_activity_raw` column. Verified
end-to-end with `AppTest` against a disposable project copy: metrics
render, the raw-activity field round-trips into the registry correctly.

### Real-fixture evaluation (Phase 4.0, 7 real local projects, 218 line items)

Static planning (`lookup stats`, fresh registry): identical to Phase
3.8's baseline -- 218 items, 0 resolved by mapping (registry starts
empty), ground truth `N/A` (all approvals synthetic, correctly excluded).
`automation diagnostics` against the default (Fake) adapter: application/
project verification both report `True` (Fake defaults), `supports_live_
execution=False`, two explicit warnings that no real adapter is
configured -- exactly the intended "nothing fabricated" posture.

### Known limitations (updated)

- Still no real `XactimateAdapter` -- the interface is now more granular
  and capability-gated, but implementing it against a live Windows
  desktop remains future work.
- `modifier_keywords` is seeded with one confirmed-real entry ("wet");
  extending it for other trades (e.g. water-damage-adjacent qualifiers in
  non-WTR categories) is a config-only change, no code required.
- `unit_mismatch` only fires when the adapter actually reports a
  populated unit; no current adapter (Fake or otherwise) reliably does
  this yet since Phase 3.6's catalog carries no structured unit -- a real
  adapter reading Xactimate's own UI would be the first genuine source.
