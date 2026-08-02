"""Renders the "Verified Catalog" top-level tab: search the verified
catalog, the "Verify in Xactimate" workflow (record what a reviewer
personally confirmed in their own licensed Xactimate environment), group-
name review, project-context confirmation, coverage metrics, and catalog
backup/restore. See docs/verified-catalog-builder.md.

Nothing here scrapes, automates, or connects to Xactimate -- every field
is reviewer-entered.
"""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from estimate_extractor.mapping.pipeline import DEFAULT_CONFIG_DIR
from estimate_extractor.ui import group_name_service as gns
from estimate_extractor.ui import project_context_service as pcs
from estimate_extractor.ui import review_service, state as ui_state
from estimate_extractor.ui import verified_catalog_service as vcs

VERIFIED_CATALOG_PATH = DEFAULT_CONFIG_DIR / "verified_xactimate_catalog.yaml"
BACKUPS_DIR = DEFAULT_CONFIG_DIR / "backups"
GROUP_NAMES_PATH = DEFAULT_CONFIG_DIR / "xactimate_group_names.yaml"


def render_verified_catalog_tab(project_dir: Path) -> None:
    st.subheader("Verified Catalog")
    st.caption(
        "Only records a reviewer has personally confirmed in their own licensed Xactimate "
        "environment may make an item automation-ready. Nothing here scrapes or automates Xactimate."
    )

    _render_coverage_metrics(project_dir)
    st.markdown("---")
    _render_search(project_dir)
    st.markdown("---")
    _render_verify_in_xactimate(project_dir)
    st.markdown("---")
    _render_group_name_review(project_dir)
    st.markdown("---")
    _render_project_context(project_dir)
    st.markdown("---")
    _render_backup_controls()


def _group_reviewed_sections(project_dir: Path) -> set[str]:
    overrides = gns.get_group_name_overrides(project_dir)
    return {name for name, entry in overrides.items() if entry.get("reviewed_xactimate_group_name") or entry.get("allow_custom")}


def _render_coverage_metrics(project_dir: Path) -> None:
    rows = review_service.build_effective_rows(project_dir)
    if not rows:
        st.info("This project hasn't been processed yet.")
        return
    records = vcs.load_verified_catalog(VERIFIED_CATALOG_PATH)
    stats = vcs.compute_coverage_stats(rows, records, project_dir, _group_reviewed_sections(project_dir))

    cols = st.columns(4)
    cols[0].metric("Total extracted items", stats["total_extracted_items"])
    cols[1].metric("Matching a verified selector", stats["items_matching_verified_selector"])
    cols[2].metric("Matching only placeholder", stats["items_matching_only_placeholder"])
    cols[3].metric("Automation-ready", stats["automation_ready_items"])

    with st.expander("Full coverage metrics"):
        st.json(stats, expanded=False)


def _render_search(project_dir: Path) -> None:
    st.markdown("**Search the verified catalog**")
    query = st.text_input("Search by description, alias, selector, category, trade, component, or unit", key="verified_catalog_search")
    records = vcs.load_verified_catalog(VERIFIED_CATALOG_PATH)
    if not query:
        st.caption(f"{len(records)} total record(s) in the catalog.")
        return

    q = query.lower()
    results = [
        r
        for r in records
        if q in r.description.lower()
        or q in r.selector.lower()
        or q in r.category.lower()
        or q in r.trade.lower()
        or q in r.component.lower()
        or q in r.unit.lower()
        or any(q in a.lower() for a in r.aliases)
    ]
    verified = [r for r in results if r.verification_status == vcs.VERIFICATION_STATUS_HUMAN_VERIFIED]
    unverified = [r for r in results if r.verification_status != vcs.VERIFICATION_STATUS_HUMAN_VERIFIED]

    if not results:
        st.warning("No match.")
        return

    if verified:
        st.success(f"{len(verified)} verified match(es)")
        for r in verified:
            uses = vcs.count_prior_successful_uses(r.catalog_record_id, r.category, r.selector, project_dir.parent)
            st.write(f"✅ **{r.category}/{r.selector}** -- {r.description} ({r.unit}) -- verified by {r.verified_by} on {r.verified_at} -- prior uses: {uses}")
    if unverified:
        st.info(f"{len(unverified)} unverified/starter suggestion(s)")
        for r in unverified:
            st.write(f"⚪ **{r.category}/{r.selector}** -- {r.description} ({r.unit}) -- status: {r.verification_status}")


def _render_verify_in_xactimate(project_dir: Path) -> None:
    st.markdown("**Verify in Xactimate**")
    rows = review_service.build_effective_rows(project_dir)
    if not rows:
        st.caption("Process this project first.")
        return

    ids = [r["line_item_id"] for r in rows]
    seed = st.session_state.get("verify_in_xactimate_seed_item")
    default_index = ids.index(seed) if seed in ids else 0
    selected_id = st.selectbox("Line item to verify", ids, index=default_index, key="verify_xactimate_item_select")
    row = next(r for r in rows if r["line_item_id"] == selected_id)

    with st.expander("Source context (immutable)", expanded=True):
        st.write(f"**Line item ID:** {row['line_item_id']}")
        st.write(f"**Original description:** {row['original_description']}")
        st.write(f"**Original quantity:** {row['original_quantity']}  **Unit:** {row['original_unit']}")
        st.write(f"**Area:** {row['area_name']}  **Section:** {row['section_name']}  **Coverage:** {row['coverage_id']}")
        st.write(f"**Source page:** {row['source_page']}  **Extraction confidence:** {row['extraction_confidence']}")
        st.write(f"**Normalized:** action={row['normalized_action']} trade={row['normalized_trade']} component={row['normalized_component']} material={row['normalized_material']}")
        st.write(f"**Current mapping status:** {row['mapping_status']}  **Review reasons:** {', '.join(row['review_reasons']) or '(none)'}")

    reviewer = ui_state.get_reviewer_name()
    with st.form(f"verify_form_{selected_id}"):
        st.markdown("Enter exactly what you observed in Xactimate's selector browser:")
        col1, col2 = st.columns(2)
        with col1:
            price_list = st.text_input("Price list (e.g. TXDF8X_JUL26)")
            price_list_location = st.text_input("Price-list location")
            price_list_date = st.text_input("Price-list date")
            category = st.text_input("Category")
            selector = st.text_input("Selector")
        with col2:
            xactimate_description = st.text_input("Xactimate description")
            unit = st.text_input("Unit")
            activity_raw = st.text_input("Raw activity symbol (+, -, &, ...)")
            activity_interpretation = st.text_input("Activity interpretation (optional, leave blank unless confirmed)")
            unit_price = st.number_input("Displayed unit price", min_value=0.0, value=0.0, step=0.01)
        green_indicator_choice = st.selectbox("Green indicator", ["unknown", "yes", "no"])
        notes = st.text_area("Reviewer notes")

        st.markdown("**Required confirmations:**")
        confirm_category_selector = st.checkbox("I personally verified this category and selector in Xactimate.")
        confirm_unit = st.checkbox("I verified that the unit matches the intended line item.")
        confirm_price_context = st.checkbox("I understand that the displayed price belongs to the selected price list.")

        scope = st.radio("Save scope", ["Save for this item only", "Save as reusable verified rule"])
        submitted = st.form_submit_button("Save verification")

    if not submitted:
        return

    confirmations = {
        "confirmed_category_selector": confirm_category_selector,
        "confirmed_unit": confirm_unit,
        "confirmed_price_context": confirm_price_context,
    }
    green_indicator = {"unknown": None, "yes": True, "no": False}[green_indicator_choice]
    fields = {
        "category": category.strip(),
        "selector": selector.strip(),
        "description": xactimate_description.strip(),
        "unit": unit.strip(),
        "activity_raw": activity_raw.strip() or None,
        "activity_interpretation": activity_interpretation.strip() or None,
        "green_indicator": green_indicator,
        "price_list": price_list.strip() or None,
        "price_list_location": price_list_location.strip() or None,
        "price_list_date": price_list_date.strip() or None,
        "unit_price": unit_price or None,
    }

    try:
        if scope == "Save for this item only":
            vcs.record_item_only_verification(project_dir, selected_id, fields, reviewer, confirmations, notes)
            st.success(f"Recorded item-only verification for {selected_id}.")
        else:
            fields["trade"] = row["normalized_trade"]
            fields["component"] = row["normalized_component"]
            fields["aliases"] = [row["original_description"]]
            fields["supported_actions"] = [row["normalized_action"]] if row["normalized_action"] != "unknown" else []
            record = vcs.add_record(
                VERIFIED_CATALOG_PATH, BACKUPS_DIR, project_dir, fields, reviewer,
                verification_status=vcs.VERIFICATION_STATUS_HUMAN_VERIFIED, confirmations=confirmations, reviewer_note=notes,
            )
            vcs.apply_verified_match(project_dir, selected_id, record, reviewer, "verified and applied via Verified Catalog tab")
            st.success(f"Saved reusable verified rule {record.category}/{record.selector} and applied it to {selected_id}.")
        st.session_state["verify_in_xactimate_seed_item"] = None
    except vcs.VerificationConfirmationError as exc:
        st.error(str(exc))
    except vcs.RecordValidationError as exc:
        st.error(f"Validation failed: {'; '.join(exc.errors)}")
    except vcs.ApprovalOverrideBlockedError as exc:
        st.error(str(exc))


def _render_group_name_review(project_dir: Path) -> None:
    st.markdown("**Group-name review**")
    st.caption("Suggestions only -- the original extracted section name is always preserved unless you explicitly accept a change.")

    rows = review_service.build_effective_rows(project_dir)
    section_names = sorted({r["section_name"] for r in rows if r["section_name"]})
    if not section_names:
        st.caption("No sections to review.")
        return

    config = gns.load_group_names(GROUP_NAMES_PATH)
    overrides = gns.get_group_name_overrides(project_dir)
    reviewer = ui_state.get_reviewer_name()

    for section_name in section_names:
        existing = overrides.get(section_name)
        suggestion = gns.suggest_group_name(section_name, config)
        status = "reviewed" if existing and (existing.get("reviewed_xactimate_group_name") or existing.get("allow_custom")) else "unreviewed"
        with st.expander(f"{section_name}  [{status}]"):
            st.write(f"Suggested: **{suggestion.suggested_group_name or '(no confident match)'}** (confidence {suggestion.confidence}, method {suggestion.method})")
            choice = st.selectbox(
                "Accept a group name",
                ["(keep original / custom)"] + config.groups,
                index=(config.groups.index(suggestion.suggested_group_name) + 1) if suggestion.suggested_group_name in config.groups else 0,
                key=f"group_choice_{section_name}",
            )
            custom_name = st.text_input("Or enter a custom name", value=(existing or {}).get("reviewed_xactimate_group_name") or "", key=f"group_custom_{section_name}")
            save_alias = st.checkbox("Save as reusable alias for this section name", key=f"group_alias_{section_name}")
            if st.button("Save group-name decision", key=f"group_save_{section_name}"):
                reviewed_name = custom_name.strip() or (choice if choice != "(keep original / custom)" else None)
                allow_custom = reviewed_name is None
                gns.set_group_name_review(project_dir, section_name, suggestion, reviewer, reviewed_group_name=reviewed_name, allow_custom=allow_custom)
                if save_alias and reviewed_name:
                    gns.save_reusable_group_alias(GROUP_NAMES_PATH, BACKUPS_DIR, section_name, reviewed_name)
                st.success(f"Saved group-name decision for {section_name!r}.")
                st.rerun()


def _render_project_context(project_dir: Path) -> None:
    st.markdown("**Xactimate project context**")
    st.caption("Reviewer-entered; used only as a future automation-context record, never to drive Xactimate.")

    context = pcs.get_project_context(project_dir)
    canonical_path = project_dir / "extraction" / "canonical_estimate.json"
    suggested_price_list = None
    if canonical_path.exists():
        import json

        canonical = json.loads(canonical_path.read_text(encoding="utf-8"))
        suggested_price_list = pcs.suggest_price_list_from_canonical(canonical)

    if suggested_price_list and not context.get("price_list"):
        st.info(f"Suggested price list from the extracted estimate: {suggested_price_list} (not yet confirmed).")

    with st.form("project_context_form"):
        profile = st.text_input("Profile", value=context.get("profile") or "")
        project_type = st.selectbox("Project type", ["", "Estimate", "FEMA Flood", "Valuations (360Value)"], index=0)
        project_name = st.text_input("Project name", value=context.get("project_name") or "")
        price_list = st.text_input("Price list", value=context.get("price_list") or suggested_price_list or "")
        tax_jurisdiction = st.text_input("Tax jurisdiction", value=context.get("tax_jurisdiction") or "")
        timezone_label = st.text_input("Timezone label", value=context.get("timezone_label") or "")
        policy_type = st.text_input("Policy type", value=context.get("policy_type") or "")
        deductible_application = st.selectbox("Deductible application", ["", "across_all_coverages", "coverage_specific"], index=0)
        confirm = st.checkbox("I confirm this project context is correct", value=context.get("confirmed", False))
        submitted = st.form_submit_button("Save project context")

    if submitted:
        pcs.set_project_context(
            project_dir,
            {
                "profile": profile or None,
                "project_type": project_type or None,
                "project_name": project_name or None,
                "price_list": price_list or None,
                "tax_jurisdiction": tax_jurisdiction or None,
                "timezone_label": timezone_label or None,
                "policy_type": policy_type or None,
                "deductible_application": deductible_application or None,
            },
            ui_state.get_reviewer_name(),
            confirmed=confirm,
        )
        st.success("Saved.")
        st.rerun()


def _render_backup_controls() -> None:
    st.markdown("**Verified catalog backups**")
    backups = vcs.list_verified_catalog_backups(BACKUPS_DIR)
    st.caption(f"{len(backups)} backup(s) in {BACKUPS_DIR}")
    if backups:
        st.caption(f"Most recent: {backups[-1].name}")
    if st.button("Restore last verified-catalog backup", key="restore_verified_catalog_backup"):
        try:
            restored = vcs.restore_last_verified_catalog_backup(VERIFIED_CATALOG_PATH, BACKUPS_DIR)
            st.success(f"Restored from {restored.name}.")
        except vcs.VerifiedCatalogError as exc:
            st.error(str(exc))
