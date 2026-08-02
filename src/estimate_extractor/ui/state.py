"""Thin wrapper around st.session_state for Phase 3.

Session state here holds *only* transient UI selections (active
project/tab, an upload pending duplicate-resolution, the local reviewer
name). Every durable fact -- projects, review decisions, catalog changes,
approvals -- is persisted to disk by the service modules, so closing the
browser tab or restarting the app never loses review data. See
docs/local-review-ui.md "State management".
"""

from __future__ import annotations

import os
import platform

import streamlit as st

_DEFAULTS = {
    "active_project_slug": None,
    "active_tab": "Projects",
    "pending_upload": None,  # (filename, bytes) awaiting duplicate-resolution
    "reviewer_name": None,
    "last_pipeline_log": [],
}


def init_state() -> None:
    for key, value in _DEFAULTS.items():
        if key not in st.session_state:
            st.session_state[key] = value


def default_reviewer_name() -> str:
    return os.environ.get("USER") or os.environ.get("USERNAME") or platform.node() or "local-reviewer"


def get_reviewer_name() -> str:
    init_state()
    return st.session_state["reviewer_name"] or default_reviewer_name()


def set_reviewer_name(name: str) -> None:
    init_state()
    st.session_state["reviewer_name"] = name


def get_active_project() -> str | None:
    init_state()
    return st.session_state["active_project_slug"]


def set_active_project(slug: str | None) -> None:
    init_state()
    st.session_state["active_project_slug"] = slug


def get_pending_upload():
    init_state()
    return st.session_state["pending_upload"]


def set_pending_upload(value) -> None:
    init_state()
    st.session_state["pending_upload"] = value


def clear_pending_upload() -> None:
    init_state()
    st.session_state["pending_upload"] = None
