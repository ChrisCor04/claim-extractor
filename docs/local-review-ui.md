# Local review UI (Phase 3)

A local-only Streamlit interface layered on top of the existing extractor
(Phase 1) and mapper (Phase 2). It lets an internal reviewer upload PDFs,
run the existing pipeline, review and correct mapping suggestions, build
up a verified Xactimate catalog over time, and export only
human-approved, fully-qualified line items for a future automation stage.

This document does not repeat what's already covered in
[docs/mapping-engine.md](mapping-engine.md) (normalization rules, scoring,
catalog format) or [docs/verified-catalog-builder.md](verified-catalog-builder.md)
(Phase 3.5's verified-selector workflow, added inside this same UI as a
"Verified Catalog" tab and a "Verify in Xactimate" action on every mapping
row) -- read both if you haven't.

## Purpose and non-goals

The full internal pipeline is now:

```
PDF -> Extractor -> canonical_estimate.json -> Normalizer -> normalized_estimate.json
    -> Mapper -> mapped_estimate.json -> Local Review UI (+ Verified Catalog Builder)
    -> approved_estimate.json -> automation_input.json
```

The UI does not reimplement any extraction or mapping logic -- it calls
the same `run_extraction()` / `run_mapping()` functions the `extract` /
`map` / `process` CLI commands use, and only adds a review/approval layer
on top. It does **not** build Xactimate desktop automation; that remains a
future phase, gated on this UI's export producing verified data.

## Local-only architecture

- Runs entirely on `127.0.0.1`. Never binds to `0.0.0.0`.
- No network calls, no telemetry, no analytics, no third-party uploads, no
  remote fonts/scripts, no paid APIs, no required LLM.
- All data is stored as local files under `projects/<slug>/` and
  `config/backups/`. No database.
- Session state (`estimate_extractor/ui/state.py`) holds only transient UI
  selections (active project/tab, reviewer name, an upload pending
  duplicate-resolution). Every durable fact -- projects, review decisions,
  catalog changes, approvals -- is written to disk by the service layer,
  so closing the browser tab or restarting the app never loses review
  data.

## Installation

```bash
pip install -r requirements-ui.txt   # streamlit + pandas, on top of the base install
# or: pip install -e ".[ui]"
```

The extractor and mapper (`extract` / `map` / `process` / `validate` /
`inspect`) work fully without this installed; only `ui` needs it.

## Launch commands

**macOS:**
```bash
source .venv/bin/activate
python -m estimate_extractor ui
```

**Windows (PowerShell):**
```powershell
.\.venv\Scripts\Activate.ps1
python -m estimate_extractor ui
```

Both open the app at `http://127.0.0.1:8501`. Options:
`python -m estimate_extractor ui --projects-dir ./projects --port 8501`.

You can also run Streamlit directly (equivalent, minus the CLI's
`--server.address 127.0.0.1` pinning, which you'd then need to pass
yourself): `streamlit run src/estimate_extractor/ui/app.py -- --projects-dir projects`.

## Project-directory structure

Every processed claim is a self-contained directory:

```
projects/<project_slug>/
  project.json                     # slug, source filename, SHA-256, timestamps
  source/original.pdf              # never overwritten
  extraction/                      # identical to `extract`'s output/<slug>/
    canonical_estimate.json
    extraction_report.json
    line_items.csv
    document_pages.json
  mapping/                         # identical to `map`'s output
    normalized_estimate.json
    mapped_estimate.json
    mapping_report.json
    mapping_review.csv
  review/
    review_state.json              # per-item status + field overrides (see below)
    review_history.json            # append-only audit log
    extraction_overrides.json      # coverage/area/section corrections only
    approved_estimate.json
    catalog_changes.json           # catalog edits made from this project
  exports/
    automation_input.json
    approved_line_items.csv
  logs/
```

`extraction/` and `mapping/` are written by the exact same code paths as
the `extract`/`map`/`process` CLI commands (`pipeline_service.py` calls
`run_extraction()` / `run_mapping()` directly) -- nothing in this UI
re-derives those facts.

## Duplicate-upload detection

Uploads are hashed with SHA-256 (content, not filename). If the same file
was already processed, the UI offers: **open existing project**, **create
new project version** (`<slug>-v2`, `<slug>-v3`, ...), or **cancel**. A
renamed copy of an already-processed PDF is still recognized as a
duplicate.

## Upload and processing workflow

1. Drag/drop or browse for one or more PDFs on the **Upload / Process**
   tab.
2. The PDF is copied into `source/original.pdf` (never overwritten).
3. The pipeline runs with stage labels shown live: *Reading PDF,
   Classifying pages, Extracting line items, Validating totals,
   Normalizing descriptions, Mapping candidates, Preparing review.*

   **Honesty note:** the extractor and mapper are not instrumented for
   sub-step progress (Phase 3 must not modify Phase 1/2 internals), so
   these labels are coarse before/after markers around the two existing
   blocking calls (`run_extraction()`, `run_mapping()`), not live
   intra-function progress. See `pipeline_service.py`'s module docstring.
4. Errors are shown as-is (stage + message), with a **Debug details**
   expander containing the real exception -- never a hidden/swallowed
   failure.

## Mapping review

The primary screen. Every line item is shown as an **effective row**:
machine-produced values with any reviewer override layered on top (see
`review_service.build_effective_rows()`). Filters: All / Partially mapped
/ Unmapped / Needs review / Missing category / Missing selector / Unknown
trade / Unknown component / Unresolved coverage / Approved / Rejected.
Sort by confidence ascending, section, trade, mapping status, or source
order.

**Editable fields:** action, trade, component, material, category,
selector, activity, mapped description, reviewer note, approval status.
**Never editable anywhere in this UI:** `line_item_id`, original
description, original quantity, original unit, source page -- these come
straight from the extractor and stay immutable.

Every edit is stored as an override, never as an in-place change:

```json
{
  "original_machine_value": "...",
  "reviewed_value": "...",
  "reviewed_at": "...",
  "review_reason": "..."
}
```

`mapped_estimate.json` / `normalized_estimate.json` are never rewritten by
a review action -- only `review/review_state.json` is. A reason is
required for every field edit.

### Extraction Review overrides

The Extraction Review screen is otherwise read-only. The only overridable
fields are `coverage_id`, `area_name`, `section_name` -- the three
attribution fields the hardening-phase coverage-attribution work already
documents as sometimes genuinely ambiguous (see
[docs/mapping-engine.md](mapping-engine.md)). They're stored separately in
`review/extraction_overrides.json`, in the same
original/reviewed/reason/timestamp shape, and also require a reason.
`canonical_estimate.json` itself is never modified.

## Approval rules

An item can be approved only when:
- `category` is present
- `selector` is present
- `activity` is present, **or** explicitly marked "not required" (a
  reviewer action requiring its own reason, via `waive_activity_requirement()`)
- `quantity` is present
- `unit` is present
- there's no unresolved fatal (`error`-status) mapping error

A `null` `coverage_id` **never** blocks approval -- it stays visible in
every table and export but is not a blocker (per the "never infer
coverage from trade" and "unresolved coverage must not fail" constraints
carried over from Phase 2).

Statuses: `unreviewed`, `approved`, `rejected`, `needs_more_information`.
Bulk approve/reject/mark-for-review and bulk field-assignment are
available; a bulk approve silently skips (and reports) any item that
doesn't yet qualify rather than force-approving it.

## Reusable mapping-rule creation ("Catalog Changes" tab)

Workflow, exactly as specified: **draft rule -> validate rule -> preview
effect -> confirm save -> backup existing catalog -> write catalog ->
re-run mapper**.

- Validation (`catalog_service.validate_rule_dict()`) rejects: missing
  required fields, a duplicate `mapping_id`, an action/trade outside the
  supported vocabulary, an unsupported unit, a conflict with an existing
  rule covering the exact same trade/component/actions/units, and --
  critically -- **a populated `selector` without an explicit
  `selector_confirmed` checkbox** ("I have verified this selector against
  a licensed Xactimate price list"). No CAT/SEL code is ever written
  without that explicit confirmation.
- Preview shows which line items in the *currently open* project would
  newly match the rule (trade + component + a canonical-term substring
  match against the original description) before you commit anything.
- A full YAML preview of the entry is shown before saving.
- On confirm: the current catalog is backed up to
  `config/backups/mapping_catalog_<timestamp>.yaml`, the entry is
  appended to `config/mapping_catalog.yaml`, and an audit entry is
  appended to the *current project's* `review/catalog_changes.json`:

  ```json
  {
    "timestamp": "...",
    "action": "add_rule",
    "mapping_id": "...",
    "previous_hash": "...",
    "new_hash": "...",
    "backup_path": "...",
    "affected_line_items": [],
    "reviewer": "...",
    "reviewer_note": "..."
  }
  ```

- **Restore last backup** reverts `config/mapping_catalog.yaml` to the
  most recent backup file.
- **Re-run mapping** re-runs normalization + mapping against the
  project's saved `canonical_estimate.json` (no PDF re-read) using the
  current catalog, shows a before/after diff of every item whose
  `(mapping_status, category, selector)` changed, and never touches
  `review/review_state.json` -- approved/edited values are preserved
  automatically because they live in a file this action doesn't write to.

## Verified Catalog tab (Phase 3.5)

A separate, top-level "Verified Catalog" tab (and a "Verify in Xactimate"
button on every Mapping Review row) implements the assisted verified-
selector workflow described in full in
[docs/verified-catalog-builder.md](verified-catalog-builder.md): a
reviewer manually transcribes what they personally confirmed in their own
licensed Xactimate selector browser, saves it as an item-only verification
or a reusable verified rule, and future compatible items match it
automatically under strict trade/component/unit/action compatibility
checks. This is a separate catalog
(`config/verified_xactimate_catalog.yaml`) and a separate audit trail from
the "Catalog Changes" tab above, though both share the same
`review/catalog_changes.json` file (distinguished by a `target` field) and
the same `config/backups/` convention.

## Output files

- **`review/approved_estimate.json`** -- every line item, with the
  original extracted facts, the normalizer's output, the mapper's
  (`machine_mapping`) suggestion, and the reviewer's (`reviewed_mapping`)
  final values shown side by side, plus review status/notes.
- **`exports/automation_input.json`** -- only `status == approved` items
  that are *also* re-validated as fully qualified at export time (defense
  in depth against a hand-edited `review_state.json`). Grouped into
  sections by `section_name`. Every excluded item is listed in
  `excluded_items` with a plain-language reason. **Fails safely**: zero
  approved items still produces a valid file with an empty `sections`
  list -- it never raises.
- **`exports/approved_line_items.csv`** -- one row per exported item;
  columns: `line_item_id, coverage_id, area_name, section_name,
  original_description, category, selector, activity, quantity, unit,
  mapped_description, reviewer_note, source_page`.

## Human-review workflow (end to end)

1. Upload -> process (Upload / Process tab).
2. Read Claim Summary; check extraction/mapping warnings.
3. Extraction Review: spot-check line items; correct
   coverage/area/section attribution if genuinely wrong (with a reason).
4. Mapping Review: filter to `Missing selector` / `Unmapped` / `Needs
   review`; for each, verify the correct Xactimate category/selector
   against a licensed price list, edit the fields (with a reason), then
   Approve. Use bulk actions for repeated patterns.
5. Optionally: "Save as reusable mapping rule" from a verified item so
   future documents with the same pattern map automatically; re-run
   mapping to apply it to the current project.
6. Export: build `approved_estimate.json` and
   `automation_input.json` / `approved_line_items.csv`. Check the excluded
   list -- anything not yet approved or not fully qualified stays out.

## UI status labels

`Extraction passed` / `Extraction needs review` / `Mapping incomplete` /
`Ready for review` / `Partially approved` / `Ready for automation export`
/ `Export blocked` -- the Export tab explicitly distinguishes "0 of N
approved" (**Export blocked**) from "M of N approved" (**Partially
approved**) from "N of N approved" (**Ready for automation export**);
"done"-style labels are never used while unresolved items remain.

## Privacy

Same guarantees as the base extractor/mapper (see the main
[README.md](../README.md#privacy)), plus: the app binds to `127.0.0.1`
only, `projects/` and `config/backups/` are gitignored (both contain PII
and, respectively, catalog history), and nothing here makes an outbound
network call.

## Troubleshooting

- **"Streamlit is not installed"** when running `python -m
  estimate_extractor ui`: `pip install -r requirements-ui.txt`.
- **A pipeline run fails**: the exact stage and message are shown inline;
  expand **Debug details** for the underlying exception. Common causes are
  the same as the base CLI's (`docs/troubleshooting.md`) -- encrypted PDF,
  unsupported file, missing OCR dependency.
- **Catalog write fails / looks wrong**: use **Restore last backup** on
  the Catalog Changes tab; every write is preceded by a timestamped backup
  in `config/backups/`.
- **A project won't open**: a malformed `project.json` or `review_state.json`
  is reported, not silently ignored -- `ProjectService.list_projects()`
  skips only malformed *manifests* from the list (so one broken project
  doesn't take down the Projects screen), but `load_project()` on that
  slug raises with the JSON error.

## Known limitations

- Live sub-stage progress is approximate (see "Upload and processing
  workflow" above) -- the extractor/mapper aren't instrumented for it by
  design.
- "Reveal project folder" uses a best-effort, OS-native command (`open` /
  `explorer` / `xdg-open`) and silently no-ops on an unrecognized
  platform; the folder path is always shown as a fallback.
- The reusable-rule "preview affected items" check is a simple
  trade+component+substring match against the *current* project only; it
  does not simulate the full scorer/matcher, so the actual effect after
  "Re-run mapping" can differ slightly (e.g. a tie with another candidate).
- Bulk field-assignment applies one shared reason to every item in the
  batch, rather than a reason per item -- acceptable for a single batch
  review action, but keep batches to genuinely-identical corrections.
- There is still no populated Xactimate selector anywhere in the shipped
  catalog (see [docs/mapping-engine.md](mapping-engine.md)); this UI is
  what makes populating it (with real, verified selectors) tractable, not
  a shortcut around doing so.
- No Xactimate desktop automation exists yet -- `automation_input.json` is
  this phase's final deliverable; consuming it is explicitly out of scope
  here.
