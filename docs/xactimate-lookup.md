# Description-first Xactimate lookup workflow (Phase 3.8)

## Purpose and non-goals

Phases 3.6/3.7 gave ClaimXtract a searchable selector catalog and a
ranked recommendation layer, but both still assumed a reviewer already
had *some* CAT/SEL candidate to look at. Phase 3.8 removes that
assumption: for each normalized line item, it decides whether a trusted
internal mapping already exists (fast CAT/SEL path) or whether the
system needs to generate a description search phrase, rank the resulting
dropdown candidates, and ask a human to resolve it -- then remembers that
resolution for next time.

This phase does **not** build live Xactimate desktop automation. It
builds the recommendation/ranking/registry machinery and a working manual
workflow, plus a clean adapter boundary a future phase can implement
against a real desktop-automation backend.

## Investigation findings

- Phase 3.6's `SelectorRecord` has no unit field and Xactimate
  descriptions almost never contain action verbs (`remove`/`replace`/
  `R&R`) as literal text -- action is expressed via a separate Activity
  symbol (see `config/xactimate_activity_symbols.yaml`). Confirmed
  directly against the real catalog: `RFG/ARMVN` = "Tear off composition
  shingles", `PNT/FENST` = "Stain - wood fence/gate" -- action words that
  *do* appear are a small, specific set (tear off, clean, paint, stain,
  seal, prime, inspect), not the generic remove/replace/install/detach/
  reset vocabulary. This directly shaped both the search-phrase generator
  (drop compound actions by default) and the ranking model (action
  absence is neutral, not a conflict).
- Phase 3.7's `RecommendationInput` already carries every field this
  phase needs (trade/component/material/action/unit/quantity) -- reused
  directly rather than duplicated (`xactimate_lookup.models` re-exports
  it).
- Phase 3.5's verified catalog (`verified_xactimate_catalog.yaml`) is
  itself a form of "trusted mapping" (compatibility-matched, human-
  verified). It is checked as a second trusted source, after the new
  registry, before falling back to description search.
- All approvals present in the local `projects/` fixtures are synthetic
  (`reviewer: "benchmark-run"`, notes like "simulated verification") --
  the same finding Phase 3.7 already documented. `lookup stats` reuses
  Phase 3.7's `real_ground_truth()` (promoted from private to public) to
  exclude them, so top-1/top-3 report `N/A` rather than a fabricated
  number.

## Final architecture

```
src/estimate_extractor/xactimate_lookup/
    models.py            LookupPlan, LookupOutcome, DropdownResult, RankedCandidate,
                          InternalMappingRecord, PopulatedFields, decision/status constants
    signature.py          deterministic item signature (trade/component/material/size/grade/action/unit -- never price)
    phrase_generator.py   concise search-phrase generation (bucketed, configurable, explainable)
    registry.py            SQLite persistence for the internal lookup registry
    ranking.py              dropdown-candidate scoring + AUTO_SELECT/REVIEW_REQUIRED/NO_MATCH
    adapter.py               XactimateAdapter ABC + FakeXactimateAdapter (no real adapter exists yet)
    orchestrator.py          plan -> search -> capture -> rank -> decide -> (dry-run or commit)
    service.py                 CLI/UI-facing orchestration: planning, recording, disabling, stats
```

Config: `config/xactimate_lookup_phrase_rules.yaml` (filler phrases,
component/material/style keyword tables, action-term allowlist),
`config/xactimate_lookup_ranking.yaml` (weights, conflict caps,
auto-select thresholds/margin).

Storage: `config/internal_lookup_registry.db` (SQLite, same
create-on-demand / backup-before-write pattern as Phase 3.6/3.5), audit
trail at `config/internal_lookup_events.json` (this registry is
cross-project data, so its audit log lives beside it in `config/`, not
inside any one project's `review/` directory).

## Lookup decision flow

```
for each normalized line item:
    signature = compute_item_signature(trade, component, material, action, unit, description)
                 -- never price or price-list identifiers

    trusted = registry.find_reusable_mapping(signature)   # exact-signature, status=approved only
           or a Phase 3.5 human-verified catalog match (compatibility-matched)

    if trusted:
        plan = search "CAT SEL" (confirms the mapping is still findable)
    else:
        phrase = generate_search_phrase(description, component, material, action)
        plan = search by phrase

    dropdowns = adapter.capture_dropdown_results()   # real or Fake
    candidates = rank(dropdowns)                      # weighted score + hard conflict caps
    decision = AUTO_SELECT | REVIEW_REQUIRED | NO_MATCH

    if decision == AUTO_SELECT and not dry_run:
        select -> read_populated_fields() -> verify match -> enter_quantity -> commit -> capture_evidence
    else:
        stop for review (see safety-stop rules)

    after human approval (record_resolution):
        apply CAT/SEL/description to the line item via review_service (audited)
        optionally save/update the registry mapping for future reuse
```

## Safety-stop rules

`orchestrator.execute_plan()` stops (never commits) when:

| Condition | `stop_reason` |
|---|---|
| Adapter can't verify the active Xactimate context | `xactimate_context_unverified` |
| No dropdown results returned | `no_results` |
| Dropdown capture raises `AdapterError` | `dropdown_extraction_failed` |
| Top candidate's score/margin/extraction-confidence insufficient | `ambiguous_candidates` |
| Top candidate has a hard conflict (wrong component/material/size/action/grade) | `hard_conflict` |
| Post-selection field read-back disagrees with the selected candidate | `populated_fields_mismatch` |
| Quantity is missing or `<= 0` | `unit_or_quantity_invalid` |

`AUTO_SELECT` additionally requires: score >= `auto_select_min` (0.88),
no hard conflict, `extraction_confidence >= min_extraction_confidence`
(0.80), **and** a `>= auto_select_margin` (0.08) lead over the runner-up
-- "Do not automatically choose the first dropdown result" is enforced by
construction (candidates are re-sorted by score, tie-broken deterministically
by row position, never assumed to already be in the right order).

`dry_run=True` (the CLI/UI default) runs the entire pipeline but never
calls `select_result` / `enter_quantity` / `commit_item`, regardless of
decision -- verified directly: `commit_item` is never invoked in any
dry-run test or the real-fixture dry-run demonstration below.

## Search phrase generation

Built from fixed ordered buckets (component, material, size, style/
grade, action) rather than lightly editing the original sentence --
deterministic, explainable (every kept/dropped term has a reason), and
fully reviewer-configurable via YAML.

```
"Remove and replace aluminum gutter/downspout up to 5 inches"
    -> "gutter aluminum up to 5"
    (component: gutter, material: aluminum, size: up to 5;
     style/action buckets empty -- compound action dropped by design)

"Tear off composition shingles - 3 tab (no haul off)"
    -> "3 tab composition shingles"      (see Phase 4.0 addendum: leading-style
                                           ordering fix changed this from
                                           "composition shingles 3 tab")

"R&R Window screen, 1 - 9 SF"
    -> "window 1-9"                     (numeric range preserved intact)

"Clean with pressure/chemical spray - siding"
    -> "siding pressure wash"           (a kept, meaningful action)

"Ice & water barrier"                   (no component/material keyword match)
    -> "ice water barrier"              (generic stopword-stripped fallback -- never empty)
```

Two real bugs were caught and fixed via direct testing against real line
items from `projects/*/mapping/normalized_estimate.json` before this was
considered done (not merely against the one worked example in the spec):

1. **Word-boundary collision**: a material keyword `"laminate"` matched
   as a plain substring inside `"laminated"` (a distinct, far more common
   roofing *style* word), producing corrupted phrases like `"shingles
   laminate laminated"`. Fixed by switching every keyword lookup
   (component/material/style) to `\bword\b` regex matching.
2. **Hyphen/dedup interaction**: naively treating every hyphen as a word
   separator (needed so `"3-tab"` and `"3 tab"` dedupe as the same term)
   also shredded genuine numeric ranges like `"1-9"` into `"1 9"`. Fixed
   by only treating a hyphen as a separator when it is *not* between two
   digits, and by canonicalizing range spacing (`"1 - 9"` / `"1 to 9"` ->
   `"1-9"`) at extraction time.

## Candidate ranking

Weighted-average scoring over **applicable** dimensions only (description
similarity, component match, material match, size match, action
compatibility, grade/style match, prior-verified-mapping bonus) -- a
dimension with no real signal to check (no size/grade wording, no prior
mapping) is excluded from the average entirely rather than diluted in at
a flat neutral score. This was a real calibration bug caught via testing:
the original design flat-averaged in neutral 0.5 defaults for every
inapplicable dimension, which capped the maximum achievable score at
~0.85 even for a textbook-perfect match (exact description + component +
material + action, no size/grade/prior-mapping signal available) --
making `auto_select_min` (0.88) structurally unreachable for the most
common real case. Fixed by renormalizing over only the dimensions that
were actually evaluated; reverified the same case then scored 1.0.

Hard conflicts (wrong component/material/size/action/grade) cap the
final score regardless of raw weighted total, mirroring
`mapping/scorer.py` and `selector_recommendation/scoring.py`'s existing
conflict-override pattern.

## Verified-catalog priority

Lookup order, exactly as specified:

1. internal registry exact-signature match (`status=approved`)
2. Phase 3.5 human-verified catalog compatibility match
3. description search -> rank -> human resolution
4. resolution saved (applied to the item + optionally the registry)
5. registry entry reused for future items with the same signature

Both trusted sources are checked before falling back to description
search; a registry hit and a verified-catalog hit are never conflated
(the verified-catalog case is represented with a `verified_catalog:`-
prefixed transient mapping_id, never persisted to the registry table).

## Manual workflow (usable today, no automation required)

Mapping Review -> select a line item -> "Xactimate Lookup" section:
shows the trusted mapping (if any) with an Apply button, or the
generated search phrase in a copyable code block with its reasoning, plus
a form to record what the reviewer found in their own Xactimate session
(CAT, SEL, description, unit, action, item number, evidence reference,
reason -- reason is mandatory and audited). "Apply this result" always
writes through `review_service` (never a direct file write); "also save
as a reusable mapping" is a separate checkbox, mirroring Phase 3.7's
Apply vs. Save-as-reusable-rule split.

Verified end-to-end with Streamlit's `AppTest` harness against a
disposable copy of a real project: filled in the form, clicked submit,
confirmed no exception, confirmed the target line item's category/
selector were updated through the real audited path, and confirmed a new
registry mapping was created.

## Files added

```
config/xactimate_lookup_phrase_rules.yaml
config/xactimate_lookup_ranking.yaml
src/estimate_extractor/xactimate_lookup/__init__.py
src/estimate_extractor/xactimate_lookup/models.py
src/estimate_extractor/xactimate_lookup/signature.py
src/estimate_extractor/xactimate_lookup/phrase_generator.py
src/estimate_extractor/xactimate_lookup/registry.py
src/estimate_extractor/xactimate_lookup/ranking.py
src/estimate_extractor/xactimate_lookup/adapter.py
src/estimate_extractor/xactimate_lookup/orchestrator.py
src/estimate_extractor/xactimate_lookup/service.py
src/estimate_extractor/ui/components/xactimate_lookup_panel.py
tests/unit/xactimate_lookup/ (conftest + 8 test files)
tests/integration/test_xactimate_lookup.py
docs/xactimate-lookup.md
```

## Files modified

- `src/estimate_extractor/cli.py` -- new `lookup` command group
  (`phrase`, `plan`, `record`, `list-mappings`, `disable`, `stats`,
  `dry-run`); `selectors search` gained the same eligible-only-default +
  `--include-uncertain` pattern already used elsewhere.
- `src/estimate_extractor/selector_recommendation/service.py` -- renamed
  `_real_ground_truth` to public `real_ground_truth` so this phase could
  reuse it without reaching into a private function (no behavior change).
- `src/estimate_extractor/ui/components/mapping_table.py` -- renders the
  new lookup panel below the Phase 3.7 recommendation panel.

## Known limitations

- No real `XactimateAdapter` implementation exists yet -- only
  `FakeXactimateAdapter`. Live desktop execution requires a future phase
  to implement the ABC against a real automation backend.
- Unit compatibility in ranking is a soft textual signal (Phase 3.6
  stores no structured unit); `DropdownResult` likewise carries no
  structured unit today.
- `review-required rate` / `no-match rate` / `description searches
  resolved` require dropdown data from an actual (or scripted) source --
  `lookup stats` reports them as `N/A` rather than fabricate them from
  planning alone; `lookup dry-run` (or the benchmark below) is how to get
  real numbers.

---

## Phase 4.0 addendum: adapter refinement, safety hardening, automation CLI

Phase 4.0 extended this same package in place -- no parallel system was
built. Every requirement in that build spec (registry fields, phrase
generation, ranking, lookup priority, adapter boundary, dry-run,
learning) was already implemented by Phase 3.8; the genuine gaps were an
adapter vocabulary mismatch, two unimplemented safety checks, a registry
schema gap, and richer CLI/UI presentation.

### Adapter interface refinement

`XactimateAdapter` methods were renamed/split to the more granular
vocabulary this phase specified, and a capability guard + recovery hook
were added:

| Before (3.8) | After (4.0) |
|---|---|
| `verify_active_context()` | `verify_application()` + `verify_project()` (separately checkable) |
| `focus_search_box()` / `clear_search_box()` | `focus_search()` / `clear_search()` |
| `enter_text(text)` | `search_by_description(phrase)` / `search_by_category_selector(cat, sel)` |
| `wait_for_dropdown_results()` + `capture_dropdown_results()` | `capture_dropdown()` (raw) + `parse_dropdown(raw)` (structured) |
| `select_result(result)` | `select_candidate(candidate)` |
| *(none)* | `recover()` -- best-effort return to a safe state after any stop/error |
| *(none)* | `supports_live_execution: bool` class attribute, default `False` |

`FakeXactimateAdapter.supports_live_execution` stays `False`; a real
adapter must explicitly set it `True`. `orchestrator.execute_plan()`
refuses `dry_run=False` against any adapter that doesn't declare it --
verified by a dedicated test that asserts the adapter is never even
touched (`adapter.log.calls == []`) before the refusal.

### New safety checks (real gaps, not just naming)

Two were genuinely unimplemented, found by reading `orchestrator.py`
directly rather than assuming: **`populated.unit` was read from the
adapter but never compared to the source item's unit.** Fixed --
`STOP_REASON_UNIT_MISMATCH` fires when both are present and disagree
(case-insensitive), and is skipped entirely when either side has no unit
signal (never a false block). Also added: `STOP_REASON_UNSUPPORTED_ADAPTER`
(see above) and `STOP_REASON_UNEXPECTED_DIALOG` (a new
`UnexpectedDialogError` adapter exception, always a hard stop, always
followed by `adapter.recover()`).

### Registry schema additions

Two columns added to `internal_mappings` (additive, no migration needed
-- no production data exists yet): `normalized_description` (a readable
rendering of trade/component/material/action, computed by
`signature.compute_normalized_description()`, distinct from both the
verbatim `source_description` and the bucketed `search_phrase`) and
`xactimate_activity_raw` (the raw observed Activity-column symbol/text,
copied verbatim like Phase 3.5's `activity_raw`/`activity_interpretation`
split -- never inferred, distinct from Phase 2's semantic `action`).

### Search-phrase generator: three more real bugs found via the spec's own new examples

Testing this phase's two new worked examples (not just the one already
validated in Phase 3.8) found three additional real bugs:

1. **`"Remove wet drywall 1/2\""` produced `"drywall"`** -- missing both
   the modifier "wet" and the size "1/2". Fixed by adding a
   `modifier_keywords` bucket (seeded with "wet", confirmed real and
   recurring in the WTR category: *"Remove wet ceiling tile & drywall and
   bag - Cat 3"*) and fixing the size regex to accept fractions
   (`\d+(?:[./]\d+)?`, not just integers/decimals) -- confirmed real:
   *"1/2° drywall - hung, taped..."*.
2. **The fraction-size fix immediately hit a second bug**: the unit-symbol
   size pattern's trailing `\b` could never match after a quote/apostrophe
   character (a non-word char followed by end-of-string is never a word
   boundary), silently dropping every symbol-unit size at the end of a
   description. Fixed by moving `\b` onto only the word-based unit
   alternatives.
3. **`"Replace laminated composition shingles"` produced `"composition
   shingles laminated"`**, not the expected `"laminated composition
   shingles"`. Investigated against the real Phase 3.6 catalog: type-
   qualifying style words genuinely **lead** real descriptions
   (*"Laminated - comp. shingle rfg."*, *"3 tab - 25 yr. - comp. shingle
   roofing"*), while grade words genuinely **trail** (*"Carpet - High
   grade"*, *"Tile floor covering - Premium grade"*). Fixed by splitting
   `style_keywords` into `leading_style_keywords` (3-tab, laminated --
   emitted before component) and `style_keywords` (grade -- emitted in
   its existing trailing position).
4. **Fixing #3 silently broke signature identity**: `signature.
   compute_grade_key()` only checked the (now grade-only) trailing list,
   so a "3-tab" item and a "laminated" item -- genuinely different
   products -- collapsed onto the same `grade_key` and therefore the same
   item signature. Caught by re-deriving both signatures immediately after
   the split and finding them equal. Fixed by having `compute_grade_key()`
   check both lists; a dedicated regression test (`test_signature_
   distinguishes_leading_style_words`) now guards this permanently.

All four fixes were re-verified against the full real 218-item corpus
(all 7 local projects) after landing: zero empty phrases, zero duplicate
words, both this phase's new worked examples and Phase 3.8's original
five examples all still exact-match.

### CLI additions

```
python -m estimate_extractor lookup registry [--active-only/--all] [--category CAT]
python -m estimate_extractor automation plan projects/<project>/mapping/mapped_estimate.json
python -m estimate_extractor automation dry-run projects/<project>/mapping/mapped_estimate.json [--dropdown-script FILE.json]
python -m estimate_extractor automation diagnostics
```

`lookup registry` is a full-detail per-mapping browser (signature,
normalized description, raw activity, evidence, timestamps) -- `lookup
list-mappings` (Phase 3.8) stays as the terse one-line-per-mapping view;
neither duplicates the other's presentation depth. `automation plan`
never touches an adapter (pure description of what a run would do);
`automation dry-run` reuses the exact same `dry_run_for_project()`/
`orchestrator.execute_plan()` machinery as `lookup dry-run`, just with
per-candidate score/reason detail in the output. `automation
diagnostics` is read-only and never attempts to launch or connect to
Xactimate -- with no real adapter configured, it says so explicitly.

### UI additions

The lookup panel now always shows (regardless of trusted vs.
description-search path): a **Lookup method** badge, **Automation
readiness** signal, cached search phrase, cached CAT/SEL, and a
**Ranking explanation** (for a trusted mapping: its usage/success/
rejection counts and who approved it; for description search: an
explicit note that no live session is connected, so nothing can be
ranked automatically yet). The manual capture form gained a raw-activity-
symbol field feeding the new `xactimate_activity_raw` column. Verified
end-to-end with `AppTest` against a disposable project copy: metrics
render, the raw-activity field round-trips into the registry correctly.

### Real-fixture evaluation (Phase 4.0, 7 real local projects, 218 line items)

Static planning (`lookup stats`, fresh registry): identical to Phase
3.8's baseline -- 218 items, 0 resolved by mapping (registry starts
empty), ground truth `N/A` (all approvals synthetic, correctly excluded).
`automation diagnostics` against the default (Fake) adapter: application/
project verification both report `True` (Fake defaults), `supports_live_
execution=False`, two explicit warnings that no real adapter is
configured -- exactly the intended "nothing fabricated" posture.

### Known limitations (updated)

- Still no real `XactimateAdapter` -- the interface is now more granular
  and capability-gated, but implementing it against a live Windows
  desktop remains future work.
- `modifier_keywords` is seeded with one confirmed-real entry ("wet");
  extending it for other trades (e.g. water-damage-adjacent qualifiers in
  non-WTR categories) is a config-only change, no code required.
- `unit_mismatch` only fires when the adapter actually reports a
  populated unit; no current adapter (Fake or otherwise) reliably does
  this yet since Phase 3.6's catalog carries no structured unit -- a real
  adapter reading Xactimate's own UI would be the first genuine source.

---

## Phase 4.1 attempt: real WindowsXactimateAdapter -- blocked at inspect-only diagnostics

Phase 4.1 set out to implement `WindowsXactimateAdapter(XactimateAdapter)`
against a real, running Xactimate desktop session (a disposable "TEST"
project was already open). Per the build spec's own gating rule, work
stopped at the first milestone ("inspect-only diagnostics... do not
proceed to live interaction until inspect-only diagnostics are
reviewed") because that milestone could not be completed: **the
application's real controls are not exposed through Windows UI
Automation at all.** No adapter code was written -- there was nothing
real to implement it against yet, and writing one anyway (e.g. against
screen coordinates) would have required guessing past a diagnostic
finding, which the build spec explicitly rules out ("never fabricate
success").

### What was confirmed running

- Xactimate process: `Xactimate online Estimate Writer-G0.exe`, a
  ClickOnce deployment (`%LocalAppData%\Apps\2.0\...`), window title
  `TEST` -- the disposable test project was open on the "Estimate Items"
  screen (search box, "Quick Entry" panel with Cat/Sel/Act/Desc/Calc/
  Unit fields, OK/Cancel, and a results grid with one existing line item
  visible in a milestone-1 screenshot).
- Loaded modules confirm a pure .NET Core WPF application --
  `coreclr.dll`, `PresentationFramework.dll`, `PresentationCore.dll`,
  `MaterialDesignThemes.Wpf.dll` (a third-party WPF control/theming
  library). No CEF/Chromium/WebView/Electron module was present, which
  rules out "it's actually a hosted browser view with no accessibility
  bridge" as the explanation.

### The blocker

Three independent checks, all against the live PID/HWND, all read-only,
all confirming the same thing:

1. Raw Win32 `EnumChildWindows` on the main HWND: **0 native child
   windows** (expected for WPF, which renders through DirectX inside a
   single `HwndWrapper`-classed host window -- not itself the problem).
2. Direct `comtypes` `IUIAutomation` calls (bypassing `pywinauto`
   entirely, to rule out a wrapper-library bug) using **all three**
   standard tree views -- `RawViewWalker`, `ControlViewWalker`,
   `ContentViewWalker` -- from `ElementFromHandle`: each returned **only
   the root window element itself**, no descendants.
3. Polled the raw tree once per second for 8 seconds *after* explicitly
   forcing the window to genuine OS foreground (`SetForegroundWindow` +
   `ShowWindow(SW_RESTORE)`, confirmed via `GetForegroundWindow`) to rule
   out a focus/timing race: **stable at 7 total nodes**, none of which
   were the search box, Quick Entry fields, grid, or OK/Cancel buttons.

The only thing UI Automation exposed at all was a single stray `Popup` ->
`ScrollViewer` (`PART_ScrollViewer`) -> `ListBoxItem` -> `TextBlock`
reading `"R&R Gutter - aluminum - up to 5\""`, orphaned in the tree
(not attached to anything the user could currently see). Structurally
this is exactly what one row of an autocomplete/search-results dropdown
looks like -- suggesting dropdown popups may get real automation peers
even though the static chrome around them does not. This was not
followed up further (would have required triggering a real dropdown,
i.e. live interaction, past the point where diagnostics were still
passing).

### Working hypothesis (unconfirmed)

Self-contained, trimmed .NET (Core) WPF publishes are a documented
source of exactly this symptom: IL trimming can strip the
reflection-discovered `OnCreateAutomationPeer` overrides most WPF
control types rely on, while a small set of core primitives Microsoft
explicitly roots against trimming (`Popup`, `ScrollViewer`,
`ListBoxItem`, `TextBlock`, `ScrollBar` -- exactly the types that *did*
show up here) survive. Xactimate's ClickOnce packaging is consistent
with a trimmed self-contained publish. This was not independently
confirmed (no access to Xactimate's build configuration) and is offered
as the most likely explanation, not a verified root cause.

### What would need to happen before Phase 4.1 can resume

- Independent confirmation with a reference tool (Microsoft's
  `Inspect.exe` from the Windows SDK, or Accessibility Insights for
  Windows) to rule out any remaining possibility that this is specific
  to the `comtypes`/`pywinauto` path used here.
- Either a fix on the Xactimate/Windows side (an accessibility-mode
  setting, an untrimmed build, an OS-level assistive-technology flag
  that changes trimming/peer behavior) or an explicit decision from the
  team to pursue a non-UIA automation strategy (e.g. coordinate- or
  image-based interaction). The latter is a materially different,
  more fragile approach than "Windows UI Automation, pywinauto, or
  another appropriate local Windows accessibility API" and was
  intentionally not attempted without that decision being made
  explicitly by a human, per this build spec's "never fabricate
  success" / "do not proceed... until diagnostics are reviewed" rules.

---

## Phase 4.2 attempt: keyboard/vision fallback investigation -- stopped before a validated design, no adapter code written

Phase 4.2 picked up exactly where 4.1 stopped: with UIA and MSAA both
confirming zero accessible controls, the team explicitly authorized
investigating a layered keyboard + window-relative-vision fallback
against the same live "TEST" project, per a detailed staged spec
(reconfirm accessibility boundary -> keyboard discovery -> geometry/
anchors -> search-box experiment -> dropdown OCR -> assisted selection
-> populated-field reading -> quantity -> single commit). The
investigation went as far as safely typing into and submitting the real
search box, then stopped -- on the user's explicit decision -- once it
produced one unintended data mutation and surfaced enough reliability
problems to warrant a checkpoint before continuing into the
higher-risk stages (selection, quantity entry, commit). **No adapter
code, CLI changes, or config profile were written** -- the core
mechanism this whole approach depends on (safely previewing multiple
search candidates without side effects) was never validated, and
writing an adapter around an unvalidated, already-known-unsafe
mechanism would itself have been a fabrication.

### Stage 1 (reconfirmed): accessibility boundary

Re-ran the Phase 4.1 UIA checks (unchanged result) and added a fresh,
independent check: legacy MSAA (`IAccessible`, via
`AccessibleObjectFromWindow` + `AccessibleChildren` -- a completely
different OS accessibility bridge from UI Automation) against the same
window. Result: **1 node, 0 children** -- the same boundary, confirmed
by a second, independent API. Attempted to install Microsoft's
Accessibility Insights for Windows for a third, fully independent
confirmation; the official MSI (downloaded from the genuine GitHub
Releases asset behind `aka.ms/accessibilityinsights-windows/download`,
hash-verified by winget for a second install attempt) could not be
installed because the required UAC elevation prompt cannot be answered
from a non-interactive automation shell (confirmed via session-ID
inspection: the elevation dialog has no interactive desktop to render
into from this context). The user chose to proceed on the two
independent confirmations already in hand rather than complete the
manual install.

### Stage 3 (done before Stage 2, for safety): window geometry and anchors

Built a client-area-relative geometry/screenshot system: `GetClientRect`
+ `ClientToScreen` for a DPI-correct client origin, `GetDpiForWindow`
(96 / 100% scale in this environment), and `PrintWindow(..., 
PW_RENDERFULLCONTENT)` for reliable client-area capture of a
GPU-rendered WPF surface (plain `BitBlt` is unreliable for this kind of
content). Reordered ahead of Stage 2's keyboard discovery deliberately:
blindly Tab-navigating an unknown, real-data-bearing app with zero
accessibility feedback to confirm what has focus was judged too risky
without a safe, verified way to establish a known starting focus state
first.

Visually mapped anchors for the Estimate Items screen (search box,
search button, "Items Search Results" link, Quick Entry Cat/Sel/Act/
Desc/Calc/unit fields, OK/Cancel, grid header/rows) as client-relative
pixel rects. **These anchors were not stable across interactions**: after
one interaction, the content pane's internal scroll position shifted
(the "Estimate Items" title and Search/Saved-state header row scrolled
off the top of the visible client area entirely), invalidating the
cached anchor coordinates. No mechanism to reliably reset scroll
position to a known state was identified before the investigation
stopped. This is the anchor-stability failure mode the build spec asked
to watch for.

### Stage 2/4: keyboard navigation and search-box interaction

**Foreground-focus was unreliable and needed a workaround.** Plain
`SetForegroundWindow` silently failed (returned without visibly
changing focus) when called from this automation context with no
recent real user input -- a Windows anti-annoyance protection
(`LockSetForegroundWindow`), not anything specific to Xactimate. Worked
around with the documented `AttachThreadInput` technique plus a
minimize/restore fallback; this combination did work once. Separately,
mid-investigation, the OS foreground window was found to be a fullscreen
game (Overwatch) that no amount of `AttachThreadInput`/restore trickery
could override -- the user switched to Xactimate manually rather than
have the agent force it, which is the right call (forcibly stealing
focus from a fullscreen exclusive application from an automation script
is not something to do unilaterally).

With focus reliably established and the correct project/screen
identity confirmed via calibrated OCR crops (`pytesseract` 5.4.0,
installed via `winget install UB-Mannheim.TesseractOCR` -- the only
successful unattended install path found; `tesseract-ocr.tesseract` and
a user-scoped install both reported "No applicable installer found"),
a single verified anchor-click into the search box plus
`pywinauto.keyboard.send_keys` (`SendInput`-based, works regardless of
UIA visibility since it targets whatever control natively has OS
keyboard focus) reliably: focused the box, cleared it (`Ctrl+A` +
`Delete`), and typed the test phrase `gutter aluminum up to 5`,
visually confirmed correct in a full-client screenshot.

**No live autocomplete dropdown appears from typing alone** -- contrary
to the build spec's assumption of a combobox-style live dropdown, this
screen's search is submit-based: nothing happens until the "Search"
button is clicked (or, presumably, Enter is pressed, not tested). Only
one full trial of this sequence was completed (not the five requested
for repeatability) before the investigation moved to the next question
and then stopped; the single trial succeeded cleanly.

### Stage 5 (attempted, caused the stopping point): dropdown/results observation

This is where the investigation found its most important result.
Clicking "Search" produced a brief "Loading" WPF popup (itself
UIA-visible, interestingly -- transient popups continue to be the one
UI-Automation-visible element class in this app, consistent with the
Phase 4.1 finding) and then settled back to the same "Items Search
Results (100 Matching Items)" summary line with no visible row-level
detail. **Clicking that "Items Search Results" link -- the only
apparent way to see individual candidate rows -- did not open a picker.
It silently added the top/default catalog match directly to the
estimate grid as a new, real line item** (`#340`, "Gutter / downspout -
galvanized - up to 5"", quantity 0, `$0.00` RCV), flipping the document
from "Saved" to "Unsaved changes". This is exactly the kind of
unintended, silent mutation the build spec's hard constraints exist to
prevent ("do not fabricate success," "do not assume the first dropdown
result is correct"), and it happened despite deliberate, verified,
window-relative anchoring -- the click itself landed exactly where
intended; the *control's behavior* was the surprise, not the aim.

The row was removed manually by the user directly in Xactimate (who, in
the same pass, also removed the project's other pre-existing line item)
rather than through any automated recovery path -- `recover()` was
never exercised programmatically. No safe, non-mutating way to preview
multiple search candidates was identified before stopping. It's
plausible one exists (a dropdown affordance different from the one
tried, a keyboard-only path, a different button) but this was not
found within the scope of this session.

### Why the investigation stopped here

Per the user's explicit decision, given three compounding reliability
findings inside a single short session -- an OS-level focus-control
problem needing a workaround, one confirmed unintended data mutation,
and an anchor-stability failure from scroll drift -- continuing into
Stages 6-9 (assisted selection, populated-field reading, quantity entry,
single commit), each of which depends on first solving the still-open
Stage 5 problem and each of which carries equal or greater mutation
risk, was not justified without first resolving how to preview results
safely. This matches the build spec's own instruction: "If the
investigation proves that keyboard/vision automation is not reliable
enough, stop honestly and report the evidence rather than forcing a
fragile implementation." The investigation does not prove the approach
is *impossible* -- the mechanical building blocks (focus, typing, OCR
identity verification) all worked -- but it is clearly not yet reliable
enough to build against, and the one unresolved question (safe results
preview) is exactly the load-bearing one.

### What would need to happen before Phase 4.2 can resume

- A human, working directly in Xactimate (not through automation),
  needs to find the actual intended mechanism for previewing multiple
  search candidates without committing one to the estimate -- the
  "Items Search Results" link is confirmed *not* that mechanism.
- Once that mechanism is known, the anchor-stability (scroll-drift) and
  foreground-focus problems both have known, testable mitigations
  (re-derive anchors from a fresh screenshot before every action rather
  than caching them; the `AttachThreadInput` workaround for focus) that
  weren't yet proven across a full end-to-end flow.
- Only after a genuinely safe, repeatable results-preview mechanism
  exists do Stages 6-9 (selection, populated-field verification,
  quantity, single disposable commit) become worth attempting -- and
  `supports_live_execution` must stay `False` until all of them succeed
  end-to-end, per the existing pilot gate.

### Environment note

`pytesseract`'s binary dependency (Tesseract OCR 5.4.0, UB-Mannheim
build) was installed system-wide via `winget` during this session and
is available at `C:\Program Files\Tesseract-OCR\tesseract.exe`. Like
Phase 4.1's `pywinauto`/`comtypes`/`pywin32`, this was not added to
`requirements*.txt` -- no adapter code exists yet to depend on it.

---

## Phase 4.2B: the safe results-chooser mechanism, found -- full pilot gate passed, no adapter code written yet

Phase 4.2B picked up exactly where 4.2 stopped, investigating -- with a
live human demonstration first, per the build spec's own "do not
automate this stage" instruction -- exactly how Xactimate's search
results chooser works, since 4.2 had confirmed a real (recoverable)
mutation from guessing at it. This session found the real mechanism,
validated every stage of the pilot gate end-to-end against the live
"TEST" project, and successfully committed and verified one disposable
line item. **No adapter code was written** -- per explicit user
direction, this session ends with a validated mechanism and a
recommendation, not an implementation.

### Three more real (recovered) mutations happened during this session

Full honesty, matching this whole project's "never fabricate success"
rule: three more unintended line-item additions happened before the
mechanism was understood, on top of Phase 4.2's one. All were caused by
the same underlying blind spot (below), not by anything fundamentally
different each time, and all were manually recovered by the user:

1. A script click at cached "search box" coordinates landed on a
   dropdown row that was open but invisible to the capture method being
   used at the time.
2. The same failure mode recurred when the agent reused a multi-step
   script (click+clear+type+diagnose) instead of following the
   single-action-with-verification discipline it had just committed to
   -- a process failure, not a new technical finding, and explicitly
   owned as such in the moment.
3. (Counted with Phase 4.2's original finding.) The user's own live
   demonstration of the real chooser also added two items in the
   course of showing the agent how selection actually works -- expected
   and informative, not a failure.

### The root cause of every capture-related mutation: the dropdown is a separate top-level window

`PrintWindow` against the main Xactimate HWND -- the capture method
used throughout Phase 4.1 and the first two-thirds of 4.2 -- **cannot
see the results dropdown at all**, because it is not part of the main
window's own rendered surface; it is a separate, undecorated, owned
top-level `HwndWrapper`-classed popup window. Every prior "nothing
appeared" screenshot in this whole investigation was potentially a
false negative. Confirmed by capturing the full virtual desktop
(`PIL.ImageGrab.grab(all_screens=True)`) instead of the single window,
which shows the dropdown perfectly. This single finding retroactively
explains every mutation in both 4.2 and 4.2B: the agent wasn't clicking
blindly into empty space, it was clicking blindly into space that
*looked* empty because the capture method had a blind spot.

Once known, the dropdown's owning `HWND` is trivially found by
enumerating top-level windows owned by the Xactimate process ID and
filtering for an unnamed, visible `HwndWrapper`-classed window that
isn't the main window -- distinguishing it from the also-present
`Loading` popup and the main window itself.

### The real trigger mechanism: `keybd_event`, not `SendInput`

Phase 4.2 established that `SendInput`-based typing (`pywinauto`'s
default) never triggers the live dropdown, no matter the pacing or
wait time, while genuine keyboard input does. Phase 4.2B found the
actual fix: typing via the older, distinct Win32 `keybd_event` API
(per-character `keybd_event(vk, 0, 0, 0)` / `keybd_event(vk, 0,
KEYEVENTF_KEYUP, 0)` pairs, ~0.1s apart) triggers the dropdown
reliably. The exact reason `keybd_event` succeeds where `SendInput`
fails was not root-caused (both are legitimate, real Win32 input
APIs -- this is not a "detection evasion" technique, simply a
discovery that this particular app's live-search binding responds to
one standard input-injection API and not another). Confirmed **5/5**
clean trials: dropdown appears with all 10 expected rows every time,
`{ESC}` closes it every time, zero grid mutations across all 5.

Two other candidate triggers were tested and ruled out: the small
dropdown-arrow toggle next to the search box opens a *different*,
single-item *search-history* popup (not the live results list); and
clicking "Search" submits the query and updates the results-count text
but does not open any inline picker. Neither is the mechanism the user
demonstrated.

### The dropdown is a fully UIA-accessible standard WPF `ListBox` -- OCR is unnecessary

This is the biggest surprise of the whole Phase 4.1/4.2/4.2B arc.
Despite the *static* application chrome having zero UIA peers (Phase
4.1's core finding, unchanged), the *dynamic* results popup is a
completely normal, fully-populated automation tree:

```
Popup
  ScrollViewer (auto_id=PART_ScrollViewer)
    ListBoxItem  (name = full description, e.g. 'Gutter / downspout - aluminum - up to 5"')
      TextBlock  -- CAT/SEL code, e.g. "SFGGUTA"
      TextBlock  -- description (same text as the ListBoxItem's own name)
      TextBlock  -- price, e.g. "$11.56"
    ... one ListBoxItem per visible row (10 for this query) ...
    ScrollBar (auto_id=PART_VerticalScrollBar)
    ScrollBar (auto_id=PART_HorizontalScrollBar)
```

Every row's CAT/SEL, description, and price are read as exact UIA
`Name` text properties -- no OCR, no ambiguity, no confidence scoring
needed for extraction itself (ranking still applies its own scoring on
top, per the existing `ranking.py`, unchanged). Each `ListBoxItem` also
exposes a live, always-current `CurrentBoundingRectangle` in screen
coordinates.

Pattern support checked directly: `InvokePattern`, `SelectionItemPattern`,
and `ScrollItemPattern` are all unsupported on these rows.
`LegacyIAccessiblePattern` **is** supported, but calling its
`DoDefaultAction()` was tested and found to be a **safe no-op** here --
it does not select or add the row (confirmed: grid stayed at 0 items
after invoking it). Real selection requires an actual mouse click --
but critically, that click can be aimed at the *live UIA-reported
rectangle's center* (fetched fresh, immediately before clicking) rather
than any cached/guessed pixel position. This eliminates the scroll-drift
and stale-anchor failure modes that caused every mutation in this
session and the prior one: **UIA supplies verified-correct geometry;
a real click performs the verified-correct action.**

### Full pilot-gate validation, end to end, on the live TEST project

With the above mechanism, every remaining pilot-gate milestone was
run for real and passed:

1. **Search entry**: `keybd_event`-typed the user-confirmed exact
   phrase (`"Gutter / downspout - aluminum - up to 5"`) into the search
   box (focused via one verified anchor click). Confirmed correct via
   screenshot each time.
2. **Dropdown capture**: 5/5 clean trials (above).
3. **Candidate parsing matches the visible UI**: the UIA-read rows were
   compared directly against the same rows visible in screenshots and
   in the user's own demonstration -- exact match, every field.
4. **Assisted selection populates the correct fields**: clicked the
   live UIA rectangle's center for the row matching CAT/SEL `SFGGUTA`
   (the exact-phrase match, $11.56/LF). Result: a new grid row (#346)
   with **Cat=SFG, Sel=GUTA, Act=&, Description="Gutter / downspout -
   aluminum - up to 5\"", Unit=LF** -- exactly the intended candidate,
   not a neighboring row (contrast with every prior coordinate-based
   attempt in 4.2, which landed on the wrong row).
5. **Quantity entry + read-back**: clicking the grid row's own
   `Quantity` cell directly (not the "Quick Entry" panel at the top of
   the screen, which turned out to be a separate "create new item" form
   that desyncs from an already-added grid row) put it into an
   editable state; `keybd_event`-typed `"10"`; committed with `Tab`
   (deliberately not `Enter`, consistent with the user's "the enter
   button should not be pressed" guidance from Phase 4.2). Read back
   as `Quantity: 10`, `RCV: $115.60` (10 x $11.56, correct), matching
   both the grid and the Quick Entry panel, which re-synced correctly
   once the row was selected.
6. **Single disposable commit + verification**: `Ctrl+S` (a plain,
   standard shortcut -- no auto-save was observed on any fixed idle
   timer) flipped the status indicator from "Unsaved changes" to
   "Saved". Verified the committed row's every field matches what was
   entered.
7. **No safety stop bypassed**: every abort condition written into this
   session's scripts (dropdown not found, target CAT/SEL missing from
   the parsed rows, window not confirmed foreground) was real and
   would have halted the flow; none were needed on the final successful
   run, but were exercised and confirmed working earlier in the session
   (e.g. the window-not-foreground stop, when a fullscreen game had OS
   focus).
8. **Evidence captured throughout**: a full-resolution screenshot was
   saved after every state-changing action this session (session-local,
   not committed to the repo).

### Known gaps -- what full pilot-gate completion does *not* yet mean

- The full destructive path (select -> quantity -> commit) was run
  successfully **once**, not five times -- unlike the read-only path,
  which has 5/5. Repeating the full flow (ideally against several
  different phrases/CAT-SEL targets, including at least one genuinely
  ambiguous phrase and one no-result phrase, per the original build
  spec's Stage 5/9) has not been done.
- The 10-item controlled assisted pilot described in the original
  build spec (Phase 4.2's "Pilot gate" section) has not been run.
- `keybd_event`'s reliability was only tested against this one search
  box, this one query, in this one window state. Whether it behaves
  identically for CAT/SEL-direct search (the trusted-mapping fast
  path), for queries that produce zero results, or after the window has
  been resized/moved/reopened, is untested.
- The quantity-cell interaction (click cell -> type -> Tab) was
  exercised once, with one integer value (10). Decimal quantities and
  the `unit_or_quantity_invalid` stop condition were not tested this
  session.
- `supports_live_execution` remains unset because **no adapter code
  exists yet** -- this was a mechanism-validation session, not an
  implementation session, per explicit user direction to write up
  findings before writing any adapter code.

### Recommendation

Building `WindowsXactimateAdapter` (or a more accurately-named
`WindowsHybridXactimateAdapter`, given it combines UIA reads with
`keybd_event` input and UIA-verified-coordinate clicks -- not a pure
accessibility-API adapter, and not a pure vision/OCR adapter either) is
now well-supported by evidence, not speculation. The mechanism for
every method on the `XactimateAdapter` ABC has a validated real-world
implementation path:

| Method | Validated approach |
|---|---|
| `verify_application` / `verify_project` | window enumeration + foreground check (Phase 4.2) + OCR/UIA identity check |
| `focus_search` / `clear_search` | one verified anchor click + `Ctrl+A`/`Delete` |
| `search_by_description` | `keybd_event`-based typing (not `SendInput`) |
| `search_by_category_selector` | untested this session -- same box, presumably same mechanism, needs a direct test |
| `capture_dropdown` / `parse_dropdown` | enumerate owned windows for the popup, walk its UIA tree -- no OCR |
| `select_candidate` | click the live UIA `CurrentBoundingRectangle` center for the matched row |
| `read_populated_fields` | read the grid row's / Quick Entry panel's UIA-absent but OCR-or-pixel-diff-readable cells (still needs definition -- the grid itself has the same zero-UIA-peer problem as the rest of the static chrome) |
| `enter_quantity` | click the grid's own Quantity cell, `keybd_event`-type, commit with `Tab` |
| `commit_item` | `Ctrl+S` |
| `capture_evidence` | screenshot, as done throughout this session |
| `recover` | `{ESC}`, confirmed safe and reliable across every trial this session |

Before writing that adapter for real, the gaps above (repeatability of
the destructive path, the 10-item pilot, decimal quantities, CAT/SEL
direct search, `read_populated_fields`'s own capture strategy for the
grid) should be closed -- but there is no longer a fundamental
feasibility question. The next session should either close those gaps
first, or begin implementation with `supports_live_execution` staying
`False` until they're closed, per the existing pilot-gate rule.

---

## Phase 4.3: WindowsXactimateAdapter implemented -- partial pilot gate passed, `supports_live_execution` stays `False`

Phase 4.3 implemented `WindowsXactimateAdapter(XactimateAdapter)` for
real (`src/estimate_extractor/xactimate_lookup/windows_adapter.py`) and
ran it against the live TEST project, dogfooding the actual adapter
class throughout rather than one-off scratch scripts. It closed several
of the gaps Phase 4.2B left open (5/5 assisted-selection repeatability,
decimal quantities, CAT/SEL direct search, one full verified commit)
and found -- and fixed -- nine real, previously-unknown bugs in the
process. It did **not** complete the full 10-item pilot the original
build spec's pilot gate requires, and found real, still-unresolved OCR
fragility in secondary populated-field reads. Per the pilot gate's own
rule ("Otherwise leave it False and report the exact blockers"),
`supports_live_execution` stays `False`.

### Files added / modified

- Added: `src/estimate_extractor/xactimate_lookup/windows_adapter.py`
  -- the adapter itself (`WindowsXactimateAdapter`, `StaleCandidateError`,
  `PopupNotFoundError`, `_RawDropdownRow`, `_split_category_selector`).
  All Windows-only imports (`ctypes`, `win32gui`, `win32ui`, `comtypes`,
  `pywinauto`, `pytesseract`) are lazy, so importing this module never
  requires Windows.
- Added: `tests/unit/xactimate_lookup/test_windows_adapter.py` -- 19
  tests covering everything testable without a live Windows/Xactimate
  session (module import safety, category/selector splitting,
  `parse_dropdown()`'s pure transformation logic, `verify_application`/
  `verify_project`/`capture_dropdown`/`select_candidate`/diagnostics
  behavior via an injectable `window_finder`, and an orchestrator
  integration test proving `dry_run=False` never touches the adapter
  when `supports_live_execution` is `False`).
- No changes to `orchestrator.py`, `ranking.py`, `registry.py`,
  `service.py`, `models.py`, `cli.py`, or any config file -- the
  adapter is a pure `XactimateAdapter` implementation, per the build
  spec's "do not redesign the existing architecture."

### The validated mechanism, as actually implemented

- **Window discovery**: enumerates top-level windows, matching on the
  literal substring `"Xactimate online Estimate Writer"` that appears
  in every window this app owns (main window, results popup, the
  transient "Loading" overlay) -- no PID/process-name lookup needed.
  The main window is the one with a non-empty title that isn't
  "Loading"; `verify_project()` compares that title against the
  adapter's configured `expected_project_name`.
- **Foreground**: `AttachThreadInput` + minimize/restore fallback
  (Phase 4.2's finding, unchanged).
- **Typing**: legacy `keybd_event`, not `SendInput` (Phase 4.2B's
  finding, unchanged) -- used for the search box, the grid's Quantity
  cell, and CAT/SEL direct-search queries alike.
- **`capture_dropdown()` / `parse_dropdown()`**: finds the separate
  top-level popup window fresh every call (never a cached handle),
  walks its UI Automation tree once, and returns raw rows carrying the
  popup's own HWND and each row's capture-time rectangle. `parse_dropdown()`
  is a pure function -- splits each row's CAT/SEL code into category
  (first 3 characters) / selector (the rest), sets
  `extraction_confidence=1.0` (exact UI Automation text, never OCR),
  and never sets a fabricated `item_number`.
- **`select_candidate()`**: re-verifies the popup handle is still a
  valid window, re-walks it fresh, confirms the candidate's exact
  CAT/SEL text still matches a live row (raising `StaleCandidateError`
  if not), re-reads *that* row's `CurrentBoundingRectangle` immediately
  before clicking (never the rectangle captured during `parse_dropdown()`),
  and clicks its live center. After clicking, checks for the
  **"Duplicate Item(s)" modal** (see below) and raises
  `UnexpectedDialogError` if present -- never dismisses it itself,
  per the existing contract ("orchestrator.py never attempts to
  dismiss... an unexpected dialog itself").
- **`read_populated_fields()`**: reads the grid's own most-recently-added
  row directly via OCR (self-locating via the same `'Cat:'`-anchored
  offset used everywhere in this adapter), not the "Quick Entry" panel
  -- see "A real, live-discovered design deviation" below. Takes three
  independent fresh reads and returns the per-field majority vote.
- **`enter_quantity()` / `read_quantity()`**: clicks the grid row's own
  Quantity cell directly, types via `keybd_event`, commits with `Tab`
  (never `Enter`, per the Phase 4.2 finding re-confirmed here), and
  reads back via an *upscaled* (4x) OCR crop -- see the decimal-point
  bug below.
- **`commit_item()`**: `Ctrl+S`.
- **`cancel_current_item()`**: right-click the row -> click "Delete" in
  the resulting context menu, at an empirically-measured offset from
  the right-click point -- see the cancel-mechanism bug below. Not
  part of the abstract contract; used by the non-destructive/
  assisted-selection trials to remove a row without ever calling
  `commit_item()`.
- **`recover()`**: best-effort `close_transient_dialogs()` (Escape if
  an unexpected window is present) + a plain Escape, then clears
  internal state. Never raises.

### Nine real bugs found and fixed this session (all via live comparison against actual screenshots, not assumed away)

1. **`cancel_current_item()`'s original implementation was a false
   success** -- it clicked the row's `#` cell and pressed Delete,
   which live testing proved **does not delete a grid row at all**,
   and a bug in its own row-count verification let it report success
   without checking correctly. Caught by manually re-screenshotting
   after a "successful" cancel and finding the row still present. Root
   cause fixed two ways: (a) discovered the real mechanism is the
   row's right-click context menu's "Delete" item (a rich menu also
   offering Cut/Copy/Paste/Resequence Line Numbers/etc.); (b) the
   post-delete verification's wait was too short (0.5s), independently
   causing a false-negative failure even once the real mechanism was
   used -- fixed by extending it to 1.2s.
2. **A real "Duplicate Item(s)" modal dialog** appears when
   `select_candidate()` targets a CAT/SEL that already exists in the
   active group ("SFG GUTA already exists in UTILITY_ROO2, Continue?",
   Yes/No) -- not documented in any prior phase, found by accident
   when a leftover row from bug #1's investigation caused a second
   selection attempt to hit it. `select_candidate()` now detects and
   raises `UnexpectedDialogError` rather than silently proceeding.
   Dismissing it by clicking "No" directly was confirmed live; whether
   `recover()`'s Escape keypress maps to the same Cancel-equivalent
   choice on this specific dialog was **not** independently
   re-verified this session (both dismissal attempts that used Escape
   also happened to follow a "No" click already having closed it,
   confounding the test) -- flagged honestly as unverified rather than
   assumed.
3. **`_locate_label()`'s single-match assumption was wrong.** The grid
   has its own "Cat" column header -- a second, real match for the
   `"Cat:"` needle used to compute the scroll-drift-correcting anchor
   offset. Tesseract's default page-segmentation mode (PSM 3)
   intermittently failed to detect the Quick Entry panel's "Cat:" label
   at all in some captured frames (confirmed by testing PSM 3/4/6/11/12
   against the same saved screenshot: only 11 and 12 found both
   occurrences), so a first-match implementation sometimes silently
   anchored on the grid header instead of Quick Entry, producing a
   wildly wrong offset. Fixed by switching to PSM 11 (sparse text) and
   explicitly sorting matches by position (topmost wins) instead of
   trusting OCR/dict iteration order.
4. **Row-position calculation used the wrong reference point.**
   `_last_row_geometry()` computed the first data row's position from
   the grid header's bottom edge, but live measurement found a real
   ~9px gap between the header and the first row that this silently
   ignored, misaligning every cell crop by that amount. Fixed by
   anchoring on the (already separately calibrated) `grid_row_1`
   anchor's own top edge instead.
5. **Column boundaries didn't account for a "Notes" column.** The grid
   header is `# | Cat | Sel | Act | Notes | Description | ...` -- an
   earlier version of the column table skipped straight from Act to
   Description, so the activity crop bled into a small calendar-icon
   glyph in the Notes column and the description crop's left edge was
   miscalibrated. Fixed via direct OCR word-bounding-box measurement
   against a real row.
6. **Column-separator gridlines occasionally OCR'd as a stray `"|"`
   character**, corrupting `category` (`"SFG"` -> `"SFG |"`) and
   `selector` (`"GUTA"` -> `"SUTA"`, the gridline pixel apparently
   perturbing which glyph Tesseract resolved the leading character to)
   read-backs. Fixed by tightening each column's crop to stay clear of
   the gridline pixels on both sides, measured precisely via OCR
   word-bounding boxes rather than eyeballed.
7. **The Quantity column's crop boundary started mid-character.** A
   live measurement found the grid's `Quantity` value renders at a
   different x-position than originally assumed (and distinct from a
   same-valued `Calc` column immediately to its left showing the same
   number) -- the original crop clipped the leading digit. Fixed with
   boundaries measured directly from OCR word positions.
8. **A decimal point can be dropped entirely by OCR at native
   crop resolution** -- `"2.5"` read back as `"25"` with the period
   simply absent from the OCR text, not a misread character a regex
   could patch. Confirmed the period was visually present (a saved,
   4x-zoomed crop was clearly legible to the eye) but sub-pixel at
   capture resolution for Tesseract. Fixed by upscaling the quantity
   crop 4x before OCR -- a standard mitigation for exactly this
   failure mode, confirmed live afterward (`2.5` and `10.5` both read
   back correctly post-fix).
9. **A double-quote (inch mark) is consistently misread as a degree
   sign** (`'up to 5"'` -> `'up to 5\xb0'`) at this crop size --
   confirmed by inspecting the actual returned character's Unicode
   codepoint (`0xb0`). Fixed with a narrow, justified normalization
   (a degree sign directly following a digit is corrected to `"`,
   since this catalog's descriptions use inch marks, never degrees;
   any other occurrence is left alone).

### A real, live-discovered design deviation from the original build spec

The build spec's stated preference order for `read_populated_fields()`
was: accessible controls > keyboard navigation > OCR crops >
committed-row verification as a **secondary**, cross-checking source.
Live testing found this doesn't fit what's actually true here: the
"Quick Entry" panel only re-syncs to a row when that row is explicitly
clicked (confirmed: it stays blank immediately after
`select_candidate()`, and only populates once `enter_quantity()`'s own
click on the Quantity cell selects the row). The grid row itself, by
contrast, is already fully legible with **no extra click and therefore
no extra risk**. This adapter reads the grid row as the *primary*
source, not a fallback -- a deliberate, documented deviation justified
by what was actually observed, not by convenience.

### Trial A: 5 non-destructive trials (search -> observe -> parse -> cancel)

All 5 run through the real adapter class (not scratch scripts), for
both the exact original phrase and the shorter phrase-generator-style
query `"gutter aluminum up to 5"` (untested until this session --
confirmed to trigger the same live dropdown reliably, though in a
different row order than the exact-phrase query).

| Trial | Dropdown appeared | Row count | SFG/GUTA present | Cancelled cleanly | Item count unchanged |
|---|---|---|---|---|---|
| 1 | yes | 10 | yes | yes | yes (0) |
| 2 | yes | 10 | yes | yes | yes (0) |
| 3 | yes | 10 | yes | yes | yes (0) |
| 4 | yes | 10 | yes | yes | yes (0) |
| 5 | yes | 10 | yes | yes | yes (0) |

**5/5 clean.**

### Trial B: 5 assisted-selection trials (select -> verify fields -> cancel before commit)

Run *after* fixes #1-9 above landed (an initial pre-fix run showed
identical, consistent field-read corruption across all 5 trials --
included here for honesty, since it's what motivated bugs #3/#6's
investigation and fix).

| Trial | Selected correct row | Populated fields exact match | `cancel_current_item()` verified | Item count after |
|---|---|---|---|---|
| 1 (pre-fix) | yes | **no** (`"SFG |"` / `"SUTA"` / degree-sign desc / `"ue"` unit) | yes | 0 |
| 2 (pre-fix) | yes | **no** (identical corruption) | yes | 0 |
| 3 (pre-fix) | yes | **no** (identical corruption) | yes | 0 |
| 4 (pre-fix) | yes | **no** (identical corruption) | yes | 0 |
| 5 (pre-fix) | yes | **no** (identical corruption) | yes | 0 |
| 1 (post-fix) | yes | **yes**, exact string match on category/selector/description/unit/action | yes | 0 |
| 2 (post-fix) | yes | **yes** | yes | 0 |
| 3 (post-fix) | yes | **yes** | yes | 0 |
| 4 (post-fix) | yes | **yes** | yes | 0 |
| 5 (post-fix) | yes | **yes** | yes | 0 |

**5/5 clean after fixes.** Selection targeting itself (via live UIA
rectangles) was correct in all 10 trials including the pre-fix ones --
every failure was in field *reading*, never in *selecting the wrong
row*.

### Quantity tests

| Quantity | Entered correctly (visual) | Read back |
|---|---|---|
| 10 | yes (RCV $115.60) | 10.0 -- correct |
| 10.5 | yes (RCV $121.39) | 10.5 -- correct |
| 0.25 | yes | 0.25 -- correct |
| 2.5 | yes (RCV $28.91) | **25.0 -- wrong** (bug #8, pre-fix) -> **2.5 -- correct** (post-fix) |

### CAT/SEL direct search

Format tested: category and selector concatenated with no separator
(`"SFGGUTA"`). Returned 5 candidates with the exact match (`SFG/GUTA`)
ranked first, in a search-relevance order visibly different from the
description-search case (confirming Xactimate's own search endpoint
distinguishes the two query styles). No mutation. This is the only
format tested; a space-separated or other format was not tried since
the concatenated format worked on the first attempt and there was no
indication it needed to be ruled out. `search_by_category_selector()`
uses this format.

### Single-line commit (Trial E)

Full flow run once through the real adapter: search -> capture ->
parse -> confirm target present -> select -> independently read
populated fields (matched target exactly) -> enter decimal quantity
2.5 -> read back (wrong pre-fix, correct post-fix) -> commit (`Ctrl+S`)
-> capture evidence -> verify committed row -> clean up (delete +
re-save). The committed row's correctness (`SFG/GUTA`, qty 2.5, RCV
$28.91, "Saved" status) was confirmed **visually** via screenshot at
every stage. One honest caveat: a *final* automated re-verification
pass immediately after quantity entry showed renewed OCR noise on
description/unit/action (though category/selector still read
correctly) -- most likely a residual cell-highlight rendering state
after the Quantity-cell click, not re-investigated further this
session for time reasons. The row was removed and the removal saved;
the TEST project was left in the same clean, empty-grid state it
started in.

### No-result case

A nonsense query (`"zzqqxxnonexistentphrase12345"`) correctly produced
`capture_dropdown()` returning zero rows with no exception and no
mutation -- exactly what `orchestrator.py`'s existing
`STOP_REASON_NO_RESULTS` handling expects.

### What was NOT completed this session

- **The full 10-item assisted pilot** (the build spec's explicit
  closing gate) was not run. What stands in for it: 5 Trial-A + 5
  Trial-B repetitions of the core flow (10 total live search/select/
  cancel cycles), one full commit cycle, one no-result case, and one
  CAT/SEL-direct-search case -- real coverage, but not the specific
  ten-mixed-item, group-scoped pilot the spec describes, and zero
  ambiguous-candidate cases were exercised live (ranking.py's ambiguous-
  candidate logic is otherwise well-covered by the existing Fake-adapter
  test suite, which this adapter's `DropdownResult` output feeds
  identically -- but that's an argument from shared code, not a live
  observation).
- **Populated-field reading is not "independently reliable"** in the
  build spec's stronger sense yet. Category and selector -- the two
  fields that actually gate `populated_fields_mismatch` most
  meaningfully -- were reliable across every post-fix trial. Description/
  unit/action showed renewed corruption in one later, different UI
  state (Trial E's final check) that wasn't root-caused. The majority-
  vote-of-3 strategy helps but does not guarantee correctness in every
  observed rendering state.
- **The "No"-click vs. Escape dismissal of the Duplicate Item(s) dialog**
  was not cleanly, independently re-verified as equivalent.
- **CAT/SEL search format** was validated for exactly one input shape;
  edge cases (a selector containing special characters like `<` or `>`,
  seen in real catalog codes such as `GUTHRA<`) were not tested through
  `search_by_category_selector()` specifically.

### `supports_live_execution`

Stays **`False`**. Per the pilot gate's own criteria:

| Gate requirement | Status |
|---|---|
| 5 non-destructive trials pass | **met** |
| 5 assisted-selection trials pass | **met** (post-fix) |
| Decimal quantities pass | **met** (post-fix) |
| Populated-field verification independently reliable | **not met** -- category/selector reliable, description/unit showed unresolved fragility in at least one state |
| Single-line commit passes | **met**, with the caveat above |
| 10-item pilot, zero wrong selections | **not attempted** |
| Ambiguous/no-result items stop safely | **no-result: met**; **ambiguous: not tested live** |
| Recovery proven | **partially** -- Escape-based recovery confirmed extensively; Duplicate-dialog-via-Escape specifically unverified |
| Full test suite passes | **met** -- 573 passed, 12 skipped (pre-existing, unrelated), 0 failed, 19 new tests added, no regressions |

### Recommendation

The remaining gap is narrower than Phase 4.2B's was: every mechanism
has a working, live-validated implementation, and the nine bugs found
this session were all fixable without questioning the underlying
approach. The next session should: (1) root-cause the Trial E final-check
field-read regression (likely a specific cell-highlight rendering state
worth reproducing deliberately rather than stumbling into again), (2)
independently re-verify Escape vs. "No" on the Duplicate Item(s) dialog,
(3) run the actual 10-item pilot with at least one genuinely ambiguous
case, then (4) revisit `supports_live_execution`.

### Environment note

`pywinauto`, `comtypes`, and `pywin32` were `pip install`ed into the
local venv for this diagnostic session but were **not** added to
`requirements*.txt` or `pyproject.toml` -- no adapter code exists yet to
depend on them, so formalizing the dependency was left for whichever
phase actually resumes implementation.

### Test suite baseline (Phase 4.1 session)

554 passed, 12 skipped, 0 failed (`tests/`, excluding two integration
files -- see below), after the Phase 3.6/3.8 real project and selector
catalog data (`projects/`, `fixtures/reference/data/`) were restored
from an out-of-band transfer (gitignored, contains real PII). The 12
remaining skips are the reference selector-screenshot library
(`fixtures/reference/extracted/`), intentionally not transferred (large,
not needed unless re-running OCR from scratch).

Two unrelated pre-existing files fail to *collect* --
`tests/integration/test_ui_pipeline_service.py` and
`tests/integration/test_verified_catalog_pipeline.py` -- both via
`src/estimate_extractor/ui/pipeline_service.py` importing
`estimate_extractor.output.csv_writer`, a module with no git history of
ever existing in this repository. Unrelated to `xactimate_lookup` and
left untouched per this build spec's constraint not to modify
service/CLI/UI code outside what implementing the adapter requires.

## Phase 4.4: final live pilot -- five new bugs found live, one full commit cycle verified, `supports_live_execution` stays `False`

Phase 4.4 was scoped as the closing validation pass: reproduce and
resolve Phase 4.3's Trial E anomaly, then run the actual 10-item
assisted pilot (with an ambiguous case and a no-result case) the build
spec's pilot gate requires, then decide `supports_live_execution`. It
did not redesign the adapter or lookup architecture -- every change
below is a conservative, pixel/OCR-parameter-level fix to something the
pilot caught live, plus one small internal-mechanism replacement in
`cancel_current_item()` justified by hard evidence that its predecessor
could not be made reliable with a better constant.

### Stage 1: Trial E anomaly -- reproduced, root-caused, fixed

5/5 clean repetitions of `enter_quantity(2.5)` immediately followed by
`read_populated_fields()` reproduced the exact Trial E corruption
deterministically (`description` truncated with a trailing `"="`
artifact, `unit='~'`, `action='a'`; category/selector/quantity always
correct). Root cause: `_anchor_offset()`'s scroll-drift anchor was the
Quick Entry panel's `"Cat:"` label, which is genuinely undetectable by
OCR exactly when its value box is empty -- precisely the state
`enter_quantity()` leaves behind, since it never touches Quick Entry's
own Cat field. Fixed by switching the anchor to the grid header's own
`"Cat"` column label (always present, regardless of Quick Entry
state), using bottommost-match preference to disambiguate from Quick
Entry's label when both exist in frame. Re-tested in isolation:
category, selector, description (exact string match including the
inch mark), unit, and action all read correctly post-fix.

While root-causing this, a second, more serious bug was found in
`_count_grid_rows()`: OCR sometimes appends a stray gridline-bleed
character (`"406 |"`) to the row-number column, and the original
`.isdigit()` row-detection check silently rejected these as non-rows,
undercounting the grid -- which made `cancel_current_item()` treat "1
real row present" as "0 rows, nothing to do," reporting success without
attempting any deletion. Fixed with `re.match(r"^\s*\d+", line)`
instead of an exact-digits check.

`cancel_current_item()`'s underlying delete mechanism (right-click ->
context-menu "Delete") continued to show intermittent false-success
behavior even after this fix, within Stage 1's testing -- documented
honestly at the time as a residual, unresolved gap rather than claimed
fixed. It was later root-caused and fixed for real in Stage 3 (below).

### Stage 2: the approved 10-item pilot plan

Presented to the user and approved before execution (streamlined mode:
one plan approval, then all 10 items run without per-item
confirmation, full results reported together). Selectors were
confirmed live via read-only probe searches before finalizing the plan,
so every "expected" target below is a real, live-verified catalog
entry, not a guess:

| # | Mode | Input | Target | Quantity |
|---|---|---|---|---|
| 1 | Description | "gutter aluminum up to 5" | SFG/GUTA | 5 |
| 2 | Description | "gutter aluminum 6" | SFG/GUTA> | 3 |
| 3 | Description | "gutter copper up to 5" | SFG/GUTC | 1.5 (decimal) |
| 4 | Description | "gutter galvanized up to 5" | SFG/GUTG | 2 |
| 5 | Description | "gutter half round aluminum up to 5" | SFG/GUTHRA< | 4 |
| 6 | CAT/SEL direct | SFG/GUTAB> | SFG/GUTAB> (trusted path) | 1 |
| 7 | Description (ambiguous) | "gutter" | REVIEW_REQUIRED expected | -- |
| 8 | Description (no-result) | "zzznonexistentqqq99999" | NO_MATCH expected | -- |
| 9 | Description (different category) | "composition shingles" | RFG/ARMVN | 3 |
| 10 | Description | "gutter box aluminum" | SFG/GUTAB | 2.5 (decimal) |

Items #7 and #8 were designated to also serve as Stage 4's ambiguous
case and Stage 5's no-result case, to avoid two redundant live
searches -- both were independently checked against the full Stage
4/5 acceptance criteria, not just the pilot's own pass/fail.

### Stage 3: execution -- five new bugs found and fixed live

The pilot was run through the real ranker (`ranking.rank_dropdown_results`
/ `classify_decision`, the same module `orchestrator.execute_plan`
calls), calling adapter methods directly rather than through
`execute_plan` (which refuses to do anything live while
`supports_live_execution` is `False` -- exactly the flag this pilot
exists to determine).

**First pass, unmodified adapter**: items 1-5 and 10 all correctly hit
`REVIEW_REQUIRED` -- not an adapter bug. The live catalog genuinely
contains multiple near-duplicate SFG gutter selectors (e.g. `GUTA` vs.
`GUTHRA<`, both "aluminum, up to 5"") that score within the
configured `auto_select_margin` (0.08) of each other given only
component/material/size signal; the phrase-rules config has no
style/grade keyword to distinguish "regular" from "half round" gutters
(only shingle/carpet/tile grade terms exist). This is the ranker
correctly refusing to guess between genuine near-ties, not a defect --
but it meant only items 6 and 9 (CAT/SEL trusted path, and a
description search with no close competitor) reached `AUTO_SELECT` and
actually exercised the select -> verify -> quantity -> commit chain.
Both immediately hit a **new** anomaly, distinct from Stage 1's:
`read_populated_fields()` called right after `select_candidate()` (the
adapter's real production call order, per `orchestrator.py` --
*before* `enter_quantity()` ever runs) returned severely garbled data
for item 9 (`category='_'`, `selector='an'`, `description=None`) once
a second grid row existed.

Root-caused via direct pixel/OCR measurement against live screenshots
(word-position measurement, crop dumps, scale/PSM sweeps -- see
`_read_populated_fields_once()`'s docstrings in `windows_adapter.py`
for the full explanation on each). Five distinct, previously-unknown
bugs, found and fixed in this order:

1. **`_GRID_ROW_HEIGHT` was never actually validated.** It was 17px,
   multiplied by `(row_count - 1)` in `_last_row_geometry()` -- always
   zero in every prior single-row test, so the constant was silently
   untested until the pilot's first two-row state. Real measured
   spacing between two live static rows is 25px. Fixed: `17` -> `25`.
2. **Category OCR misread at native scale/PSM.** `"RFG"` read as
   `"RFC"` at `--psm 7` (the default) across every scale 1x-8x tried;
   `--psm 6` reads it correctly at every scale. Fixed: category now
   uses `psm=6` plus the same 4x upscaling already used for
   activity/unit.
3. **`selector`/`activity`/`description` column boundaries only ever
   fit a 4-character selector code.** A longer code (`"GUTAB>"`, 6
   characters) visually overflowed the old `selector` boundary
   (563-608); the spillover landed inside `activity`'s crop (608-628),
   corrupting it. `description`'s right edge (915) similarly truncated
   a longer description. All three widened based on live OCR
   word-position measurement.
4. **`unit` column position was calibrated for the wrong row state.**
   The existing boundary (1073-1099) was measured against the row
   *highlighted by `enter_quantity()`'s own cell click* -- but
   `read_populated_fields()` is actually called by the real
   orchestrator flow *before* `enter_quantity()` ever runs, against
   the **static** (unhighlighted) row, where the real content sits
   further right (measured: `"SQ"` at x=1095-1110). Also found: 4x
   upscaling misread `"SQ"` as `"$Q"`; 6x reads it correctly.
5. **`_CONTEXT_MENU_DELETE_OFFSET` was a fixed pixel offset into a
   menu whose row count is state-dependent**, not merely imprecise.
   "Undo Delete Line Item" appears/disappears depending on whether
   there's undo history in the current session, shifting every menu
   item below it by ~23px -- confirmed by measuring the same menu
   twice in the same session and getting two different "Delete"
   positions. The previous offset (calibrated in Phase 4.3, a
   fresh-session state) silently landed on the disabled "Reverse
   Paste" item instead, explaining several consecutive silent
   `cancel_current_item()` failures both in Phase 4.3 and early in
   this session. **Fixed properly, not just re-measured**: replaced
   the fixed offset with `_click_context_menu_item()`, which grabs a
   full desktop screenshot (a context menu is a separate top-level
   window, invisible to the client-area `PrintWindow` capture used
   everywhere else in this file), OCRs it, and clicks the literal
   text `"Delete"` -- matching on the exact line so `"Delete"` doesn't
   false-match inside `"Undo Delete Line Item"`. Verified live: 2/2
   successful deletions after the fix, versus repeated silent failures
   before it (both with the stale offset and with a freshly
   re-measured-but-still-fixed offset).

After all five fixes, item 9 (RFG/ARMVN) achieved a **full, correctly
verified live commit**: selected, populated fields matched
(`category='RFG'`, `selector='ARMVN'`, `unit='SQ'`), quantity 3
entered and committed, and the resulting row was confirmed **both**
programmatically (`read_quantity()` on a later, settled call returned
`3.0`) **and visually** (screenshot showing `#416 RFG ARMVN ... 3 3 SQ
... RCV $158.85`). One honest gap: `read_quantity()` called
*immediately* after `commit_item()`'s `Ctrl+S` returned `None` in the
pilot run itself (not wrong data -- a transient read failure in a
narrow timing window right after save, the same class of intermittent
capture-timing gap documented in Phase 4.3/4.4 Stage 1); re-querying
moments later returned the correct value every time.

Item 6 (SFG/GUTAB>, CAT/SEL trusted path) selected correctly
(`category`/`selector` matched) but safely stopped on a genuine
**unit mismatch** (`'uF'` vs. expected `'LF'`) -- not investigated
further given time budget; documented as a residual, per-selector-code
OCR gap rather than claimed fixed. No quantity was entered, no commit
attempted.

A separate, transient issue was hit and confirmed reproducible-but-
intermittent: two searches (`"gutter aluminum 6"`, `"gutter copper up
to 5"`) initially failed with `"No results popup appeared within 5.0s"`
immediately after a prior item's search; re-run individually with a
0.5s settling delay after `clear_search()`, both succeeded and produced
the expected `REVIEW_REQUIRED` result. Consistent with the existing
documented pattern of live UI timing sensitivity around search-state
transitions; not newly root-caused this session.

**Complete final per-item results** (second, fixes-applied run, plus
the individually re-run items 2/3):

| # | Decision | Outcome | Rows before -> after |
|---|---|---|---|
| 1 | REVIEW_REQUIRED | safe stop (genuine near-tie, correct ranker behavior) | 0 -> 0 |
| 2 | REVIEW_REQUIRED | safe stop (same) | 2 -> 2 |
| 3 | REVIEW_REQUIRED | safe stop (same) | 2 -> 2 |
| 4 | REVIEW_REQUIRED | safe stop (same) | 0 -> 0 |
| 5 | REVIEW_REQUIRED | safe stop (same) | 0 -> 0 |
| 6 | AUTO_SELECT | selected correctly, safe stop on unit mismatch | 0 -> 1 |
| 7 | REVIEW_REQUIRED | safe stop (Stage 4 case -- see below) | 1 -> 1 |
| 8 | NO_MATCH | safe stop (Stage 5 case -- see below) | 1 -> 1 |
| 9 | AUTO_SELECT | **selected, verified, quantity entered, committed, re-verified** | 1 -> 2 |
| 10 | REVIEW_REQUIRED | safe stop (genuine near-tie, correct ranker behavior) | 2 -> 2 |

Zero wrong selections. Zero unintended mutations. Recovery (either
`adapter.recover()` or the pilot script's own error handling) was
invoked and succeeded every time it was needed.

### Stage 4: ambiguous live case (pilot item #7)

`search_by_description("gutter")` against the live adapter (not a
mocked/catalog-backed list) returned 10 real candidates, all scoring
1.000 on description/component match alone (no material/size signal to
disambiguate a single bare word), correctly classified
`REVIEW_REQUIRED`. No candidate was selected, no quantity entered, no
commit attempted, evidence captured, grid row count unchanged (1 -> 1).

### Stage 5: no-result live case (pilot item #8)

`search_by_description("zzznonexistentqqq99999")` against the live
adapter returned zero dropdown rows with no exception, correctly
classified `NO_MATCH`. No selection, no quantity, no commit, evidence
captured, row count unchanged (1 -> 1). The search UI's own state was
confirmed back to normal implicitly -- item 9's search immediately
afterward succeeded cleanly.

### Stage 6: final cleanup

Both remaining rows (item 6's uncommitted `SFG/GUTAB>` selection, item
9's committed `RFG/ARMVN` row) were removed via `cancel_current_item()`
using the fixed OCR-based context-menu click -- 2/2 successful on the
first attempt post-fix. Project saved. Final state visually confirmed:
0 grid rows, Grand Total $0.00, "Saved" status, `Utility Room` group
subtotal $0.00 -- matching the project's state before Stage 3 began.

### Regression tests added

`tests/unit/xactimate_lookup/test_windows_adapter.py` gained two
guards (21 tests total, up from 19): one locking `_GRID_ROW_HEIGHT` at
its corrected value with a docstring explaining why the stale value
was never actually exercised by any single-row test, and one asserting
the `selector`/`activity` grid columns don't overlap and are wide
enough for a 6+ character selector code. The deeper live OCR/pixel
behavior these guard against can only be meaningfully validated
against a real Xactimate screenshot (this module's existing testing
philosophy, per its own docstring) -- these are regression guards
against a future edit silently drifting the constants back, not a
substitute for the live verification above. The `_click_context_menu_item()`
fix was verified live (2/2) rather than via unit test, for the same
reason.

### Full test suite

575 passed, 12 skipped (pre-existing, unrelated -- see Phase 4.1's test
suite baseline note above), 0 failed, run excluding the same two
pre-existing-broken integration files documented under Phase 4.3 (`estimate_extractor.output.csv_writer`
does not exist in this repository; confirmed unrelated to any
`xactimate_lookup` change, in this session or prior ones). Two more
tests than Phase 4.3's 573-passed baseline, matching the two new
regression tests added this session. No regressions.

### `supports_live_execution`

Stays **`False`**. Evaluated against all 12 execution-gate criteria:

| Gate requirement | Status |
|---|---|
| OCR anomaly resolved/bounded with evidence | **met** -- Trial E root-caused and fixed, verified in isolation |
| All clear (AUTO_SELECT) items select correctly | **met for the 2 items that reached it** -- but only 2/10 pilot items ever reached AUTO_SELECT (the other 6 REVIEW_REQUIRED outcomes were correct ranker behavior, not adapter failures, so this criterion has a thin evidence base) |
| All quantities read back exactly | **not cleanly met** -- item 9's quantity was entered and later confirmed correct both ways, but the *immediate* automated readback right after commit returned `None`, a timing gap a real unattended pipeline would have to treat as a failure |
| All committed rows verified | **met for the 1 item committed** -- `RFG/ARMVN`, qty 3, visually and programmatically confirmed |
| Ambiguous case stops safely | **met** (Stage 4 / item 7) |
| No-result case stops safely | **met** (Stage 5 / item 8) |
| No wrong selections | **met** -- zero across all 10 items |
| No unintended mutations | **met** |
| Recovery succeeds where invoked | **met** -- every invocation succeeded |
| TEST project returns to original clean state | **met** -- visually confirmed |
| Full test suite passes | **met** -- 575 passed, 12 skipped (pre-existing), 0 failed |
| Evidence exists for every pilot item | **met** |

Ten of twelve criteria are cleanly met. The two that aren't --
quantity-readback timing, and only one full committed item across the
whole 10-item pilot (all five of this session's newly-found-and-fixed
bugs were found *within* this same pilot run, so they have not yet
been re-verified by an independent pilot with the fixes already in
place going in) -- are exactly the kind of thin-evidence gaps the
pilot gate exists to catch. A production line-item pipeline commits
many items in sequence; this session's live evidence for "reliably
correct across more than one full cycle, on the first attempt, with
fixes already validated going in" does not yet exist.

### Recommendation

The remaining gap is narrower than Phase 4.3's: this session found and
fixed five real bugs (all newly-discovered, none anticipated), reduced
`cancel_current_item()` from a fragile fixed-offset mechanism to a
robust OCR-text-located one (2/2 live), and achieved one fully-verified
live commit end-to-end. The next session should: (1) re-run the
10-item pilot fresh, with all five Stage 3 fixes already in place
rather than found mid-run, to build real confidence across more than
one committed item; (2) investigate the item 6 unit-mismatch OCR gap
(`'uF'` vs `'LF'`) the same way Stage 3's other column issues were
resolved; (3) investigate the immediate-post-commit `read_quantity()`
timing gap; (4) consider whether the phrase-rules config needs a
gutter-style disambiguator (e.g. "K-style" vs. "half round") so
genuinely distinguishable line items don't structurally collapse into
`REVIEW_REQUIRED` -- out of scope for this adapter-only phase, but
worth flagging since it affects the SFG category broadly, not just
this pilot; then (5) revisit `supports_live_execution`.

## Phase 4.5: production-readiness pass -- bounded quantity-verification polling built, four more live bugs found and fixed, clean pilot repeated -- `supports_live_execution` stays `False`, single blocker: `cancel_current_item()` reliability

Phase 4.5 was scoped narrowly: close Phase 4.4's two open gate items
(the post-commit quantity-readback timing gap, and the thin pilot
evidence from bugs being found mid-run) and decide production
readiness. No redesign, no new features, no refactor of working code.

### Gate 1: quantity-readback timing -- root-caused, bounded polling built

Live timestamped polling immediately after `enter_quantity()` (the
adapter's actual pre-Phase-4.5 call context for a readback, not
`commit_item()` as Phase 4.4's addendum imprecisely described it --
corrected here) reproduced a genuine transient `None` on the first
live trial. Root cause, once isolated: **not** a settle-timing issue
alone -- `read_quantity()`'s 4x upscale (added in Phase 4.3 to recover
a decimal point native resolution can drop) blurs a *short* value (a
single digit, e.g. `"7"`) into an empty OCR result, while native
resolution reads the same short value correctly. Fixed by reading both
scales and preferring whichever contains a decimal point the other is
missing, otherwise preferring native. Verified live: single-digit ("7")
and decimal ("2.5") cases both read correctly after the fix.

Built `verify_quantity_committed(expected_quantity, timeout_s=5.0,
interval_s=0.25)`: polls `read_quantity()`, terminating on the first
of -- observed value matches (success), `_unexpected_dialog_present()`
(wrong context, aborts immediately), or timeout. Every attempt's
elapsed time, whether a grid row was located, and the observed value
are recorded in `.samples` for diagnostics. This is a genuine bounded
poll, not a longer fixed sleep -- a fixed sleep just moves the same
race to a different, still-unbounded worst case.

### Three more live bugs found and fixed while root-causing Gate 1

None were anticipated; all were found by exercising the adapter
through realistic multi-row, multi-action live sequences that Phase
4.4's pilot hadn't happened to hit:

1. **`_count_grid_rows()` misreads a 4+ row grid at `--psm 6`.** A
   real, clearly-legible `"422"` was read as `"a2"` or `"ry"` --
   non-randomly, confirmed by re-testing the same crop at five PSM
   modes and three scales. This directly caused a **live wrong-row
   mutation**: `enter_quantity()` computed the "last row" from an
   undercounted total and entered a quantity into an existing,
   already-correct row instead of the newly-selected one, silently
   overwriting its value (`SFG/GUTG`'s quantity corrupted from `2` to
   `6` mid-session). Fixed: `--psm 11` (sparse text, no layout
   assumed) plus 2x upscale reads every row correctly in the same live
   state where `--psm 6` failed.
2. **`enter_quantity()`'s new-row check was a single-shot
   check-and-raise, not a poll.** The row count it read immediately
   after `select_candidate()`'s click could be transiently stale for
   the same reason as (1) above (before that fix) -- reproduced live:
   a correctly-added third row raised `AdapterError("... row count did
   not increase")` even though the row was visibly present and
   correctly counted moments later. Replaced with a bounded poll
   (3.0s timeout, 0.3s interval) that also aborts immediately on an
   unexpected dialog, matching the Gate 1 polling philosophy.
3. **`_click_context_menu_item()` (the OCR-based "Delete" click added
   in Phase 4.4) broke on a different screen resolution.** This
   session ran on a 1920x1080 screen, versus Phase 4.4's 2560x1440;
   Windows flips a context menu upward when there isn't enough room
   below the cursor, and the original crop only ever looked
   below-and-right of the click point. Fixed: the search region is now
   centered on the click point (clamped to the virtual screen).
   Fixing this exposed two further layers in the same mechanism,
   fixed in the same pass: `--psm 6` merges/loses the standalone
   "Delete" line once the (now larger) crop also contains the busy
   background grid behind the menu -- `--psm 4` ("single column of
   text") isolates it reliably where `--psm 6` didn't; and even with
   both fixes, a single OCR pass can still flatly misread the word
   itself (`"Delete"` read as `"betete"` -- confirmed *stable*, not
   per-attempt noise, by re-grabbing and re-OCRing the same live state
   three times and getting `"betete"` all three times). A plain
   fuzzy-ratio match couldn't safely distinguish that misread from
   `"undo delete line item"` (which must never match) -- both score
   similarly low. Levenshtein edit distance can: `"betete"` is 2 edits
   from `"delete"`, every other real single-word item in that menu
   (closest: `"select"`) is >= 3 edits away, and multi-word lines are
   excluded by a single-word restriction regardless of distance.

All four fixes (the read_quantity dual-scale fix plus these three)
are individually live-verified. See `windows_adapter.py`'s inline
docstrings at each fix site for the exact reproduction evidence.

### Gate 2: TEST project reset, exact same 10-item pilot repeated clean

TEST project confirmed at 0 rows / $0.00 / "Saved" before starting.
The identical Phase 4.4 pilot plan (same 10 items, same search
inputs/CAT-SEL, same expected quantities) was re-run unmodified through
the real ranker, with `verify_quantity_committed()` replacing the
single-shot post-commit read.

| # | Mode | Decision | Outcome | Elapsed |
|---|---|---|---|---|
| 1 | Description | REVIEW_REQUIRED | safe stop (genuine near-tie) | 5.5s |
| 2 | Description | REVIEW_REQUIRED | safe stop (same) | 4.7s |
| 3 | Description | REVIEW_REQUIRED | safe stop (same) | 5.2s |
| 4 | Description | REVIEW_REQUIRED | safe stop (same) | 5.7s |
| 5 | Description | REVIEW_REQUIRED | safe stop (same) | 6.8s |
| 6 | CAT/SEL direct | AUTO_SELECT | selected correctly (SFG/GUTAB>), safe stop on unit mismatch | 7.6s |
| 7 | Description (ambiguous) | REVIEW_REQUIRED | safe stop (expected) | 3.4s |
| 8 | Description (no-result) | NO_MATCH | safe stop (expected) | 5.3s |
| 9 | Description (different category) | AUTO_SELECT | selected+committed (RFG/ARMVN, qty 3); **quantity verification timed out** | 20.2s |
| 10 | Description | REVIEW_REQUIRED | safe stop (genuine near-tie) | 5.1s |

Total elapsed 69.5s, average 7.0s/item. Zero wrong selections. Zero
unintended mutations within the pilot itself (the wrong-row mutation
in bug #1 above happened during Gate 1 root-causing, in throwaway
diagnostic rows, *before* the TEST project was reset for this clean
run). Items 1-5, 7, 10 reproduce Phase 4.4's finding exactly: the SFG
gutter catalog has multiple genuine near-duplicate selectors this
ranking config's signals can't distinguish -- correct, repeatable,
conservative ranker behavior, not an adapter defect. Items 6 and 9
reached `AUTO_SELECT` again, exactly as in Phase 4.4, confirming that
result is repeatable rather than a fluke.

**Item 9's quantity verification timed out** (`stop_reason="timeout"`,
5 attempts, 5.63s, `observed=None` on every attempt) despite the
underlying data being entered correctly -- confirmed both by a later
re-query (`read_quantity()` returned `3.0` moments after the pilot
finished) and visually (`RCV $158.85`, matching `3 x $52.95`). This
means `verify_quantity_committed()`'s bounded-polling *mechanism*
worked exactly as designed -- it never silently reported success on
unconfirmed data, and it produced a fully diagnosable timing record --
but the real-world settle time after `commit_item()` can exceed the
5.0s default budget on at least one occasion, which the isolated Gate
1 root-cause testing (which found sub-second settle times every time,
30 samples across two different single/two-row states) did not
surface. Not further tuned this session; flagged as a real, measured
gap between isolated root-cause testing and full-pilot conditions.

### Cleanup: `cancel_current_item()` required manual intervention on both rows

Both pilot rows (item 6's uncommitted `SFG/GUTAB>` selection, item 9's
committed `RFG/ARMVN` row) needed to be removed to return the TEST
project to its starting state. `cancel_current_item()` -- using the
Phase 4.4 mechanism plus this session's three additional context-menu
fixes -- was retried 20 times per row and failed 100% of the time on
both (`"could not locate the 'Delete' context-menu item"`). Live
diagnosis on each failure found a *different* underlying cause each
time (further OCR misreads/segmentation issues distinct from the three
already fixed this session), consistent with a pattern rather than a
single remaining bug: this application's rendering makes OCR-located
context-menu clicking inherently fragile in a way conservative,
targeted fixes keep narrowing but haven't closed. Both rows were
removed via a one-off manual coordinate click (visually confirmed
"Delete" position, not an adapter code path) so the pilot's cleanup
requirement could be verified independently of this specific
mechanism's reliability. The TEST project's final state was visually
confirmed identical to its starting state: 0 rows, $0.00, "Saved".

This is the same class of gap Phase 4.4 already flagged honestly
("`cancel_current_item()` retains residual automation-timing
unreliability") -- narrowed by three real fixes this session (a
directional-crop bug and a segmentation bug that were unconditional
failures, plus a stable-misread case), but not eliminated. A durable
fix would OCR-locate menu item text with a fundamentally different
strategy (e.g. UI Automation against the menu's own window, if it
exposes one, rather than a screenshot) -- out of scope for a
conservative fix, flagged as a follow-up.

### Regression tests added

`tests/unit/xactimate_lookup/test_windows_adapter.py` gained 3 tests
(24 total, up from 21): `_levenshtein_distance()`'s behavior on the
live-reproduced misread and its exclusion case (extracted from
`_click_context_menu_item()` to a module-level function specifically
so it's unit-testable), and `QuantityVerificationResult`'s shape
(match/sample recording, default empty samples). The four live
pixel/OCR fixes themselves are verified live, not via unit test, per
this file's established testing philosophy (deep live behavior is
validated manually against a real session, not mocked) -- consistent
with how Phase 4.4's equivalent fixes were handled.

### Full test suite

578 passed, 12 skipped (pre-existing, unrelated), 0 failed. Three more
tests than Phase 4.4's 575-passed baseline, matching the three new
regression tests. No regressions.

### `supports_live_execution`

Stays **`False`**.

| Validation criterion | Status |
|---|---|
| Zero wrong selections | **met** |
| Zero unintended mutations (within the clean pilot) | **met** |
| Quantity verification succeeds | **not met** -- item 9 timed out (data was correct; verification wasn't confirmed within budget) |
| Cleanup succeeds | **not met** -- 0/2 rows removed autonomously; both needed manual intervention |
| Commit verification succeeds | **partially** -- field-level (category/selector) verification succeeded; quantity verification did not |
| Ambiguous cases stop safely | **met** |
| No-result cases stop safely | **met** |
| Evidence exists for every item | **met** |
| TEST project ends exactly where it started | **met** (after manual cleanup) |

**Single blocker:** `cancel_current_item()`'s OCR-located context-menu
click is not yet reliable enough for unattended production use. It
failed 100% autonomously (0/2, 20 retries each) in this session's
clean pilot, after three additional conservative fixes this same
session each closed one failure mode and exposed another. This is the
one gate criterion that failed outright rather than partially; the
quantity-verification timeout is a secondary, lower-severity finding
(the underlying data was correct; only the confirmation step was slow)
that would also need to close, but cleanup reliability is the harder,
more clearly disqualifying blocker -- an adapter that cannot reliably
remove/correct a line item without a human watching every context-menu
click cannot be trusted for unattended live execution.

### Recommendation

Do not enable `supports_live_execution` yet. Before revisiting it: (1)
replace `cancel_current_item()`'s screenshot-OCR menu click with a
fundamentally different location strategy (UI Automation against the
context menu's own window, if Windows exposes one for a WPF native
menu -- not confirmed either way this session) rather than continuing
to patch the OCR approach one failure mode at a time; (2) raise
`verify_quantity_committed()`'s default timeout or investigate why
settle time exceeded 5.0s in full-pilot conditions when isolated
testing never observed more than a fraction of a second; (3) once both
close, re-run this same 10-item pilot a third time and require 100%
autonomous cleanup success before flipping the flag.

## Phase 4.6: deterministic recovery replaces OCR-click deletion, quantity-verification root-caused as an OCR bug not a timing bug -- `supports_live_execution` stays `False`, single blocker: intermittent quantity-verification timeout despite correct data

Phase 4.6 retired the OCR-located context-menu click entirely (three
rounds of conservative fixes in Phase 4.4/4.5 each closed one failure
mode and exposed another -- treated as evidence of structural
fragility, not a bug count converging on zero) and replaced it with a
deterministic, non-OCR mechanism. It also root-caused Phase 4.5's
"quantity verification times out" finding down to its real cause.

### Stage 1: recovery methods investigated, in the requested order

1. **Xactimate Undo (Ctrl+Z) after a commit** -- tested live, twice
   (once unfocused, once with the row explicitly clicked/focused
   first): no effect either time, row count unchanged, "Saved" status
   unchanged. The row context menu's "Undo ..." item is genuinely
   scoped to reverting a *deletion* (its live-observed label was
   always "Undo Delete Line Item", never "Undo Add"), and a fresh
   add's context menu shows no "Undo" entry at all in some states --
   consistent with Undo not applying to a fresh commit. **Not viable.**
2. **A documented/discoverable Delete Item command** -- exists (the
   context menu's "Delete" item, same one Phase 4.4/4.5 targeted via
   OCR); the gap was never its existence, only how to invoke it
   reliably.
3. **Context-menu keyboard mnemonic** -- not separately pursued once
   method 4 succeeded; the menu items have no visible underlined
   mnemonic characters in any captured screenshot.
4. **UIA/MSAA access to the open context menu** -- the context menu
   IS a real top-level WPF popup window (same `HwndWrapper[Xactimate
   online Estimate Writer-...]` class marker as the main window and
   the search-results dropdown) and DOES expose a full UI Automation
   tree. But its items are `Telerik.Windows.Controls.RadMenuItem`
   controls whose `CurrentName`, `CurrentAutomationId`, and
   `LegacyIAccessible.CurrentName` all return either empty or a
   generic `"Telerik.Windows.Controls.RadMenuItem Header:
   Items.Count:N"` ToString() dump -- text-based lookup isn't just
   unreliable here, it's **not available at all** (confirmed by
   dumping every item's Name/AutomationId/ClassName/HelpText/Legacy
   properties live). `IUIAutomationInvokePattern.Invoke()` raised
   ("NULL COM pointer access" -- not implemented on this control);
   `LegacyIAccessiblePattern.DoDefaultAction()` ran without error but
   was confirmed live to be a safe no-op (matches this adapter's
   already-documented finding for dropdown rows). What UIA DOES give
   reliably: **structural position**. The "Undo " slot (index 5 in the
   flat child list) is always present as an element -- collapsed to a
   `(0,0,0,0)` rect when inapplicable, never removed from the tree --
   so the menu's total item count (26) and every other item's index
   are stable regardless of undo-history state. "Delete" is reliably
   at index 11 (a ~24px-tall real item, immediately before a ~7px
   separator) -- confirmed identical across two independently-measured
   live states (a fresh add, and after a prior delete).
5. **Keyboard navigation through the menu** -- not pursued; method 4's
   structural-index + real-mouse-click combination worked and is
   simpler.
6. **Stable visual interaction (OCR)** -- explicitly not attempted
   further per this phase's brief; superseded by method 4.

### Selected recovery strategy

`_click_delete_via_uia()`: right-click the target row (as before) to
open the context menu, locate its popup HWND by window class + shape
(narrow, tall, empty title -- not by title, since like the dropdown
popup this window has none), walk its flat UIA child list, verify the
structural invariant (exactly 26 items; index 11 is ~24px tall, not a
~7px separator -- refuses to click rather than guess if this doesn't
hold), read that item's LIVE `CurrentBoundingRectangle` (never a
cached/guessed position), and issue a real mouse click at its center
via the same `_click_screen()` primitive used everywhere else in this
file. No OCR, no text matching, no pixel-offset guessing -- UIA
supplies the coordinate, a plain click performs the action (since
semantic invocation doesn't work on this control).

`cancel_current_item()` (last row) and the new `delete_existing_item(category,
selector)` (a specific row anywhere in the grid, by identity --
required for the multi-row trials below, not previously possible since
the old mechanism only ever computed the *last* row's position) both
use this. `delete_existing_item()` independently verifies, after the
click, that: the target identity is gone, exactly one row disappeared,
and every other row's identity and relative order is unchanged --
comparing full before/after grid snapshots, not just a row count.

### Why the prior OCR-click strategy was retired, not patched again

Phase 4.4 fixed a directional-crop bug and a PSM-segmentation bug.
Phase 4.5 fixed a stable character misread ("Delete" read as
"betete"). Re-testing that same OCR mechanism fresh at the start of
this phase found it **still** failed 100% of the time on a live
retry -- this session's screen state produced a *fourth*, different
failure (the standalone "Delete" line merged into an unrelated
background line, `"Misc. Item Attachments Delete]"`). Four rounds of
"root-cause and fix" across two phases, each closing exactly the one
failure mode reproduced and exposing a new one on the next real
session, is the definition of a structurally fragile approach, not a
converging bug count -- OCR-reading a semi-transparent menu overlaid
on a busy, content-dependent background is inherently unstable in a
way pixel/text tuning cannot fully close. The UIA-structural approach
has no OCR step at all in its critical path, eliminating this entire
class of failure.

### Five single-row recovery trials

Clean start (0 rows) -> add one disposable item -> commit -> verify
-> `cancel_current_item()` -> verify 0 rows / $0.00 / Saved.

| Trial | Item | Delete result | Delete time | Final rows |
|---|---|---|---|---|
| 1 | SFG/GUTA qty 5 | OK | 3.48s | 0 |
| 2 | SFG/GUTC qty 2 | OK | 3.48s | 0 |
| 3 | SFG/GUTG qty 3 | OK | 3.47s | 0 |
| 4 | RFG/ARMVN qty 1 | OK | 3.49s | 0 |
| 5 | SFG/GUTHRA< qty 4 | OK (after an unrelated row-count bug found+fixed mid-trial -- see below) | -- | 0 |

**5/5 deletions succeeded**, each in a consistent ~3.5s (right-click
settle + UIA walk + click + post-click settle + verification --
matches the fixed timing budget in `cancel_current_item()`, not
variable OCR retry time).

### Three multi-row targeted-delete trials

Clean start -> add three distinct disposable rows -> `delete_existing_item()`
on the MIDDLE row's identity -> verify the other two are unchanged and
in the same order -> verify count decreased by exactly one -> clean up
all remaining rows -> verify 0 rows / $0.00 / Saved.

| Trial | Rows added | Deleted (middle) | Other rows intact | Count -1 exactly | Final rows |
|---|---|---|---|---|---|
| 1 | GUTA, GUTC, ARMVN | SFG/GUTC | yes | yes | 0 |
| 2 | GUTG, GUTHRA<, GUTAB | SFG/GUTHRA< | yes | yes | 0 |
| 3 | GUTA>, ARMVN, GUTC> | RFG/ARMVN | yes | yes | 0 |

**3/3 trials passed. Zero wrong-row deletions** -- every trial's
"before" and "after" full-grid identity snapshots matched exactly
(target removed, both others present, unchanged, same relative order).

### New defects found and fixed while running these trials

Two new, previously-unknown bugs surfaced live during Stage 3 (not
anticipated, found by exercising realistic multi-step sequences):

1. **`_count_grid_rows()` fails in BOTH directions depending on
   content density.** Phase 4.5 fixed a dense-grid (4+ row) misread by
   switching to `--psm 11`. This session found the same function
   returns an EMPTY result on a single-row grid with the same fixed
   400px-tall crop (mostly blank space below one line of text) --
   `--psm 6` reads that same sparse case correctly. Then, investigating
   further, a 2-row crop was found where NEITHER `--psm 6` nor `--psm
   11` read the second row correctly at any single scale tried (1x-6x)
   -- the specific misread of one row's digits varied by scale
   (empty, "ast", "a2", "4-4", "acd" -- different garbage each time).
   Root cause: crop height and blank-space ratio measurably change
   which PSM mode Tesseract's layout analysis prefers, and no single
   (height, PSM, scale) combination was correct across every density
   tested. Fixed by trying four combinations --
   `(100px, psm 6, 2x)`, `(100px, psm 6, 3x)`, `(400px, psm 11, 2x)`,
   `(400px, psm 6, 2x)` -- and taking the MAXIMUM row count found
   across all four, not the first non-empty one: undercounting is the
   dangerous direction (it already caused a real wrong-row mutation,
   below), while overcounting fails safe.
2. **This undercounting directly caused a live wrong-row mutation**
   during Stage 3 root-cause testing (in throwaway diagnostic rows,
   *before* Stage 5's clean pilot -- not during the pilot itself, and
   fully cleaned up before Stage 5 began): `enter_quantity()` computed
   the "new row" position from an undercounted total (3 instead of 4)
   and entered a quantity into an existing, already-correct row
   instead of the newly-selected one, silently overwriting `SFG/GUTG`'s
   quantity from `2` to `6`. This is the same class of bug Phase 4.5
   fixed once already (a different root cause, same consequence);
   `enter_quantity()`'s own new-row-count check (already a bounded poll
   since Phase 4.5) benefits directly from the `_count_grid_rows()` fix
   above, since it shares that function.

### Stage 4: post-commit quantity-verification timing

Live timestamped polling immediately after `enter_quantity()` (not
`commit_item()` -- Phase 4.5's addendum imprecisely described the call
context; corrected here) found `read_quantity()` returning a
**confidently wrong, non-empty, stable value** -- a real "5" read as
"3" at native resolution, every attempt, not an occasional misread.
Separately, a real "1" was misread as "oo"/"ji" at 2x-3x scale but
read correctly at 1x/4x/6x. **No single fixed OCR scale was correct
for every digit value tried** (1, 5, 2.5, 7 each failed at a different
scale). This reframes Phase 4.5's finding entirely: the "quantity
verification times out" symptom was never primarily a SETTLE-TIMING
problem -- it was `read_quantity()` confidently returning wrong
answers that just happened to never equal the expected value within
the old timeout budget. Fixed with the same "multiple independent
reads, majority vote" strategy `read_populated_fields()` already uses
across repeat captures, applied here across three scales (1x, 4x, 6x)
of the SAME capture instead: `{1x, 4x, 6x}` each independently got 3
of the 4 test values right (never the same 3), so a majority vote
across those three scales got all 4 right.

**Timing evidence after the fix** (5 fresh commits, fine-grained
polling from the instant `commit_item()`/`enter_quantity()` returned):

| Item | Row appears | Quantity readable+correct |
|---|---|---|
| SFG/GUTA qty 5 | t+0.00s | t+0.00s |
| SFG/GUTC qty 2.5 | t+0.00s | t+0.00s |
| SFG/GUTG qty 3 | t+0.00s | t+0.00s |
| RFG/ARMVN qty 1 | t+0.00s | t+0.00s |
| SFG/GUTHRA< qty 4 | t+0.00s | t+0.00s |

All 5 commits read the correct quantity on the very first poll
(sub-millisecond) once the OCR itself was fixed -- confirming there is
effectively no real settle delay; the earlier multi-second waits were
entirely the polling loop retrying a consistently-wrong OCR read, not
waiting out a real render delay.

### Final polling configuration

`verify_quantity_committed(expected_quantity, timeout_s=3.0)` (down
from Phase 4.5's `5.0` -- the new default is a ~1000x conservative
margin over the ~0s observed real-world time, not a tight fit to it,
since an occasional genuinely slower render remains possible and this
must never become an unbounded wait). Progressive intervals: five
attempts at 0.1s apart, then 0.4s apart afterward. Terminates on the
first of:

- observed value matches (`stop_reason="matched"`)
- the SAME non-matching value observed twice in a row
  (`stop_reason="wrong_value"` -- a stable wrong reading isn't a
  settle-timing issue polling can fix; surfacing it immediately is
  more honest than burning the full budget)
- the row that was previously found stops being found
  (`stop_reason="conflicting_row"` -- the grid changed under the poll)
- an unexpected dialog appears (`stop_reason="wrong_context"`)
- timeout (`stop_reason="timeout"`)

Three new regression tests (via monkeypatched internals -- the
progressive-interval and termination-condition LOGIC is pure Python
and testable without a live session, unlike the OCR itself) cover:
delayed-but-eventually-correct (the scenario the whole mechanism
exists for), stable-wrong-value early termination, and bounded timeout
on persistent `None`.

### Stage 5: focused pilot rerun (same approved 10-item plan, unmodified)

TEST project confirmed at 0 rows before starting.

| # | Decision | Outcome | Qty verify | Elapsed |
|---|---|---|---|---|
| 1-5, 7, 10 | REVIEW_REQUIRED | safe stop (genuine catalog near-ties/ambiguous -- same as Phase 4.4/4.5, confirms repeatability) | -- | 3.8-7.3s |
| 6 | AUTO_SELECT | selected correctly (SFG/GUTAB>), fields matched, committed | **timeout** (3 attempts, 3.81s) | 19.5s |
| 8 | NO_MATCH | safe stop (expected) | -- | 5.9s |
| 9 | AUTO_SELECT | selected correctly (RFG/ARMVN), fields matched, committed | **matched** (1 attempt, 0.000s) | 17.2s |

Zero wrong selections. Zero unintended mutations within this pilot run
(the mutation noted above happened during earlier Stage 3 diagnostic
testing, in disposable rows, fully cleaned up before this pilot
started). Item 9 committed and verified cleanly, exactly matching
Stage 4's timing evidence. **Item 6's quantity verification timed out**
despite `select_candidate`/`read_populated_fields` both succeeding --
inspected immediately afterward, the row's actual committed quantity
was correct (`1`, confirmed by both a fresh `read_quantity()` call and
visual inspection), but the automated verification did not confirm it
within the 3.0s/3-attempt budget during the live poll itself. Not
further root-caused this session (budget) -- this is the one gate
criterion that did not cleanly pass.

### Cleanup: fully autonomous, zero manual intervention

Both remaining rows (item 6's committed `SFG/GUTAB>`, item 9's
committed `RFG/ARMVN`) were removed via repeated `cancel_current_item()`
calls with no manual coordinate-click fallback -- unlike Phase 4.4 AND
Phase 4.5, both of which required at least one manual, out-of-band
click to finish cleanup. 4 attempts total: 2 failed
(`"could not invoke the 'Delete' context-menu item"`, then
`"row count did not decrease"` -- not further root-caused this
session), 2 succeeded, ending at 0 rows / $0.00 / "Saved", matching
the TEST project's starting state exactly. **This is the headline
result of this phase**: recovery is no longer 100%-manual-fallback-
dependent the way it was at the end of Phase 4.5.

### Full test suite

581 passed, 12 skipped (pre-existing, unrelated), 0 failed. Three more
tests than Phase 4.5's 578-passed baseline, matching the three new
`verify_quantity_committed()` regression tests. No regressions.

### `supports_live_execution`

Stays **`False`**.

| Gate criterion | Status |
|---|---|
| 1. Five single-row recovery trials pass | **met** (5/5) |
| 2. Three multi-row targeted-delete trials pass | **met** (3/3) |
| 3. Zero wrong rows deleted | **met** |
| 4. Post-commit quantity verification succeeds within the bounded timeout | **not met** -- item 6 timed out in the Stage 5 pilot |
| 5. Focused pilot has zero wrong selections | **met** |
| 6. No unintended mutations (within the pilot) | **met** |
| 7. Ambiguous/no-result cases stop safely | **met** |
| 8. All committed rows independently verified | **not met** -- item 6's commit was correct but not independently confirmed within budget |
| 9. TEST project ends empty/$0.00/Saved without manual cleanup | **met** -- first time this has been achieved in this project |
| 10. All tests pass | **met** -- 581 passed, 12 skipped (pre-existing), 0 failed |

**Single remaining blocker:** intermittent post-commit quantity-
verification timeout. The underlying committed data has been correct
in every case checked this session (including item 6's, confirmed
immediately after its timeout) -- this is a verification-confirmation
gap, not a data-integrity gap -- but the gate requires verification to
actually succeed within budget for every commit, and it didn't for one
of two committed items in the Stage 5 pilot. Given Stage 4's isolated
timing evidence showed 5/5 clean instant successes, the failure
appears to be a genuine residual intermittency (not yet reproduced
in a way that supports a further targeted fix) rather than a
systematic, always-reproducible defect -- but "usually works, verified
correct after the fact" does not meet this gate's bar of "verification
succeeds within the bounded timeout, every time, in the pilot."

### Recommendation

The primary objective of this phase -- deterministic, non-OCR recovery
-- succeeded clearly: 9/9 recovery trials, zero wrong-row deletions,
and the first fully-autonomous pilot cleanup in this project's history.
The remaining gap is narrower and more isolated than any prior phase's:
one intermittent verification timeout, on data that was actually
correct. Before revisiting `supports_live_execution`: (1) collect more
timing/OCR samples specifically around item 6-like cases (CAT/SEL
trusted-path items) to determine whether the intermittency correlates
with that path specifically or is genuinely random; (2) consider
whether `verify_quantity_committed()` should retry the ENTIRE
select-enter-commit sequence (not just re-poll a read) on a stable
`wrong_value`/`timeout`, since Stage 4 showed the underlying commit is
reliable even when verification isn't; (3) once quantity verification
is confirmed reliable across a larger sample, re-run this same 10-item
pilot once more and require both gate criteria 4 and 8 to pass cleanly
before flipping the flag.

## Phase 4.7: reliable unit + quantity verification -- full framework built and live-validated, `supports_live_execution` stays `False`, single blocker: category+selector OCR reliability outside SFG/RFG

Phase 4.7 built the unit-verification framework Phase 4.6 didn't have
(independent unit tracking, normalization, a disabled-by-default
conversion policy, identity-based row lookup before reading anything)
and found/fixed a blocking prerequisite bug plus two new OCR bugs
along the way. The deliberate incompatible-unit safety stop worked
correctly. The remaining gap is precisely diagnosed: row identification
by category+selector OCR is not yet reliable enough within the bounded
polling window, most severely for catalog categories never exercised
before this phase.

### Blocking prerequisite: search navigation was broken all session

Before any unit work could start, every search failed with a
misleading "no results popup" error. Root cause: this session's
window rendered with a real ~62px vertical drift, and
`focus_search()`/`_reset_scroll_state()` used RAW, uncorrected pixel
anchors for both the tab-bar click and the search-box click -- unlike
every grid-reading path in this file, which always applies
`_anchor_offset()`'s live-measured correction first. Investigation
found the two clicks needed OPPOSITE treatment: the tab bar
(Items/Components/.../Labor Summary) is fixed window chrome and must
stay uncorrected (applying the grid's offset to it overshot and
literally landed on a different tab, "Labor Minimums," reproduced
live); the search box IS part of the scrollable grid pane and DOES
need the correction (confirmed live: search failed with the raw
anchor, succeeded with the corrected one). Fixed accordingly -- not a
uniform rule, a per-element determination based on what each element
actually is.

### Stage 1-3: unit data model, normalization, conversion policy

`UnitVerificationResult` keeps `source_unit`, `expected_xactimate_unit`,
`observed_xactimate_unit` (raw OCR, never overwritten), and
`unit_normalized` as independent fields, with `unit_match_state` one
of `exact_match` / `normalized_synonym` / `verified_conversion` /
`source_unit_missing` / `expected_unit_missing` / `observed_unit_missing`
/ `incompatible` / `unreadable`. `_UNIT_SYNONYMS` contains only the
build spec's own evidence-backed examples (EA/EACH, HR/HOUR, DA/DAY,
WK/WEEK, MO/MONTH); SF/SQ, LF/SF, EA/LF, HR/EA are deliberately absent.
`_VERIFIED_UNIT_CONVERSIONS` is an empty dict by default (Stage 3
policy: conversions disabled by default) -- populated only transiently
by one test proving the mechanism works, never by production code
this phase. `check_unit_compatibility()` is a pure, module-level
function (directly unit-testable) implementing Stage 8's priority
order; a quantity match never overrides a unit conflict --
`CommittedRowVerification.compatibility` is derived from the unit
outcome alone.

### Stage 5/6/7: row identification, unit reading, quantity reading

`verify_committed_row(category, selector, expected_quantity,
source_unit, expected_xactimate_unit)` replaces reading from a
presumed last-row index with identity-based lookup: it polls the grid,
reads CAT+SEL at every row via `_read_category_selector_at()`, and
only proceeds to read quantity/unit once EXACTLY one row matches.
Zero matches within the timeout -> `commit_verification_failed`. More
than one match -> `conflicting_row` (refuses to guess). This is a real
behavior change from Phase 4.6's `verify_quantity_committed()`, which
always read the last row unconditionally regardless of whether that
was actually the row in question.

**New unit-reading strategy** (`_read_unit_at()`): tries 5 (scale,
PSM) combinations, applies a narrow, evidence-backed OCR-confusion
correction (`"UF"` -> `"LF"` -- Tesseract consistently misreads a real
"LF" as lowercase "u" at several scales; confirmed live, and safe
because none of the 6 evidence-backed real units start with "U"), and
requires 2+ combinations to agree on a real, known unit
(`_KNOWN_XACTIMATE_UNITS`) before returning a normalized value --
otherwise returns the raw text with no normalized value, surfaced as
`unreadable` rather than a guess. Investigation found the unit
column's ACTUAL position varies by row-highlight state MORE than
previously documented -- a THIRD real position (`x=1078-1089`) was
found live immediately after `commit_item()`, in addition to Phase
4.4's two ("highlighted-by-quantity-click" and "static"). The column
boundary was widened to `(1070, 1122)` to cover all three, relying on
the multi-scale/vocabulary strategy to extract the real word from
whatever margin surrounds it rather than a zero-tolerance tight crop.

**Live-caught, fixed**: the `selector` column crop can visually
include the neighboring `activity` symbol for a SHORT code (reproduced
live: real "GUTA" OCR'd as "GUTA &") -- both
`_read_category_selector_at()` and `_read_populated_fields_once()`
now keep only the first whitespace-separated OCR token, since a real
selector never legitimately contains a space.

### Stage 8: the deliberate incompatible-unit test -- passed

One of the 10-trial matrix's items (SFG/GUTAB>, a real LF item)
was deliberately given a wrong declared unit (`source_unit=
expected_xactimate_unit="EA"`). The pre-commit compatibility check
correctly classified this as `incompatible` / `hard_stop` and the
trial script stopped BEFORE `enter_quantity()` or `commit_item()` were
ever called -- exactly Stage 8/9's required behavior, confirmed live,
not just in the pure-logic unit tests.

### Stage 9/10: 10-trial live matrix across 6 evidence-backed units

Real Xactimate items confirmed live for each unit, beyond the
long-established SFG(LF)/RFG(SQ): `PLM/TLT` "Toilet" (EA),
`PNT/LAB` "Painter - per hour" (HR), `TMP/GEN` "Generator ... per
day" (DA), `CLN/FCC` "Clean and deodorize carpet" (SF).

| # | CAT/SEL | Unit | Qty | Outcome |
|---|---|---|---|---|
| 1 | SFG/GUTA | LF | 5 | committed, verified (quantity+unit both matched) |
| 2 | SFG/GUTC | LF | 2.5 | committed; unit read as `unreadable` post-commit |
| 3 | RFG/ARMVN | SQ | 3 | committed; row identification failed post-commit (`commit_verification_failed`) |
| 4 | PLM/TLT | EA | 1 | committed; row identification failed (category OCR: "PLM" read as "PLN") |
| 5 | PNT/LAB | HR | 7 | committed; row identified but quantity did not match within budget |
| 6 | TMP/GEN | DA | 2 | committed; row identification failed (category OCR: "TMP" read as "TMI") |
| 7 | CLN/FCC | SF | 10.5 | committed; row identification failed (category OCR: "CLN" read as "CLM") |
| 8 | SFG/GUTG | LF | 0.25 | pre-commit populated-fields read badly garbled; committed anyway, verification failed |
| 9 | SFG/GUTAB> | LF (declared EA) | 3 | **correctly hard-stopped before commit** (deliberate incompatible-unit test) |
| 10 | SFG/GUTA> | LF | 6 | committed, verified (quantity+unit both matched) |

Cleanup succeeded on every trial (10/10, zero manual intervention).
Zero wrong selections. Zero unintended mutations.

**Root cause of the identification failures, investigated per this
phase's explicit checklist**: NOT wrong-row identification (in every
case independently re-checked afterward, the correct row's data was
present and correct), NOT stale geometry, NOT duplicate CAT/SEL rows,
NOT save-state timing. It IS OCR inconsistency in category reading,
and it splits into two distinct sub-causes:

1. **New-category OCR is unvalidated and shows STABLE, deterministic
   misreads**: "PLM"->"PLN", "TMP"->"TMI", "CLN"->"CLM" reproduced
   across every scale/PSM tried (1x-6x, 4 PSM modes each) -- not
   per-attempt noise a multi-scale vote can out-vote. Critically, a
   narrow character-substitution fix (the same class of fix that
   safely corrected "UF"->"LF" for units) is NOT safe here: "M" and
   "N" are confused in BOTH directions across different real
   categories in the SAME session ("PLM" misread as "PLN", but "CLN"
   misread as "CLM") -- a blind "N"->"M" correction would fix one and
   break the other. This was investigated and deliberately NOT
   applied, per this phase's own conservative-correction rule ("a
   narrow OCR-confusion rule that cannot transform another valid
   [category] incorrectly").
2. **Even SFG/RFG -- the categories every prior phase validated --
   still show intermittent identification misses** under real timing
   pressure (item 3/RFG failed in the matrix; item 9/RFG failed again
   in the Stage 11 pilot below), though a manual re-check moments
   later always found the correct data present. This matches Phase
   4.6's already-documented residual timing/OCR intermittency, now
   shown to affect the NEW identity-based lookup the same way it
   affected the old last-row lookup.

### Stage 11: focused pilot rerun (same approved 10-item plan, unmodified)

TEST project confirmed at 0 rows before starting. Items 1-5, 7, 10
again correctly `REVIEW_REQUIRED` (genuine catalog near-ties,
reproduces Phase 4.4/4.5/4.6 exactly). Item 8 correctly `NO_MATCH`.

Items 6 (SFG/GUTAB>) and 9 (RFG/ARMVN) both reached `AUTO_SELECT`,
selected correctly, passed the pre-commit unit compatibility check
(`compatible`), and committed. Item 6's row WAS correctly identified
post-commit and its unit verified `compatible` -- but its quantity did
not match within the polling budget (`quantity_matched=False`),
though a direct re-read of that exact row moments later returned the
correct value (`1.0`, matching what was entered). Item 9's row
identification itself failed within budget
(`commit_verification_failed`) -- a direct re-check afterward found
`RFG/ARMVN` present with the correct quantity (`3.0`). In both cases
the underlying committed data was correct every time it was
independently checked; only the bounded, live verification call
failed to confirm it in time.

Cleanup succeeded fully autonomously (2 of 4 attempts failed with the
same class of transient error `cancel_current_item()` has shown since
Phase 4.6, 2 succeeded, zero manual intervention) -- final state
visually confirmed identical to the start: 0 rows, $0.00, "Saved".

### Full test suite

600 passed, 12 skipped (pre-existing, unrelated), 0 failed -- 19 more
tests than Phase 4.6's 581-passed baseline, matching the 19 new
regression tests (pure-logic unit-compatibility tests plus
monkeypatched `verify_committed_row()` polling-logic tests: delayed-
but-correct identification, stable wrong-unit detection, conflicting-
row, timeout, and post-failure cleanup information availability). No
regressions.

### `supports_live_execution`

Stays **`False`**.

| Gate criterion | Status |
|---|---|
| 1. Committed rows identified by stable identity | **not met** -- failed for RFG/ARMVN in both the matrix and the pilot, and for every new-category item |
| 2. Committed quantities independently verified | **not met** -- correct data, but verification itself failed to confirm within budget on multiple trials |
| 3. Committed units independently verified | **met when row identification succeeded**; not reachable when it didn't |
| 4. Source/observed units compatible or verified-conversion | **met** whenever reached |
| 5. Unit mismatches stop before unsafe entry/commit | **met** -- the deliberate incompatible-unit trial stopped correctly, live |
| 6. 10-trial matrix passes | **not met** -- 2/10 cleanly verified, 1/10 correctly safety-stopped, 7/10 committed with correct data but unconfirmed verification |
| 7. Cleanup succeeds without manual intervention | **met** -- 10/10 in the matrix, 1/1 in the pilot |
| 8. Zero wrong selections | **met** |
| 9. No unintended mutations | **met** |
| 10. Ambiguous/no-result cases stop safely | **met** |
| 11. TEST project ends empty/$0.00/Saved | **met** |
| 12. All tests pass | **met** -- 600 passed, 12 skipped (pre-existing), 0 failed |

**Single remaining blocker:** category+selector OCR reliability for
row identification, within the bounded polling window, is not yet
sufficient for `verify_committed_row()` to consistently confirm a
correct commit -- most severely for catalog categories this project
has never exercised before this phase (PLM, TMP, CLN show stable,
deterministic misreads that cannot be safely auto-corrected without
risking a DIFFERENT real category), and still intermittently even for
the two categories (SFG, RFG) every prior phase validated. This is a
verification-confirmation gap, not a data-integrity gap: every
committed row's actual data was correct in 100% of cases independently
re-checked this session.

### Recommendation

The unit-verification framework itself is complete and working:
normalization, disabled-by-default conversions, identity-first
lookup, and the pre-commit incompatibility safety stop all passed
live, not just in isolated tests. The remaining gap is narrower and
more specific than any prior phase's: category-column OCR accuracy,
not the surrounding verification machinery. Before revisiting
`supports_live_execution`: (1) build a proper evidence-backed
category vocabulary (beyond the 6 categories this project has now
touched) with a disambiguation strategy that accounts for the
bidirectional M/N-style confusion found this session (e.g. weighting
by which candidate correction is closer to text ALSO confirmed present
elsewhere on the same row, not category text in isolation); (2)
investigate whether `verify_committed_row()`'s polling budget or
attempt cadence needs adjustment specifically for the identification
step (distinct from the quantity-specific tuning Phase 4.6 already
did); (3) once row identification is reliably confirmed across a
larger, more diverse sample, re-run both the unit matrix and the
focused pilot and require all twelve gate criteria before flipping the
flag.

## Phase 4.8: trustworthy commit verification -- row identity moved off OCR text search onto row-count-delta structural evidence, `supports_live_execution` stays `False`, single blocker: intermittent quantity-cell OCR at the identified row

Phase 4.7 left one precisely diagnosed gap: `verify_committed_row()`
identified the committed row by searching every row's OCR'd
category+selector text for a match, and that search was not reliable
enough within the bounded polling window, worst for catalog categories
never exercised before Phase 4.7 (PLM, TMP, CLN) and still
intermittently for SFG/RFG. Phase 4.8's mandate was explicit: stop
tuning OCR, find the strongest available independent evidence that the
committed row is the row that was intentionally selected, and build
verification around that -- treating this as a validation exercise,
not a construction exercise.

### Investigation, in the required order

**Semantic application data.** Three independent clipboard methods
tried live against a real committed row (plain `Ctrl+C`, `Ctrl+C` via
the row's context-menu Copy item at its measured UIA structural index,
`Ctrl+A`+`Ctrl+C`) -- all three left the clipboard's format list
unchanged from baseline (3 unrelated custom formats, never
`CF_UNICODETEXT`/`CF_TEXT`). **Rejected**: Xactimate's grid does not
place row text on the clipboard through any of these paths.
Double-clicking a grid row (the standard "open details" gesture) did
not open a details dialog; it instead changed Quick Entry's panel to a
"Number of Items Selected" multi-select summary, confirmed by a full
screenshot and `_unexpected_dialog_present()` returning `False`.
**Rejected**: no details dialog exists on this path. A fresh UIA tree
walk of the main window this session found exactly one element with
zero children, reconfirming Phase 4.1's original finding under current
conditions; MSAA was not re-walked with new code this session (Phase
4.1's already-thorough MSAA finding -- zero peers, matching UIA -- was
relied on directly rather than re-derived, to keep this phase's time
budget on verification, not re-answering an already-answered
question). **Rejected**: no accessible grid-row API exists beyond what
every prior phase has already used (bounding rectangles + OCR).

**Before/after state comparison and row-insertion evidence.** Every
row insertion observed anywhere in this project, across every phase,
has appended at the end of the grid. Combined with
`snapshot_grid_identities()` (already used by `delete_existing_item()`
since Phase 4.6 to verify deletions), a snapshot taken before an item
is selected, compared against a snapshot taken after commit, makes the
committed row's position and the surrounding rows' integrity provable
without reading a single character of OCR: **adopted** as the primary
identification mechanism.

**Exact dropdown provenance.** `select_candidate()` already acts on a
`Candidate` read via exact UIA text (`extraction_confidence=1.0`), not
OCR -- so WHAT was intended is already certain before verification
ever runs. **Adopted implicitly**: `verify_commit()`'s `category`/
`selector` parameters are that already-certain identity; the method
never re-derives it.

**Description, selector, unit, quantity, category OCR.** All five
remain OCR-based and were downgraded relative to the structural
evidence above. Quantity and unit remain load-bearing (dedicated hard
`trust_state`s, never downgraded to "supporting" -- a quantity or unit
conflict is never overridden by a quantity match, continuing Phase
4.7's rule). Description is read and recorded for the evidence bundle
but used in no automated pass/fail decision -- fuzzy-matching noisy
OCR description text reliably is exactly the kind of new OCR-tuning
work this phase does not do. Category and selector OCR at the
now-known row are corroborating only: unreadable does not block
`VERIFIED`; readable-but-contradicting downgrades to
`REVIEW_REQUIRED`, never a hard stop by itself.

### Design: `verify_commit()` / `CommitVerification`

Replaces `verify_committed_row()`/`CommittedRowVerification`
completely (retired, not left dangling, matching the Phase 4.6
precedent for the old OCR-click deletion method). Callers snapshot the
grid with `snapshot_grid_identities()`, then act, then call
`verify_commit(before_snapshot, category, selector, expected_quantity,
...)`. `trust_state` is one of `VERIFIED`, `REVIEW_REQUIRED`,
`VERIFICATION_FAILED`, `CONFLICTING_ROW`, `UNIT_MISMATCH`,
`QUANTITY_MISMATCH` -- see the method's docstring in
`windows_adapter.py` for the exact precedence rules.

**Live-caught snapshot-timing bug, found and fixed before any trial
counted as evidence:** the first live run snapshotted immediately
before `commit_item()`, matching the initial (wrong) assumption that
commit inserts the row. Every trial reported `VERIFICATION_FAILED`
(row count never changed within budget) even though the row was
provably committed (a `leave_committed=True` trial ended with 1 row on
screen). Direct evidence resolved this: the snapshot taken right after
`enter_quantity()` (before `commit_item()`) already showed 1 row, with
noisy category OCR on the not-yet-saved cell (e.g. `SFG` read as
`zs`). Xactimate inserts the pending row into the grid as soon as a
candidate is selected; `commit_item()` (Ctrl+S) finalizes/saves that
row rather than inserting a new one. Fixed by moving the required
snapshot point to before `select_candidate()`, documented explicitly
in both `snapshot_grid_identities()`'s and `verify_commit()`'s
docstrings so no future caller repeats the mistake. All live trials
below use the corrected snapshot point.

**Live-caught settle-timing gap, addressed with one bounded retry:**
after the fix above, two isolated trials (PLM/TLT, PNT/LAB) and both
committed pilot items still returned an unreadable quantity at the
freshly-identified row on the first read. Added one bounded
settle-and-reread (0.4s delay, one retry, mirroring the polling
pattern already used everywhere else in this method) for the
quantity/unit reads specifically. Re-running the same trials afterward
showed the retry did not change the outcome for PLM/TLT or PNT/LAB --
elapsed time confirms the retry fired, but the second read also came
back empty. This is treated as a genuine, currently-unresolved OCR
limitation on those particular renders, not a bug -- consistent with
this file's existing Phase 4.5/4.6 notes that no single scale or
timing reliably reads every quantity value. No further OCR tuning was
attempted, per this phase's explicit scope.

### Isolated live trial matrix (9 trials, direct CAT/SEL path)

Deliberately reused the categories Phase 4.7 flagged as unreliable for
OCR-text-search row identification (PLM/TLT, TMP/GEN, CLN/FCC,
RFG/ARMVN), plus a deliberate 2-row sequence (trial 8 left committed,
trial 9 committed against it) to exercise the unchanged-pre-existing-
rows check with more than one row present.

| # | CAT/SEL | qty/unit | `trust_state` | row_index | preexisting unchanged | cat/sel OCR agrees | qty matched | unit compat |
|---|---|---|---|---|---|---|---|---|
| 1 | SFG/GUTA | 5/LF | VERIFIED | 0 | true | true | true | compatible |
| 2 | RFG/ARMVN | 3/SQ | REVIEW_REQUIRED | 0 | true | false (`RFC`) | true | compatible |
| 3 | PLM/TLT | 1/EA | REVIEW_REQUIRED | 0 | true | false (`PLN`/`jm`) | **unreadable** | compatible |
| 4 | PNT/LAB | 7/HR | REVIEW_REQUIRED | 0 | true | true | **unreadable** | **unreadable** |
| 5 | TMP/GEN | 2/DA | REVIEW_REQUIRED | 0 | true | false (unreadable cat) | true | compatible |
| 6 | CLN/FCC | 10.5/SF | REVIEW_REQUIRED | 0 | true | false (`cu`) | true | compatible |
| 7 | SFG/GUTG | 0.25/LF | REVIEW_REQUIRED | 0 | true | false (`GUIG`) | true | **unreadable** |
| 8 | SFG/GUTA | 6/LF | VERIFIED | 0 | true | true | true | compatible |
| 9 | SFG/GUTC | 2.5/LF (row 2, after 8) | VERIFIED | 1 | true | true | true | compatible |

Row identity (row-count delta + deterministic last-row position +
unchanged pre-existing rows): **9/9 correct**, including trial 9's
proof that a second commit against a non-empty grid correctly
identifies the new row as index 1 while confirming trial 8's row at
index 0 was untouched. Zero `CONFLICTING_ROW`, zero
`VERIFICATION_FAILED`, zero wrong selections, zero unintended
mutations. Every `REVIEW_REQUIRED` was caused by category/selector or
quantity/unit OCR falling short, never by structural misidentification
-- and every one correctly stopped short of `VERIFIED` rather than
guessing. Cleanup succeeded for all 9 trials plus the final sweep
(0 rows confirmed after).

### Repeated production pilot (same approved 10-item plan, unmodified)

TEST project confirmed at 0 rows before starting. Items 1-5, 7, 10
again correctly `REVIEW_REQUIRED` (genuine catalog near-ties,
reproduces Phase 4.4/4.5/4.6/4.7 exactly -- ranking/phrase-generator
code has not changed since before any phase-specific adapter work, per
`git log`). Item 8 correctly `NO_MATCH`.

Items 6 (SFG/GUTAB>) and 9 (RFG/ARMVN) both reached `AUTO_SELECT`,
selected correctly, passed the pre-commit unit compatibility check,
and committed. Both were **structurally identified correctly**
(row_index 0 and 1 respectively, pre-existing rows unchanged both
times) -- a direct improvement over Phase 4.7's same two pilot items,
where item 9's row identification itself failed within budget. Both
came back `REVIEW_REQUIRED` because the quantity could not be read at
the identified row even after the settle-retry (item 6: qty `1`, item
9: qty `3`) -- unit read and compatible in both. Cleanup succeeded (2
of the ~6 attempts failed with the same transient
`cancel_current_item()` error class documented since Phase 4.6, later
attempts in the same bounded retry succeeded) -- final state confirmed
0 rows.

### Full test suite

602 passed, 12 skipped (pre-existing, unrelated -- `tests/integration/
test_ui_pipeline_service.py` and `test_verified_catalog_pipeline.py`
fail to collect on an unrelated missing `estimate_extractor.output`
module; confirmed present on `main` before this phase's changes via
`git stash`), 0 failed. `CommittedRowVerification`/
`verify_committed_row()`'s 6 tests were replaced with 9 new tests
against `CommitVerification`/`verify_commit()` (structural delta
identification, unreadable-category-does-not-block-VERIFIED,
readable-contradicting-category-downgrades, row-count-delta-other-
than-1, pre-existing-rows-changed, timeout, quantity mismatch, unit
mismatch, cleanup-identity-availability). No regressions.

### `supports_live_execution`

Stays **`False`**.

| Gate criterion | Status |
|---|---|
| 1. Committed rows identified by stable identity | **met** -- 11/11 live commits (9 isolated + 2 pilot) structurally identified correctly, including the multi-row case |
| 2. Committed quantities independently verified | **not met** -- correct data whenever independently re-checked, but the quantity cell was unreadable in 4/11 live commits even after one bounded settle-retry |
| 3. Committed units independently verified | **met** whenever the row was identified (11/11); unreadable in 2/11, correctly downgraded rather than assumed |
| 4. Source/observed units compatible or verified-conversion | **met** in every case reached |
| 5. Unit mismatches stop before unsafe entry/commit | **met** -- unchanged from Phase 4.7, not re-exercised this phase (no new pre-commit logic changed) |
| 6. Category OCR is supporting evidence only, never primary identification | **met** -- row identity never depends on category/selector OCR; contradictions downgrade to REVIEW_REQUIRED, never block VERIFIED when unreadable |
| 7. Cleanup succeeds without manual intervention | **met** -- 9/9 isolated trials, 1/1 pilot cleanup pass (bounded automatic retries absorbed all transient failures) |
| 8. Zero wrong selections | **met** |
| 9. No unintended mutations | **met** -- pre-existing rows independently confirmed unchanged on every commit |
| 10. Ambiguous/no-result cases stop safely | **met** |
| 11. TEST project ends empty/$0.00/Saved | **met** |
| 12. All tests pass | **met** -- 602 passed, 12 skipped (pre-existing), 0 failed |

**Single remaining blocker:** quantity-cell OCR immediately after
commit is intermittently unreadable (4/11 live commits this phase,
across both single-digit and multi-character values, with no
reproducible per-value pattern -- a settle-retry did not resolve it).
Row identity is no longer the blocker -- it was 100% correct across
every live commit this phase, including the two pilot items that
Phase 4.7 could not both identify. The remaining gap is narrower and
different in kind: an unreadable, not incorrect, data-confirmation
read on an already-correctly-identified row. The trust policy already
handles this safely (`REVIEW_REQUIRED`, never a false `VERIFIED`,
never a wrong identification) -- but a policy that routes a
meaningful fraction of live commits to human review is not yet strong
enough that a reviewer would reasonably trust the adapter's
`VERIFIED` conclusion as the normal case.

### Recommendation

Row identification is solved: the structural approach (row-count delta
+ deterministic last-row position + unchanged-pre-existing-rows) is
correct with no observed exceptions across every category tried this
phase, including the ones Phase 4.7 could never reliably identify. The
remaining work is narrowly scoped to the post-commit quantity/unit
read, distinct from row identification and distinct from category OCR
(now explicitly out of the trust path). Smallest next step: instrument
several more post-commit quantity reads across a range of values and
delays to determine whether the unreadable cases are timing-bound
(a longer or multi-attempt settle window would fix them) or
scale/rendering-bound (the same failure mode Phase 4.5/4.6 already
documented for the pre-commit quantity cell, which no single fixed
scale resolved) -- then decide between a longer bounded retry budget
or accepting `REVIEW_REQUIRED` as the correct steady-state outcome for
a meaningful fraction of commits and re-scoping the gate accordingly.
