"""Renders the Projects screen: a table of local projects with
open/reprocess/delete/reveal-folder/clear-outputs actions."""

from __future__ import annotations

import platform
import subprocess
from pathlib import Path

import streamlit as st

from estimate_extractor.ui import review_service, state as ui_state
from estimate_extractor.ui.project_service import ProjectService


def _reveal_in_file_manager(path: Path) -> None:
    """Best-effort, OS-native 'reveal in Finder/Explorer'. Never raises --
    on an unrecognized platform or a failed command, the UI just shows the
    path so the user can navigate there manually."""
    try:
        system = platform.system()
        if system == "Darwin":
            subprocess.run(["open", str(path)], check=False)
        elif system == "Windows":
            subprocess.run(["explorer", str(path)], check=False)
        elif system == "Linux":
            subprocess.run(["xdg-open", str(path)], check=False)
    except OSError:
        pass


def render_projects_panel(projects: ProjectService) -> None:
    st.subheader("Projects")
    records = projects.list_projects()
    if not records:
        st.info("No local projects yet. Use the 'Upload / Process' tab to create one.")
        return

    for record in records:
        project_dir = projects.project_dir(record.slug)
        summary = review_service.get_project_summary(project_dir, record)
        with st.container(border=True):
            cols = st.columns([3, 2, 2, 2, 2])
            cols[0].markdown(f"**{summary.slug}**\n\n{summary.source_filename}")
            cols[1].markdown(f"Carrier: {summary.carrier or '—'}\n\nClaim #: {summary.claim_number or '—'}")
            cols[2].markdown(f"Insured: {summary.insured_name or '—'}")
            cols[3].markdown(
                f"Extraction: {summary.extraction_status or 'not run'}\n\nMapping: {summary.mapping_status or 'not run'}"
            )
            cols[4].markdown(
                f"Items: {summary.total_line_items} · Approved: {summary.approved_count} · "
                f"Unresolved: {summary.unresolved_count}"
            )
            st.caption(f"Last processed: {summary.last_processed_at or 'never'}")

            action_cols = st.columns(5)
            if action_cols[0].button("Open", key=f"open_{record.slug}"):
                ui_state.set_active_project(record.slug)
                st.rerun()
            if action_cols[1].button("Reprocess", key=f"reprocess_{record.slug}"):
                st.session_state[f"pipeline_ran_{record.slug}"] = False
                st.session_state[f"reprocess_requested_{record.slug}"] = True
                st.session_state[f"created_slug_{record.source_sha256}"] = record.slug
                ui_state.set_active_project(record.slug)
                st.info("Go to 'Upload / Process' and re-upload the same file, or edit outputs directly here.")
            if action_cols[2].button("Reveal folder", key=f"reveal_{record.slug}"):
                _reveal_in_file_manager(project_dir)
                st.caption(str(project_dir))
            if action_cols[3].button("Clear outputs", key=f"clear_{record.slug}"):
                st.session_state[f"confirm_clear_{record.slug}"] = True
            if action_cols[4].button("Delete", key=f"delete_{record.slug}"):
                st.session_state[f"confirm_delete_{record.slug}"] = True

            if st.session_state.get(f"confirm_clear_{record.slug}"):
                st.warning("This removes all extraction/mapping/review/export outputs but keeps the source PDF.")
                c1, c2 = st.columns(2)
                if c1.button("Confirm clear", key=f"do_clear_{record.slug}"):
                    projects.clear_generated_outputs(record.slug)
                    st.session_state[f"confirm_clear_{record.slug}"] = False
                    st.rerun()
                if c2.button("Cancel", key=f"cancel_clear_{record.slug}"):
                    st.session_state[f"confirm_clear_{record.slug}"] = False
                    st.rerun()

            if st.session_state.get(f"confirm_delete_{record.slug}"):
                st.error("This permanently deletes the project, including its source PDF. This cannot be undone.")
                c1, c2 = st.columns(2)
                if c1.button("Confirm delete", key=f"do_delete_{record.slug}"):
                    projects.delete_project(record.slug)
                    if ui_state.get_active_project() == record.slug:
                        ui_state.set_active_project(None)
                    st.session_state[f"confirm_delete_{record.slug}"] = False
                    st.rerun()
                if c2.button("Cancel", key=f"cancel_delete_{record.slug}"):
                    st.session_state[f"confirm_delete_{record.slug}"] = False
                    st.rerun()
