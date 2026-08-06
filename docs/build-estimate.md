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

## Phase 5.1 — Finish Live Xactimate Group Control

Picks up exactly where Phase 5.0 left off: live-validate group selection,
discover group creation, and implement `ensure_group()`/`select_group()`/
`verify_group()` on `WindowsXactimateAdapter`.

### Stage 1-2: environment restored, group selection ground-truthed

The Windows Firewall dialog that blocked Phase 5.0's session was gone;
Xactimate had foreground focus with the TEST project's Estimate Items
screen open, baseline clean (0 rows, `TEST` → `Utility Room`). Group
selection was ground-truthed 5/5 times (alternating `TEST`/`Utility Room`,
committing one disposable item each time and reading the tree's Subtotal
column as independent evidence) -- click-to-select is real and reliable.

### Stage 3: group creation, and two real mechanism bugs found live

**Creation**, discovered via the tree's right-click context menu (a
*different* menu from the grid row's, despite an identical 26-item flat
count): `Cut, Copy, Paste, [sep], Select>, Deselect>, [sep], Expand>,
Collapse>, [sep], Filter Options..., Tree View, List View, Grouping
Selection>, [sep], New..., Edit..., Delete, Dimension, [sep], Grouping...,
Global Changes..., Global Item Sort by>, [sep], Save Macro..., Retrieve
Macro...` -- "New..." (index 15) opens a "New Group" dialog (Name field +
Coverage dropdown + Append/Insert/Attach buttons, only "Attach" enabled
when right-clicking an existing node); typing a name and clicking Attach
creates it as a child, live-confirmed multiple times.

**Bug 1 -- the tree does not preserve insertion order.** A row index
computed once and reused after other actions is not trustworthy; two
independent attempts to delete a specific just-created row by a presumed
index instead deleted the pre-existing baseline group (`Utility Room`),
confirmed by direct visual/OCR inspection both times, and recovered by
recreating it. Fixed by requiring every group-tree action to re-locate its
target row fresh, by OCR text match, immediately before acting -- never a
cached or presumed index.

**Bug 2 -- Delete does not reliably target the right-clicked row.** Even
with fresh row-lookup, a right-click alone, or a left-click followed only
by `time.sleep()` (tried up to 1.5s), did not make Delete operate on the
intended row -- reproduced live multiple times, always removing the
*previous* row instead. What worked, reproduced twice: forcing a real
window repaint (a screenshot capture, not a sleep) between the left-click
and the right-click. The selection change apparently only commits
internally once the window processes a paint cycle. `delete_group()` is
additionally self-verifying (checks which group actually vanished after
each attempt and restores anything wrongly removed before retrying) as a
second line of defense, since even the repaint fix is not proven perfectly
reliable across every tree size.

### Stage 4: implementation, and a precise miscalibration found live

`ensure_group()`, `select_group()`, `verify_group()` implemented using the
tree's own "Group" column header as a self-contained OCR anchor (not the
grid's "Cat" anchor, which is unreliable whenever the grid has zero
rows -- the common case group operations run in).

Live testing surfaced a real, precise bug: the row-to-row pixel spacing
used for click positioning was calibrated at 15px early in Stage 3, close
enough that `row_index=1` clicks happened to land correctly but
`row_index=2` clicks did not (confirmed live: a click computed for
"Dwelling Roof" landed on "Utility Room" instead, proven by where a
committed probe item's cost actually appeared). Remeasured precisely via
OCR word-level top-positions on a real 3-row tree: 22-23px between child
rows, not 15. Fixed, and reproduced live afterward: `select_group()`
correctly distinguished `row_index=1` from `row_index=2` and the
committed-item ground truth landed in the intended group both times.

`verify_group()` was designed first as a passive, non-mutating check (a
blue-pixel selection-highlight count). Live testing found the highlight
did not reliably track which group item entry actually targets --
confirmed twice: `select_group()` provably changed the real target (per
ground-truth commit) while the highlight-based read reported the old
group, or nothing confidently selected. Replaced with the same
ground-truth method Stage 2 established: commit one disposable, known
item, confirm the *target* group's Subtotal cell gained content, then
always clean up (`_cleanup_probe_item()`, bounded, best-effort, matches
every other cleanup helper in this file). OCR on the small Subtotal crop
itself proved unreliable even after locating the real column position
fresh; a non-white-pixel-count comparison against a known-blank row
(the project root) proved reliable instead. Live-confirmed for the
positive case (`verify_group("Dwelling Roof")` → `True` right after
`select_group("Dwelling Roof")`) before the session's live-testing window
closed (see below) -- the symmetric negative-direction case
(`select_group("Utility Room")` → `verify_group("Utility Room")`) had not
yet been re-confirmed with the final pixel-comparison implementation.

### Session end: Xactimate closed unexpectedly

Partway through re-confirming `verify_group()`'s second direction, the
Xactimate process was found to have exited entirely (no window, no
process, no crash dialog) -- not initiated by any action this session
took. Relaunching Xactimate and reopening the TEST project was outside
this session's authorized scope ("use" an already-open session was
authorized; launching a new one was not). The multi-group live pilot
(Stage 5), Build Estimate UI live validation (Stage 6), and final cleanup
verification (Stage 7) could not run as a result.

**Last confirmed live state:** TEST project, `TEST` → `Utility Room` +
`Dwelling Roof` (the latter a leftover disposable test group), 0 grid
rows. Cleanup of `Dwelling Roof` to fully restore the original baseline
did not complete before the process exited.

### What's proven vs. not, precisely

- Proven live, repeatedly: group creation, group selection (including in
  a 3-row tree), and one full ground-truth verification cycle.
- Proven live once, not yet re-confirmed: `verify_group()`'s negative
  case with the final (pixel-comparison) implementation.
- Not run this session: the multi-group pilot, Build Estimate UI live
  execution, and automated end-of-session cleanup verification.
- 7 new unit tests (pure logic + mocked application/project-state error
  paths) pass; these do not substitute for the live multi-group pilot.

### Next session, first step

Confirm Xactimate is running with the TEST project open (relaunch if
authorized), then: (1) delete the leftover `Dwelling Roof` group to
restore the exact original baseline; (2) re-run `verify_group()`'s
negative-direction case once to close out Stage 4; (3) run the Stage 5
multi-group pilot (Exterior / Dwelling Roof / Front Elevation, mixed
units, a REVIEW_REQUIRED item, a NO_MATCH item); (4) Stage 6 Build
Estimate UI validation against the Aranda project; (5) Stage 7 cleanup
verification.

## Phase 5.2: Safe Autofill, live bug fixes, and a launch-mechanism blocker

Continued directly from the state above. Stage 1 (restore TEST baseline)
and Stage 2 (finish group-control validation) both completed live and
cleanly. Stage 3 (multi-group pilot) started live, surfaced and fixed
three real bugs (below), then was cut short when Xactimate closed again
and could not be relaunched by this session -- see "Launch-mechanism
finding" below. Everything not requiring a live Xactimate session (Safe
Autofill wiring, capability flags, the display-profile gate, the
unresolved-rows UI, and Aranda Pass A) was completed and tested against
the Fake adapter / the real on-disk Aranda project fixture.

### Three real bugs found live and fixed

1. **`verify_group()`'s Subtotal check compared against the wrong
   baseline.** The original (Phase 5.1) implementation compared the
   target row's Subtotal-cell pixel count against the project ROOT
   row's count as a "known blank" baseline. Live testing found
   different rows have different inherent icon/gridline noise (a
   genuinely-blank child row read ~1775px, already past a
   root-row-based "blank+400" threshold) -- a reproducible false
   positive. Fixed by comparing each row's Subtotal cell against its
   OWN pixel count before vs. after the probe commit, never against a
   different row.
2. **The group tree's click/OCR-text/subtotal row-position formulas
   disagreed with each other once more than ~4 groups existed.** Three
   different constant pairs (a click-tuned (27, 23), a separately-tuned
   OCR-text (25, 15)) were each "close enough" to work by margin alone
   at low row indices, but diverged from each other as rows piled up --
   confirmed live: `_find_group_row()` (via the OCR-text formula)
   returned index 5 for a row the click/subtotal formula would have
   read as index 4, so `verify_group()` measured the WRONG physical
   row's pixels and returned false negatives. Fixed by re-measuring
   real row positions directly via OCR word-level top coordinates on a
   live 5-row tree (exactly 20px apart, 23px below the header, dead
   consistent) and using that ONE formula everywhere a row's vertical
   position is needed.
3. **A single, consistently-dropped OCR character silently defeated
   `delete_group()`.** OCR read "Front Elevation" as "frontelevaion"
   (missing the 't') on every one of 5 consecutive fresh captures --
   not transient noise. `_group_name_matches()`'s substring-only check
   never matched, so `delete_group()` concluded the group didn't exist
   and returned `True` ("already gone") without deleting anything.
   Fixed with a fuzzy (`difflib.SequenceMatcher` ratio ≥ 0.75) fallback
   after substring containment fails -- calibrated live against both
   the real miss (~0.87) and genuinely different group names (~0.3).

Also added two bounded-retry fixes for real timing issues: a
newly-created group occasionally wasn't found by the very next
snapshot (`ensure_group()` now retries up to 3 times, 0.8s apart), and
`select_group()` immediately after `ensure_group()` on a brand-new
(auto-selected) group could leave the tree in a transient inline-rename
focus state that destabilized `verify_group()`'s pixel baseline
(`verify_group()` now presses Escape before capturing, and retries the
whole probe once).

All three bugs and both timing fixes were found via a genuine live
Stage 3 pilot run failing, root-caused with targeted diagnostic
screenshots and pixel measurements (not guessed), fixed, and
re-confirmed live before moving on -- see the session's evidence
screenshots under the (gitignored) automation-evidence directory.

### Launch-mechanism finding: Xactimate requires a Xactimate.com session

Xactimate closed again during Stage 3 (same unexplained-exit class as
Phase 5.1's session end). This session is authorized to start Xactimate
if it isn't running, so two launch attempts were made: the Start Menu's
generic AppsFolder shell path, and the proper `.appref-ms` ClickOnce
shortcut at `Verisk Analytics, Inc\Xactimate online Estimate Writer-G0
.appref-ms`. **Both produced the same result**: a window titled
`OEW_Incorrect_App_Launch` showing "Xactimate Warning: This application
can only be launched from Xactimate.com. If you have encountered this
warning otherwise, please contact your administrator for further
assistance." Two pre-existing "Xactimate - Google Chrome" browser
windows were found (not touched) that are almost certainly the correct
entry point -- Xactimate Online Estimate Writer is apparently normally
launched from a logged-in xactimate.com web session, not directly from
a desktop/Start Menu shortcut. Since that requires an active user login
session, and this session's explicit authorization is "do not attempt
to bypass login or authentication," live automation stopped here per
the Stage 1 constraint ("if the TEST project cannot be positively
identified, stop live execution") rather than guessing further. The
harmless warning dialog was dismissed (a simple OK click, no auth
interaction); the stray launch process was left alone.

**For the next live session**: relaunch Xactimate from the
xactimate.com web portal (likely via one of the existing Chrome
windows), confirm the TEST project loads, then resume at Stage 3's next
task list below.

### Safe Autofill: design and current gating

"Safe Autofill" is implemented as a UI-level opt-in, not a new
execution engine -- `execution_runner.run_execution_plan()` already ran
continuously through all groups/tasks without pausing on ordinary
successful items (Phase 5.0's design). The only thing gating live
commits is `WindowsXactimateAdapter.supports_live_execution`, a class
attribute that defaults to `False` and is deliberately never flipped
globally by this phase (the multi-group pilot that would justify that
was interrupted -- see above). Instead, Build Estimate's new "Confirm
project" step constructs a real adapter, independently verifies
application/project state, computes capability flags
(`service.compute_capability_flags()`) and a display-profile check
(`adapter.verify_display_profile()`), and only if
`safe_autofill_available` is True (real adapter + verified live state +
group control + `supports_live_execution=True`) does it offer a "Safe
Autofill" checkbox. Checking it sets `supports_live_execution=True` on
that ONE constructed adapter instance, for that ONE run -- never the
class default. `production_project_allowed` and `unattended_mode_
allowed` are hard-pinned `False` in code (not just by convention) and
have no live-diagnostics path that can turn them `True`.

### Remaining work for the next live session

Stage 3 (cross-group commit verification with the fixed formulas),
Stage 4 (a real live Safe Autofill run against Build Estimate), Stage 7
(live pause/resume/application-interruption), Stage 8 Passes B/C
(controlled-subset and expanded Aranda execution), and Stage 11 (final
TEST-project cleanup) all require a live Xactimate session and did not
run this phase. The TEST project's last confirmed state (after Stage
2's cleanup) was baseline-clean (`Utility Room` + `Dwelling Roof` only,
0 grid rows); Stage 3's interrupted pilot may have left `Exterior` and/
or `Front Elevation` groups present (both empty, 0 committed rows,
confirmed via live screenshot) -- clean these up as the very first step
of the next live session before anything else.

## Phase 5.3: final sign-off attempt — five more live bugs found and fixed, one unresolved

Continued directly from Phase 5.2. Xactimate was confirmed running with
TEST open at session start. Stage 1 (restore baseline) completed
cleanly. Stages 2-3 (commit-capable pilot, cross-group proof) surfaced
five additional real bugs beyond Phase 5.2's three -- each found via a
live failure, root-caused with direct evidence (never guessed), and
fixed:

1. **`_locate_label()`'s exact-match-after-colon-strip check silently
   failed on the main grid's own "Cat" header**, because PSM 11 reads
   it with an unstable trailing artifact (`"Cat|"` on one capture,
   `"Cat,"` on the very next capture of the same unchanged screen).
   With no fallback, `_anchor_offset()` matched Quick Entry's "Cat:"
   label instead and computed a wildly wrong correction (dy=-211
   instead of the real ~-42) -- `focus_search()` and everything built
   on it was clicking off-window. Fixed by stripping all trailing
   non-alphanumeric characters, not a fixed set.
2. **The Phase 5.2 fuzzy group-name match failed on a second, noisier
   OCR corruption**: "Exterior" read as "eteior" (leading 'x' entirely
   absent) embedded in a row string with substantial unrelated icon
   noise, scoring only 0.52 against the whole-string comparison. Fixed
   with a sliding-window best-match ratio (0.80 on the real case, 0.17-
   0.38 on every unrelated-name pair -- same 0.75 threshold).
3. **The group tree has its own independent scroll position that
   nothing reset** -- mid-pilot, after several groups existed, it
   scrolled the "Group"/"Subtotal" header out of the captured area
   entirely, breaking every group-tree operation including re-
   verifying a group that had already succeeded moments earlier for a
   prior task. Fixed with an explicit mouse-wheel-up reset at the start
   of every group-tree entry point.
4. **`verify_group()`'s probe cleanup cancelled down to zero rows
   unconditionally** -- on a group re-verified after an earlier task in
   it already committed real content (exactly what resume does), this
   destroyed that real content along with the probe's own disposable
   row. Confirmed live: a $435.20 committed row vanished after the
   next task's group re-verification. Fixed by capturing the row count
   before the probe and cancelling down to exactly that, never to zero.
5. **(Critical) A task that safely stops after `select_candidate()`
   (field-mismatch or unit-mismatch) left its pending grid row
   uncancelled.** The task itself never calls `commit_item()`, so this
   looked safe by inspection -- but live testing proved a LATER,
   unrelated `commit_item()` call (a different task, or `verify_group
   ()`'s own probe cycle) saves the estimate's current on-screen state
   wholesale, silently persisting the abandoned row with Xactimate's
   default quantity, never having gone through the module's own commit
   path. Confirmed live: two "REVIEW_REQUIRED, never committed" tasks
   became real, saved, priced grid rows. Fixed by explicitly cancelling
   the pending selection before returning from either stop path.

All five are committed with tests (Fake-adapter unit tests where the
logic allows it, live-evidence-backed commit messages throughout).

### What did NOT get resolved this session

After fixes 1-5, a live pilot successfully got past group creation/
verification for both `Dwelling Roof` and `Exterior` and NO_MATCH/
REVIEW_REQUIRED classification worked correctly, but a **$330.31
discrepancy** was found between Grand Total ($765.51) and the sum of
every group's visible subtotal ($435.20, all in `Dwelling Roof`) --
`Utility Room` and `Exterior` both show empty/zero, `TEST` root shows
zero, yet the total doesn't reconcile. $330.31 exactly matches an
earlier `PLM/TLTRS` commit (bug 5, above) that was believed cleaned up.
A full page reload (navigating away and back) did not change the
figure, ruling out a stale display. Direct investigation (per-group
OCR of the Subtotal cell, an Item-# search for the original row number,
Xactimate's own "Summary Totals Report") did not resolve it within
this session's time budget -- the Summary report opens as an external
PDF in a separate browser window (not part of Xactimate's main window),
which was opened but not yet read before the session ended.

**This is the concrete, precise remaining blocker**: before any further
live pilot work, a human needs to open the TEST project directly in
Xactimate, find where the $330.31 actually lives (most likely: read the
already-generated Summary Totals Report PDF, or check Xactimate's own
"Coverage Limits" breakdown), and either identify why it's invisible to
per-group Subtotal inspection or manually remove it. Automated cleanup
could not verify success and must not be trusted as complete.

### Live-proven this session (before the above blocker)

- Stage 1 baseline restore: clean.
- Group creation/selection/verification for `Dwelling Roof` and
  `Exterior` both reached `GROUP_COMPLETED` after all five fixes.
- `RFG/FELT15` reached AUTO_SELECT and committed a real row (qty 10,
  observed) into `Dwelling Roof` -- trust_state landed REVIEW_REQUIRED
  because the observed unit OCR'd as "sa" instead of "SQ", a known-class
  OCR issue, not a safety failure (nothing was silently trusted).
- NO_MATCH (`ZZZ/ZZZ`) and REVIEW_REQUIRED (ambiguous ranking, unit
  mismatch, field mismatch) all correctly failed only their own task
  and let the run continue -- confirmed for real live outcomes, not
  just the Fake-adapter test suite.
- Not yet proven live: a clean AUTO_SELECT -> VERIFIED commit landing
  correctly in a SECOND group (the cross-group placement proof Stage 3
  requires) -- blocked by the unresolved discrepancy above before this
  could be attempted again.

### Next session, first step

1. Resolve the $330.31 discrepancy (see above) before any further live
   mutation -- do not assume automated cleanup succeeded.
2. Re-run a fresh multi-group pilot using the proven-exact items from
   this session's probing (`SFG/GUTA`, `SFG/GUTC`, `SFG/GUTAB` for LF;
   `PLM/TLTRS`, `PLM/TLTFL` for EA; `RFG/FELT15` for SQ -- all confirmed
   AUTO_SELECT live) to get the cross-group commit proof.
3. Stages 4 (Build Estimate UI live execution), 5 (pause/resume), 6
   (Aranda controlled subset), and 7 (final cleanup) all still need a
   live run.

## Phase 5.4: cross-group validation completed, cleanup verification strengthened, Safe Autofill signed off

The $330.31 discrepancy from Phase 5.3 was manually resolved before
this phase started (a real, financially active line item whose visible
row looked zero/inactive -- a cleanup-verification defect, not a
hidden calculation, tax, or deleted-group residue). This phase's
mission: stop trusting visible-state checks alone, strengthen cleanup
verification with real financial reconciliation, and complete the
remaining live validation (cross-group commit proof, the real Build
Estimate UI path, pause/resume, and a controlled Aranda subset) needed
to decide Safe Autofill's readiness.

### Stage 1-2: a trusted baseline and real financial reconciliation

Added `capture_estimate_baseline()` / `verify_estimate_matches_baseline()`
/ `ReconciliationResult` to `windows_adapter.py`: a baseline records
every group's row identities (category/selector/quantity/unit), each
group's Subtotal text, the Grand Total, and the Saved indicator;
reconciliation re-reads all of it fresh and reports every mismatch,
never just a boolean. Two bugs were found and fixed in this new
mechanism itself before it could be trusted:

1. Reusing the Phase 5.3 group-name fuzzy matcher for financial text
   was too lenient -- `"$0.00"` vs `"$50.00"` scored 0.91 against the
   same 0.75 threshold, well above it, because a short string gives a
   sliding window too much room to find a coincidental overlap. Fixed
   by making financial-field comparison strict/exact instead of fuzzy
   -- deliberately biased toward false positives over false negatives,
   since missing a real financial change is what this feature exists
   to prevent.
2. Strict-exact comparison then produced its own false positive: the
   SAME physically-blank Subtotal cell OCR'd as two different noise
   strings ("dy", then "ni") on two live captures seconds apart, with
   nothing on screen changing. Fixed with `_canonicalize_financial_text()`,
   which collapses any digit-free reading to `""` before comparing.

### Stage 3: permanent regression coverage

Eleven new tests in `test_windows_adapter.py` cover the baseline/
reconciliation mechanism directly: hidden financial residue, a
visually-zero-but-financially-active row, group-subtotal and
Grand-Total mismatches, OCR-noise tolerance, quantity changes on the
same row identity, required Saved state, a group that can't be
selected during verification, and baseline mismatch blocking
continuation.

### Stage 4: cancellation trials found a second real residue bug

Three controlled cancellation trials (PLM/TLTRS, PLM/TLT, PLM/TLTFL --
Phase 5.3's known-problem paths) reproduced the **exact same $330.31**
figure as fresh, real financial residue: `_cancel_pending_selection()`'s
single `cancel_current_item()` call failed silently on its first
attempt with no retry. Fixed with a bounded 3-attempt retry, plus a
best-effort `commit_item()` (save) after a successful cancel -- the
prior version left the estimate in an unsaved state even after a
clean cancel. Re-run trials: clean.

### Stage 5-7: a real cross-group commit proof

`RFG/FELT15` (this phase's one consistently reliable live item)
committed successfully into both `Dwelling Roof` (qty 10) and
`Exterior` (qty 5) in independent runs, verified by direct per-group
grid inspection after each -- zero wrong-group writes at this stage.
Two real, session-specific OCR reliability problems blocked broader
item coverage and were worked around rather than fought: `PLM` category
consistently misread as `PLN`, and `LF`-unit items (`SFG/GUTA`,
`SFG/GUTC`, `SFG/GUTAB`) consistently misread their unit as garbage --
neither is a ranking-threshold problem, so thresholds were not weakened.
Cleanup after the pilot reconciled to the Stage 1 baseline with zero
mismatches.

### Stage 8: the real Build Estimate UI path — a live wrong-group-write bug found and fixed

Driven entirely through Streamlit's `AppTest` harness (never a direct
`run_execution_plan()` call): project confirmation, the capability-flags
table (showing `Safe Autofill available: True` through the UI for the
first time this project), the display-profile check, Preview (dry run,
confirmed to touch nothing), the Safe Autofill checkbox, and Execute
all worked. Execute's first live run, however, revealed a real defect:
a row intended for `Dwelling Roof` (via an unreviewed suggested
Xactimate group name, `"Roof"`) committed into `Utility Room` instead.

Root cause: `_find_group_row()` returned the *first* tree row whose
text matched `group_name`, not the *best* match. `"Roof"` scored an
exact substring match against the correct `"Dwelling Roof"` row, but
*also* cleared the fuzzy threshold (0.857, above 0.75) against the
earlier, unrelated `"Utility Room"` row -- sharing "Roo" with "Room" --
and `"Utility Room"` appeared first in the tree. Fixed: exact substring
matches are now always searched first (in row order); only when no row
contains one does the code fall back to the single best (highest-ratio)
fuzzy match across all rows, never the first one to merely clear the
threshold. Two regression tests lock this in, including one that
reproduces the exact live ratios. The wrongly-placed row was deleted,
reconciliation confirmed clean, and a second live Execute (post-fix)
correctly placed the row in `Dwelling Roof`. All other Stage 8
objectives -- progress reflected in the persisted plan, the
unresolved-rows table correctly collecting a `REVIEW_REQUIRED` task
(a deliberately conservative post-commit trust downgrade, not a bug --
see `verify_commit()`'s `cat_sel_contradicts` path), and all four
report files generated -- were confirmed live.

### Stage 9: live pause and resume through the real UI

A two-group, two-task plan was run through a real Execute click with a
deterministic (not polled/racy) pause: a wrapper around the live
adapter's `verify_commit()` flips the adapter's active-project
expectation the instant task 1's commit finalizes, synchronously, in
the same call stack -- guaranteeing the run-level availability check
before group 2 sees it. Result: `run_state=paused`, task 1 completed,
task 2 genuinely untouched (`started_at=null`). A **fresh** `AppTest`
session (simulating reopening the app) then loaded the persisted paused
plan automatically (never via "Build / refresh," which discards
progress), re-confirmed TEST, and resumed: task 1's `completed_at`
timestamp was byte-for-byte unchanged (proving it was skipped, not
re-executed) and task 2 completed into the correct group. Reconciled
clean after cleanup.

### Stage 10: a controlled Aranda subset — a second false positive found and fixed

Real Aranda line items were approved through the existing, sanctioned
review mechanism (`edit_mapping_field()` + `approve_item()`, which
still enforces `can_approve()`'s gate) across two real groups
(`Dwelling Roof`, `Front Elevation`), matching the same controlled-test
pattern already established for this project's `line_0001`. A first
attempt paired the real item's description ("laminated composition
shingles") with `FELT15` and was correctly rejected by the ranking
system's own grade/style conflict check -- a reassuring finding, not a
bug. The corrected pairing (`line_0005`, literally "Roofing felt - 15
lb.") committed live, and `verify_commit()` correctly caught a real
quantity-read discrepancy (33.66 expected vs. an unstable OCR read) as
`QUANTITY_MISMATCH` rather than trusting it -- proof the verification
gate does its job on genuinely new data, not just the items rehearsed
all phase. This surfaced one more reconciliation false positive: a
genuinely empty/zero Subtotal cell OCR'd as digit-free noise on one
capture and as a real-looking `"$0.0"` on another; `_canonicalize_
financial_text()` treated the two as different (one had a digit).
Fixed: a reading whose only digit/decimal-point characters are zeros
now also collapses to `""`, same as noise -- any other digit still
forces the full comparison, so this cannot mask a real change. A new
regression test locks this in. `verify_group()`'s own live probe was
observed to fail transiently on 2-3 occasions across repeated rapid
Execute retries against otherwise-correct, previously-proven group
names -- confirmed transient by an immediate direct re-test returning
`True`, consistent with this project's established, accepted class of
live OCR/UI timing variability (the function's own docstring already
tolerates this: a false negative only routes a task to
`REVIEW_REQUIRED`, never a wrong execution). Also found and corrected:
a stale `group_name_overrides.json` entry mapping `"Dwelling Roof"` to
the wrong live Xactimate group name (`"Roof"`, which does not exist in
the real TEST project) -- corrected through the same sanctioned
`group_name_service.set_group_name_review()` mechanism. All live
residue cleaned up; final reconciliation against the Stage 1 baseline
passed with zero mismatches.

### Capability decision

`WindowsXactimateAdapter.supports_live_execution` (flipped to `True` in
this phase) and `safe_autofill_available` remain `True`, now backed by
substantially more evidence than the initial flip: a real cross-group
commit proof (twice over, Stages 5-9), the real Build Estimate UI path
exercised end to end including a live-found-and-fixed wrong-group-write
bug, live pause/resume with a proven no-duplicate-commit guarantee, and
a controlled real-claim subset. `production_project_allowed` and
`unattended_mode_allowed` remain separately gated `False` in
`service.py`, untouched by this phase, as required.

### Bugs found and fixed this phase (all with regression tests)

1. Financial-field fuzzy matching too lenient for short strings
   (rewritten to strict/exact).
2. OCR-noise false positive on blank financial cells (digit-presence
   canonicalization).
3. **(Critical)** `_cancel_pending_selection()`'s single, unretried
   cancel attempt could leak real financial residue -- reproduced the
   exact same $330.31 figure live. Fixed with bounded retry + save.
4. **(Critical)** `_find_group_row()`'s first-match (not best-match)
   scan caused a real live wrong-group commit when a short, unreviewed
   suggested group name fuzzy-matched an earlier, unrelated group
   before reaching its correct exact-substring match.
5. Zero-value financial readings ("$0.0") not recognized as equivalent
   to blank/noise, causing a false residue mismatch on an actually-
   clean group.

### What did not fully resolve this session

`verify_group()`'s live probe showed transient false negatives during
Stage 10's rapid repeated Execute retries against the Aranda project.
Every occurrence self-corrected on direct re-test and never caused an
unsafe outcome (only routed tasks to `REVIEW_REQUIRED`), so this was
not treated as a blocking defect -- but it means Stage 10's Aranda
subset did not end with a single, unbroken, fully-`VERIFIED` live
commit; it ended with a full live commit path proof (search, select,
quantity entry, commit) whose own verification correctly declined to
over-trust a genuinely uncertain OCR read. Future sessions doing rapid
back-to-back live Execute cycles against the same project may want to
add a short settle delay between runs if this recurs.
