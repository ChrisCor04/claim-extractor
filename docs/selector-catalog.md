# Canonical Xactimate selector database (Phase 3.6)

A permanent, local database of Xactimate selector identity --
**Category, Selector, Description** -- built once by OCR'ing reviewer-
supplied Xactimate selector-browser screenshots. This is a reference
lookup table, not a pricing database, and not a copy of Xactimate's
proprietary catalog: no unit price, price list, or region ever appears in
its schema.

This document assumes you've read
[docs/verified-catalog-builder.md](verified-catalog-builder.md) -- that
phase's `verified_xactimate_catalog.yaml` records *human-verified,
reviewer-confirmed* mappings one at a time. This phase's
`master_selectors.db` is different and complementary: an OCR'd reference
index a reviewer can *search* while doing that verification, so they
don't have to open Xactimate itself just to check whether a category code
they half-remember is `RFG` or `ROF`.

## Purpose and non-goals

The canonical identity is **(Category, Selector)** as the primary key,
with `Description` as the third required field. Nothing else is
structural to this schema:

- **Not a pricing database.** No unit price field exists on the canonical
  record at all.
- **Not a price-list recreation.** `COFC8X_JUL26` and similar price-list
  identifiers never appear as matching keys.
- **Not a bulk copy of Xactimate's catalog.** The source screenshots were
  supplied directly by the user as part of this phase's build spec, for
  the purpose of building this specific reference tool -- this module
  never scrapes, automates, or reverse-engineers Xactimate to obtain
  additional data beyond what was supplied.

## Pipeline

```
Reference ZIP (fixtures/reference/*.zip, gitignored, treated as immutable)
        |
Extract -> fixtures/reference/extracted/Xactimate_Reference_Library_v3/
        |
Inventory screenshots (filesystem walk, not the supplied index CSVs)
        |
Per screenshot:
  OCR (Tesseract, 3x upscale + --psm 6)  -> word bounding boxes
  Detect header row + Sel/Description column boundaries
  Detect title-bar category (separate targeted OCR pass)
  Parse rows -> (selector_raw, description_raw, confidence, truncated)
        |
Merge/deduplicate by (category, selector) across all screenshots
        |
Validate (no empty keys, no dupes, full provenance, full manifest coverage)
        |
Persist: master_selectors.db / .csv / .json,
         screenshot_processing_manifest.json, selector_review_queue.csv,
         reports/selector_extraction_report.md
```

Module layout, `src/estimate_extractor/selector_catalog/`:

| Module | Responsibility |
|---|---|
| `models.py` | `SelectorRecord`, `SourceReference`, `ScreenshotManifestEntry`, `normalize_description()` |
| `image_inventory.py` | Walks the extracted library on disk (filesystem is authoritative, not the bundled index CSVs) |
| `ocr_engine.py` | Tesseract word-box wrapper + dedicated title-bar OCR pass |
| `table_region.py` | Header/column-boundary detection; title-bar category regex parsing |
| `row_parser.py` | Row grouping, column assignment, truncation detection, punctuation-preserving selector extraction |
| `deduplicator.py` | Cross-screenshot merge by `(category, selector)`, conflict detection |
| `validator.py` | Completion invariants (used by both the pipeline's own gate and `selectors validate`) |
| `database.py` | SQLite schema, upserts, exact/substring/fuzzy search |
| `exporter.py` | CSV/JSON/manifest/review-queue/report writers |
| `pipeline.py` | Resumable orchestration |

## OCR approach and why it needed tuning

Xactimate selector-browser screenshots pack an entire data table into a
fairly small dialog -- real row heights as low as ~18-20px in the
reference library (screenshot dimensions ranged 832x1300 to 1043x1387).
That's below Tesseract's comfortable operating range. Measured directly
against a real screenshot during this phase's build:

| Preprocessing | Mean word confidence |
|---|---|
| Native resolution, default PSM | ~50-60 |
| 3x upscale (LANCZOS), `--psm 6` | ~77-90+ |

`--psm 6` ("assume a single uniform block of text") matches the dialog's
actual layout and also produces clean per-row line grouping via
Tesseract's own `block/par/line_num`, which `row_parser.py` relies on
directly rather than re-implementing row-clustering geometry.

**Title-bar detection required a second, separate pass.** An *unfocused*
Xactimate dialog renders its title bar in a visibly lighter gray than a
focused one; this reference library contains a real example
(`Screenshots_By_CAT/ELE/ELE_001_...png`, discussed below). Plain
upscaling left it unreadable to Tesseract. A tight grayscale threshold
(`pixel < 220 -> black`) after a 4x upscale of just the top title strip
recovered it perfectly, and this dedicated pass is now used
unconditionally for every screenshot (it's cheap, and it works for both
focused and unfocused windows).

## Folder vs. title-bar category (a real, not hypothetical, case)

The build spec anticipated that a screenshot's folder placement and its
in-image title bar might disagree, and required recording both rather
than silently picking one. **This is not a hypothetical edge case in this
library**: `Screenshots_By_CAT/ELE/ELE_001_1e4f2223-...png` is filed under
`ELE` but its title bar reads "Selectors for **FNC**" (Finish Carpentry --
crown molding, casing, door jamb items). Verified by direct visual
inspection during this phase's build and covered by
`tests/integration/test_selector_catalog_pipeline.py::test_folder_title_mismatch_is_detected_on_a_real_known_case`.

Handling: the **folder name is always the primary-key category** (per the
build spec). When the title-bar OCR produces a different category, every
record from that screenshot gets `category_mismatch: true`,
`title_bar_category` set to the observed (different) value, and
`"category_mismatch"` added to `review_reasons` -- surfaced, never
resolved automatically.

## Selector punctuation

Selector codes are stored **exactly as OCR'd**, including `+`, `-`, `<`,
`>`, `/`, and repeated-suffix forms (`ST` / `ST-` / `ST+` / `ST++`, `SG2`
/ `SG2+`, `SH5/8`). Nothing in this pipeline strips, normalizes, or
"cleans up" a selector value. A plausibility check on the selector token
(`row_parser.py`) exists only to reject OCR noise bleeding in from
background UI elements (e.g. a stray `|` from a breadcrumb panel that
landed in the selector column's x-range on some screenshots) -- it never
alters a token that passes the check.

## Deduplication and conflicts

Screenshots overlap heavily (scroll position, repeat navigation), so the
same `(category, selector)` is commonly OCR'd multiple times.
`deduplicator.py` merges these, keeping **every** source image reference.
When merged descriptions disagree, three outcomes are possible, in order:

1. **Truncation relationship** (one is a UI-truncated prefix of the
   other, e.g. ending in `...`): the complete one wins silently, no
   review needed beyond noting the truncation.
2. **Near-duplicate OCR noise** (≥90% text-similarity after
   normalization -- e.g. a degree mark `°` misread as an apostrophe on
   one screenshot but correctly on another): treated as the same row read
   twice, not a conflict.
3. **Genuine conflict** (meaningfully different text, e.g. two different
   real Xactimate items that a selector-code misread collapsed onto the
   same key): flagged `needs_review`, `"conflicting_descriptions"` added
   to `review_reasons`, and **every** distinct description preserved in
   `conflicting_descriptions` -- never silently discarded, never silently
   picked.

## SQLite schema

```sql
CREATE TABLE selectors (
    category TEXT NOT NULL,
    selector TEXT NOT NULL,
    description_original TEXT NOT NULL,
    description_normalized TEXT NOT NULL,
    needs_review INTEGER NOT NULL DEFAULT 0,
    review_reasons TEXT NOT NULL DEFAULT '[]',
    ocr_confidence REAL,
    title_bar_category TEXT,
    category_mismatch INTEGER NOT NULL DEFAULT 0,
    conflicting_descriptions TEXT NOT NULL DEFAULT '[]',
    primary_source_image TEXT,
    source_images_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (category, selector)
);
CREATE INDEX idx_selectors_category ON selectors(category);
CREATE INDEX idx_selectors_selector ON selectors(selector);
CREATE INDEX idx_selectors_description_normalized ON selectors(description_normalized);
CREATE INDEX idx_selectors_needs_review ON selectors(needs_review);
```

Stdlib `sqlite3` only -- no new runtime dependency.

## CLI

```bash
python -m estimate_extractor selectors import fixtures/reference/ClaimXtract_Xactimate_Master_Selector_Source_v1.zip
python -m estimate_extractor selectors validate
python -m estimate_extractor selectors stats
python -m estimate_extractor selectors search "ridge vent"
python -m estimate_extractor selectors search --category RFG "pipe jack"
python -m estimate_extractor selectors export-csv
python -m estimate_extractor selectors review-queue
python -m estimate_extractor selectors cleanup
```

`import` accepts either the ZIP (extracted automatically, idempotently,
into `fixtures/reference/extracted/`) or an already-extracted directory.
It is **resumable**: without `--force`, a re-run consults
`screenshot_processing_manifest.json` and skips every screenshot already
marked `processed`, loading its cached OCR results
(`fixtures/reference/data/.candidates/`) instead of re-running Tesseract.
`--category CAT` and `--limit N` scope a run for debugging.

`cleanup` runs the targeted QA fixes described below in "QA cleanup pass"
against an already-imported database, backing it up first
(`config/backups/master_selectors_<timestamp>.db`) -- see that section
for exactly what it does and does not change.

Ordinary use never requires hand-editing the database or CSV files.

## Search

`database.search_records()` covers exact CAT/SEL lookup, selector-across-
categories lookup, description substring search (against
`description_normalized`), and category-constrained search.
`database.fuzzy_search_records()` ranks by `difflib` similarity against
normalized description text -- read-only, never mutates a source record.

## Validation

`selectors validate` (and the pipeline's own completion gate) enforces:
every record has a non-empty category/selector/description; no duplicate
`(category, selector)` keys; every record carries at least one
`source_images` provenance entry; every screenshot the library contains
appears in the manifest with a terminal status (`processed`, `skipped`
with a reason, or `failed` with an error) -- nothing is ever silently
unaccounted for.

## QA cleanup pass

A post-hoc QA spot-check of the first full import (13,440 records) found
three real, evidence-backed problems, each investigated against the
actual reference screenshots before any fix was written (never guessed).
`selectors cleanup` runs all three fixes against an existing database
without a full re-import; `run_import()` also carries the underlying
fixes forward automatically for future imports.

**1. Unit-column contamination.** `row_parser._assign_columns()` assigned
words to the Selector/Description columns using each word's LEFT edge.
Tesseract occasionally produces a low-confidence duplicate/"ghost"
bounding box for a Unit-column token (confirmed on the real ACC/ANCR
screenshot: a second "EA" detection, confidence 0.33-0.44, a few pixels
wider than the real one) whose left edge lands just inside the
description column's boundary even though the word visually belongs to
Unit. Fix: assign by word **center**, not left edge (verified: the ghost
"EA"'s center falls outside the boundary even though its left edge
didn't). Critically, a heuristic pre-filter (`description ends in a known
unit token`) is **not** proof of contamination -- checked against the
real database, 347 records matched the heuristic, and 342 of them were
completely legitimate text ("...over 180 SF", "...per LF"). The cleanup
pass re-parses each candidate's actual source screenshot with the fixed
column logic and only ever changes a description when the fresh result is
related to the original by an **exact** `original == new + " " + token`
relationship -- proving the only difference is the trailing Unit token,
not unrelated OCR variance. (A real near-miss during this QA pass: a
merged record's `primary_source_image` is not always the screenshot that
produced its *stored* description -- `deduplicator.py` can pick the "best"
candidate from a different source than `source_images[0]` -- so an early,
looser version of this fix corrected `TIL/SWR>`'s "121 to 150 SF" into an
unrelated "101 to 120 SF" from a different row entirely. The exact-
reconstruction check above closes that gap: anything that doesn't match
it stays untouched and is flagged `possible_unit_column_contamination`
for review instead.)

**2. Malformed-shaped selectors.** Confidence-based `needs_review`
flagging missed real garbage like `RFG/stiR+` (confidence 0.85) and
`MTL/Bites` (confidence 0.88) -- both above the 0.85 threshold despite
being visibly corrupted. Every legible selector across the whole 13,440-
record library is upper-case-with-punctuation, so `is_selector_plausible()`
now flags any selector containing a lowercase letter, independent of
confidence. Where an exact-normalized-description match exists on a
well-formed selector from a genuinely different source screenshot ("
identical evidence from another source screenshot", per the build spec),
the malformed record is resolved into it (provenance merged, malformed
key removed) rather than left as a phantom duplicate; otherwise it stays
exactly as OCR'd, flagged `malformed_selector_candidate`.

**3. Title-bar false mismatches.** The original per-screenshot rule
(`title_bar_category != folder`) produced real false positives: the DOR
folder's 18 screenshots split 10/8 between reading "DOR" and "ELE" -- not
one systemic mismatch, just noisy OCR on a near-50/50 coin flip. Compare
the confirmed-real case: the ELE folder's 9 screenshots read "FNC" on 6
of them (67%). Per-screenshot title-bar OCR confidence turned out **not**
to separate these cases (both clustered at 66-78%), so confidence alone
was not a usable signal. The fix instead requires folder-wide
**corroboration**: at least 2 independent screenshots, comprising a clear
majority (>50%) of that folder's readings, agreeing on the SAME alternate
category, before `category_mismatch` is set. The folder name remains
authoritative either way; `title_bar_category` is still recorded
unconditionally as supporting metadata, corroborated or not.

Before/after on the real database: 1,230 → 295 category-mismatch records
(935 false positives removed); 347 unit-contamination candidates → 1
genuine correction, 342 confirmed legitimate, 4 left unresolved for
review; 1,022 malformed-shaped selectors → 122 resolved from overlapping
screenshots, 900 still awaiting review.

## Automation-readiness guarantee

`database.get_automation_eligible_records()` is the only sanctioned way
to treat catalog records as safe for automatic mapping: it filters out
every record with `needs_review=True` (malformed selector, probable unit
contamination, corroborated category mismatch, low OCR confidence,
truncation, or a conflicting description). This catalog is a reference/
search tool for human reviewers, not an automation source -- any future
integration point that consumes `master_selectors.db` for automation
purposes must filter through this function rather than reading search
results directly.

## Known limitations

- **Character-confusion OCR errors** (`I`/`1`, `O`/`0`) are a well-known
  Tesseract limitation this pipeline does not attempt to heuristically
  "fix" -- doing so risks silently inventing a different, wrong selector
  code, which the build spec explicitly forbids. These cases mostly
  surface as lower-confidence reads or the malformed-shape check
  (`needs_review`), but not always -- a single-character substitution
  between visually similar glyphs doesn't always produce a lowercase
  letter or depress Tesseract's own confidence score. A human reviewer
  using `selectors search` against a category they're verifying in
  Xactimate is the intended backstop.
- If OCR misreads the **selector code itself** differently across two
  screenshots of the same real item (rather than just the description),
  the two reads become two different database records rather than
  merging -- deduplication keys on the selector text as read, which is
  correct behavior (never silently declare two different-looking codes
  "the same"), but it means true coverage of a given real selector can be
  slightly overstated by raw record count. The malformed-selector cross-
  screenshot resolution (see "QA cleanup pass" above) recovers some of
  these when one of the two reads is well-formed and the description
  matches exactly; it cannot recover cases where both reads are garbled.
- The near-duplicate similarity threshold (0.90) that separates "OCR
  noise" from "genuine conflict" is a tuned heuristic, not a certainty --
  see `deduplicator.py`.
- The malformed-selector-resolution and title-bar-corroboration
  thresholds (exact description match; 2+ screenshots, >50% majority)
  were calibrated against this specific reference library's real data,
  not derived from first principles -- a future library with very
  different overlap patterns might need different constants.
- This is a reference/search tool for reviewers, not a replacement for
  Xactimate itself, and not a claim of completeness against Xactimate's
  full catalog -- it only contains what was in the supplied screenshots.
