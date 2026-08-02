# Troubleshooting

## "No PDF files found"

`extract` on a directory only looks at top-level `*.pdf` files unless you
pass `--recursive`.

## `EncryptedPDFError`

The PDF is password-protected. Remove the password first (the extractor
intentionally does not attempt to guess/crack passwords) and re-run.

## `UnsupportedPDFError`

Either the file doesn't exist, isn't actually a PDF, or PyMuPDF couldn't
open it (corrupted file). Try opening it in a normal PDF viewer first to
confirm it's valid.

## Extraction succeeds but `claim_number` is `null`

Check `document_pages.json` / `extraction_report.json` first:

- If every page's classification looks reasonable and no
  `MISSING_CLAIM_NUMBER` issue explanation surprises you, this may be
  correct: not every carrier labels a "Claim Number" at all (USAA's
  template only has Member Number + L/R Number -- see
  `tests/expected/odom_insurance.json` for a worked example). Check
  `claim.member_number` / `claim.lr_number` instead.
- If you believe the label really is present, run
  `estimate_extractor inspect <pdf> --page N` on the page you expect it on
  and check the "Row token stream" output -- if the label line doesn't
  parse the way `parsing/metadata.py:FIELD_LABELS` expects (e.g. a new
  carrier spells it "Claim No.:" instead of "Claim Number:"), add the
  variant to `FIELD_LABELS`.

## `status: needs_review` with no obvious errors

`needs_review` is the *normal*, expected outcome for most real documents --
it means "usable output exists, but at least one field's confidence was
below `validation.low_confidence_threshold` (default 0.80), or an
arithmetic check didn't reconcile within tolerance." Read
`extraction_report.json`'s `issues` array; each one has a `severity`,
`message`, and `suggested_action` pointing at the source page. This is by
design (see the spec's confidence-scoring philosophy: "never hide
uncertainty behind a high confidence score").

## `reconciliation.within_tolerance: false`

Check `reconciliation.note` first -- it's populated whenever reconciliation
fails, and the most common cause is not a parsing bug: the document has
more than one coverage (or a "Paid When Incurred" sub-coverage for
code-upgrade items) and line items aren't reliably attributable to a single
coverage from the printed layout (see docs/canonical-schema.md "Known
limitations" #1). The calculated total intentionally sums *every* line
item across all coverages, so it will not match a single coverage's
reported "Line Item Total" in that situation. This was verified by hand
against two of the six fixtures (Wei Tang, Odom) where the exact dollar
difference traces to a specific "Paid When Incurred" or code-upgrade
sub-total.

## OCR dependency errors

```
OCRDependencyMissingError: OCR was requested (--enable-ocr) but
pytesseract/Pillow are not installed...
```

Install the Python side (`pip install -r requirements-dev.txt` or
`pip install .[ocr]`) **and** the Tesseract binary itself (see README
"Optional OCR setup" for macOS/Windows commands). `pytesseract` is a thin
wrapper around the `tesseract` CLI binary -- it does not bundle it.

## A specific line item's fields look shifted or wrong

1. Find its `line_item_id` in `canonical_estimate.json` and check
   `source.raw_text` -- it's the exact consumed text lines, in order. This
   usually makes it obvious whether the tokenizer misclassified a line
   (e.g. treated a measurement as a quantity, or a stray footnote digit as
   part of a totals row).
2. Cross-reference `debug/page_NNN.json` for that item's `page_start` to
   see the full page's raw text and word-level bounding boxes.
3. If the row shape doesn't match any existing `ColumnSchema` (see
   docs/carrier-adapters.md), the carrier likely needs a schema tweak, not
   a tokenizer change.

## Fixture PDFs and privacy

The six development/test fixture PDFs contain real customer PII (names,
addresses, phone numbers, claim numbers). They are excluded via
`.gitignore` (`fixtures/*.pdf`) and must never be committed to a
repository, especially a public one. `output/` (generated extraction
results) is also gitignored for the same reason. If you need to share a
reproduction case, use `--redact-debug-output` and still review the result
by hand before sending it anywhere -- redaction only strips
emails/phones/ZIPs, not names or addresses.
