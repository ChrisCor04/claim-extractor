# Mapping engine (Phase 2)

The mapping engine is a second, independent stage that runs **after**
extraction. It never re-reads the PDF and never alters an extracted fact --
it only reads `canonical_estimate.json` and adds a parallel, deterministic
normalization/mapping layer on top of it.

```
canonical_estimate.json
        |
   Normalizer            action / trade / component / material / attributes
        |                (config/normalization_rules.yaml)
normalized_estimate.json
        |
   Matcher + Scorer       deterministic weighted scoring against the catalog
        |                 (config/mapping_catalog.yaml, config/mapping_scoring.yaml)
mapped_estimate.json
        |
   Mapping validator       catalog-wide + per-item issue detection
        |
mapping_report.json
mapping_review.csv
```

Extraction and mapping remain two independently usable stages. The
extractor's own tests, output contracts, and CLI (`extract`) are untouched.
`map` runs the mapping stage alone against an existing
`canonical_estimate.json`; `process` runs both stages back-to-back for a
PDF.

## Purpose and non-goal

This stage answers: *given what was extracted, what trade/action/component
concept is this line item, and which (if any) verified Xactimate catalog
entry does it correspond to?*

It deliberately does **not** invent Xactimate CAT/SEL codes. See "Xactimate
data integrity" below -- this is the single most important constraint on
the whole stage.

## CLI

```bash
# Map an already-extracted canonical_estimate.json
python -m estimate_extractor map "output/aranda-insurance/canonical_estimate.json"

# Full pipeline: PDF -> extract -> normalize -> map -> validate -> outputs
python -m estimate_extractor process "fixtures/Aranda Insurance.pdf"

# A directory, recursively
python -m estimate_extractor process fixtures --recursive
```

`process` never lets an unresolved mapping abort the run: unmapped or
low-confidence items come out as `status: needs_review` (or `unmapped`),
not a pipeline failure. It only stops a given file on a fatal extraction
error (bad PDF, missing OCR dependency); other files in a `--recursive`
batch still run. Extraction warnings are propagated into
`mapping_report.json` as `POSSIBLE_UPSTREAM_EXTRACTION_ISSUE` issues so a
reviewer can see when a low-confidence mapping is downstream of a
low-confidence extraction, not a mapping-stage problem.

## Output files

Written alongside the four extraction outputs, in the same
`output/<slug>/` directory:

| File | Contents |
|---|---|
| `normalized_estimate.json` | One entry per extracted line item: the untouched `original` block plus the detected `normalized` action/trade/component/material/attributes and a `confidence` breakdown. |
| `mapped_estimate.json` | One entry per line item: `coverage_id` (preserved, including `null`), the same `normalization` block, and a `mapping` outcome (`status`, `best_match`, `alternatives`, `needs_review`, `review_reasons`). |
| `mapping_report.json` | Document-level `summary` (counts per status, average confidence) plus a flat, typed `issues` list (catalog-wide and per-item). |
| `mapping_review.csv` | One row per line item, 21 columns, built for spreadsheet-based human review (see `CSV_COLUMNS` in `mapping/outputs.py`). |

`mapping_report.json`'s summary always satisfies
`mapped + partially_mapped + needs_review + unmapped == total_items`.

## Normalization rules (`config/normalization_rules.yaml`)

Rules are plain, case-insensitive **substring** patterns (not regex),
matched in file order -- **first match wins**. A rule sets `action` alone,
or `trade`/`component`/`material` together. This is deliberately simple and
auditable rather than a statistical model: every classification a document
receives can be traced to one specific YAML entry.

Rule order matters when patterns overlap. For example, "Hip / Ridge cap -
Standard profile - **composition shingles**" contains the substring
"composition shingles", which would incorrectly match the generic
composition-shingle rule if that rule appeared first. The file lists
specific roofing-accessory rules (ridge cap, starter course, drip edge,
valley metal, vents, flashing) **before** the generic shingle rules for
exactly this reason -- see the comment at the top of the "Trade / component
/ material" section in the YAML.

A separate `attribute_patterns` list (regex-based, applied universally to
every description regardless of trade) extracts and preserves dimensional
and material attributes that must never be lost or invented: felt
inclusion, tab count, weight (lb), slope range, "up to N-inch" sizes,
opening dimensions (`16' x 7'`), and SF ranges. Preserving these matters
because a mapping decision (or a human reviewer's decision) can depend on
them even though the normalizer doesn't use them for trade/component
detection.

Unmatched fields fall back to the literal string `"unknown"` -- the
normalizer never invents a concept that isn't backed by a rule.

### How to add a mapping

1. Add or extend a rule in `config/normalization_rules.yaml` (action, or
   trade+component+material) if the description isn't recognized yet.
   Check `python -m pytest tests/unit/test_action_detector.py
   tests/unit/test_trade_detector.py tests/unit/test_component_detector.py`
   after any change; add a matching unit test for the new pattern.
2. If a verified Xactimate category/selector exists for that
   component/action/unit combination, add an entry to
   `config/mapping_catalog.yaml`. Do not guess -- see "Xactimate data
   integrity" below.
3. Run the full fixture pipeline (`python -m estimate_extractor process
   fixtures --recursive`) and check `mapping_review.csv` for the affected
   items.

## Xactimate data integrity (critical constraint)

**No CAT/SEL code in this repository was guessed.** `config/mapping_catalog.yaml`
only populates a `category`/`selector` when supported by:

- a verified catalog entry already present in the repository, or
- an explicitly documented authoritative mapping source.

As of Phase 2, the only trade with a populated `category` is roofing
(`"RFG"`), and only because the literal text `Component RFG300` /
`Component RFG240` was independently verified present in the source
fixture PDFs' own ITEL pricing notes during the extraction audit. Every
other trade (gutters, siding, windows, doors, drywall, painting, flooring,
fencing, HVAC, electrical, general labor, debris removal, ...) currently
has `category: null, selector: null, requires_review: true` with an
explanatory note (e.g. *"No verified Xactimate category is available in
this repository for gutters."*).

**No `selector` is populated anywhere in the current catalog.** This is
intentional, not a bug: a Xactimate selector requires a licensed price
list this repository does not have. `test_no_selector_is_populated_without_verified_source`
in `tests/unit/test_mapping_catalog.py` enforces this as an invariant. Any
future PR that adds a selector must cite the authoritative source in the
catalog entry's `note` field, and should update that test's assumption
deliberately, not accidentally.

The practical effect: **no line item can currently reach `status: mapped`**
(mapped requires both `score >= 0.92` *and* a non-null selector). Every
matched item tops out at `partially_mapped` (score high enough, selector
missing -> forced review) or lower. This is the expected, honest behavior
of an unpopulated catalog, not a scoring bug -- see the benchmark numbers
in the main implementation report for confirmation across all six
fixtures.

## Scoring (`config/mapping_scoring.yaml`, `mapping/matcher.py`)

Deterministic weighted scoring, evaluated for every catalog entry whose
`trade`/`component` aren't `unknown`:

| Component | Weight |
|---|---|
| Component match | 0.35 |
| Trade match | 0.20 |
| Action compatibility | 0.15 |
| Unit compatibility | 0.15 |
| Material-term similarity | 0.10 |
| Section/context compatibility | 0.05 |

Material similarity uses `rapidfuzz` if installed, otherwise stdlib
`difflib.SequenceMatcher` -- no LLM, no network call, no required
dependency beyond the standard library.

A high text-similarity score alone can never produce high confidence:
component mismatch, trade mismatch, action mismatch, unit mismatch, and
attribute conflict (e.g. catalog entry requires `felt_included: false` but
the item explicitly has felt) each **hard-cap** the final score,
independent of how well the material text matches. Two or more candidates
scoring within `tie_margin` (0.03) of each other force `needs_review` with
`"tied_candidates"` regardless of the raw score.

### Confidence thresholds

| Score | Status |
|---|---|
| 0.92 - 1.00 (and selector present) | `mapped` |
| 0.80 - 0.91 | `partially_mapped` |
| 0.60 - 0.79 | `needs_review` |
| < 0.60 | `unmapped` |

A missing selector always forces at least `needs_review`, even when
category-level confidence is high (`missing_selector_forces_review: true`
in `config/mapping_scoring.yaml`).

## Coverage handling

`coverage_id` (including `null`) is carried through unmodified from
extraction into every mapping-stage output. The mapper never blocks on a
null coverage, never infers a coverage from trade/component, and never
drops an item because coverage is unresolved. A null `coverage_id` at
mapping time produces an `UNRESOLVED_COVERAGE` info-level issue in
`mapping_report.json` -- informational, not a failure.

## Mapping statuses

`mapped`, `partially_mapped`, `needs_review`, `unmapped`, `unsupported`,
`error`. **No extracted line item ever disappears** -- every line item in
`canonical_estimate.json` has exactly one corresponding entry in
`mapped_estimate.json`, regardless of status. This is enforced by
`tests/integration/test_mapping_pipeline.py` against all six real
fixtures.

## Human-review workflow

1. Run `process` (or `map` against an existing canonical file).
2. Open `mapping_review.csv` in a spreadsheet. Sort/filter by
   `mapping_status` and `needs_review`.
3. For `partially_mapped` items, `category` is usually populated but
   `selector` is null -- a human picks the exact Xactimate selector.
4. For `unmapped`/`needs_review` items, check `review_reasons`: an unknown
   component/trade usually means the normalization rules need a new
   pattern (see "How to add a mapping" above); a tied-candidates reason
   means the catalog needs a disambiguating attribute or a human pick.
5. Cross-reference `mapping_report.json`'s `issues` list, which carries a
   `suggested_action` string per issue code.

## Known limitations

- The catalog is intentionally sparse (roofing category only, no
  selectors at all) -- see "Xactimate data integrity" above. This is a
  starting framework, not a populated database. **Phase 3.5's Verified
  Catalog Builder** (see
  [docs/verified-catalog-builder.md](verified-catalog-builder.md)) is the
  intended way to grow real, human-verified selector coverage over
  time -- it uses a separate `config/verified_xactimate_catalog.yaml`
  file with an explicit `human_verified` vs. `screenshot_transcribed` vs.
  `placeholder` verification status, rather than extending this sparse
  starter catalog directly.
- Normalization is substring-based and file-order-dependent; an
  unanticipated overlapping pattern can require a manual reordering fix
  (as happened once during Phase 2 development -- see the "Rule order
  matters" note above). There is no automatic specificity ranking.
- Material similarity scoring is lexical (fuzzy string match), not
  semantic -- two materials that are functionally equivalent but worded
  very differently will not score highly on that component.
- Attribute-conflict detection only checks attributes a catalog entry
  explicitly declares in `required_attributes`; it cannot catch a conflict
  the catalog doesn't know to check for.
- This stage has no awareness of Xactimate line-item minimums, waste
  factors, or price-list versioning -- it only classifies and matches; a
  human still verifies every selector before any automation consumes it.

**Do not feed `mapped_estimate.json` into Xactimate desktop automation
without a human verifying every `category`/`selector` pair first.** No
code in this repository has verified a selector against a licensed
Xactimate price list.
