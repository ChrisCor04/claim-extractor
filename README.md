# estimate-extractor

A multi-carrier property-insurance estimate PDF extractor. It converts a
carrier's estimate PDF (State Farm, Travelers, USAA, Farmers/Mid-Century,
Allstate, or a generic Xactimate-style layout) into a stable,
carrier-agnostic **canonical JSON** representation, with full provenance,
confidence scoring, and validation/reconciliation reporting.

```
Carrier Estimate PDF
        |
Page classification
        |
Text/table extraction
        |
Optional OCR fallback
        |
Carrier-aware parsing
        |
Canonical estimate JSON  (extraction stage -- see below)
        |
Validation and reconciliation
        |
Normalizer               (mapping stage -- see docs/mapping-engine.md)
        |
Normalized estimate JSON
        |
Matcher + scorer
        |
Mapped estimate JSON
        |
Mapping validator
        |
Mapping report + review CSV
        |
Local Review UI          (Phase 3 -- see docs/local-review-ui.md)
        |
Verified Xactimate catalog builder  (Phase 3.5 -- see docs/verified-catalog-builder.md)
        |
Approved estimate JSON + review history
        |
Automation input JSON + approved line items CSV
```

## Purpose and non-goals

This project has four internal stages, all offline and all living in
this one repository.

**Extraction** answers: *what does the uploaded insurance estimate document
actually say?* It converts a carrier PDF into a stable, carrier-agnostic
canonical JSON representation with full provenance and confidence scoring.
It does not interpret or classify line items in any Xactimate-aware way.

**Mapping** (Phase 2, `src/estimate_extractor/mapping/`) answers: *given
what was extracted, what trade/action/component concept is this, and which
verified Xactimate catalog entry (if any) does it match?* It reads only
`canonical_estimate.json`, never the PDF, and never alters an extracted
fact. See [docs/mapping-engine.md](docs/mapping-engine.md) for the full
pipeline, normalization rules, scoring, and catalog format.

**Local Review UI** (Phase 3, `src/estimate_extractor/ui/`) answers: *which
mapping suggestions has a human actually verified, and what's safe to hand
to automation?* A local-only Streamlit app for uploading PDFs, running the
above two stages, correcting/approving mapping suggestions, building up a
verified Xactimate catalog over time, and exporting only approved,
fully-qualified line items. See
[docs/local-review-ui.md](docs/local-review-ui.md).

**Verified Catalog Builder** (Phase 3.5, layered into `src/estimate_extractor/ui/`)
answers: *has a human actually confirmed this exact category/selector in
Xactimate, or are we still guessing?* A reviewer manually transcribes what
they personally verified in their own licensed Xactimate selector
browser -- category, selector, description, unit, activity symbol,
price-list context -- once per distinct item; ClaimXtract then reuses that
verified record automatically for every future compatible item, under
strict trade/component/unit/action compatibility checks (never on text
similarity alone). See
[docs/verified-catalog-builder.md](docs/verified-catalog-builder.md).

**Canonical Selector Database** (Phase 3.6, `src/estimate_extractor/selector_catalog/`)
answers: *what Category/Selector/Description combinations does Xactimate
actually define?* A permanent, local, offline-searchable reference index
(SQLite + CSV + JSON) built once by OCR'ing reviewer-supplied Xactimate
selector-browser screenshots -- not a pricing database, not a bulk copy of
Xactimate's catalog, just Category/Selector/Description with full
provenance back to the source screenshot. It's a lookup tool a reviewer
searches *while* doing Phase 3.5's verification work, not a replacement
for it. See [docs/selector-catalog.md](docs/selector-catalog.md).

All five parts deliberately do **not**:

- invent Xactimate CAT/SEL codes without a verified source (see
  "Xactimate data integrity" in docs/mapping-engine.md),
- generate an ESX file or drive any desktop automation,
- adjudicate coverage, interpret policy language, or recommend a
  repair scope or depreciation outcome.

Those remain out of scope for a future automation stage; keeping them out
is what keeps both the canonical schema and the mapping output stable
enough for that stage to build on.

Everything runs **fully offline** -- no network calls, no LLM, no paid API
required at runtime, on both macOS and Windows.

## Supported carriers

| Carrier | Adapter key | Notes |
|---|---|---|
| State Farm | `state_farm` | |
| Travelers | `travelers` | |
| USAA | `usaa` | Per-line Overhead & Profit column; often no "Claim Number" label (Member Number + L/R Number instead) |
| Farmers / Mid-Century | `farmers` | |
| Allstate | `allstate` | No per-line tax column; descriptions frequently wrap around the numeric block |
| *(fallback)* | `generic` | Used when no carrier crosses the detection confidence threshold (default 0.70) |

See [docs/carrier-adapters.md](docs/carrier-adapters.md) for how adapters
work and how to add a new carrier.

## Architecture

```
src/estimate_extractor/
  pdf/            Layer 1/2/3: native text extraction (PyMuPDF), word/line
                   layout, the Xactimate-row tokenizer, and the optional
                   OCR fallback (pytesseract + Tesseract, disabled by default)
  classification/ Page classification (carrier-agnostic) + carrier detection
  adapters/       One CarrierProfile (detection keywords + column schema)
                   per carrier, built on a shared BaseCarrierAdapter
  parsing/        Carrier-agnostic parsing: claim metadata, coverages,
                   sections/areas, line items, notes, totals, and the
                   cross-page continuation state machine
  normalization/  Decimal-safe money/date/unit/text normalization
  validation/     Structural rules + arithmetic reconciliation ->
                   extraction_report.json
  output/         canonical_estimate.json / line_items.csv /
                   extraction_report.json / document_pages.json writers
  models/         Pydantic v2 canonical schema (+ JSON Schema export)
  pipeline.py     Wires the above into one run_extraction() call
  mapping/        Phase 2: normalizer, action/trade/component detectors,
                   Xactimate catalog, deterministic scorer/matcher, mapping
                   validator, pipeline, output writers -- see
                   docs/mapping-engine.md
  ui/             Phase 3 (+3.5): local Streamlit review UI --
                   project/pipeline/review/catalog/export services, plus
                   Phase 3.5's verified_catalog_service.py (stable
                   selector identity + price observations),
                   group_name_service.py, and project_context_service.py
                   -- see docs/local-review-ui.md and
                   docs/verified-catalog-builder.md
  selector_catalog/ Phase 3.6: OCR pipeline (image inventory, table-region
                   detection, row parsing, deduplication, validation,
                   SQLite persistence, exporters) that builds the
                   permanent Category/Selector/Description reference
                   database from screenshots -- see docs/selector-catalog.md
  cli.py          extract / map / process / ui / catalog / selectors / validate / inspect
```

Config files driving the mapping stage live in `config/` (not hardcoded in
Python): `normalization_rules.yaml`, `mapping_catalog.yaml`,
`mapping_scoring.yaml`, plus Phase 3.5's `verified_xactimate_catalog.yaml`,
`xactimate_group_names.yaml`, `xactimate_activity_symbols.yaml`.
`config/backups/` holds timestamped backups written before every catalog
edit (both the Phase 3 placeholder catalog and the Phase 3.5 verified
catalog).

## Installation

Requires **Python 3.11+**.

### macOS

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e .
```

If your system Python is older than 3.11 (check with `python3 --version`),
install a newer one first, e.g. via Homebrew:

```bash
brew install python@3.12
/opt/homebrew/bin/python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e .
```

### Windows (PowerShell)

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install -e .
```

### Optional OCR setup

OCR (Layer 3) is disabled by default and only used for pages with little or
no native text, and only when you pass `--enable-ocr`. It uses local
Tesseract (no cloud service, no API key):

```bash
pip install -r requirements-dev.txt   # installs pytesseract + Pillow
```

Then install the Tesseract binary itself:

- **macOS**: `brew install tesseract`
- **Windows**: install from
  [UB-Mannheim's Tesseract build](https://github.com/UB-Mannheim/tesseract/wiki)
  and ensure `tesseract.exe` is on `PATH`, or set the `TESSERACT_CMD`
  environment variable to its full path.

### Optional local review UI setup

The Phase 3 review UI (Streamlit) is optional and only needed for `python
-m estimate_extractor ui`:

```bash
pip install -r requirements-ui.txt   # installs streamlit + pandas
```

## CLI usage

```bash
# Single file
python -m estimate_extractor extract "fixtures/Aranda Insurance.pdf"

# A directory, recursively
python -m estimate_extractor extract fixtures --recursive

# With OCR fallback enabled
python -m estimate_extractor extract "fixtures/Odom Insurance.pdf" --enable-ocr

# Validate a previously generated canonical_estimate.json against the schema
python -m estimate_extractor validate "output/aranda-insurance/canonical_estimate.json"

# Inspect one page's classification + row-token breakdown (debugging aid)
python -m estimate_extractor inspect "fixtures/Garrety Insurance Estimate.pdf" --page 5

# Map an already-extracted canonical_estimate.json (mapping stage alone)
python -m estimate_extractor map "output/aranda-insurance/canonical_estimate.json"

# Full pipeline: PDF -> extract -> normalize -> map -> validate -> outputs
python -m estimate_extractor process "fixtures/Aranda Insurance.pdf"
python -m estimate_extractor process fixtures --recursive

# Launch the local review UI at http://127.0.0.1:8501 (binds to localhost only)
python -m estimate_extractor ui
python -m estimate_extractor ui --projects-dir ./projects --port 8501

# Inspect/search/validate the verified Xactimate catalog (Phase 3.5)
python -m estimate_extractor catalog list
python -m estimate_extractor catalog search "pipe jack"
python -m estimate_extractor catalog validate
python -m estimate_extractor catalog stats
python -m estimate_extractor catalog export-review
python -m estimate_extractor catalog restore-latest

# Build/query the canonical Xactimate selector database (Phase 3.6)
python -m estimate_extractor selectors import fixtures/reference/ClaimXtract_Xactimate_Master_Selector_Source_v1.zip
python -m estimate_extractor selectors validate
python -m estimate_extractor selectors stats
python -m estimate_extractor selectors search "ridge vent"
python -m estimate_extractor selectors search --category RFG "pipe jack"
python -m estimate_extractor selectors export-csv
python -m estimate_extractor selectors review-queue
```

Options for `extract`: `--output-dir`, `--enable-ocr`, `--carrier
{state_farm,travelers,usaa,farmers,allstate,generic}` (force an adapter
instead of auto-detecting), `--strict` (treat any warnings as exit code 1
even without errors), `--debug` (always write per-page debug JSON),
`--log-level`, `--overwrite/--no-overwrite`, `--recursive`,
`--redact-debug-output`, `--config <path>`. `process` accepts the same
options plus mapping-stage output; `map` accepts `--output-dir`; `ui`
accepts `--projects-dir` and `--port`. `catalog` subcommands are
read/audit/recovery tooling only -- creating or confirming a verified
record is a UI (or service-layer) action; see
[docs/verified-catalog-builder.md](docs/verified-catalog-builder.md).
`selectors import` is resumable (`--force` to reprocess) and accepts
`--category`/`--limit` to scope a run; see
[docs/selector-catalog.md](docs/selector-catalog.md).

Exit codes: `0` success · `1` completed with review warnings (extraction or
mapping) · `2` extraction failure · `3` invalid arguments · `4`
dependency/configuration problem.

See [docs/mapping-engine.md](docs/mapping-engine.md) for what `map`/`process`
write, how normalization and scoring work, and the Xactimate
data-integrity policy (no invented CAT/SEL codes); see
[docs/local-review-ui.md](docs/local-review-ui.md) for the `ui` command,
project structure, and review/approval/export workflow.

### Example output (real run against the fixture set)

```
$ python -m estimate_extractor extract "fixtures/Aranda Insurance.pdf"
Processing Aranda Insurance.pdf ...
  Carrier: State Farm (1.00)
  Pages: 15
  Estimate pages: 11
  Excluded pages: 4
  Coverages: 2
  Sections: 9
  Line items: 42
  Warnings: 1
  Reconciliation: PASS
  Status: success
  Output: output/aranda-insurance/
```

```
output/aranda-insurance/
  canonical_estimate.json
  extraction_report.json
  line_items.csv
  document_pages.json
  raw_text/page_001.txt ... page_015.txt
  debug/page_001.json ... page_015.json
```

Running `process` instead of `extract` additionally writes, in the same
directory, the four mapping-stage outputs described in
[docs/mapping-engine.md](docs/mapping-engine.md):

```
output/aranda-insurance/
  normalized_estimate.json
  mapped_estimate.json
  mapping_report.json
  mapping_review.csv
```

## Schema

See [docs/canonical-schema.md](docs/canonical-schema.md) for the full
field-by-field explanation (provenance pattern, depreciation notation,
IDs, known limitations) and
[`schemas/canonical_estimate.schema.json`](schemas/canonical_estimate.schema.json)
for the machine-readable JSON Schema.

## Tests

```bash
pip install -r requirements-dev.txt
pytest -q
```

This runs the extraction unit suite (money/date/unit/text normalization,
the row tokenizer, carrier detection, page classification including the
instructional-sample guard, arithmetic reconciliation, output
serialization, cross-page continuation, coverage attribution), the mapping
unit suite (action/trade/component detection, normalization, catalog
validation, scoring/matching including conflict caps and tie handling,
mapping validation), the Phase 3 UI-service unit suite (project creation
and duplicate detection, review-state persistence and field overrides,
approval-rule validation, bulk actions, catalog-rule validation/backup/
restore, approved-estimate and automation-export generation including
export filtering), the Phase 3.5 verified-catalog unit suite
(category+selector compound uniqueness and exact punctuation preservation,
verification-confirmation gating, screenshot-transcribed vs. human-verified
status, matching safety including unit/action/negative-pattern conflict
handling and suffix-sensitive selectors, item-only vs. reusable-rule
verification, no-silent-overwrite-of-approved-items, catalog backup/
restore/audit, group-name suggestion/alias/fuzzy-match, project-context
confirmation gating), and four integration suites: extraction against all
six real fixture PDFs checked against hand-verified golden files in
`tests/expected/`, the full extract-to-map pipeline against those same six
fixtures (asserts no line item ever disappears or is altered by mapping),
the full extract-to-map-to-review pipeline through the UI service layer
against those same six fixtures (create project, approve/reject/correct
items, export, reopen from disk, confirm persistence), and the full
verified-catalog workflow against those same six fixtures (synthetic,
clearly-labeled test-only verified record improves matching for compatible
items only, unrelated items and prior approvals stay untouched, exports
stay verified-and-approved-only) -- see
[docs/local-review-ui.md](docs/local-review-ui.md) and
[docs/verified-catalog-builder.md](docs/verified-catalog-builder.md)
"Tests". Streamlit rendering itself isn't unit tested (not feasible
without a browser); only the service layer it calls is.

It also runs the Phase 3.6 selector-catalog unit suite (selector-
punctuation preservation, folder-vs-title-bar category detection and
mismatch flagging, row grouping/column assignment, truncation detection,
cross-screenshot deduplication including near-duplicate-vs-genuine-
conflict handling, validator completion invariants, SQLite persistence
and search, resumable-import behavior against a fake OCR engine) and,
when the reference screenshot library is present locally, an integration
suite that runs the real, local Tesseract pipeline against real
screenshots from all eight required categories (RFG, ELE, PLM, PNT, WTR,
FNC, WDV, XST), including the one real, verified folder/title-bar
mismatch in the library -- see
[docs/selector-catalog.md](docs/selector-catalog.md) "Known limitations"
for the OCR-accuracy tradeoffs this surfaced.

**Fixture PDFs contain real customer PII and are gitignored** -- integration
tests skip gracefully (not fail) if `fixtures/originals/*.pdf` aren't
present in your checkout (see `fixtures/supplements/` for the matching
supplement PDFs, not currently wired into any test). The Phase 3.6
reference screenshot library (`fixtures/reference/`) is similarly
gitignored (large, Xactimate-proprietary-adjacent) and its integration
tests skip gracefully if `fixtures/reference/extracted/` isn't present.

Run every fixture through the CLI and print a summary table:

```bash
python scripts/run_all_fixtures.py
python scripts/summarize_results.py
```

## Troubleshooting

See [docs/troubleshooting.md](docs/troubleshooting.md).

## Privacy

- No network calls, no telemetry, nothing is uploaded anywhere.
- Ordinary logs are redacted (emails/phones/ZIPs stripped) at INFO level
  and above (`logging_config.py:RedactingFilter`).
- Full raw page text is written only to local `debug/*.json` /
  `raw_text/*.txt` output files; pass `--redact-debug-output` to redact
  those too.
- **Fixture PDFs and generated `output/` must never be committed to a
  (especially public) repository** -- both are excluded via `.gitignore`.
  Do not put real customer data in README screenshots, issue reports, or
  test fixtures beyond what's already gitignored.
- The Phase 3 review UI binds to `127.0.0.1` only (never `0.0.0.0`), makes
  no network calls, and stores everything under `projects/` and
  `config/backups/` -- both gitignored, both contain PII. See
  [docs/local-review-ui.md "Privacy"](docs/local-review-ui.md#privacy).

## Known limitations

Full detail in [docs/canonical-schema.md "Known limitations"](docs/canonical-schema.md#known-limitations)
and [docs/troubleshooting.md](docs/troubleshooting.md); summary:

- `coverage_id` on sections/line-items is `null` when a document has
  multiple coverages and no unique, evidence-backed attribution could be
  found (`parsing/coverage_attribution.py` only assigns a `coverage_id`
  when an exact-sum partition against the printed coverage totals has
  exactly one solution; ties or no-match cases stay `null` with an
  explanatory note, never a guess). Verified against real fixtures: Garcia
  and Odom fully resolve and match their documents' own "Recap by Room"
  tables; Aranda, Bagi, and Wei Tang correctly stay `null`.
- Reconciliation excludes "Paid When Incurred" / code-upgrade line items
  from the primary calculated total (reported separately as
  `paid_when_incurred_total`) and still reports `FAIL` with an explanatory
  `note` for the remaining cases where per-coverage totals can't be
  reconciled against a single document-wide reported total.
- The mapping stage's Xactimate catalog (`config/mapping_catalog.yaml`) is
  intentionally sparse -- only the roofing category is populated, and no
  selector is populated at all, since no licensed Xactimate price list is
  in this repository. See
  [docs/mapping-engine.md](docs/mapping-engine.md#xactimate-data-integrity-critical-constraint).
- `bounding_boxes`/`line_ranges` on line items are empty; only page-level
  word bounding boxes are captured (Layer 1/2 text-stream parsing, not full
  2-D spatial table reconstruction).
- Area/section nesting is inferred from label-line adjacency heuristics
  (see docs/canonical-schema.md #2), verified against the fixture set by
  hand but not guaranteed for an unseen carrier template.
- A handful of pages across the fixture set land in `unknown`
  classification (flagged as an info-level issue, excluded from the
  estimate body) rather than being force-classified.
- OCR (Layer 3) uses plain `image_to_string`/`image_to_data` Tesseract
  output with no image preprocessing (deskew, binarization); scanned pages
  with poor scan quality may still extract poorly. It was implemented and
  is behind a clean `OCREngine` protocol, but was not exercised against a
  real scanned fixture (none of the six fixtures are image-only).
- The Phase 3 review UI's "reveal project folder" and pipeline
  stage-progress labels are both best-effort (OS-native open command;
  coarse before/after markers rather than live sub-step progress) -- see
  [docs/local-review-ui.md "Known limitations"](docs/local-review-ui.md#known-limitations).
- The verified Xactimate catalog (`config/verified_xactimate_catalog.yaml`)
  ships with **zero** `human_verified` records -- only 58 non-production
  `screenshot_transcribed` ACT-category (acoustical treatments) rows used
  to exercise the architecture. Building real automation-ready coverage
  requires actual reviewer time in a licensed Xactimate environment. See
  [docs/verified-catalog-builder.md "Known limitations"](docs/verified-catalog-builder.md#known-limitations).
- The canonical selector database (Phase 3.6) is built by local OCR and
  inherits OCR's known limitations -- in particular, character-confusion
  misreads (`I`/`1`, `O`/`0`) are not heuristically corrected (doing so
  risks silently inventing a wrong selector code), and a misread of the
  selector code itself (rather than the description) across two
  screenshots of the same real item produces two separate database
  records rather than one merged one. See
  [docs/selector-catalog.md "Known limitations"](docs/selector-catalog.md#known-limitations).

## Next-step roadmap

1. Use the Verified Catalog tab (Phase 3.5), searching the new canonical
   selector database (Phase 3.6, `selectors search`) as a reference while
   doing it, to build real `human_verified` records from a licensed
   Xactimate environment -- one reviewer verification at a time, reused
   automatically for every future compatible item under strict
   compatibility checks. See
   [docs/verified-catalog-builder.md](docs/verified-catalog-builder.md)
   and [docs/selector-catalog.md](docs/selector-catalog.md).
2. Grow `config/mapping_catalog.yaml` (the Phase 2 placeholder catalog)
   similarly, from an authoritative, documented source -- never guessed
   (see [docs/mapping-engine.md](docs/mapping-engine.md#xactimate-data-integrity-critical-constraint)).
3. Xactimate desktop automation that consumes `automation_input.json`
   once a meaningful number of selectors have been human-verified
   (explicitly out of scope for this repository at this stage; Phases 3
   and 3.5 exist specifically to make that verification process
   tractable and safe, not to build the automation itself).
4. Coverage attribution for the remaining unresolved documents, if a
   reliable signal can be found beyond the printed text (e.g. accepting
   the source ESX/XML alongside the PDF, when available, as a secondary
   input).
5. Full spatial (word-bounding-box-driven) table reconstruction as a
   fallback for carriers whose linearized text stream doesn't follow the
   one-field-per-line convention this MVP relies on.
6. Broader carrier coverage as more sample documents become available.
