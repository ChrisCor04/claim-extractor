"""Renders the "Build Estimate" screen (Phase 5.0 Priority 4; Phase 5.2
Safe Autofill): builds a persisted, group-by-group ExecutionPlan from
every APPROVED line item, displays it grouped by Section exactly like
Mapping Review, and offers a dry-run preview and a live execute -- both
reusing execution_runner.run_execution_plan() unchanged. A real
WindowsXactimateAdapter is only ever constructed here (lazy import,
matching windows_adapter.py's own platform-safety convention) --
nothing above this file touches desktop automation.

Live execution stays gated by WindowsXactimateAdapter.supports_live_
execution, which defaults to False at the class level and is NEVER
flipped globally by this panel. "Safe Autofill" here means: the user
explicitly confirms the target project (constructing a real adapter and
independently verifying application/project/display-profile state),
then explicitly opts in via a checkbox that is only offered when
service.compute_capability_flags() reports safe_autofill_available --
which itself requires a positively-verified live adapter with working
group control. Only THEN does this panel set supports_live_execution=
True on that one constructed adapter INSTANCE, for that one run. See
docs/build-estimate.md.
"""

from __future__ import annotations

import csv
import io
import json
import subprocess
import time
from pathlib import Path

import streamlit as st

from estimate_extractor.mapping.pipeline import DEFAULT_CONFIG_DIR
from estimate_extractor.xactimate_lookup import phrase_generator, ranking, service
from estimate_extractor.xactimate_lookup.execution_plan import (
    CURRENT_SCHEMA_VERSION,
    ExecutionPlanError,
    GROUP_COMPLETED,
    RUN_STATE_COMPLETED,
    RUN_STATE_PAUSED,
    STOP_REASON_GROUP_VERIFICATION_FAILURE,
    STOP_REASON_NORMAL_COMPLETION,
    STOP_REASON_PROJECT_LEVEL_HARD_STOP,
    STOP_REASON_PROJECT_VERIFICATION_FAILURE,
    STOP_REASON_PROTECTED_ROW_REFUSAL,
    STOP_REASON_TASK_LEVEL_STOPS,
    TASK_COMPLETED,
    TASK_FAILED,
    TASK_PENDING,
    TASK_REVIEW_REQUIRED,
    TASK_SKIPPED,
    TEST_ONLY_PROJECT_NAME,
    build_execution_plan,
    classify_unmapped_rows,
    diagnose_run,
    is_plan_stale,
    load_execution_plan,
    reset_unfinished_tasks,
    save_execution_plan,
    task_has_committed_row,
)
from estimate_extractor.xactimate_lookup.execution_reports import (
    TASK_CSV_COLUMNS,
    _task_row,
    write_all_execution_reports,
)
from estimate_extractor.xactimate_lookup.execution_runner import run_execution_plan, skip_task
from estimate_extractor.xactimate_lookup.models import LOOKUP_PATH_DESCRIPTION_SEARCH, LOOKUP_PATH_TRUSTED

EVIDENCE_DIR = DEFAULT_CONFIG_DIR.parent / "automation_evidence"

#: Phase 5.5D Stage 1: captured once, at module import time -- proves
#: (or disproves) whether the process currently serving this page has
#: actually loaded the CURRENT source. Streamlit's file-watcher re-
#: imports a changed module on its own, so this timestamp jumping
#: forward on a rerun after an edit is the expected, useful signal;
#: it staying frozen across many edits is the "stale process" symptom
#: this exists to catch (see the live incident this phase responds to).
_MODULE_IMPORTED_AT = time.strftime("%Y-%m-%dT%H:%M:%S")


def _get_git_revision() -> str | None:
    """Best-effort, never raises -- None if git isn't available or this
    isn't a git checkout. `git -C <dir>` lets git discover the repo
    root itself from this file's own directory rather than guessing a
    fixed number of parents."""
    try:
        result = subprocess.run(
            ["git", "-C", str(Path(__file__).resolve().parent), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


def _get_git_status_short() -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(Path(__file__).resolve().parent), "status", "--short"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None

#: Task states a human still needs to look at -- never conflated with
#: TASK_COMPLETED (Phase 5.2 Stage 9: "never present 'completed' when a
#: task only reached REVIEW_REQUIRED").
UNRESOLVED_TASK_STATES = (TASK_REVIEW_REQUIRED, TASK_FAILED, TASK_SKIPPED)


def _construct_windows_adapter(xactimate_project_name: str):
    """Lazily imports and constructs a real WindowsXactimateAdapter --
    isolated in one place so a non-Windows/missing-dependency/no-running-
    Xactimate failure produces one clear error message instead of an
    unhandled exception."""
    from estimate_extractor.xactimate_lookup.windows_adapter import WindowsXactimateAdapter

    return WindowsXactimateAdapter(expected_project_name=xactimate_project_name, evidence_dir=EVIDENCE_DIR)


def _task_table_rows(tasks) -> list[dict]:
    return [
        {
            "Row": t.row_label,
            "Line item": t.line_item_id,
            "Description": t.description,
            "CAT": t.category,
            "SEL": t.selector,
            "Qty (source)": t.source_quantity,
            "Unit (source)": t.source_unit,
            "Qty (observed)": t.observed_quantity,
            "Unit (observed)": t.observed_unit,
            "State": t.state,
            "Trust state": t.trust_state,
            "Detail": t.stop_detail or t.error or "",
        }
        for t in tasks
    ]


def _unresolved_table_rows(tasks) -> list[dict]:
    """The exact column set Phase 5.2 Stage 9 requires -- source row,
    source page, group, description, source/entered/observed quantity
    and unit, CAT/SEL, lookup method, result, reason, evidence, recovery
    outcome. Built from `_task_row()` (execution_reports.py) so this
    table and the JSON/CSV export always describe a task identically."""
    rows = []
    for t in tasks:
        r = _task_row(t)
        rows.append(
            {
                "Source row": r["row_label"],
                "Source page": r["source_page"],
                "Group": r["section_name"],
                "Description": r["description"],
                "Source qty": r["source_quantity"],
                "Source unit": r["source_unit"],
                "Entered qty": r["entered_quantity"],
                "Observed qty": r["observed_quantity"],
                "Expected unit": r["expected_unit"],
                "Observed unit": r["observed_unit"],
                "CAT": r["category"],
                "SEL": r["selector"],
                "Lookup method": r["lookup_strategy"],
                "Result": r["state"],
                "Reason": r["stop_detail"] or r["stop_reason"] or r["error"] or "",
                "Evidence": r["evidence_path"] or "",
                "Recovery": r["recovery_outcome"] or "",
            }
        )
    return rows


def _capability_flags_rows(flags) -> list[dict]:
    return [
        {"Capability": "Planning available", "Value": flags.planning_available},
        {"Capability": "Live adapter available", "Value": flags.live_adapter_available},
        {"Capability": "Group control available", "Value": flags.group_control_available},
        {"Capability": "Safe Autofill available", "Value": flags.safe_autofill_available},
        {"Capability": "Resume available", "Value": flags.resume_available},
        {"Capability": "Production project allowed", "Value": flags.production_project_allowed},
        {"Capability": "Unattended mode allowed", "Value": flags.unattended_mode_allowed},
        {"Capability": "Multi-group creation available", "Value": flags.multi_group_creation_available},
    ]


def _resume_instructions(project_name: str) -> str:
    return (
        f"Open Xactimate / Open project {project_name!r} / Return to the Estimate Items screen / "
        f"Click 'Confirm project' above, then Execute to resume."
    )


def _execution_report_csv_bytes(plan) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, quoting=csv.QUOTE_MINIMAL)
    writer.writerow(TASK_CSV_COLUMNS)
    for task in plan.tasks:
        row = _task_row(task)
        writer.writerow([row[col] for col in TASK_CSV_COLUMNS])
    return buf.getvalue().encode("utf-8")


def _execution_report_json_bytes(plan) -> bytes:
    summary = plan.summary()
    data = {
        **plan.to_dict(),
        "summary": {
            "completed": summary.completed,
            "review_required_count": len(summary.review_required_labels),
            "skipped": summary.skipped,
            "failed_count": len(summary.failed_labels),
            "total": summary.total,
        },
    }
    return json.dumps(data, indent=2, default=str).encode("utf-8")


def render_build_estimate_panel(project_dir: Path, project_slug: str) -> None:
    import pandas as pd

    st.subheader("Build Estimate")
    st.caption(
        "Builds a persisted, group-by-group execution plan from every APPROVED line item in Mapping Review, "
        "then drives it into Xactimate group by group -- creating/selecting/verifying each group before any "
        "item is entered, and independently verifying every commit. See docs/build-estimate.md."
    )

    plan = load_execution_plan(project_dir)

    # Phase 5.5D Stage 1: proves (or disproves) which code is actually
    # running BEFORE anything else on this page -- a live incident this
    # phase responds to was caused in part by testing against a stale
    # Streamlit process that had never re-imported current source.
    # Always visible, never behind a button click.
    with st.expander("App / plan diagnostics (Phase 5.5D)", expanded=False):
        git_revision = _get_git_revision()
        git_status = _get_git_status_short()
        d1, d2 = st.columns(2)
        d1.caption(f"App source revision: `{git_revision or 'unknown (not a git checkout?)'}`")
        d2.caption(f"App process/module import time: `{_MODULE_IMPORTED_AT}`")
        if git_status:
            st.caption("Uncommitted working-tree changes present (this process is running EDITED, not just committed, source):")
            st.code(git_status, language="diff")
        else:
            st.caption("Working tree clean relative to the last commit (or git unavailable).")
        if plan is not None:
            st.caption(f"Execution-plan created: `{plan.created_at}` -- updated: `{plan.updated_at}`")
            st.caption(f"Execution-plan schema version: `{plan.schema_version}` (current: `{CURRENT_SCHEMA_VERSION}`)")
            if is_plan_stale(plan):
                st.warning(
                    "This persisted plan predates the current execution code's schema -- rebuild it "
                    "(\"Build / refresh execution plan\" / \"Rebuild TEST plan\" below) before Execute. "
                    "Execute will refuse to run against it as-is."
                )
        else:
            st.caption("No persisted execution plan yet.")

    # Phase 5.5D Stage 2: full task-table audit of the CURRENTLY
    # persisted plan, plus exact counts -- available before any
    # confirmation/Execute, so a stale or malformed plan is visible up
    # front rather than only discovered mid-run.
    if plan is not None:
        with st.expander("Plan audit -- full task table (Phase 5.5D)", expanded=False):
            audit_rows = []
            for t in plan.tasks:
                r = _task_row(t)
                last_attempt = t.search_attempts[-1] if t.search_attempts else None
                audit_rows.append({
                    "Source row": r["row_label"],
                    "Line item": r["line_item_id"],
                    "Group": r["section_name"],
                    "Source description": r["description"],
                    "CAT": r["category"],
                    "SEL": r["selector"],
                    "Began unmapped": r["began_unmapped"],
                    "Lookup strategy": r["lookup_strategy"],
                    "Requested strategy": r["requested_lookup_strategy"],
                    "Actual strategy": r["actual_lookup_strategy"],
                    "Strategy reason": r["lookup_strategy_reason"],
                    "Search phrase (last attempt)": last_attempt["search_text"] if last_attempt else None,
                    "Status": r["state"],
                    "Stop reason": r["stop_reason"],
                    "Recovery outcome": r["recovery_outcome"],
                    "Completed at": r["completed_at"],
                })
            st.dataframe(pd.DataFrame(audit_rows), use_container_width=True, hide_index=True)

            none_none_tasks = [
                t for t in plan.tasks
                if (t.category is None and t.selector is None and t.actual_lookup_strategy is None and t.error and "None" in str(t.error))
            ]
            missing_began_unmapped = [t for t in plan.tasks if not hasattr(t, "began_unmapped")]
            mismatched_cat_sel_routing = [
                t for t in plan.tasks
                if t.lookup_strategy == "test_description_first" and t.actual_lookup_strategy == LOOKUP_PATH_TRUSTED
            ]
            a1, a2, a3, a4 = st.columns(4)
            a1.metric("Completed", sum(1 for t in plan.tasks if t.state == TASK_COMPLETED))
            a2.metric("Review required", sum(1 for t in plan.tasks if t.state == TASK_REVIEW_REQUIRED))
            a3.metric("Failed", sum(1 for t in plan.tasks if t.state == TASK_FAILED))
            a4.metric("Skipped", sum(1 for t in plan.tasks if t.state == TASK_SKIPPED))
            a5, a6, a7, a8 = st.columns(4)
            a5.metric("Not attempted", sum(1 for t in plan.tasks if t.state == TASK_PENDING))
            a6.metric("Terminal skipped on rerun", plan.last_run_skipped_already_terminal)
            a7.metric("Routed: CAT/SEL", sum(1 for t in plan.tasks if t.actual_lookup_strategy == LOOKUP_PATH_TRUSTED))
            a8.metric("Routed: description", sum(1 for t in plan.tasks if t.actual_lookup_strategy == LOOKUP_PATH_DESCRIPTION_SEARCH))
            a9, a10, a11 = st.columns(3)
            a9.metric('Contains "None None"', len(none_none_tasks))
            a10.metric("Missing began_unmapped", len(missing_began_unmapped))
            a11.metric("test_description_first routed by CAT/SEL", len(mismatched_cat_sel_routing))
            if mismatched_cat_sel_routing:
                st.error(
                    f"{len(mismatched_cat_sel_routing)} task(s) requested test_description_first routing but "
                    f"were actually routed by CAT/SEL -- this is exactly the unsafe-routing incident Phase "
                    f"5.5B/5.5D exist to prevent. Rebuild the plan and re-run."
                )

    # Phase 5.5A: project confirmation must render BEFORE any early
    # return caused by an empty/missing plan -- a project with zero
    # approved rows (and nothing built yet) used to hit `plan is None`
    # or the approved-only ExecutionPlanError below and `return` before
    # this section ever ran, permanently hiding the "Confirm project"
    # button (and therefore the TEST-only checkbox, which depends on a
    # confirmation that could never happen). See docs/build-estimate.md.
    st.markdown("---")
    st.markdown("**Target Xactimate project**")
    xactimate_project_name = st.text_input(
        "Xactimate project name (must match the project currently open in Xactimate)",
        key="build_estimate_xactimate_project_name",
    )

    if st.button("Confirm project", key="build_estimate_confirm_project", disabled=not xactimate_project_name.strip()):
        try:
            adapter = _construct_windows_adapter(xactimate_project_name.strip())
        except Exception as exc:  # pragma: no cover -- exercised live on Windows only
            st.session_state["build_estimate_confirmation"] = {"project_name": xactimate_project_name.strip(), "error": str(exc)}
        else:
            diagnostics = service.run_diagnostics(adapter)
            flags = service.compute_capability_flags(adapter, diagnostics)
            display_profile = adapter.verify_display_profile() if hasattr(adapter, "verify_display_profile") else None
            st.session_state["build_estimate_confirmation"] = {
                "project_name": xactimate_project_name.strip(),
                "application_verified": diagnostics.application_verified,
                "project_verified": diagnostics.project_verified,
                "flags": flags,
                "display_profile": display_profile,
                "error": None,
            }
        st.rerun()

    confirmation = st.session_state.get("build_estimate_confirmation")
    project_confirmed = False
    display_profile_ok = True
    safe_autofill_ready = False
    #: Phase 5.5C Stage 10: defaults False (never assume live sibling-
    #: group creation is available before a real adapter has positively
    #: confirmed it) -- only set from confirmation["flags"] below.
    multi_group_creation_available = False
    if confirmation and confirmation.get("project_name") == xactimate_project_name.strip():
        if confirmation.get("error"):
            st.error(f"Could not construct the Xactimate adapter: {confirmation['error']}")
        else:
            flags = confirmation["flags"]
            display_profile = confirmation["display_profile"]
            project_confirmed = bool(confirmation["application_verified"] and confirmation["project_verified"])
            multi_group_creation_available = flags.multi_group_creation_available

            if project_confirmed:
                st.success(f"Confirmed: Xactimate is open on project {xactimate_project_name.strip()!r}, Estimate Items screen.")
            else:
                st.error(
                    "Could not positively identify the target Xactimate project -- refusing to proceed. "
                    + _resume_instructions(xactimate_project_name.strip())
                )

            # Shown regardless of confirmation outcome -- exactly when a
            # user needs "why isn't this available" visibility most.
            with st.expander("Capability flags", expanded=not project_confirmed):
                st.dataframe(pd.DataFrame(_capability_flags_rows(flags)), use_container_width=True, hide_index=True)
                for note in flags.notes:
                    st.caption(note)

            display_profile_ok = display_profile is None or display_profile["ok"]
            if display_profile is not None:
                with st.expander("Display / calibration check", expanded=not display_profile["ok"]):
                    for c in display_profile["checks"]:
                        st.caption(c)
                    if display_profile["ok"]:
                        st.success("Display profile matches the validated calibration -- safe to run live.")
                    else:
                        st.error("Display profile check failed -- live execution is blocked:")
                        for r in display_profile["blocking_reasons"]:
                            st.caption(f"- {r}")

            safe_autofill_ready = project_confirmed and flags.safe_autofill_available and display_profile_ok

    # Phase 5.5: TEST-only option to also include rows with no CAT/SEL
    # yet (searched live by description instead of excluded outright).
    # Only ever true when the project the user actually confirmed LIVE,
    # this run, is exactly "TEST" -- never a stale/typed-but-unconfirmed
    # value.
    test_only_confirmed = bool(
        project_confirmed and display_profile_ok and xactimate_project_name.strip() == TEST_ONLY_PROJECT_NAME
    )
    include_unmapped = False
    if test_only_confirmed:
        include_unmapped = st.checkbox(
            "Include rows missing CAT/SEL and search by description",
            key="build_estimate_include_unmapped_rows",
        )
        st.warning(
            "TEST only. Unmapped rows will be searched by description. "
            "Existing mapping approval rules are unchanged outside this run."
        )
        if include_unmapped:
            eligibility = classify_unmapped_rows(project_dir)
            counts = eligibility.counts()
            ec1, ec2, ec3, ec4, ec5 = st.columns(5)
            ec1.metric("Mapped rows", counts["mapped"])
            ec2.metric("Unmapped, description-search", counts["unmapped_eligible"])
            ec3.metric("Blocked: missing quantity", counts["blocked_missing_quantity"])
            ec4.metric("Blocked: missing unit", counts["blocked_missing_unit"])
            ec5.metric("Blocked: unresolved group", counts["blocked_unresolved_group"])

    st.markdown("---")
    col1, col2 = st.columns(2)
    if col1.button("Build / refresh execution plan from approved items"):
        try:
            if include_unmapped:
                plan = build_execution_plan(
                    project_dir, project_slug,
                    include_unmapped_rows=True, xactimate_project_name=xactimate_project_name.strip(),
                )
            else:
                plan = build_execution_plan(project_dir, project_slug)
            save_execution_plan(plan, project_dir, allow_shrink=True)
            st.success(f"Built a plan with {len(plan.tasks)} task(s) across {len(plan.groups)} group(s).")
        except ExecutionPlanError as exc:
            st.error(str(exc))

    if plan is None:
        st.info(
            "No execution plan yet -- approve line items in Mapping Review, or (TEST only, once confirmed above) "
            "check \"Include rows missing CAT/SEL\", then build a plan here."
        )
        return

    st.caption(f"Plan {plan.plan_id} -- run_state: **{plan.run_state}** -- last updated {plan.updated_at}")
    summary = plan.summary()
    pending_count = sum(1 for t in plan.tasks if t.state == TASK_PENDING)
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Completed", summary.completed)
    m2.metric("Review required", len(summary.review_required_labels))
    m3.metric("Skipped", summary.skipped)
    m4.metric("Failed", len(summary.failed_labels))

    if project_confirmed:
        cc1, cc2, cc3 = st.columns(3)
        cc1.metric("Groups in plan", len(plan.groups))
        cc2.metric("Ready (pending)", pending_count)
        cc3.metric("Uncertain (review/failed)", len(summary.review_required_labels) + len(summary.failed_labels))

    # Phase 5.5B, Objective 3: exact, non-guessed accounting of what the
    # run did and why it stopped where it did -- built only from
    # persisted state, so it's available even after a UI rerun.
    diagnostics = diagnose_run(plan)
    if diagnostics.stopped_after_row is not None:
        st.caption(
            f"Execution stopped after {diagnostics.stopped_after_row} because: {diagnostics.stop_reason_summary}"
        )
    st.caption(f"Remaining unattempted rows: {diagnostics.remaining_unattempted}")
    # Phase 5.5D Stage 8: the exact fixed-vocabulary category run_
    # execution_plan() set at its actual exit point -- never displays
    # "execution complete" when tasks remain unattempted, and always
    # distinguishes a protected-row refusal from an ordinary group/task
    # stop rather than folding it into the same generic message.
    _STOP_REASON_CATEGORY_LABELS = {
        STOP_REASON_NORMAL_COMPLETION: "Normal completion -- every task reached a terminal state.",
        STOP_REASON_PROJECT_VERIFICATION_FAILURE: "Project-level: Xactimate application/project could not be verified before the run started.",
        STOP_REASON_PROJECT_LEVEL_HARD_STOP: "Project-level hard stop: Xactimate/project verification failed mid-run, or the persisted plan was rejected as stale.",
        STOP_REASON_GROUP_VERIFICATION_FAILURE: "One or more groups failed verification (see the group tables above) -- their tasks were marked Review Required.",
        STOP_REASON_PROTECTED_ROW_REFUSAL: "STOPPED: a cleanup/verification step would have deleted a row this run already successfully committed -- refused. See the destructive-action audit log.",
        STOP_REASON_TASK_LEVEL_STOPS: "Task-level safety stops and/or tasks not yet attempted this run.",
    }
    if plan.stop_reason_category:
        label = _STOP_REASON_CATEGORY_LABELS.get(plan.stop_reason_category, plan.stop_reason_category)
        if plan.stop_reason_category == STOP_REASON_PROTECTED_ROW_REFUSAL:
            st.error(f"Exact stop reason: {label}")
        else:
            st.caption(f"Exact stop reason: {label}")
    with st.expander("Run diagnostics (exact counts)", expanded=False):
        d1, d2, d3, d4 = st.columns(4)
        d1.metric("Completed", diagnostics.completed)
        d2.metric("Review required", diagnostics.review_required)
        d3.metric("No match", diagnostics.no_match)
        d4.metric("Failed (other)", diagnostics.failed)
        d5, d6, d7, d8 = st.columns(4)
        d5.metric("Skipped", diagnostics.skipped)
        d6.metric("Not attempted", diagnostics.not_attempted)
        d7.metric("Routed: CAT/SEL", diagnostics.routed_by_cat_sel)
        d8.metric("Routed: description", diagnostics.routed_by_description)
        st.caption(f"Skipped as already-terminal on the most recent run (resume): {diagnostics.skipped_already_terminal_last_run}")
        st.caption(f"Attempted this run: {diagnostics.completed + diagnostics.review_required + diagnostics.no_match + diagnostics.failed + diagnostics.skipped}")

    # Phase 5.5B, Objective 4: TEST-only reset/rebuild actions. Shown
    # only for the same confirmed-exactly-TEST condition that gates the
    # unmapped-row checkbox above -- never for a normal/production
    # project.
    if test_only_confirmed:
        st.markdown("**TEST-only plan maintenance**")
        rc1, rc2 = st.columns(2)
        unfinished_count = sum(1 for t in plan.tasks if t.state != TASK_COMPLETED and t.state != TASK_PENDING)
        if rc1.button(
            "Reset unfinished TEST execution", key="build_estimate_reset_unfinished",
            disabled=unfinished_count == 0,
            help="Resets REVIEW_REQUIRED/FAILED/SKIPPED tasks back to pending so they can be retried. "
                 "Never touches already-COMPLETED (successfully committed) tasks.",
        ):
            reset_count = reset_unfinished_tasks(plan, project_dir, full_reset=False)
            st.success(f"Reset {reset_count} unfinished task(s) back to pending. Completed tasks were left untouched.")
            st.rerun()

        if rc2.button(
            "Rebuild TEST plan from current PDF", key="build_estimate_rebuild_test_plan",
            help="Regenerates ALL tasks fresh from the current approved/eligible rows -- including tasks that "
                 "already completed. Use 'Reset unfinished' above instead if you want to keep completed rows.",
        ):
            try:
                plan = build_execution_plan(
                    project_dir, project_slug,
                    include_unmapped_rows=True, xactimate_project_name=xactimate_project_name.strip(),
                )
                save_execution_plan(plan, project_dir, allow_shrink=True)
                st.success(f"Rebuilt a fresh plan with {len(plan.tasks)} task(s) across {len(plan.groups)} group(s).")
                st.rerun()
            except ExecutionPlanError as exc:
                st.error(str(exc))
        st.caption(
            "\"Reset unfinished\" preserves successful commits and only resets rows that didn't finish. "
            "\"Rebuild\" discards ALL task state, including completed rows, and starts over."
        )

    for group in plan.groups:
        group_tasks = plan.tasks_in_group(group.group_id)
        header = f"{group.section_name or '(no section)'} -> Xactimate group {group.xactimate_group_name!r} -- {group.state}"
        with st.expander(header, expanded=group.state not in (GROUP_COMPLETED,)):
            if not group.group_name_reviewed:
                st.warning(
                    "This section's Xactimate group name has not been reviewed yet -- "
                    "it will still be used, but confirm it in a future group-name review step."
                )
            st.dataframe(pd.DataFrame(_task_table_rows(group_tasks)), use_container_width=True, hide_index=True)
            if group.error:
                st.error(f"Group error: {group.error}")

            pending_in_group = [t for t in group_tasks if t.state == TASK_PENDING]
            if pending_in_group:
                skip_ids = st.multiselect(
                    "Skip specific rows in this group (excludes them from the next run without executing them)",
                    [t.task_id for t in pending_in_group],
                    format_func=lambda tid: next(t.line_item_id for t in pending_in_group if t.task_id == tid),
                    key=f"skip_select_{group.group_id}",
                )
                reason = st.text_input("Reason for skipping", key=f"skip_reason_{group.group_id}")
                if st.button("Skip selected rows", key=f"skip_button_{group.group_id}", disabled=not skip_ids):
                    if not reason.strip():
                        st.error("A reason is required to skip a row.")
                    else:
                        for tid in skip_ids:
                            skip_task(plan, tid, reason, project_dir)
                        st.success(f"Skipped {len(skip_ids)} row(s).")
                        st.rerun()

    st.markdown("---")
    st.markdown("**Run against Xactimate**")

    safe_autofill_enabled = False
    if project_confirmed:
        if safe_autofill_ready:
            safe_autofill_enabled = st.checkbox(
                "Enable Safe Autofill (continuous live execution of safe tasks -- see docs/build-estimate.md)",
                key="build_estimate_safe_autofill_enabled",
            )
        else:
            st.info(
                "Safe Autofill is not available for this session -- see Capability flags / Display check above. "
                "Preview (dry run) is still available."
            )

    # Phase 5.5C Stage 10 (revised): live investigation found Xactimate's
    # "New Group" command reliably creates only the FIRST TWO sibling
    # groups of a session -- a 3rd+ group reproducibly nests under the
    # 2nd regardless of which state-reset strategy precedes it (see
    # docs/build-estimate.md Phase 5.5C). ensure_group() itself refuses
    # (raises before any task executes) if a created group's ancestry
    # doesn't check out, and run_execution_plan()'s group loop already
    # catches that per-group -- marking that group's tasks REVIEW_
    # REQUIRED with a clear reason and `continue`-ing to the next group
    # -- rather than aborting the whole run or writing to the wrong
    # group. That existing safety net means a multi-group plan is
    # already safe to Execute directly: groups 1-2 complete normally,
    # any group beyond that comes back Review Required instead of
    # corrupting anything. This is informational, not a gate -- Execute
    # is never disabled here. The one-group selector below is an
    # OPTIONAL convenience for deliberately targeting a single group
    # (e.g. to avoid burning time on groups already known to fail),
    # never a forced step.
    if len(plan.groups) > 1 and not multi_group_creation_available:
        st.info(
            f"This plan spans {len(plan.groups)} groups. Xactimate reliably creates only the first two groups "
            "of a session as top-level siblings -- Execute will run all groups; any group beyond the 2nd will "
            "safely come back as Review Required (never written to the wrong group) instead of failing the "
            "whole run. See Run diagnostics after Execute for exactly which groups need a follow-up run. "
            "Optionally, use the selector below to build a plan for just one group first."
        )
        with st.expander("Build a plan for just one group (optional)", expanded=False):
            group_options = {
                g.group_id: (
                    f"{g.section_name or '(no section)'} -> Xactimate group {g.xactimate_group_name!r} "
                    f"({len(plan.tasks_in_group(g.group_id))} task(s))"
                )
                for g in plan.groups
            }
            selected_group_id = st.selectbox(
                "Group to run this session",
                options=list(group_options.keys()),
                format_func=lambda gid: group_options[gid],
                key="build_estimate_single_group_selection",
            )
            if st.button("Build one-group plan for the selected group", key="build_estimate_build_single_group_plan"):
                try:
                    if include_unmapped:
                        restricted_plan = build_execution_plan(
                            project_dir, project_slug,
                            include_unmapped_rows=True, xactimate_project_name=xactimate_project_name.strip(),
                            restrict_to_group_id=selected_group_id,
                        )
                    else:
                        restricted_plan = build_execution_plan(
                            project_dir, project_slug, restrict_to_group_id=selected_group_id,
                        )
                    save_execution_plan(restricted_plan, project_dir, allow_shrink=True)
                    st.success(
                        f"Built a one-group plan with {len(restricted_plan.tasks)} task(s) for "
                        f"{group_options[selected_group_id]}."
                    )
                    st.rerun()
                except ExecutionPlanError as exc:
                    st.error(str(exc))

    c1, c2 = st.columns(2)
    if c1.button("Preview (dry run -- never touches Xactimate's data)", disabled=not project_confirmed or pending_count == 0):
        try:
            adapter = _construct_windows_adapter(xactimate_project_name.strip())
        except Exception as exc:  # pragma: no cover -- exercised live on Windows only
            st.error(f"Could not construct the Xactimate adapter: {exc}")
        else:
            phrase_rules = phrase_generator.load_phrase_rules()
            ranking_config = ranking.load_ranking_config()
            try:
                preview_plan = run_execution_plan(plan, adapter, ranking_config, phrase_rules, project_dir, dry_run=True)
            except Exception as exc:  # pragma: no cover -- exercised live on Windows only
                st.error(f"Preview failed unexpectedly ({exc!r}) -- no task states were changed.")
            else:
                st.info("Dry run complete -- no task states were changed, nothing was entered into Xactimate.")
                st.dataframe(pd.DataFrame(_task_table_rows(preview_plan.tasks)), use_container_width=True, hide_index=True)

    # Phase 5.5D Stage 2: never silently execute a legacy plan --
    # run_execution_plan() itself also refuses (defense in depth), but
    # gating the button here gives an immediate, specific reason
    # instead of a post-click error.
    plan_stale = is_plan_stale(plan)
    if plan_stale:
        st.error(
            f"This plan's schema (version {plan.schema_version}) predates the current execution code "
            f"(version {CURRENT_SCHEMA_VERSION}) -- Execute is disabled until it's rebuilt. Use "
            f"\"Build / refresh execution plan\" or \"Rebuild TEST plan\" above."
        )

    execute_label = "Execute (Safe Autofill)" if safe_autofill_enabled else "Execute"
    if c2.button(execute_label, disabled=not project_confirmed or pending_count == 0 or plan_stale, type="primary"):
        try:
            adapter = _construct_windows_adapter(xactimate_project_name.strip())
        except Exception as exc:  # pragma: no cover -- exercised live on Windows only
            st.error(f"Could not construct the Xactimate adapter: {exc}")
        else:
            if safe_autofill_enabled:
                # Explicit, scoped opt-in for THIS run's adapter instance
                # only -- WindowsXactimateAdapter's class default stays
                # False. Gated above on compute_capability_flags()
                # reporting safe_autofill_available (a real, positively-
                # verified adapter with working group control) and a
                # clean display-profile check.
                adapter.supports_live_execution = True
            phrase_rules = phrase_generator.load_phrase_rules()
            ranking_config = ranking.load_ranking_config()
            reports_dir = project_dir / "execution" / "reports"
            try:
                executed_plan = run_execution_plan(plan, adapter, ranking_config, phrase_rules, project_dir, dry_run=False)
            except Exception as exc:  # pragma: no cover -- exercised live on Windows only
                # Hard stop: something failed outside the per-task safety
                # net (e.g. state could not be persisted to disk). Never
                # claim success -- whatever WAS persisted before this is
                # still on disk and safe to inspect/resume from; this
                # panel does not guess further.
                st.error(
                    f"Execution stopped unexpectedly ({exc!r}). Any tasks already completed before this point were "
                    f"already persisted and are safe. Reload this page to see the plan's actual current state before retrying."
                )
            else:
                if executed_plan.run_state == RUN_STATE_COMPLETED:
                    st.success(f"Run completed. Reports written to {reports_dir}.")
                elif executed_plan.run_state == RUN_STATE_PAUSED:
                    app_ok = adapter.verify_application() and adapter.verify_project()
                    if not app_ok:
                        st.warning(f"Run paused -- Xactimate is unavailable or the wrong project is active. {_resume_instructions(xactimate_project_name.strip())}")
                    else:
                        st.warning(
                            f"Run paused (run_state={executed_plan.run_state}) -- see the group/task tables above for why. "
                            f"Reports for progress so far were written to {reports_dir}. Click Execute again to resume."
                        )
                st.rerun()

    st.markdown("---")
    st.markdown("**Unresolved rows**")
    unresolved_tasks = [t for t in plan.tasks if t.state in UNRESOLVED_TASK_STATES]
    if not unresolved_tasks:
        st.caption("No unresolved rows.")
    else:
        row_numbers = ", ".join(str(t.source_order + 1) for t in unresolved_tasks)
        st.warning(f"Rows requiring review: {row_numbers}")
        st.dataframe(pd.DataFrame(_unresolved_table_rows(unresolved_tasks)), use_container_width=True, hide_index=True)

        ac1, ac2, ac3 = st.columns(3)
        selected_id = ac1.selectbox(
            "Select a row to resolve / retry / skip",
            [t.task_id for t in unresolved_tasks],
            format_func=lambda tid: next(t.row_label for t in unresolved_tasks if t.task_id == tid),
            key="unresolved_row_selector",
        )
        if ac2.button("Retry selected row (Resolve)", key="unresolved_retry"):
            task = plan.task_by_id(selected_id)
            if task_has_committed_row(task):
                # Phase 5.9 (live-caught): this button used to reset
                # straight to TASK_PENDING with no commit-evidence check
                # at all -- for a task whose row genuinely landed but
                # carries a low-confidence trust_state (state ==
                # TASK_REVIEW_REQUIRED), that meant the next Execute
                # would re-search and re-commit it, duplicating a real
                # row. Blocked the same way reset_unfinished_tasks() now
                # is; use "Rebuild TEST plan" (a deliberate full
                # restart) if this row genuinely needs re-executing.
                st.error(
                    f"{task.row_label} has evidence of a prior real commit (trust_state={task.trust_state!r}) -- "
                    f"refusing to retry automatically, since that would risk a duplicate row in Xactimate. "
                    f"Reconcile against the live estimate first, or use 'Rebuild TEST plan' for a deliberate full restart."
                )
            else:
                task.state = TASK_PENDING
                task.stop_reason = None
                task.stop_detail = None
                task.error = None
                save_execution_plan(plan, project_dir)
                st.success(f"{task.row_label} reset to pending -- click Execute above to retry it.")
                st.rerun()
        skip_reason = ac3.text_input("Reason (for Skip)", key="unresolved_skip_reason")
        if ac3.button("Skip selected row", key="unresolved_skip", disabled=not skip_reason.strip()):
            skip_task(plan, selected_id, skip_reason, project_dir)
            st.success("Row skipped.")
            st.rerun()

        st.caption("Resume remaining: click Execute above -- it always resumes from the persisted plan and never re-runs a completed task.")

    st.markdown("**Export results**")
    ec1, ec2, ec3 = st.columns(3)
    if ec1.button("Write reports to project folder", key="write_reports_now"):
        reports_dir = write_all_execution_reports(plan, project_dir)
        st.success(f"Reports written to {reports_dir}")
    ec2.download_button(
        "Download JSON", _execution_report_json_bytes(plan), file_name=f"{plan.plan_id}_execution_report.json",
        mime="application/json", key="download_json",
    )
    ec3.download_button(
        "Download CSV", _execution_report_csv_bytes(plan), file_name=f"{plan.plan_id}_execution_report.csv",
        mime="text/csv", key="download_csv",
    )

    if summary.review_required_labels:
        with st.expander(f"Review required ({len(summary.review_required_labels)})", expanded=False):
            for label in summary.review_required_labels:
                st.caption(label)
    if summary.failed_labels:
        with st.expander(f"Failed ({len(summary.failed_labels)})", expanded=False):
            for label in summary.failed_labels:
                st.caption(label)
