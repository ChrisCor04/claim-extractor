# Phase 5.0 — Working Assisted Supplement Builder

Continues directly from Phase 4.8 (see docs/xactimate-lookup.md). Phase 4.x
proved the item-level mechanics (search, select, quantity, unit
compatibility, commit verification) live, one item at a time, in whatever
Xactimate group happened to be active. Phase 5.0's mission was
integration, not new item-level mechanics: wire the already-complete
extraction/mapping/review pipeline into a real, group-aware, resumable
execution engine, and repair what was broken along the way.

## Priority 1: repaired broken integration

`cli.py` and `ui/pipeline_service.py` both imported from
`estimate_extractor.output.{csv_writer,json_writer,report_writer}` -- a
package that had never existed in this repository's git history. Root
cause, not just the missing files: `.gitignore` had an **unanchored**
`output/` pattern (intended to ignore the CLI's generated
`--output-dir output` directory at the repo root) that also matched
`src/estimate_extractor/output/`, silently untracking the real source
package. Fixed by anchoring it to `/output/` and adding the package back
(`csv_writer.py`, `json_writer.py`, `report_writer.py`, matching the
established writer pattern in `mapping/outputs.py`). Verified live: the
`extract`/`process` CLI commands and the Streamlit UI (including its
Upload/Process and Catalog Changes tabs, both of which import
`pipeline_service.py` transitively) all now run against the real Aranda
fixture without error. 8 new regression tests
(`tests/unit/test_output_writers.py`).

## Priority 2: Mapping Review grouped by Area/Section

Restructured `ui/components/mapping_table.py` around the existing
canonical Area/Section model -- no new grouping concept. Each group
renders as its own expander (Page/CAT/SEL/Readiness/Reason per row) with a
one-click "approve all ready in this group" action, plus a readiness
filter and a group multiselect filter. The existing cross-group multiselect
bulk actions and single-item editor are preserved under an "Advanced"
expander. Validated headlessly (Streamlit `AppTest`) against the real
`aranda-insurance` project -- all 9 real Area/Section groups render
correctly.

## Priority 3: first-class, persisted ExecutionPlan

`xactimate_lookup/execution_plan.py` adds `ExecutionPlan` /
`ExecutionTask` / `GroupExecutionState`: one task per **approved** line
item (`review_service.can_approve()` already guarantees category,
selector, quantity, and unit are present, so every task carries an
unambiguous human-approved CAT/SEL to search for directly, never a
phrase-based description search), grouped by Section -- Xactimate's real
group granularity, resolved via the existing `group_name_service`
vocabulary -- in source document order. Persisted as plain JSON at
`project_dir/execution/execution_plan.json`, matching every other
project artifact's storage convention.

Known limitation, not fixed this phase because it's outside scope (see
"Do not redesign existing systems"): canonical.py's `Area.area_id`/
`Section.section_id` foreign keys do not survive past the mapping stage --
`mapping/models.py`'s `OriginalItem` only carries the denormalized
`area_name`/`section_name` strings. This module (and everything else in
the mapping/review/lookup stack) uses names as the real identity key,
consistent with the rest of the codebase.

## A real defect found and fixed: commit verification was never wired in

While building the execution runner, found that `orchestrator.
execute_plan()` committed live items via `commit_item()` but never called
Phase 4.8's `verify_commit()` afterward -- meaning no commit made through
the only path any real caller uses was ever independently verified. Fixed
with a duck-typed hook (a grid snapshot taken before `select_candidate()`,
`verify_commit()` called after a successful commit, only when the adapter
implements both -- today, `WindowsXactimateAdapter`). `LookupOutcome`
gains a `verification` field, left untyped so `xactimate_lookup` stays
adapter-agnostic. The full existing orchestrator test suite passed
unchanged, confirming adapters without the hook are unaffected.

## Priorities 5-8: group-aware, resumable execution runner

`xactimate_lookup/execution_runner.py` runs an `ExecutionPlan` group by
group, in source order, reusing `orchestrator.execute_plan()` unchanged
(every task's CAT/SEL is wrapped as a transient trusted `LookupPlan`,
exactly like a verified-catalog match already is).

- **Priority 5 (group safety):** a group must be `ensure_group()`'d,
  `select_group()`'d, and independently `verify_group()`'d before any task
  inside it runs. These three methods are duck-typed exactly like
  `verify_commit()` -- an adapter without them, or a group that fails
  verification, never gets its tasks silently executed against whatever
  group happens to be active; every task in it is marked
  `REVIEW_REQUIRED` instead, and the run continues with the next group.
- **Priority 6 (resume):** the plan is saved after every single task. A
  run started against a reloaded, partially-completed plan
  (`load_execution_plan()`) skips every already-terminal task and
  continues from exactly where it left off.
- **Priority 7 (quantity/unit provenance):** `ExecutionTask` tracks
  `source_quantity`/`entered_quantity`/`observed_quantity` and
  `source_unit`/`expected_unit`/`observed_unit` as three independent
  fields each, never overwritten in place -- `observed_*` is populated
  only from `verify_commit()`'s own independently-read values.
- **Priority 8 (continue-on-error):** a commit is only ever marked
  `COMPLETED` when `verify_commit()` independently returns
  `trust_state=VERIFIED`. Every other outcome (no verification support,
  a non-`VERIFIED` trust state, a pre-commit safety stop, an unexpected
  exception) routes to `REVIEW_REQUIRED` or `FAILED`, never a silent
  success claim. One task's failure never aborts the run; only a failed
  application/project re-check between groups does (matching "only stop
  when continuing could corrupt the estimate").

Exercised via a `GroupAwareFakeAdapter` (10 tests): happy path, partial
group-verification failure, no group support at all, non-`VERIFIED` trust
states, committed-without-verification-support, dry run, an unexpected
mid-run exception, an unverifiable application/project state, and a
resumed partial run.

## Priority 4: Build Estimate UI tab

A new "Build Estimate" tab (between Mapping Review and Catalog Changes)
builds/persists the plan, displays it grouped by Section with per-row
CAT/SEL/quantity/unit/state/trust-state, a live
completed/review-required/skipped/failed summary, per-group skip
controls, and Preview (dry run) / Execute buttons. Execute constructs a
real `WindowsXactimateAdapter` (lazy import, isolated so a non-Windows or
no-Xactimate-running failure surfaces as one clear message) and reuses
`execution_runner.run_execution_plan()` unchanged -- it does **not**, and
must not, override `WindowsXactimateAdapter.supports_live_execution`
(`False` as of Phase 4.8): every task will safely come back
`REVIEW_REQUIRED` with `stop_reason=unsupported_adapter` until that gate
is deliberately flipped after further live validation. Validated
headlessly against the real `aranda-insurance` project.

## Required reports

`xactimate_lookup/execution_reports.py`: `execution_report.json` (full
plan snapshot + computed summary), `execution_report.csv` (one row per
task, spreadsheet-ready), `unresolved_row_summary.json`
(review-required/failed/skipped only, with reasons), `structured_audit.json`
(every task's search/select/verify/evidence trail, grouped by Xactimate
group). Written automatically to `project_dir/execution/reports/`
whenever a real run completes OR pauses partway through.

## Priority 5, live half: blocked, not faked

The group tree panel was located live (a real, project-rooted tree in the
main Estimate Items screen: `TEST` -> `Utility Room`, confirmed by a real
screenshot) and confirmed -- like every other part of this window across
every prior phase -- to be **not** UI-Automation-accessible (the main
window exposes itself as a WPF `HwndWrapper` with an inaccessible single
child, `NULL COM pointer access`, matching Phase 4.1's original finding
exactly). Click coordinates for the tree's first two rows were calibrated
against a real screenshot.

Live investigation of the actual mechanics -- does clicking a row change
which group new items land in, and how, given no visible highlight
appeared after a click; how to create a group that doesn't exist yet --
was **blocked for the remainder of this session** by an unrelated Windows
Firewall/Security prompt ("Do you want to allow public and private
networks to access this app? ... Python") that seized foreground focus
and could not be returned to Xactimate. Dismissing that dialog was
correctly refused by this session's own auto-mode safety classifier, since
it falls outside the Xactimate-only scope this session was authorized for
-- it was not attempted again by any other means.

`ensure_group()`/`select_group()`/`verify_group()` are therefore **not
implemented** on `WindowsXactimateAdapter` this phase. This is a
deliberate choice, not an oversight: writing pixel-coordinate group-click/
group-creation code without being able to verify live that it selects the
intended group (rather than, say, an unrelated row, or nothing at all)
would risk exactly the kind of unverified automation this whole project
has spent eight phases refusing to ship. `execution_runner.py`'s
duck-typed group check already handles this correctly and safely today --
every group is `GROUP_FAILED`, every task in it `REVIEW_REQUIRED`, with a
clear "Adapter does not support group operations" reason -- so the rest
of the system degrades honestly rather than pretending group-awareness
exists.

**Next session, first step:** dismiss the Windows Security/Firewall
dialog (Allow or Cancel -- Cancel is the more conservative choice, since
nothing in this adapter needs network access), confirm Xactimate regains
foreground focus, then resume the live investigation: (1) confirm/deny
click-to-select via the same ground-truth method planned this session
(click a row, commit a disposable item, check which group's Subtotal
column changed); (2) find the group-creation mechanism (right-click
context menu was the leading hypothesis, not yet tried); (3) find a
reliable way to read back which group is currently active, independent of
having just clicked it.
