# Selector recommendation layer (Phase 3.7)

## Purpose and non-goals

Phase 3.6 built a searchable canonical Xactimate selector database
(`master_selectors.db`, 13,318 records, 10,088 eligible). By itself, that
database is a reference/search tool -- it has no idea which line item on
a given claim a selector belongs to.

Phase 3.7 adds a **recommendation layer** on top of it: for each Phase 2
normalized line item, it proposes ranked candidate Xactimate CAT/SEL
pairs with an explainable score, and hands them to a human reviewer.

**Core rule, unchanged from the build spec:** the selector database
provides possible candidates, never verified mappings. A database result
never becomes approved merely because it ranks first. Only a
human-approved selector may become a verified mapping or automation-ready
output. Nothing in this phase builds Xactimate desktop automation.

## Architecture

```
src/estimate_extractor/selector_recommendation/
    models.py               RecommendationInput, Candidate, RecommendationResult
    query.py                eligible-only-by-default reads from master_selectors.db
    candidate_generation.py trade/component category hints -> keyword pre-filter (bounded pool)
    compatibility.py        action/unit/grade/distinction signals (soft, never a hard reject)
    scoring.py               weighted deterministic score + hard conflict caps
    ranker.py                state classification + verified/selector/placeholder priority merge
    explanations.py          turns signals into match_reasons / penalties strings
    service.py                orchestration, apply/reject/save-rule, feedback events, evaluation
```

This is a separate package from `mapping/` (the Phase 2 deterministic
mapper) -- it consumes Phase 2's *output* (`normalized_estimate.json`,
`mapped_estimate.json`, and `review_service.build_effective_rows()`), it
never rereads a PDF and never modifies the Phase 2 mapper's behavior.

## Candidate generation (staged, bounded)

For each item:

1. **Category hints.** `config/selector_recommendation_rules.yaml` maps a
   normalized trade (and, additively, a normalized component) to one or
   more likely Xactimate categories. When a hint is available, only those
   categories are queried from the database.
2. **Fallback.** If no hint applies, the full eligible pool (needs_review
   = 0) is loaded once per recommendation run and cheaply token-filtered
   -- never full-table fuzzy-scored per item.
3. **Keyword/token pre-filter.** Both paths are additionally capped by a
   shared-token overlap ranking before scoring ever runs, per
   `candidate_limits` in `config/selector_recommendation_scoring.yaml`.
4. **Scoring** (see below) runs only on this bounded pool.

### Category hint calibration

`trade_category_hints` was **not** guessed from category-code naming
conventions. Every entry was checked by directly sampling real
description text per category from the real, 13,318-record
`master_selectors.db` (see the investigation captured in
`config/selector_recommendation_rules.yaml`'s header comment). This
caught a real, already-known Phase 3.6 limitation in the process: several
categories contain a genuine, uncorroborated minority of cross-filed rows
(e.g. some `DOR`-folder rows are actually electrical wiring, with
`title_bar_category="ELE"` but `category_mismatch=False` because a lone
screenshot's title-bar disagreement never clears Phase 3.6's
corroboration bar -- see `docs/selector-catalog.md` "QA cleanup pass").
A category was only added to a trade's hint list when the **majority** of
its sampled rows matched that trade. Trades with no confident majority
match (e.g. fencing) are deliberately left unmapped -- the fallback,
unconstrained-but-bounded search still finds real candidates for them, it
is simply not the fast path.

## Compatibility and scoring

`compatibility.py` computes soft signals -- category-hint membership,
action wording match/conflict/none/unlabeled, a textual unit signal, a
grade-word conflict, and "important distinction" conflicts (remove vs.
replace vs. detach-and-reset, labor vs. material, differing material
types) from a configurable list of mutually exclusive keyword groups.
None of these reject a candidate outright.

`scoring.py` combines eight weighted components (description similarity,
category compatibility, component similarity, material match, action
match, attribute match, verified-rule evidence, unit compatibility) from
`config/selector_recommendation_scoring.yaml`, then applies **hard caps**
-- a real category/action/unit/distinction conflict ceilings the final
score regardless of how high the raw weighted total is, mirroring
`mapping/scorer.py`'s existing conflict-override pattern. Every
contributing signal and every cap is turned into a `match_reasons` /
`penalties` string by `explanations.py` -- nothing is opaque.

Missing context (no component, no material, no unit) reduces confidence
via neutral defaults; it never crashes the recommender and never counts
as a conflict.

## Unit handling

`SelectorRecord` (Phase 3.6) intentionally has no `unit` field. Unit
compatibility here is a **soft textual signal only**: if the source
item's unit has a recognizable textual footprint (e.g. "per SF", "per
hour") and the candidate description contains a *different* recognizable
unit's footprint, that's a soft conflict (capped, not rejected). No
signal at all (the common case) is neutral, never a penalty. If the
Phase 3.5 verified catalog has an exact verified unit for a candidate,
that verified data outranks this inference entirely (see below).

## Verified-catalog / placeholder priority

Ranking priority, exactly as specified:

1. an existing approved item-level human override (the item is marked
   `locked_by_approval`; recommendations still display for visibility,
   but applying a *different* candidate requires an explicit
   `allow_override_approved=True` call -- never silent)
2. an existing Phase 3.5 human-verified reusable catalog rule (always
   ranked first among candidates, score fixed at 1.0 -- it is
   authoritative evidence, not a heuristic estimate)
3. eligible Phase 3.6 selector-catalog recommendations (this module's
   scored/ranked output)
4. needs-review Phase 3.6 selector references (only present at all when
   the reviewer explicitly enables "Include uncertain selector
   references"; always carries `source_needs_review=True` and a
   quality-penalty explanation)
5. the Phase 2 placeholder catalog suggestion (`mapped_estimate.json`'s
   original `best_match`, when it has both a category and a selector) --
   always ranked last

`ranker.merge_and_rank()` deduplicates by (category, selector) so a
verified rule that happens to also appear in the selector-catalog pool is
shown once, at its verified rank.

## Safety gates (unchanged protections)

- `query.py` defaults every read to `needs_review = 0`; `include_uncertain`
  is the one explicit, always-visibly-labeled way to see more.
- `service.apply_candidate()` writes **only** through
  `review_service.edit_mapping_field()` / `approve_item()` -- the same
  audited path the rest of the review UI uses. It requires a non-empty
  reason and raises `RecommendationApplyBlockedError` (never silently
  overwrites) when the target item is already approved with a different
  category/selector, unless the caller explicitly passes
  `allow_override_approved=True`.
- `service.save_recommendation_as_verified_rule()` delegates entirely to
  `verified_catalog_service.add_record()` / `apply_verified_match()` --
  the same three required confirmations, the same backup-before-write,
  the same audit trail. No new verification bypass is introduced.
- Automation readiness is untouched: `verified_catalog_service.
  is_automation_ready()` still requires a `human_verified` catalog record
  or an item-only verification. Applying (even approving) an eligible
  Phase 3.6 candidate does **not**, by itself, satisfy that gate -- see
  the integration test
  `test_applying_eligible_candidate_alone_does_not_grant_automation_readiness`.
- Every reviewer action (accept / reject / mark irrelevant / choose
  another selector / save a reusable rule) is recorded as an audited,
  append-only event in `projects/<project>/review/
  selector_recommendation_events.json`. Nothing here retrains or reweights
  anything automatically from a single decision.

## CLI

```
python -m estimate_extractor selectors recommend projects/<project>/mapping/mapped_estimate.json [--top N] [--include-uncertain]
python -m estimate_extractor selectors evaluate [--projects-dir DIR]
python -m estimate_extractor selectors recommendation-stats [PROJECT_DIR]
python -m estimate_extractor selectors search <query> [--category CAT] [--fuzzy] [--include-uncertain]
```

`selectors recommend` prints ranked candidates per item and (by default)
writes `projects/<project>/review/selector_recommendations.json`.
`selectors search` (the existing Phase 3.6 manual browser) now defaults
to eligible-only with an `--include-uncertain` toggle, matching the
recommendation layer's own default.

## UI

The Mapping Review tab's single-item editor gained a "Recommended
Xactimate Selectors" section (Apply / Apply & approve / Reject / Mark
irrelevant / Open source screenshot / Save as reusable rule, each
candidate labeled VERIFIED / CATALOG / UNCERTAIN / PLACEHOLDER) and a
manual selector-catalog search widget that feeds into the same audited
apply path. Verified end-to-end with Streamlit's `AppTest` harness
(headless script execution + simulated widget clicks against a disposable
copy of a real project) -- confirmed no exceptions and that a clicked
"Apply" candidate is correctly persisted through `review_service` and
recorded as a feedback event.

## Evaluation ground truth

`selectors evaluate` distinguishes real human-verified ground truth from
synthetic test-only approvals. Every local project's current `approved`
entries in this repository are stamped `reviewer: "benchmark-run"` with
notes like "simulated verification" / "benchmark approval" -- artifacts
of an earlier phase's benchmark run, not real reviewer decisions.
`service._is_synthetic_item_state()` excludes anything matching those
markers from ground truth, so `top1_agreement` / `top3_agreement` report
`N/A` for every fixture today, per the build spec's explicit instruction
not to fabricate accuracy from synthetic approvals. The mechanism is
real and will start reporting real numbers the moment a project has a
genuine (non-benchmark-run) human approval.

## Known limitations

- `verified_rule_recall` is computed by independently re-checking
  `verified_catalog_service.find_verified_matches()` against each item's
  row and comparing to what the recommender actually surfaced -- with an
  empty verified catalog (this repository's current state) it reports
  `N/A`, not 0, since there is nothing to recall.
- The unit-compatibility signal is text-pattern-based (Phase 3.6 stores
  no unit field); it will miss unit information not literally spelled out
  in a candidate's description.
- `trade_category_hints` covers the trades with confident real-data
  evidence in this reference library; `fencing` has none and falls back
  to the slower unconstrained search path.
- Latency (~30-40ms/item observed against the real 13,318-record catalog)
  scales with pool size before the keyword pre-filter; very large
  estimates would batch-load the pool once per project run (already the
  case in `recommend_for_project`) rather than per item.
