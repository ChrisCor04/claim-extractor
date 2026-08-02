# Page classification

`classification/pages.py:classify_page` assigns exactly one
`PageClassification` to every page, plus `include_in_estimate` (should this
page's content be treated as real estimate data) and `confidence` +
human-readable `reasons`.

## Classifications

`carrier_letter`, `settlement_notice`, `instructional_sample`, `faq`,
`claim_metadata`, `coverage_summary`, `estimate_detail`,
`estimate_detail_continuation`, `roof_diagram`, `measurement_summary`,
`tax_recap`, `overhead_profit_recap`, `recap_by_room`, `depreciation_guide`,
`replacement_cost_explanation`, `attachment`, `blank`, `unknown`.

## The one rule that matters most

> Instructional sample pages must never overwrite real claim metadata.

This is checked twice, independently:

1. **At classification time**: a page matching a known placeholder marker
   (`Smith, Joe & Jane`, `00-0000-000`, `GUIDE_EXAMPLE`, `John Smith`,
   `1234 Oak Street`, `ABC1234001H`, ...) or generic instructional phrasing
   ("sample estimate", "guide to understanding", "provided for reference
   only") is classified `instructional_sample` **before** any other check
   runs -- even if the same page also contains what looks like a complete,
   numbered, QUANTITY-columned line-item table (this is exactly what the
   Wei Tang, Aranda, and Bagi fixtures do: their "guide" pages embed a full
   fake estimate for illustration).
2. **At extraction time**: `parsing/metadata.py` and
   `parsing/coverages.py` only scan pages where
   `is_metadata_eligible(page_record)` is true, which excludes
   `instructional_sample` (and `blank`/`unknown`/`attachment`).
   `parsing/state_machine.py:walk_estimate_body` only walks pages
   classified `estimate_detail`/`estimate_detail_continuation`, which by
   construction excludes `instructional_sample` pages, so no line item can
   ever originate from one.
3. **At validation time**: `validation/rules.py:check_claim` re-checks the
   *final* extracted claim number/insured name against the same placeholder
   list and raises a `fatal`-severity `INSTRUCTIONAL_SAMPLE_VALUE_SUSPECTED`
   issue if either one somehow matches -- a deliberate belt-and-suspenders
   check in case a future carrier's placeholder text isn't in the marker
   list yet.

All three of these are exercised by the integration test suite
(`tests/integration/test_fixtures.py`) against the real fixtures, and by
`tests/unit/test_page_classification.py` with synthetic pages.

## Why some fields are still eligible even when "excluded" from the estimate

`include_in_estimate=False` controls whether a page's line items are part
of the priced estimate body. It does **not** mean the page is excluded from
metadata scanning. For example, State Farm's
`replacement_cost_explanation` pages repeat the real claim number, insured
name, and cause of loss in a different template -- excluding them from
metadata scanning would throw away a legitimate, corroborating source. Only
`instructional_sample`, `blank`, `unknown`, and `attachment` are excluded
from metadata scanning (`parsing/metadata.py:is_metadata_eligible`).

## Known gaps

A handful of pages across the fixture set land in `unknown` (see
`PAGE_UNKNOWN_CLASSIFICATION` info-level issues in `extraction_report.json`)
-- typically a mid-document attachment or a page whose content doesn't
match any of the heuristics above strongly enough. These pages are excluded
from the estimate body (`include_in_estimate=False`) and flagged, rather
than guessed at.
