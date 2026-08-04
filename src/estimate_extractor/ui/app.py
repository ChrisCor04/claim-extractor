"""Phase 3 local review UI entrypoint.

Run directly:
    streamlit run src/estimate_extractor/ui/app.py

Or via the CLI wrapper (recommended -- it pins --server.address to
127.0.0.1 explicitly so the app never binds to 0.0.0.0):
    python -m estimate_extractor ui [--projects-dir DIR]

Everything in this file only renders widgets and delegates to the
sibling *_service.py modules; see docs/local-review-ui.md.
"""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

from estimate_extractor.config import Config
from estimate_extractor.mapping.pipeline import load_mapping_engine_config
from estimate_extractor.ui import state as ui_state
from estimate_extractor.ui.components.build_estimate_panel import render_build_estimate_panel
from estimate_extractor.ui.components.catalog_editor import render_catalog_editor
from estimate_extractor.ui.components.export_panel import render_export_panel
from estimate_extractor.ui.components.extraction_panel import render_claim_summary, render_extraction_review
from estimate_extractor.ui.components.issue_panel import render_review_history
from estimate_extractor.ui.components.mapping_table import render_mapping_review
from estimate_extractor.ui.components.project_summary import render_projects_panel
from estimate_extractor.ui.components.upload_panel import render_upload_panel
from estimate_extractor.ui.components.verified_catalog_panel import render_verified_catalog_tab
from estimate_extractor.ui.project_service import ProjectService


def _resolve_projects_dir() -> Path:
    args = sys.argv[1:]
    if "--projects-dir" in args:
        idx = args.index("--projects-dir")
        if idx + 1 < len(args):
            return Path(args[idx + 1]).expanduser().resolve()
    return Path("projects").resolve()


@st.cache_resource
def _load_engine_config():
    return load_mapping_engine_config()


def main() -> None:
    st.set_page_config(page_title="ClaimXtract Review", layout="wide")
    ui_state.init_state()

    projects_dir = _resolve_projects_dir()
    projects = ProjectService(projects_dir)
    config = Config.default()

    try:
        engine_config = _load_engine_config()
    except Exception as exc:  # a malformed config/*.yaml must not crash the whole app
        st.error(f"Failed to load the mapping engine configuration: {exc}")
        with st.expander("Debug details"):
            st.exception(exc)
        return

    st.title("ClaimXtract — Local Claim Review")
    st.caption(f"Local-only, offline. Projects stored at: {projects_dir}")

    with st.sidebar:
        st.subheader("Reviewer")
        name = st.text_input("Reviewer / workstation name", value=ui_state.get_reviewer_name())
        ui_state.set_reviewer_name(name)
        st.markdown("---")
        active = ui_state.get_active_project()
        st.caption(f"Active project: {active or '(none)'}")
        st.markdown("---")
        st.caption("estimate_extractor Phase 3 — no network calls, no telemetry, no cloud storage.")

    tabs = st.tabs(
        [
            "Projects",
            "Upload / Process",
            "Claim Summary",
            "Extraction Review",
            "Mapping Review",
            "Build Estimate",
            "Catalog Changes",
            "Verified Catalog",
            "Export",
        ]
    )

    with tabs[0]:
        render_projects_panel(projects)

    with tabs[1]:
        render_upload_panel(projects, config, engine_config)

    active_slug = ui_state.get_active_project()
    project_dir = projects.project_dir(active_slug) if active_slug and projects.project_exists(active_slug) else None

    with tabs[2]:
        if project_dir is None:
            st.info("Open a project from the Projects tab first.")
        else:
            render_claim_summary(project_dir)

    with tabs[3]:
        if project_dir is None:
            st.info("Open a project from the Projects tab first.")
        else:
            render_extraction_review(project_dir)

    with tabs[4]:
        if project_dir is None:
            st.info("Open a project from the Projects tab first.")
        else:
            render_mapping_review(project_dir)
            st.markdown("---")
            render_review_history(project_dir)

    with tabs[5]:
        if project_dir is None:
            st.info("Open a project from the Projects tab first.")
        else:
            render_build_estimate_panel(project_dir, active_slug)

    with tabs[6]:
        if project_dir is None:
            st.info("Open a project from the Projects tab first.")
        else:
            render_catalog_editor(project_dir)

    with tabs[7]:
        if project_dir is None:
            st.info("Open a project from the Projects tab first.")
        else:
            render_verified_catalog_tab(project_dir)

    with tabs[8]:
        if project_dir is None:
            st.info("Open a project from the Projects tab first.")
        else:
            render_export_panel(project_dir)


if __name__ == "__main__":
    main()
