"""Renders the Catalog Changes screen: the reusable-mapping-rule builder
(draft -> validate -> preview -> confirm -> backup -> write -> audit),
backup/restore controls, and the re-run-mapping action. Xactimate CAT/SEL
codes are never invented -- see catalog_service.validate_rule_dict()'s
selector_confirmed requirement.
"""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from estimate_extractor.mapping.catalog import load_catalog
from estimate_extractor.mapping.pipeline import DEFAULT_CONFIG_DIR, load_mapping_engine_config
from estimate_extractor.ui import catalog_service, pipeline_service, review_service, state as ui_state
from estimate_extractor.ui.components.issue_panel import render_catalog_change_log

CATALOG_PATH = DEFAULT_CONFIG_DIR / "mapping_catalog.yaml"
BACKUPS_DIR = DEFAULT_CONFIG_DIR / "backups"


def render_catalog_editor(project_dir: Path) -> None:
    st.subheader("Catalog Changes")
    st.caption(
        "Xactimate CAT/SEL codes are never invented here -- a selector can only be saved "
        "with explicit confirmation that it was verified against a licensed price list."
    )

    _render_rerun_section(project_dir)
    st.markdown("---")
    _render_rule_builder(project_dir)
    st.markdown("---")
    _render_backup_controls()
    st.markdown("---")
    render_catalog_change_log(project_dir)


def _render_rerun_section(project_dir: Path) -> None:
    st.markdown("**Re-run mapping**")
    st.caption(
        "Re-runs normalization + mapping against this project's saved canonical_estimate.json "
        "using the current catalog. Approved reviewed values are never overwritten -- they live in "
        "a separate review-state file this action never touches."
    )
    if st.button("Re-run mapping now", key="rerun_mapping_button"):
        before_rows = {
            r["line_item_id"]: (r["mapping_status"], r["category"], r["selector"])
            for r in review_service.build_effective_rows(project_dir)
        }
        engine_config = load_mapping_engine_config()
        with st.spinner("Re-running mapping..."):
            pipeline_service.rerun_mapping_for_project(project_dir, engine_config)
        after_rows = {
            r["line_item_id"]: (r["mapping_status"], r["category"], r["selector"])
            for r in review_service.build_effective_rows(project_dir)
        }

        changed = [lid for lid in after_rows if before_rows.get(lid) != after_rows[lid]]
        st.success(f"Mapping re-run complete. {len(changed)} item(s) have a newly changed suggestion.")
        if changed:
            with st.expander(f"Show {len(changed)} changed item(s)"):
                for lid in changed:
                    st.write(f"`{lid}`: {before_rows.get(lid)} -> {after_rows[lid]}")


def _render_rule_builder(project_dir: Path) -> None:
    st.markdown("**Save as reusable mapping rule**")
    seed_item = st.session_state.get("catalog_editor_seed_item")
    if seed_item:
        st.caption(f"Seeded from line item `{seed_item}` (selected on the Mapping Review tab).")

    rows = review_service.build_effective_rows(project_dir)
    seed_row = next((r for r in rows if r["line_item_id"] == seed_item), None) if seed_item else None

    with st.form("rule_builder_form"):
        mapping_id = st.text_input("Rule name / mapping_id (unique)")
        terms_raw = st.text_area(
            "Source description patterns (one per line, lowercase substrings)",
            value=(seed_row["original_description"].lower() if seed_row else ""),
        )
        trade = st.text_input("Trade", value=(seed_row["normalized_trade"] if seed_row else "") or "")
        component = st.text_input("Component", value=(seed_row["normalized_component"] if seed_row else "") or "")
        material = st.text_input(
            "Material (used as the default Xactimate description)", value=(seed_row["normalized_material"] if seed_row else "") or ""
        )
        actions_raw = st.text_input("Valid actions (comma-separated)", value=(seed_row["normalized_action"] if seed_row else "") or "")
        units_raw = st.text_input("Valid units (comma-separated)", value=(seed_row["unit"] if seed_row else "") or "")
        category = st.text_input("Xactimate category", value=(seed_row["category"] if seed_row else "") or "")
        selector = st.text_input("Xactimate selector (leave blank unless verified)", value=(seed_row["selector"] if seed_row else "") or "")
        activity = st.text_input("Xactimate activity", value=(seed_row["activity"] if seed_row else "") or "")
        xactimate_description = st.text_input("Xactimate description", value=material)
        confidence_base = st.slider("Confidence base", 0.0, 1.0, 0.75, 0.01)
        notes_raw = st.text_area("Notes (one per line)")
        selector_confirmed = st.checkbox("I have verified this selector against a licensed Xactimate price list", value=False)
        submitted = st.form_submit_button("Validate & preview")

    if not submitted:
        return

    rule = {
        "mapping_id": mapping_id.strip(),
        "canonical_terms": [t.strip() for t in terms_raw.splitlines() if t.strip()],
        "trade": trade.strip(),
        "component": component.strip(),
        "allowed_actions": [a.strip() for a in actions_raw.split(",") if a.strip()],
        "allowed_units": [u.strip() for u in units_raw.split(",") if u.strip()],
        "xactimate": {
            "category": category.strip() or None,
            "selector": selector.strip() or None,
            "activity": activity.strip() or None,
            "description": xactimate_description.strip() or None,
        },
        "confidence_base": confidence_base,
        "requires_review": True,
        "notes": [n.strip() for n in notes_raw.splitlines() if n.strip()],
        "selector_confirmed": selector_confirmed,
    }

    existing_catalog = load_catalog(CATALOG_PATH)
    errors = catalog_service.validate_rule_dict(rule, existing_catalog)

    if errors:
        st.error("This rule cannot be saved yet:")
        for e in errors:
            st.write(f"- {e}")
        return

    affected = catalog_service.preview_affected_items(rule, rows)
    st.success("Rule is valid.")
    st.write(f"This rule would affect **{len(affected)}** line item(s) in the current project:")
    if affected:
        st.write(", ".join(affected))

    st.markdown("**YAML preview:**")
    st.code(catalog_service.rule_to_yaml_preview(rule), language="yaml")

    reviewer_note = st.text_area("Reviewer note for the audit log", key="rule_save_note")
    if st.button("Confirm and save to catalog", key="confirm_save_rule"):
        try:
            result = catalog_service.save_rule(
                CATALOG_PATH, BACKUPS_DIR, project_dir, rule, ui_state.get_reviewer_name(), reviewer_note, affected
            )
            st.success(f"Saved rule '{rule['mapping_id']}'. Backup written to {result.backup_path}.")
            st.session_state["catalog_editor_seed_item"] = None
            st.info("Use 'Re-run mapping now' above to apply this rule to the current project.")
        except catalog_service.CatalogValidationError as exc:
            st.error(f"Validation failed at save time: {'; '.join(exc.errors)}")
        except catalog_service.CatalogServiceError as exc:
            st.error(f"Could not save the catalog: {exc}")


def _render_backup_controls() -> None:
    st.markdown("**Catalog backups**")
    backups = catalog_service.list_backups(BACKUPS_DIR)
    st.caption(f"{len(backups)} backup(s) in {BACKUPS_DIR}")
    if backups:
        st.caption(f"Most recent: {backups[-1].name}")
    if st.button("Restore last backup", key="restore_last_backup"):
        try:
            restored = catalog_service.restore_last_backup(CATALOG_PATH, BACKUPS_DIR)
            st.success(f"Catalog restored from {restored.name}. Use 'Re-run mapping now' to apply.")
        except catalog_service.CatalogServiceError as exc:
            st.error(str(exc))
