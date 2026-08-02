"""Renders the Export screen: builds approved_estimate.json and
automation_input.json / approved_line_items.csv, and clearly reports which
items were excluded and why. Never exports an item that isn't both
status=approved and fully qualified (category + selector present) -- see
export_service.py.
"""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from estimate_extractor.ui import export_service, review_service


def render_export_panel(project_dir: Path) -> None:
    st.subheader("Export")

    rows = review_service.build_effective_rows(project_dir)
    if not rows:
        st.info("Nothing to export yet -- process this project first.")
        return

    total = len(rows)
    approved = sum(1 for r in rows if r["status"] == review_service.STATUS_APPROVED)
    ready = sum(1 for r in rows if r["status"] == review_service.STATUS_APPROVED and r["can_approve"])

    if ready == 0:
        st.warning("Export blocked: no line items are both approved and fully qualified (category + selector) yet.")
    elif ready < total:
        st.info(f"Partially approved: {ready} of {total} line items are ready for automation export.")
    else:
        st.success(f"Ready for automation export: all {total} line items are approved and qualified.")

    st.caption(f"{approved} approved / {total} total line items.")

    if st.button("Build approved_estimate.json", key="export_build_approved"):
        path = export_service.write_approved_estimate(project_dir)
        st.success(f"Wrote {path}")

    if st.button("Export automation_input.json + approved_line_items.csv", key="export_build_automation"):
        result = export_service.write_automation_input(project_dir)
        if result.exported_count == 0:
            st.warning(
                f"Export written, but it contains 0 items ({result.excluded_count} excluded). "
                f"Nothing is ready for automation yet -- see the excluded list below."
            )
        else:
            st.success(
                f"Exported {result.exported_count} item(s), {result.excluded_count} excluded. "
                f"See {result.automation_input_path}"
            )

    data, excluded = export_service.build_automation_input(project_dir)
    if excluded:
        with st.expander(f"Excluded items ({len(excluded)})"):
            for item in excluded:
                st.write(f"`{item['line_item_id']}`: {item['reason']}")

    if data["sections"]:
        with st.expander(f"Included sections ({len(data['sections'])})"):
            for section in data["sections"]:
                st.write(f"**{section['name']}** ({len(section['items'])} item(s), coverage_id={section['coverage_id']})")
