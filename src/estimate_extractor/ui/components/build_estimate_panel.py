"""Renders the "Build Estimate" screen (Phase 5.0 Priority 4): builds a
persisted ExecutionPlan from every APPROVED line item, displays it grouped
by Section exactly like Mapping Review, and offers a dry-run preview and a
live execute -- both reusing execution_runner.run_execution_plan()
unchanged. A real WindowsXactimateAdapter is only ever constructed here
(lazy import, matching windows_adapter.py's own platform-safety
convention) -- nothing above this file touches desktop automation.

Live execution stays gated by WindowsXactimateAdapter.supports_live_
execution (False as of Phase 4.8 -- see docs/xactimate-lookup.md): every
task will safely come back REVIEW_REQUIRED with stop_reason=
"unsupported_adapter" until that gate is deliberately flipped after
further live validation. This panel does not, and must not, override
that gate itself.
"""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from estimate_extractor.mapping.pipeline import DEFAULT_CONFIG_DIR
from estimate_extractor.xactimate_lookup import phrase_generator, ranking
from estimate_extractor.xactimate_lookup.execution_plan import (
    ExecutionPlanError,
    GROUP_COMPLETED,
    RUN_STATE_COMPLETED,
    TASK_PENDING,
    build_execution_plan,
    load_execution_plan,
    save_execution_plan,
)
from estimate_extractor.xactimate_lookup.execution_runner import run_execution_plan, skip_task

EVIDENCE_DIR = DEFAULT_CONFIG_DIR.parent / "automation_evidence"


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


def render_build_estimate_panel(project_dir: Path, project_slug: str) -> None:
    import pandas as pd

    st.subheader("Build Estimate")
    st.caption(
        "Builds a persisted, group-by-group execution plan from every APPROVED line item in Mapping Review, "
        "then drives it into Xactimate group by group -- creating/selecting/verifying each group before any "
        "item is entered, and independently verifying every commit. See docs/build-estimate.md."
    )

    plan = load_execution_plan(project_dir)

    col1, col2 = st.columns(2)
    if col1.button("Build / refresh execution plan from approved items"):
        try:
            plan = build_execution_plan(project_dir, project_slug)
            save_execution_plan(plan, project_dir)
            st.success(f"Built a plan with {len(plan.tasks)} task(s) across {len(plan.groups)} group(s).")
        except ExecutionPlanError as exc:
            st.error(str(exc))
            return

    if plan is None:
        st.info("No execution plan yet -- approve line items in Mapping Review, then build a plan here.")
        return

    st.caption(f"Plan {plan.plan_id} -- run_state: **{plan.run_state}** -- last updated {plan.updated_at}")
    summary = plan.summary()
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Completed", summary.completed)
    m2.metric("Review required", len(summary.review_required_labels))
    m3.metric("Skipped", summary.skipped)
    m4.metric("Failed", len(summary.failed_labels))

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

    if summary.review_required_labels:
        with st.expander(f"Review required ({len(summary.review_required_labels)})", expanded=True):
            for label in summary.review_required_labels:
                st.caption(label)
    if summary.failed_labels:
        with st.expander(f"Failed ({len(summary.failed_labels)})", expanded=True):
            for label in summary.failed_labels:
                st.caption(label)

    st.markdown("---")
    st.markdown("**Run against Xactimate**")
    xactimate_project_name = st.text_input(
        "Xactimate project name (must match the project currently open in Xactimate)", key="build_estimate_xactimate_project_name"
    )
    pending_count = sum(1 for t in plan.tasks if t.state == TASK_PENDING)
    st.caption(f"{pending_count} task(s) still pending.")

    c1, c2 = st.columns(2)
    if c1.button("Preview (dry run -- never touches Xactimate's data)", disabled=not xactimate_project_name.strip() or pending_count == 0):
        try:
            adapter = _construct_windows_adapter(xactimate_project_name.strip())
        except Exception as exc:  # pragma: no cover -- exercised live on Windows only
            st.error(f"Could not construct the Xactimate adapter: {exc}")
        else:
            phrase_rules = phrase_generator.load_phrase_rules()
            ranking_config = ranking.load_ranking_config()
            preview_plan = run_execution_plan(plan, adapter, ranking_config, phrase_rules, project_dir, dry_run=True)
            st.info("Dry run complete -- no task states were changed, nothing was entered into Xactimate.")
            st.dataframe(pd.DataFrame(_task_table_rows(preview_plan.tasks)), use_container_width=True, hide_index=True)

    if c2.button("Execute", disabled=not xactimate_project_name.strip() or pending_count == 0, type="primary"):
        try:
            adapter = _construct_windows_adapter(xactimate_project_name.strip())
        except Exception as exc:  # pragma: no cover -- exercised live on Windows only
            st.error(f"Could not construct the Xactimate adapter: {exc}")
        else:
            phrase_rules = phrase_generator.load_phrase_rules()
            ranking_config = ranking.load_ranking_config()
            executed_plan = run_execution_plan(plan, adapter, ranking_config, phrase_rules, project_dir, dry_run=False)
            reports_dir = project_dir / "execution" / "reports"
            if executed_plan.run_state == RUN_STATE_COMPLETED:
                st.success(f"Run completed. Reports written to {reports_dir}.")
            else:
                st.warning(
                    f"Run paused (run_state={executed_plan.run_state}) -- see the group/task tables above for why. "
                    f"Reports for progress so far were written to {reports_dir}. Click Execute again to resume."
                )
            st.rerun()
