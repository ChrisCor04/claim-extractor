"""Renders the "Xactimate Lookup" section inside the Mapping Review item
editor: trusted-mapping-first status, lookup method, cached CAT/SEL and
search phrase, a ranking explanation, an automation-readiness signal, and
the manual capture form for a dropdown result a reviewer found in their
own Xactimate session.

This is the manual workflow the build spec requires be usable before any
desktop automation exists -- nothing here drives Xactimate itself. Every
write goes through xactimate_lookup.service.record_resolution, which
itself only writes through review_service (item-level) and the internal
lookup registry (reusable mapping), both backup-before-write and
audit-logged, mirroring selector_recommendation_panel.py exactly.
"""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from estimate_extractor.mapping.pipeline import DEFAULT_CONFIG_DIR
from estimate_extractor.ui import review_service, state as ui_state
from estimate_extractor.xactimate_lookup import phrase_generator, service, signature as signature_mod
from estimate_extractor.xactimate_lookup.models import LOOKUP_PATH_TRUSTED, MAPPING_STATUS_APPROVED

VERIFIED_CATALOG_PATH = DEFAULT_CONFIG_DIR / "verified_xactimate_catalog.yaml"


def _lookup_method_label(plan) -> str:
    return "CAT/SEL (trusted mapping)" if plan.path == LOOKUP_PATH_TRUSTED else "Description search"


def render_lookup_workflow(project_dir: Path, row: dict) -> None:
    st.markdown("**Xactimate Lookup**")

    line_item_id = row["line_item_id"]
    normalized_by_id = service.recommendation_service.load_normalized_items(project_dir)
    item = service.build_lookup_input(row, normalized_by_id.get(line_item_id))

    rules = phrase_generator.load_phrase_rules()
    item_signature = signature_mod.compute_item_signature(
        item.trade, item.component, item.material, item.action, item.source_unit, item.original_description or "", rules
    )

    resolved_registry = service.DEFAULT_REGISTRY_DB_PATH
    verified_records = []
    if VERIFIED_CATALOG_PATH.exists():
        from estimate_extractor.ui import verified_catalog_service as vcs

        verified_records = vcs.load_verified_catalog(VERIFIED_CATALOG_PATH)

    conn = service.registry.create_database(resolved_registry)
    try:
        plan = service.orchestrator.build_lookup_plan(item, conn, rules, verified_records)
    finally:
        conn.close()

    phrase_result = phrase_generator.generate_search_phrase(item.original_description or "", item.component, item.material, item.action, rules)

    # -- Always-shown status header: lookup method, cached CAT/SEL, cached
    # search phrase, automation readiness -- regardless of which path is active.
    col1, col2 = st.columns(2)
    col1.metric("Lookup method", _lookup_method_label(plan))
    trusted = plan.trusted_mapping
    automation_ready = trusted is not None and trusted.status == MAPPING_STATUS_APPROVED
    col2.metric("Automation readiness", "Backed by trusted mapping" if automation_ready else "Not ready -- needs resolution")
    st.caption(f"Source description: {item.original_description or '(none)'}")
    st.caption(f"Cached search phrase: `{phrase_result.phrase}`")
    st.caption(f"Cached CAT/SEL: {f'{trusted.category}/{trusted.selector}' if trusted else '(none yet)'}")

    if plan.path == LOOKUP_PATH_TRUSTED:
        with st.expander("Ranking explanation", expanded=True):
            if trusted.mapping_id.startswith("verified_catalog:"):
                st.caption("Backed by a Phase 3.5 human-verified catalog rule (compatibility-matched on trade/component/unit/action).")
            else:
                st.caption(
                    f"Backed by internal registry mapping {trusted.mapping_id}: used {trusted.usage_count}x, "
                    f"{trusted.success_count} successful, {trusted.rejection_count} rejected. "
                    f"Approved by {trusted.reviewer}: {trusted.approval_reason!r}"
                )
        st.success(f"Trusted internal mapping found: **{trusted.category}/{trusted.selector}** -- {trusted.xactimate_description}")
        reviewer = ui_state.get_reviewer_name()
        reason = st.text_input("Reason (required to apply)", key=f"lookup_trusted_reason_{line_item_id}")
        if st.button("Apply trusted CAT/SEL to this item", key=f"lookup_apply_trusted_{line_item_id}"):
            if not reason.strip():
                st.error("A reason is required.")
            else:
                try:
                    service.record_resolution(
                        project_dir, resolved_registry, service.DEFAULT_BACKUPS_DIR, item, item_signature, plan.search_input,
                        category=trusted.category, selector=trusted.selector, xactimate_description=trusted.xactimate_description,
                        unit=trusted.unit, action=trusted.action, xactimate_item_number=trusted.xactimate_item_number,
                        reviewer=reviewer, approval_reason=reason, save_as_reusable_mapping=False,
                    )
                    if not trusted.mapping_id.startswith("verified_catalog:"):
                        service.record_reuse_outcome(resolved_registry, trusted.mapping_id, success=True)
                    st.success("Applied.")
                    st.rerun()
                except service.LookupApplyBlockedError as exc:
                    st.error(str(exc))
        return

    st.info("No trusted internal mapping yet -- search Xactimate's top search box by description.")
    with st.expander("Ranking explanation"):
        st.caption(
            "No live Xactimate session is connected, so dropdown candidates cannot be captured or ranked "
            "automatically in this build. Record what you personally found below; once approved, it becomes "
            "the trusted mapping for future items with this same signature."
        )
    st.caption("Search phrase (click to copy):")
    st.code(phrase_result.phrase, language=None)
    with st.expander("Why this phrase"):
        for reason_text in phrase_result.reasons:
            st.caption(f"+ {reason_text}")
        for bucket in phrase_result.dropped:
            st.caption(f"- dropped: {bucket}")

    st.markdown("**Record what you found in Xactimate's dropdown**")
    reviewer = ui_state.get_reviewer_name()
    with st.form(key=f"lookup_record_form_{line_item_id}"):
        col1, col2 = st.columns(2)
        with col1:
            category = st.text_input("Category (CAT)", key=f"lookup_cat_{line_item_id}")
            selector = st.text_input("Selector (SEL)", key=f"lookup_sel_{line_item_id}")
            xactimate_description = st.text_input("Xactimate description", key=f"lookup_desc_{line_item_id}")
            item_number = st.text_input("Xactimate item/row number (if shown)", key=f"lookup_itemnum_{line_item_id}")
        with col2:
            unit = st.text_input("Unit", key=f"lookup_unit_{line_item_id}")
            action_value = st.text_input("Action (optional)", key=f"lookup_action_{line_item_id}")
            activity_raw = st.text_input("Raw Xactimate activity symbol (optional, e.g. +, R&R)", key=f"lookup_activity_raw_{line_item_id}")
            evidence = st.text_input("Evidence reference (screenshot path/log id, optional)", key=f"lookup_evidence_{line_item_id}")
        save_as_reusable = st.checkbox("Also save as a reusable mapping for future similar items", value=True, key=f"lookup_save_reusable_{line_item_id}")
        reason = st.text_input("Reason (required, audited)", key=f"lookup_form_reason_{line_item_id}")
        approve_too = st.checkbox("Also mark this item approved", key=f"lookup_approve_too_{line_item_id}")
        submitted = st.form_submit_button("Apply this result")

    if submitted:
        if not reason.strip():
            st.error("A reason is required.")
        elif not category.strip() or not selector.strip():
            st.error("Category and selector are both required.")
        else:
            try:
                record = service.record_resolution(
                    project_dir, resolved_registry, service.DEFAULT_BACKUPS_DIR, item, item_signature, phrase_result.phrase,
                    category=category.strip(), selector=selector.strip(), xactimate_description=xactimate_description.strip() or category.strip(),
                    unit=unit.strip() or None, action=action_value.strip() or None, xactimate_item_number=item_number.strip() or None,
                    reviewer=reviewer, approval_reason=reason, evidence_reference=evidence.strip() or None,
                    xactimate_activity_raw=activity_raw.strip() or None,
                    approve=approve_too, save_as_reusable_mapping=save_as_reusable,
                )
                if record:
                    st.success(f"Applied and saved reusable mapping {record.mapping_id}.")
                else:
                    st.success("Applied to this item.")
                st.rerun()
            except service.LookupApplyBlockedError as exc:
                st.error(str(exc))
            except service.MappingConflictError as exc:
                st.error(str(exc))
            except service.LookupServiceError as exc:
                st.error(str(exc))
