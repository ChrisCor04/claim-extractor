"""Renders the Mapping Review screen -- the primary reviewer workflow:
browse/filter/sort every line item's normalization + mapping result,
bulk-approve/reject/assign, and edit individual fields with a required
reason (each edit is stored as an override + review-history entry, never
as an in-place change to mapped_estimate.json -- see review_service.py).
"""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from estimate_extractor.mapping.models import ActionType, TradeType
from estimate_extractor.ui import review_service, state as ui_state
from estimate_extractor.ui.components.selector_recommendation_panel import (
    render_manual_selector_search,
    render_selector_recommendations,
)
from estimate_extractor.ui.components.xactimate_lookup_panel import render_lookup_workflow
from estimate_extractor.ui.review_service import (
    EDITABLE_MAPPING_FIELDS,
    STATUS_APPROVED,
    STATUS_NEEDS_MORE_INFO,
    STATUS_REJECTED,
    ApprovalBlockedError,
)

DISPLAY_COLUMNS = [
    "line_item_id",
    "original_description",
    "coverage_id",
    "area_name",
    "section_name",
    "quantity",
    "unit",
    "normalized_action",
    "normalized_trade",
    "normalized_component",
    "normalized_material",
    "mapping_status",
    "category",
    "selector",
    "activity",
    "mapped_description",
    "confidence",
    "needs_review",
    "review_reasons",
    "approved",
    "rejected",
    "reviewer_note",
]

FILTER_OPTIONS = [
    "All",
    "Partially mapped",
    "Unmapped",
    "Needs review",
    "Missing category",
    "Missing selector",
    "Unknown trade",
    "Unknown component",
    "Unresolved coverage",
    "Approved",
    "Rejected",
]

SORT_OPTIONS = ["Source order", "Confidence ascending", "Section", "Trade", "Mapping status"]


def _apply_filter(rows: list[dict], choice: str) -> list[dict]:
    if choice == "All":
        return rows
    predicates = {
        "Partially mapped": lambda r: r["mapping_status"] == "partially_mapped",
        "Unmapped": lambda r: r["mapping_status"] == "unmapped",
        "Needs review": lambda r: r["mapping_status"] == "needs_review",
        "Missing category": lambda r: not r["category"],
        "Missing selector": lambda r: not r["selector"],
        "Unknown trade": lambda r: r["normalized_trade"] == TradeType.UNKNOWN.value,
        "Unknown component": lambda r: r["normalized_component"] == "unknown",
        "Unresolved coverage": lambda r: r["coverage_id"] is None,
        "Approved": lambda r: r["status"] == STATUS_APPROVED,
        "Rejected": lambda r: r["status"] == STATUS_REJECTED,
    }
    pred = predicates.get(choice)
    return [r for r in rows if pred(r)] if pred else rows


def _apply_sort(rows: list[dict], choice: str) -> list[dict]:
    if choice == "Confidence ascending":
        return sorted(rows, key=lambda r: (r["confidence"] if r["confidence"] is not None else -1))
    if choice == "Section":
        return sorted(rows, key=lambda r: (r["section_name"] or ""))
    if choice == "Trade":
        return sorted(rows, key=lambda r: (r["normalized_trade"] or ""))
    if choice == "Mapping status":
        return sorted(rows, key=lambda r: (r["mapping_status"] or ""))
    return rows  # source order


def render_mapping_review(project_dir: Path) -> None:
    st.subheader("Mapping Review")
    rows = review_service.build_effective_rows(project_dir)
    if not rows:
        st.info("This project has no mapped line items yet -- process it from the Upload / Process tab.")
        return

    filter_choice = st.selectbox("Filter", FILTER_OPTIONS, key="mapping_filter")
    sort_choice = st.selectbox("Sort by", SORT_OPTIONS, key="mapping_sort")
    filtered = _apply_sort(_apply_filter(rows, filter_choice), sort_choice)

    st.caption(f"{len(filtered)} of {len(rows)} line items shown.")

    import pandas as pd

    display_df = pd.DataFrame([{k: r.get(k) for k in DISPLAY_COLUMNS} for r in filtered])
    st.dataframe(display_df, use_container_width=True, hide_index=True)

    _render_bulk_actions(project_dir, filtered)
    st.markdown("---")
    _render_item_editor(project_dir, filtered)
    st.markdown("---")
    render_manual_selector_search(project_dir)


def _render_bulk_actions(project_dir: Path, rows: list[dict]) -> None:
    st.markdown("**Bulk actions**")
    ids = [r["line_item_id"] for r in rows]
    selected = st.multiselect("Select line items", ids, key="bulk_selected_ids")
    if not selected:
        st.caption("Select one or more line items above to enable bulk actions.")
        return

    reviewer = ui_state.get_reviewer_name()
    c1, c2, c3 = st.columns(3)
    if c1.button(f"Approve selected ({len(selected)})", key="bulk_approve"):
        result = review_service.bulk_set_status(project_dir, selected, STATUS_APPROVED, reviewer)
        st.success(f"Approved {len(result.applied)} item(s).")
        if result.blocked:
            st.warning(f"{len(result.blocked)} item(s) could not be approved:")
            for lid, reasons in result.blocked.items():
                st.caption(f"- {lid}: {'; '.join(reasons)}")
        st.rerun()
    if c2.button(f"Reject selected ({len(selected)})", key="bulk_reject"):
        review_service.bulk_set_status(project_dir, selected, STATUS_REJECTED, reviewer)
        st.success(f"Rejected {len(selected)} item(s).")
        st.rerun()
    if c3.button(f"Mark for later review ({len(selected)})", key="bulk_needs_info"):
        review_service.bulk_set_status(project_dir, selected, STATUS_NEEDS_MORE_INFO, reviewer)
        st.success(f"Marked {len(selected)} item(s) as needing more information.")
        st.rerun()

    with st.expander("Bulk-assign a field to the selected items"):
        field = st.selectbox("Field", sorted(EDITABLE_MAPPING_FIELDS), key="bulk_assign_field")
        value = st.text_input("Value", key="bulk_assign_value")
        reason = st.text_input("Reason (required)", key="bulk_assign_reason")
        if st.button("Apply to selected", key="bulk_assign_apply"):
            if not reason.strip():
                st.error("A reason is required.")
            else:
                result = review_service.bulk_assign_field(project_dir, selected, field, value, reviewer, reason)
                st.success(f"Updated {len(result.applied)} item(s).")
                if result.blocked:
                    st.warning(f"{len(result.blocked)} item(s) failed.")
                    for lid, reasons in result.blocked.items():
                        st.caption(f"- {lid}: {'; '.join(reasons)}")
                st.rerun()


def _render_item_editor(project_dir: Path, rows: list[dict]) -> None:
    st.markdown("**Edit a single item**")
    ids = [r["line_item_id"] for r in rows]
    if not ids:
        return
    selected_id = st.selectbox("Line item", ids, key="single_item_select")
    row = next(r for r in rows if r["line_item_id"] == selected_id)
    reviewer = ui_state.get_reviewer_name()

    st.write(f"Original: *{row['original_description']}* -- qty {row['quantity']} {row['unit']} (page {row['source_page']})")
    if row["review_reasons"]:
        st.caption(f"Review reasons: {', '.join(row['review_reasons'])}")
    if not row["can_approve"]:
        st.caption(f"Cannot approve yet: {'; '.join(row['approval_block_reasons'])}")

    action_values = [a.value for a in ActionType]
    trade_values = [t.value for t in TradeType]

    with st.form(key=f"edit_form_{selected_id}"):
        action = st.selectbox("Action", action_values, index=action_values.index(row["normalized_action"]) if row["normalized_action"] in action_values else 0)
        trade = st.selectbox("Trade", trade_values, index=trade_values.index(row["normalized_trade"]) if row["normalized_trade"] in trade_values else 0)
        component = st.text_input("Component", value=row["normalized_component"] or "")
        material = st.text_input("Material", value=row["normalized_material"] or "")
        category = st.text_input("Category", value=row["category"] or "")
        selector = st.text_input("Selector", value=row["selector"] or "")
        activity = st.text_input("Activity", value=row["activity"] or "")
        mapped_description = st.text_input("Mapped description", value=row["mapped_description"] or "")
        note = st.text_area("Reviewer note", value=row["reviewer_note"] or "")
        waive_activity = st.checkbox("Activity intentionally not required", value=row["activity_required_waived"])
        reason = st.text_input("Reason for this edit (required if any field above changed)")
        submitted = st.form_submit_button("Save changes")

        if submitted:
            proposed = {
                "action": action,
                "trade": trade,
                "component": component,
                "material": material or None,
                "category": category or None,
                "selector": selector or None,
                "activity": activity or None,
                "mapped_description": mapped_description or None,
            }
            current = {
                "action": row["normalized_action"],
                "trade": row["normalized_trade"],
                "component": row["normalized_component"],
                "material": row["normalized_material"],
                "category": row["category"],
                "selector": row["selector"],
                "activity": row["activity"],
                "mapped_description": row["mapped_description"],
            }
            changed_fields = {k: v for k, v in proposed.items() if v != current[k]}

            if changed_fields and not reason.strip():
                st.error("A reason is required to save field changes.")
            else:
                for field, value in changed_fields.items():
                    review_service.edit_mapping_field(project_dir, selected_id, field, value, reviewer, reason)
                if waive_activity and not row["activity_required_waived"]:
                    review_service.waive_activity_requirement(
                        project_dir, selected_id, reviewer, reason or "Marked activity as not required."
                    )
                if note != (row["reviewer_note"] or ""):
                    review_service.set_reviewer_note(project_dir, selected_id, note, reviewer)
                st.success("Saved.")
                st.rerun()

    c1, c2, c3, c4 = st.columns(4)
    if c1.button("Approve", key=f"approve_{selected_id}"):
        try:
            review_service.approve_item(project_dir, selected_id, reviewer)
            st.success(f"{selected_id} approved.")
        except ApprovalBlockedError as exc:
            st.error(str(exc))
        st.rerun()
    if c2.button("Reject", key=f"reject_{selected_id}"):
        review_service.reject_item(project_dir, selected_id, reviewer)
        st.success(f"{selected_id} rejected.")
        st.rerun()
    if c3.button("Save as reusable mapping rule", key=f"save_rule_{selected_id}"):
        st.session_state["catalog_editor_seed_item"] = selected_id
        st.info("Switch to the 'Catalog Changes' tab to finish building the rule from this item.")
    if c4.button("Verify in Xactimate", key=f"verify_xactimate_{selected_id}"):
        st.session_state["verify_in_xactimate_seed_item"] = selected_id
        st.info("Switch to the 'Verified Catalog' tab to record what you verify in Xactimate.")

    st.markdown("---")
    render_selector_recommendations(project_dir, row)
    st.markdown("---")
    render_lookup_workflow(project_dir, row)
