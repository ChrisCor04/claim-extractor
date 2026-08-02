"""Renders review-history and catalog-change audit trails for a project --
used by the Catalog Changes tab and available as a standalone log view."""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from estimate_extractor.ui import catalog_service, review_service


def render_review_history(project_dir: Path) -> None:
    history = review_service.get_review_history(project_dir)
    st.markdown(f"**Review history** ({len(history)} events)")
    if not history:
        st.caption("No review actions recorded yet.")
        return
    with st.expander("Show review history", expanded=False):
        for event in reversed(history):
            st.write(
                f"`{event['timestamp']}` **{event['action']}** on `{event.get('line_item_id') or '(catalog)'}` "
                f"by {event.get('reviewer', 'unknown')}"
            )
            if event.get("field_changes"):
                st.json(event["field_changes"], expanded=False)
            if event.get("note"):
                st.caption(event["note"])


def render_catalog_change_log(project_dir: Path) -> None:
    changes = catalog_service.get_catalog_changes(project_dir)
    st.markdown(f"**Catalog change log** ({len(changes)} changes made from this project)")
    if not changes:
        st.caption("No catalog changes have been made from this project.")
        return
    for change in reversed(changes):
        with st.container(border=True):
            st.write(f"`{change['timestamp']}` **{change['action']}** — `{change['mapping_id']}` by {change.get('reviewer', 'unknown')}")
            st.caption(f"hash {change['previous_hash'][:10]} -> {change['new_hash'][:10]}")
            if change.get("affected_line_items"):
                st.caption(f"Affected line items at save time: {', '.join(change['affected_line_items'])}")
            if change.get("reviewer_note"):
                st.caption(change["reviewer_note"])
            st.caption(f"Backup: {change['backup_path']}")
