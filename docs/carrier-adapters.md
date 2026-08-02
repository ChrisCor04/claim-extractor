# Carrier adapters

## Design

`adapters/base.py` defines `CarrierAdapter` (the interface: `detect`,
`classify_page`, `extract_claim_metadata`, `extract_coverages`,
`extract_estimate_body`) and `BaseCarrierAdapter`, the shared implementation
every concrete adapter inherits. A concrete adapter
(`adapters/state_farm.py`, `travelers.py`, `usaa.py`, `farmers.py`,
`allstate.py`, `generic.py`) is just a `CarrierProfile` (detection
keywords + a line-item `ColumnSchema`) handed to `BaseCarrierAdapter`:

```python
PROFILE = CarrierProfile(
    key="usaa",
    display_name="USAA",
    detection_keywords=("usaa casualty insurance company", "claims.usaa.com", "usaa confidential"),
    column_schema=ColumnSchema(core_fields=("unit_price", "tax", "overhead_and_profit", "replacement_cost_value")),
)

class USAAAdapter(BaseCarrierAdapter):
    def __init__(self) -> None:
        super().__init__(PROFILE)
```

All the actual parsing logic lives once in `parsing/*.py`
(`metadata.py`, `coverages.py`, `state_machine.py`, `line_items.py`,
`sections.py`, `totals.py`, `notes.py`) and is carrier-agnostic except for
the small pieces of carrier-specific configuration a profile supplies. This
was a deliberate requirement (see the build spec: "shared parsing logic
handles the common estimate layout... do not duplicate the entire parser
per carrier") and is why adding a sixth carrier required ~20 lines of
adapter code, not a parallel copy of the parsing pipeline.

### One deliberate deviation from the spec's illustrative `CarrierAdapter` protocol

The spec's protocol sketch lists `extract_sections` and `extract_line_items`
as two separate methods. In `BaseCarrierAdapter` these are combined into one
`extract_estimate_body` call. The reason: sections and line items are not
independent passes over the document -- a section spans page breaks, and
line items on a continuation page must attach to the section that was
already open, which requires walking the document exactly once while
carrying that state forward (`parsing/state_machine.py:walk_estimate_body`).
Implementing them as two separate, un-memoized passes would mean either
re-walking the whole document twice for no benefit, or building a caching
layer to avoid that -- both add complexity without adding correctness. The
`CarrierAdapter` protocol type still documents the split conceptually (via
its docstring) for interface clarity.

## Why page classification runs before carrier detection

Per the spec's implementation-priority ordering, `classification/pages.py`
is deliberately carrier-agnostic: it recognizes "sample estimate" /
"guide to understanding" language, `QUANTITY` column headers, `CONTINUED -`
markers, roof-diagram density, and known literal placeholder strings
(`Smith, Joe & Jane`, `GUIDE_EXAMPLE`, `John Smith`, etc. -- see
`INSTRUCTIONAL_PLACEHOLDER_MARKERS`), all of which work identically
regardless of carrier. `BaseCarrierAdapter.classify_page` is a passthrough
to this shared classifier; no adapter currently needs to override it.

## Column schemas (why they differ)

Verified by hand against the real fixture PDFs' extracted text (see the
`_inspect/` working notes used during development, not checked in): the
Xactimate-style line-item row always follows the same physical shape
(description line, then quantity+unit, then a run of per-field values, one
per physical text line) but the *order and presence* of fields after
RCV varies:

| Carrier            | Core money fields (in order, after qty+unit) | Trailing zone quirks |
|---------------------|-----------------------------------------------|------------------------|
| State Farm, Farmers | unit_price, tax, RCV                          | AGE/LIFE, DEPREC., ACV, then CONDITION, DEP% *after* ACV |
| Travelers           | unit_price, tax, RCV                          | AGE/LIFE, COND., DEP%, DEPREC., ACV -- ACV last |
| USAA                | unit_price, tax, **overhead_and_profit**, RCV | AGE/LIFE, DEPREC., ACV |
| Allstate            | unit_price, RCV (**no tax column**)           | AGE/LIFE+CONDITION combined on one line ("7/30 yrs Avg."), DEP%, DEPREC., ACV; descriptions frequently wrap around the numeric block |

`parsing/line_items.py:assemble_line_item` handles this generically: it
consumes `schema.core_fields` in the fixed order the carrier declares, then
enters a "trailing zone" loop that classifies each subsequent line by
*pattern* (age/life, condition word, percentage, bracketed depreciation,
plain money) rather than by position, so State Farm's "ACV before
CONDITION/DEP%" and Travelers' "ACV last" both fall out of the same loop
without a carrier-specific branch.

## Adding a seventh carrier

1. Confirm its detection keywords don't collide with an existing carrier's
   (check via a quick `grep -i` across the other fixtures, as documented in
   the development notes).
2. Determine its column schema by reading one raw estimate-detail page's
   extracted text (`estimate_extractor inspect <pdf> --page N`) and noting
   the field order between quantity+unit and the trailing zone.
3. Add `adapters/<carrier>.py` with a `CarrierProfile` + thin
   `BaseCarrierAdapter` subclass (state_farm.py is the shortest example).
4. Register it in `pipeline.py:build_adapter_registry()`.
5. If its instructional/sample pages use new placeholder text not already
   covered by `classification/pages.py:INSTRUCTIONAL_PLACEHOLDER_MARKERS`,
   add the literal marker there (deliberately literal, not a generic
   pattern -- see that module's docstring for why).
