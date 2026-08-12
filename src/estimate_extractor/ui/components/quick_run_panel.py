"""A small front door for the common claim-to-Xactimate workflow."""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from estimate_extractor.config import Config
from estimate_extractor.mapping.pipeline import MappingEngineConfig
from estimate_extractor.ui import pipeline_service, state as ui_state
from estimate_extractor.ui.project_service import DuplicateSourceError, ProjectService, sha256_bytes
from estimate_extractor.xactimate_lookup import phrase_generator, ranking, service
from estimate_extractor.xactimate_lookup.execution_plan import (
    RUN_STATE_COMPLETED,
    TASK_PENDING,
    TEST_ONLY_PROJECT_NAME,
    ExecutionPlanError,
    build_execution_plan,
    is_plan_stale,
    load_execution_plan,
    save_execution_plan,
)
from estimate_extractor.xactimate_lookup.execution_reports import build_review_queue
from estimate_extractor.xactimate_lookup.execution_runner import run_execution_plan


def _construct_adapter(project_name: str, project_dir: Path):
    from estimate_extractor.xactimate_lookup.windows_adapter import WindowsXactimateAdapter

    return WindowsXactimateAdapter(
        expected_project_name=project_name,
        evidence_dir=project_dir / "execution" / "evidence",
    )


def _process_project(
    projects: ProjectService,
    config: Config,
    engine_config: MappingEngineConfig,
    slug: str,
) -> None:
    messages: list[str] = []
    status = st.empty()

    def progress(stage: str, detail: str | None = None) -> None:
        messages.append(stage if not detail else f"{stage}: {detail}")
        status.info("\n".join(f"- {message}" for message in messages))

    with st.spinner("Reading and preparing the estimate …"):
        pipeline_service.run_pipeline_for_project(
            projects.source_pdf_path(slug),
            projects.project_dir(slug),
            config,
            engine_config,
            progress_callback=progress,
        )
    projects.mark_processed(slug)
    ui_state.set_active_project(slug)
    status.empty()


def _add_file(
    projects: ProjectService,
    config: Config,
    engine_config: MappingEngineConfig,
    uploaded,
    create_version: bool,
) -> str:
    data = uploaded.getvalue()
    digest = sha256_bytes(data)
    existing = projects.find_by_source_hash(digest)
    if existing is not None and not create_version:
        ui_state.set_active_project(existing.slug)
        return existing.slug

    try:
        record = projects.create_project(
            uploaded.name,
            data,
            allow_new_version=bool(existing and create_version),
        )
    except DuplicateSourceError:
        existing = projects.find_by_source_hash(digest)
        if existing is None:
            raise
        ui_state.set_active_project(existing.slug)
        return existing.slug

    _process_project(projects, config, engine_config, record.slug)
    return record.slug


def _plan_for_quick_run(project_dir: Path, project_slug: str, project_name: str):
    plan = load_execution_plan(project_dir)
    if plan is not None:
        if is_plan_stale(plan):
            raise ExecutionPlanError(
                "The saved plan uses older code. Open Advanced → Build Estimate to review and rebuild it; "
                "Quick Run will not discard prior task state automatically."
            )
        return plan

    include_unmapped = project_name == TEST_ONLY_PROJECT_NAME
    plan = build_execution_plan(
        project_dir,
        project_slug,
        include_unmapped_rows=include_unmapped,
        xactimate_project_name=project_name if include_unmapped else None,
    )
    save_execution_plan(plan, project_dir, allow_shrink=True)
    return plan


def _render_plan_status(project_dir: Path) -> None:
    plan = load_execution_plan(project_dir)
    if plan is None:
        st.caption("No execution plan yet. Execute will create it automatically.")
        return

    summary = plan.summary()
    columns = st.columns(4)
    columns[0].metric("Items", len(plan.tasks))
    columns[1].metric("Committed", sum(task.commit_state == "committed" for task in plan.tasks))
    columns[2].metric("Ready", sum(task.state == TASK_PENDING for task in plan.tasks))
    columns[3].metric(
        "Needs review",
        len(summary.review_required_labels) + len(summary.failed_labels),
    )

    queue = build_review_queue(plan)
    if queue:
        with st.expander(f"Human review queue ({len(queue)})", expanded=False):
            st.dataframe(queue, width="stretch", hide_index=True)


def render_quick_run_panel(
    projects: ProjectService,
    config: Config,
    engine_config: MappingEngineConfig,
) -> None:
    st.subheader("Add claim and run")
    st.caption("Upload a PDF, open the matching project in Xactimate, then click Execute.")

    uploaded = st.file_uploader(
        "Claim PDF", type=["pdf"], accept_multiple_files=False, key="quick_claim_pdf"
    )
    create_version = False
    if uploaded is not None:
        existing = projects.find_by_source_hash(sha256_bytes(uploaded.getvalue()))
        if existing is not None:
            st.info(f"Already added as **{existing.slug}**. Add File will open it without reprocessing.")
            create_version = st.checkbox("Create a new version instead", key="quick_create_version")
        if st.button("Add file", type="primary", width="stretch", key="quick_add_file"):
            try:
                slug = _add_file(projects, config, engine_config, uploaded, create_version)
            except pipeline_service.PipelineServiceError as exc:
                st.error(f"Processing stopped at {exc.stage}: {exc}")
                with st.expander("Technical details"):
                    st.exception(exc.cause or exc)
            except Exception as exc:
                st.error(f"Could not add the file: {exc}")
                with st.expander("Technical details"):
                    st.exception(exc)
            else:
                ui_state.set_active_project(slug)
                st.success(f"Ready: {slug}")
                st.rerun()

    records = projects.list_projects()
    slugs = [record.slug for record in records]
    active = ui_state.get_active_project()
    if active not in slugs:
        active = slugs[-1] if slugs else None
        ui_state.set_active_project(active)
    if not slugs:
        st.info("Add a claim PDF to begin.")
        return

    selected = st.selectbox(
        "Claim",
        slugs,
        index=slugs.index(active) if active in slugs else len(slugs) - 1,
        key="quick_project_selector",
    )
    if selected != active:
        ui_state.set_active_project(selected)
        st.rerun()

    project_dir = projects.project_dir(selected)
    record = projects.load_project(selected)
    st.caption(f"Source: {record.source_filename} · Local project: {selected}")

    st.markdown("#### Execute in Xactimate")
    project_name = st.text_input(
        "Open Xactimate project name",
        placeholder="Example: TEST",
        key="quick_xactimate_project_name",
    ).strip()
    st.caption("Leave Xactimate open on Estimate Items with no dialog or dropdown visible.")
    _render_plan_status(project_dir)

    if st.button(
        "Execute estimate",
        type="primary",
        width="stretch",
        disabled=not project_name,
        key="quick_execute",
    ):
        try:
            adapter = _construct_adapter(project_name, project_dir)
            diagnostics = service.run_diagnostics(adapter)
            flags = service.compute_capability_flags(adapter, diagnostics)
            display = adapter.verify_display_profile()
            if not (diagnostics.application_verified and diagnostics.project_verified):
                raise RuntimeError(f"Xactimate is not open on project {project_name!r} at Estimate Items.")
            if not display["ok"]:
                raise RuntimeError("Display check failed: " + "; ".join(display["blocking_reasons"]))
            if not flags.safe_autofill_available:
                raise RuntimeError("Safe live execution is unavailable in this session.")

            plan = _plan_for_quick_run(project_dir, selected, project_name)
            if not any(task.state == TASK_PENDING for task in plan.tasks):
                st.info("Nothing is waiting to run. Review items are available below or in Advanced.")
            else:
                adapter.supports_live_execution = True
                with st.spinner("Executing in Xactimate … keep both windows open."):
                    result = run_execution_plan(
                        plan,
                        adapter,
                        ranking.load_ranking_config(),
                        phrase_generator.load_phrase_rules(),
                        project_dir,
                        dry_run=False,
                    )
                if result.run_state == RUN_STATE_COMPLETED:
                    st.success("Execution pass complete. Review items were saved for follow-up.")
                else:
                    st.warning("Execution paused safely. Completed progress was saved.")
                st.rerun()
        except (ExecutionPlanError, RuntimeError) as exc:
            st.error(str(exc))
        except Exception as exc:
            st.error("Execution stopped safely. Completed progress remains saved.")
            with st.expander("Technical details"):
                st.exception(exc)

    with st.expander("Tips", expanded=False):
        st.markdown(
            "- Uploading the same PDF opens its existing project by default.\n"
            "- Execute resumes pending tasks and does not rerun completed tasks.\n"
            "- OCR-only uncertainty is retained in the human review queue.\n"
            "- Use Advanced for mapping edits, retries, exports, and diagnostics."
        )
