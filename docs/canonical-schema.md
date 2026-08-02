# Canonical estimate schema

The canonical schema is defined as typed Pydantic v2 models in
[`src/estimate_extractor/models/canonical.py`](../src/estimate_extractor/models/canonical.py)
and published as JSON Schema at
[`schemas/canonical_estimate.schema.json`](../schemas/canonical_estimate.schema.json)
(regenerate with `python -c "import json; from estimate_extractor.models.canonical import CanonicalEstimate; json.dump(CanonicalEstimate.model_json_schema(), open('schemas/canonical_estimate.schema.json','w'), indent=2)"`).

`schema_version` is currently `"1.0.0"`. Carrier-specific quirks are
captured through optional fields (`Coverage.carrier_code`,
`LineItem.category_heading`, free-text `raw_values`) rather than by
changing the top-level shape per carrier -- every carrier adapter emits the
same schema.

## Top-level shape

```
schema_version, document, claim, contacts, coverages, areas, sections,
line_items, summary_totals, source_pages, validation_state
```

See the original build prompt (or the pydantic models directly) for the
full field-by-field shape; this document focuses on the parts that aren't
self-explanatory from the field names, and on what to expect when data is
absent or uncertain.

## Provenance pattern (`FieldValue`)

Every claim field that could plausibly be wrong is wrapped:

```json
{
  "value": "4399W552P",
  "confidence": 0.99,
  "source_pages": [3, 4],
  "raw_values": ["4399W552P"],
  "normalized": true
}
```

- `value` is `None` when nothing could be determined even though the field
  was looked for.
- The whole `ClaimData` field is `None` (not an empty `FieldValue`) when the
  label never appeared anywhere in the document at all -- e.g. USAA
  estimates have no "Claim Number" label anywhere, so
  `claim.claim_number` is `null`, and `member_number`/`lr_number` are
  populated instead. **This is correct behavior, not a bug**: the extractor
  never fabricates a claim number to fill the field.
- `raw_values` preserves every distinct raw string seen across pages, even
  when they conflict (see "Known limitations" below) -- `value` is a
  majority vote, not necessarily "the" truth.

## Depreciation notation

- `(123.45)` → `depreciation_type: "recoverable"`
- `<123.45>` → `depreciation_type: "nonrecoverable"`
- `0.00` / `(0.00)` → `depreciation_type: "none"`
- A bare positive number with no bracket, or a genuinely missing
  depreciation field on an item that also has no age/life/condition data →
  `depreciation_type: "unknown"` and the line item is flagged
  `needs_review`. This is deliberate: the source notation is ambiguous and
  the extractor does not guess which kind of depreciation was intended.

## IDs

`coverage_001`, `area_001`, `section_001`, `contact_001`, `summary_001`
(3-digit, zero-padded) and `line_0001` (4-digit). IDs are assigned in
document order within a single extraction run by `IdFactory`
(`parsing/state_machine.py`) and are stable for that run, but are **not**
guaranteed stable across two separate runs of the same document unless the
document and code are both unchanged (there is no persistent ID store).

## Known limitations

These are deliberate, honestly-surfaced simplifications -- each one was a
choice to leave a field `null`/best-effort rather than fabricate a
confident-looking but unverifiable value.

1. **`coverage_id` on areas/sections/line-items is often `null` when a
   document has more than one coverage.** Printed Xactimate-style estimates
   do not consistently restate which coverage a given section rolls up
   into -- the split is usually implicit in how the original ESX file was
   built, and is not always recoverable from the flattened PDF text (we
   verified this by hand against the Aranda fixture: the "Exterior"/
   "Dwelling" section group's RCV is ~$116 off from the printed Coverage A
   -Dwelling total, and no consistent partition of the remaining sections
   closes that gap). When a document has exactly one coverage, every
   section unambiguously belongs to it and `coverage_id` is always
   populated.
2. **Area/section granularity is approximated.** A bare label line that has
   no measurement/QUANTITY content of its own and is immediately followed
   by another label is treated as an "area" wrapping the section(s) that
   follow; a label with its own content is a "section" directly, with
   `area_id = null`. This matches every area/section pairing we could
   verify by hand in the fixture set, but a carrier whose template nests
   areas differently may not be classified with full fidelity.
3. **`bounding_boxes` and `line_ranges` on `LineItemSource` are populated
   as empty lists.** The parser works from the linearized per-page text
   stream (Layer 1/2 combined, see docs/carrier-adapters.md), not from a
   full 2-D spatial reconstruction, so exact word-level bounding boxes for
   a given field aren't tracked through to the line-item level. Page-level
   word bounding boxes ARE captured (`pdf/layout.py`) and written to
   `debug/page_NNN.json` for inspection.
4. **Description-wrap reconstruction is heuristic.** When a description
   wraps around the numeric block (seen in the Allstate fixture: "Remove
   Laminated - comp. shingle rfg. - w/" ... [numbers] ... "felt"), the
   parser merges a short trailing fragment back into the description only
   when the description-so-far looks incomplete (ends on a dangling
   hyphen/connector like "w/", "-", "for") and the fragment isn't itself
   shaped like a new category heading. This is right on every case in the
   fixture set (verified by hand) but is not a guarantee for unseen
   phrasing; merged items are flagged `needs_review` with reason
   `possible_description_wrap` so a human can double check.
5. **`estimate_name` is left `null`.** No carrier in the fixture set labels
   this consistently enough to extract with confidence (Xactimate estimate
   identifiers appear in ad hoc positions, e.g. embedded in a redacted-
   looking string on Travelers continuation pages).
