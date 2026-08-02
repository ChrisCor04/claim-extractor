# Verified Xactimate catalog builder (Phase 3.5)

An assisted workflow, layered on top of the existing extractor (Phase 1),
mapper (Phase 2), and local review UI (Phase 3), for building a *verified*
Xactimate catalog: category/selector identity a human reviewer has
personally confirmed in their own licensed Xactimate environment, entered
into ClaimXtract once and reused automatically (under strict compatibility
checks) for every future matching item.

## Why exact CAT/SEL verification is required

Phase 2's mapper can recognize a line item's trade, action, and component
from its description, but it cannot see Xactimate's actual price list --
it has no way to know, from text alone, whether "Suspended ceiling tile"
means selector `ST`, `ST-`, `ST+`, or `ST++` (standard vs. high vs.
premium grade), or whether "R&R Gutter" needs `remove_and_replace` vs. a
separate remove + install pair. Guessing would risk mapping visually
similar items to the wrong component, grade, activity, or unit --
silently wrong automation input is worse than no automation input.

The alternative extreme -- scraping or reverse-engineering Xactimate's
full catalog -- is out of scope, and this repository has no license to
redistribute Xactimate's proprietary price-list data. **This module does
neither.** It only records what a human reviewer personally looked up and
confirmed, one selector at a time, and reuses that confirmation
automatically only when a new item is unambiguously compatible with it.

## What CAT and SEL represent

- **Category (CAT)** -- a broad Xactimate item family (e.g. `RFG` for
  roofing, `ACT` for acoustical treatments). Not unique on its own.
- **Selector (SEL)** -- a specific item within a category (e.g. `ST` vs.
  `ST+` vs. `ST++`). Selector text is **not** globally unique across
  categories -- e.g. `MN` (labor minimum) can validly exist in both `RFG`
  and `ACT`.
- **Compound identity**: a Xactimate item is uniquely identified by
  **(category, selector)** together, never by selector alone. Every
  uniqueness check, lookup, and cache key in this module uses that pair.

Selector text is preserved **exactly**, including `+`, `-`, `++`, and
suffixes like `RS`/`2` -- these are never stripped, normalized, or
treated as noise. `ST` and `ST+` are different items with different
prices and different meanings; conflating them defeats the entire point
of verification.

## Stable selector identity vs. price-list observations

A Xactimate item's *identity* (category, selector, description, unit,
trade/component classification) is separate from what it costs in any
given price list. The same selector's price varies by price list,
location, and month. This module models that explicitly:

```json
{
  "catalog_record_id": "xactimate_rfg_pipe_jack",
  "category": "RFG",
  "selector": "VERIFIED_SELECTOR",
  "description": "Flashing - pipe jack",
  "unit": "EA",
  "activity_raw": "&",
  "activity_interpretation": null,
  "green_indicator": null,
  "aliases": ["R&R Flashing - pipe jack", "Pipe jack flashing"],
  "trade": "roofing",
  "component": "pipe_flashing",
  "material": null,
  "supported_actions": ["remove_and_replace"],
  "negative_patterns": [],
  "verification_status": "human_verified",
  "verified_at": "2026-07-31T00:00:00+00:00",
  "verified_by": "local reviewer name",
  "verification_method": "xactimate_selector_browser",
  "verification_notes": "Verified manually in authorized Xactimate environment.",
  "price_observations": [
    {
      "observation_id": "obs_001",
      "price_list": "TXDF8X_JUL26",
      "location": "Dallas/Fort Worth, TX",
      "price_list_date": "2026-06-30",
      "language": "English (US)",
      "type": "Xactware",
      "unit_price": 59.30,
      "observed_at": "2026-07-31T00:00:00+00:00",
      "observed_by": "local reviewer name"
    }
  ]
}
```

A price observation can never be added without a `category`+`selector`
that already has a stable record; adding a *new* price observation for an
already-known selector never creates a second identity record and never
changes `catalog_record_id`. A displayed unit price is never treated as
permanent -- automation readiness (below) never depends on it.

Raw `Act` column symbols (`+`, `-`, `&`) observed in the selector browser
are preserved exactly in `activity_raw`. `activity_interpretation` stays
`null` until a reviewer explicitly fills it in -- this codebase never
guesses what the symbols mean. Same for `green_indicator`: a tri-state
(`null`/`true`/`false`) preserved as-observed, never inferred.

## Verification statuses

| Status | Meaning | Automation-ready? |
|---|---|---|
| `human_verified` | A reviewer personally confirmed category, selector, unit, and price-list context in their own licensed Xactimate environment, with all three required confirmations checked. | Yes |
| `screenshot_transcribed` | Transcribed from a screenshot supplied during this phase's build spec (the ACT category table), to exercise the catalog architecture. **Not production data.** | No, never |
| `placeholder` | Reserved for future use. | No |

`config/verified_xactimate_catalog.yaml` ships with 58
`screenshot_transcribed` ACT-category records (acoustical treatments --
ceiling tile, grid, tin panel, etc.), transcribed verbatim from the
"Selectors for ACT (COFC8X_JUL26)" screenshot in the build spec. They
exist only to prove the architecture works end to end (compound
uniqueness, punctuation preservation, price observations, search) and are
deliberately tagged `trade: other` / `component: unknown` so they can
never accidentally match a roofing/gutter/siding fixture item. **No
screenshot-transcribed record can ever become automation-ready** --
`AUTOMATION_READY_STATUSES` contains only `human_verified`, enforced in
code (`verified_catalog_service.is_automation_ready()`), not just by
convention.

## The manual verification workflow

1. Open an unresolved item in Mapping Review; click **Verify in
   Xactimate**.
2. The Verified Catalog tab shows the item's immutable source context
   (description, quantity, unit, area/section/coverage, source page,
   extraction confidence, normalized action/trade/component/material,
   current mapping candidates and review reasons) -- read-only, exactly
   as extracted.
3. Separately, open Xactimate, select the correct price list, and search
   the selector browser for the matching category/selector.
4. Enter what you observed into ClaimXtract: price list, location, date,
   category, selector, Xactimate description, unit, raw activity symbol,
   optional interpretation, displayed unit price, green indicator
   (yes/no/unknown), notes.
5. Check the three required confirmations (below) -- a record cannot
   become `human_verified` without all three.
6. Choose **Save for this item only** (records a one-off verification
   scoped to that `line_item_id`, in `review/selector_verifications.json`
   -- never promoted to a reusable rule) or **Save as reusable verified
   rule** (adds/updates a stable catalog record, backs up the catalog
   first, and audits the change).
7. For a reusable rule, review the preview (new matches / changed matches
   / already-approved conflicts) before confirming.
8. Future matching items surface this record automatically as a
   **verified catalog match**, distinct from the mapper's own
   (unverified) suggestion -- both are shown, never silently merged.

### Required confirmations

A record can become `human_verified` only when the reviewer checks:

- "I personally verified this category and selector in Xactimate."
- "I verified that the unit matches the intended line item."
- "I understand that the displayed price belongs to the selected price
  list."

Enforced in code
(`verified_catalog_service.VerificationConfirmationError`), not just in
the UI -- any code path that tries to create/upgrade a `human_verified`
record without all three raises.

## Matching safety

A verified record is only ever surfaced as a match when **all** of the
following hold -- text similarity alone is never sufficient:

- trade matches (unless the record's trade is unset)
- component matches (unless the record's component is unset)
- unit matches exactly (case-insensitive)
- the item's normalized action is in the record's `supported_actions`
  (when the record declares any)
- none of the record's `negative_patterns` appear in the item's
  description
- **and**, only after all of the above, the item's description or one of
  the record's `aliases` textually identifies it (substring match)

This is why `ST` (standard) and `ST+` (high grade) never collapse into
each other in this system: adding `negative_patterns: ["high grade",
"premium grade"]` to the `ST` record (or simply relying on `ST+`'s more
specific alias) keeps them distinct, verified in
`tests/unit/test_verified_catalog_service.py::test_suffix_sensitive_selector_handling_via_negative_patterns`.

**A verified match is purely additive.** Applying one
(`verified_catalog_service.apply_verified_match()`) writes through the
same, already-audited `review_service.edit_mapping_field()` used
everywhere else in the review UI -- it never touches
`mapped_estimate.json` directly, and it **never silently overwrites an
already-approved item with a different category/selector** (raises
`ApprovalOverrideBlockedError` unless the caller explicitly passes
`allow_override_approved=True` with its own audited reason).

## Group-name handling

`config/xactimate_group_names.yaml` holds the 129-entry canonical group
vocabulary supplied in the build spec (rooms, elevations, trades, and
special project groups), plus a small, reviewer-extensible `aliases`
table. `group_name_service.suggest_group_name()` tries, in order: exact
match, alias table, substring containment, then fuzzy similarity
(stdlib `difflib`, ≥0.60 confidence) -- and returns `no_match` rather
than a low-confidence guess. **A suggestion never forces a rename**: the
original extracted section name is always preserved; a reviewer accepts
the suggestion, picks a different existing group, keeps the original, or
enters a custom name, and may save the decision as a reusable alias for
future documents.

## Automation-readiness rules

An item may be `automation_ready` only when **all** hold:

1. It passes the base Phase 3 approval gate (`review_service.can_approve()`
   -- category, selector, activity-or-waived, quantity, unit all present,
   no fatal mapping error).
2. Its review status is `approved`.
3. Its `(category, selector)` is backed by either a `human_verified`
   catalog record **or** a `human_verified` item-only verification for
   that exact line item -- never a `screenshot_transcribed` or
   `placeholder` record.
4. The Xactimate group for its section has been reviewed (accepted,
   overridden, or explicitly marked "keep custom") -- not left
   unreviewed by default.

The displayed unit price is **never** part of this gate -- Xactimate
prices the item from its own active price list at automation time; this
system's job is to identify the item, not to override its price.

## Project-context profiles

`project_context_service.py` stores reviewer-confirmed project-level
context (`profile`, `project_type`, `project_name`, `price_list`,
`tax_jurisdiction`, `timezone_label`, `policy_type`,
`deductible_application`) for future automation phases. The only field
this module will ever pre-fill from extracted evidence is `price_list`
(suggested from `canonical_estimate.json`'s claim data) -- and even that
is never marked `confirmed` until a reviewer explicitly checks the
confirmation box. Nothing here opens or drives Xactimate.

## Backups and restore

Every write to `config/verified_xactimate_catalog.yaml` is preceded by a
timestamped backup at
`config/backups/verified_xactimate_catalog_<timestamp>.yaml` (same
pattern as the Phase 3 placeholder-catalog builder, distinguished by
filename prefix). `catalog restore-latest` (CLI) or "Restore last
verified-catalog backup" (UI) reverts to the most recent backup.

## Audit history

Every catalog mutation (`add_verified_record`, `upgrade_verification_status`,
`add_price_observation`) appends one entry to the *current project's*
`review/catalog_changes.json` (shared with Phase 3's placeholder-catalog
audit trail, distinguished by a `"target": "verified_catalog"` field):
timestamp, action, `mapping_id` (the `catalog_record_id`), previous/new
file hash, backup path, affected line items, reviewer, and note. Item-only
verifications are recorded with full provenance in
`review/selector_verifications.json`. Nothing is ever silently overwritten
-- see "Matching safety" above.

## Required files

```
config/
  verified_xactimate_catalog.yaml   # stable records + embedded price observations
  xactimate_group_names.yaml        # canonical group vocabulary + aliases
  xactimate_activity_symbols.yaml   # raw Act-column symbols, interpretation null until confirmed
  backups/
    verified_xactimate_catalog_<timestamp>.yaml
    xactimate_group_names_<timestamp>.yaml

projects/<project>/review/
  catalog_drafts.json          # reserved for in-progress rule drafts (UI session-scoped today)
  catalog_changes.json         # shared Phase 3 + Phase 3.5 audit trail (target field distinguishes)
  selector_verifications.json  # item-only human verifications
  group_name_overrides.json    # per-section group-name review decisions
  xactimate_project_context.json
```

## CLI

```bash
python -m estimate_extractor catalog list [--status human_verified] [--category RFG]
python -m estimate_extractor catalog search "pipe jack"
python -m estimate_extractor catalog validate
python -m estimate_extractor catalog stats
python -m estimate_extractor catalog export-review [--output path.csv]
python -m estimate_extractor catalog restore-latest
```

No command lets you write an unvalidated or unconfirmed record -- the
CLI is read/audit/recovery tooling; creating and confirming records is a
UI (or direct service-layer) action, by design ("do not require users to
manage YAML directly").

## Known limitations

- This is a manual transcription tool. It does not, and will not,
  automate looking things up in Xactimate.
- The shipped catalog has zero `human_verified` records -- the 58 seed
  rows are explicitly non-production (`screenshot_transcribed`, ACT
  category only). Building real coverage requires actual reviewer time
  in a licensed Xactimate environment.
- Verified-match search is a compatibility-gated substring/alias check,
  not a full semantic matcher -- a genuinely novel phrasing of an already
  -verified item's description may not be recognized until an alias is
  added.
- "Prior successful uses" is computed live by scanning all local
  projects' approvals at query time (never a stored, potentially stale
  counter) -- this is fine at local, single-reviewer scale but is an
  O(projects × items) scan, not indexed.
- Group-name fuzzy suggestion is `difflib`-based, not a trained model;
  low-confidence cases correctly fall through to `no_match` rather than
  guessing.

## Why this system does not scrape or bulk-copy Xactimate

Xactimate's price-list data is Xactware's proprietary, licensed product.
This repository has no license to redistribute it, and attempting to
scrape, OCR, or reverse-engineer it would create exactly the kind of
technical, maintenance, and legal exposure the build spec explicitly
rules out. Every record in `config/verified_xactimate_catalog.yaml` is
either (a) hand-transcribed screenshot data supplied directly in this
phase's build spec for architecture testing (never treated as verified),
or (b) something a reviewer will personally type in after looking it up
themselves in their own licensed environment. The system's value is the
*verification workflow and reuse mechanism*, not a bundled copy of
Xactimate's catalog.
