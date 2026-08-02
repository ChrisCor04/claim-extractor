"""Renders the Upload / Process screen: drag-and-drop (Streamlit's native
file_uploader) one or more PDFs, resolve duplicate-upload detection, then
run the existing extract+map pipeline with live stage progress. Never
hides a pipeline failure -- errors are shown with a debug expander instead
of being caught and discarded.
"""

from __future__ import annotations

import streamlit as st

from estimate_extractor.config import Config
from estimate_extractor.mapping.pipeline import MappingEngineConfig
from estimate_extractor.ui import pipeline_service, state as ui_state
from estimate_extractor.ui.project_service import DuplicateSourceError, ProjectService, sha256_bytes


def render_upload_panel(projects: ProjectService, config: Config, engine_config: MappingEngineConfig) -> None:
    st.subheader("Upload / Process")
    st.caption(
        "Drag in one or more insurance-estimate PDFs, or click to browse. "
        "Processing runs the existing extractor and mapper locally -- nothing leaves this machine."
    )

    uploaded_files = st.file_uploader(
        "Insurance estimate PDF(s)", type=["pdf"], accept_multiple_files=True, key="pdf_uploader"
    )
    if not uploaded_files:
        return

    for uploaded in uploaded_files:
        st.markdown("---")
        _handle_one_upload(projects, config, engine_config, uploaded)


def _handle_one_upload(projects: ProjectService, config: Config, engine_config: MappingEngineConfig, uploaded) -> None:
    data = uploaded.getvalue()
    file_hash = sha256_bytes(data)
    decision_key = f"upload_decision_{file_hash}"

    existing = projects.find_by_source_hash(file_hash)
    st.markdown(f"**{uploaded.name}**")

    if existing is not None and st.session_state.get(decision_key) is None:
        st.warning(f"This exact file was already processed as project '{existing.slug}' (created {existing.created_at}).")
        choice = st.radio(
            "This file has already been uploaded. What would you like to do?",
            ["Open existing project", "Create new project version", "Cancel"],
            key=f"radio_{file_hash}",
        )
        if st.button("Confirm", key=f"confirm_{file_hash}"):
            st.session_state[decision_key] = choice
            st.rerun()
        return

    decision = st.session_state.get(decision_key)

    if existing is not None and decision == "Cancel":
        st.info("Upload cancelled.")
        return

    if existing is not None and decision == "Open existing project":
        ui_state.set_active_project(existing.slug)
        st.success(f"Opened existing project '{existing.slug}'. See the Projects tab or the other tabs above.")
        return

    allow_new_version = existing is not None and decision == "Create new project version"

    created_slug_key = f"created_slug_{file_hash}"
    if st.session_state.get(created_slug_key) is None:
        try:
            record = projects.create_project(uploaded.name, data, allow_new_version=allow_new_version)
        except DuplicateSourceError as exc:
            st.error(f"Could not create project: {exc}")
            return
        st.session_state[created_slug_key] = record.slug
    slug = st.session_state[created_slug_key]

    _run_pipeline_with_progress(projects, config, engine_config, slug)


def _run_pipeline_with_progress(projects: ProjectService, config: Config, engine_config: MappingEngineConfig, slug: str) -> None:
    ran_key = f"pipeline_ran_{slug}"
    reprocess_key = f"reprocess_requested_{slug}"
    if st.session_state.get(ran_key) and not st.session_state.get(reprocess_key):
        st.success(f"Project '{slug}' already processed this session.")
        if st.button("Open in Claim Summary", key=f"open_after_process_{slug}"):
            ui_state.set_active_project(slug)
            st.rerun()
        return

    project_dir = projects.project_dir(slug)
    pdf_path = projects.source_pdf_path(slug)

    progress_box = st.empty()
    stage_log: list[str] = []

    def on_progress(stage: str, detail: str | None = None) -> None:
        stage_log.append(stage if not detail else f"{stage}: {detail}")
        progress_box.info("\n".join(f"- {s}" for s in stage_log))

    try:
        with st.spinner(f"Processing {slug} ..."):
            pipeline_service.run_pipeline_for_project(
                pdf_path, project_dir, config, engine_config, progress_callback=on_progress
            )
        projects.mark_processed(slug)
        st.session_state[ran_key] = True
        st.session_state[reprocess_key] = False
        ui_state.set_active_project(slug)
        st.success(f"Processed '{slug}'. See Claim Summary / Mapping Review above.")
    except pipeline_service.PipelineServiceError as exc:
        st.error(f"Processing failed at stage '{exc.stage}': {exc}")
        with st.expander("Debug details"):
            st.exception(exc.cause or exc)
