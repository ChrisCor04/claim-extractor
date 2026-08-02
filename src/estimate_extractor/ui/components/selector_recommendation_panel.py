"""Renders the "Recommended Xactimate Selectors" section inside the
Mapping Review item editor (Phase 3.7), plus a manual selector-catalog
search widget. Every write action here goes through
selector_recommendation.service, which itself never writes anything
except via the existing audited review_service / verified_catalog_service
paths -- see docs/selector-recommendation.md.

A recommendation is always a suggestion: nothing here can approve an item
or create a verified rule without the same reason/confirmation gates the
rest of the review UI already enforces.
"""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from estimate_extractor.mapping.pipeline import DEFAULT_CONFIG_DIR
from estimate_extractor.selector_recommendation import service
from estimate_extractor.ui import review_service, state as ui_state
from estimate_extractor.ui import verified_catalog_service as vcs

_REFERENCE_DIR = DEFAULT_CONFIG_DIR.parent / "fixtures" / "reference"
DEFAULT_SELECTOR_DB_PATH = _REFERENCE_DIR / "data" / "master_selectors.db"
DEFAULT_EXTRACTED_ROOT = _REFERENCE_DIR / "extracted"
VERIFIED_CATALOG_PATH = DEFAULT_CONFIG_DIR / "verified_xactimate_catalog.yaml"
BACKUPS_DIR = DEFAULT_CONFIG_DIR / "backups"


def _badge(candidate) -> str:
    if candidate.source == "verified_catalog":
        return "✅ VERIFIED"
    if candidate.source == "placeholder_mapping":
        return "⚪ PLACEHOLDER"
    if candidate.source_needs_review:
        return "⚠️ UNCERTAIN"
    return "🔵 CATALOG"


def render_selector_recommendations(project_dir: Path, row: dict) -> None:
    st.markdown("**Recommended Xactimate Selectors**")

    if not DEFAULT_SELECTOR_DB_PATH.exists():
        st.info("Selector catalog not built yet -- run `selectors import` first (see docs/selector-catalog.md).")
        return

    line_item_id = row["line_item_id"]
    include_uncertain = st.checkbox(
        "Include uncertain selector references", value=False, key=f"rec_include_uncertain_{line_item_id}",
        help="Also show needs_review=1 selector-catalog records, always visibly labeled and never auto-applied.",
    )

    try:
        result = service.recommend_for_single_item(
            project_dir,
            DEFAULT_SELECTOR_DB_PATH,
            line_item_id,
            verified_catalog_path=VERIFIED_CATALOG_PATH,
            include_uncertain=include_uncertain,
        )
    except service.RecommendationServiceError as exc:
        st.error(str(exc))
        return

    if row["status"] == review_service.STATUS_APPROVED:
        st.caption(
            "This item is already approved -- applying a different candidate requires an explicit override "
            "(not offered here; use the item editor above to change it deliberately)."
        )

    if result is None or not result.candidates:
        st.caption("No defensible candidate. Try the manual search below.")
        return

    st.caption(f"Recommendation state: **{result.state}** ({len(result.candidates)} candidate(s), {result.latency_ms:.1f} ms)")

    reviewer = ui_state.get_reviewer_name()
    reason = st.text_input(
        "Reason (required to apply, reject, or mark a candidate)", key=f"rec_reason_{line_item_id}"
    )

    for candidate in result.candidates:
        with st.container(border=True):
            st.write(f"**#{candidate.rank}  {candidate.category}/{candidate.selector}**  {_badge(candidate)}  score={candidate.score:.2f}")
            st.caption(candidate.description)
            if candidate.match_reasons:
                st.caption("Why: " + "; ".join(candidate.match_reasons))
            if candidate.penalties:
                st.caption("Penalties: " + "; ".join(candidate.penalties))
            if candidate.ocr_confidence is not None:
                st.caption(f"Selector-catalog OCR confidence: {candidate.ocr_confidence:.2f}")

            key_suffix = f"{line_item_id}_{candidate.rank}_{candidate.category}_{candidate.selector}"
            c1, c2, c3, c4, c5 = st.columns(5)

            if c1.button("Apply", key=f"apply_{key_suffix}"):
                if not reason.strip():
                    st.error("A reason is required.")
                else:
                    try:
                        service.apply_candidate(project_dir, line_item_id, candidate, reviewer, reason)
                        st.success("Applied.")
                        st.rerun()
                    except service.RecommendationApplyBlockedError as exc:
                        st.error(str(exc))

            if c2.button("Apply & approve", key=f"apply_approve_{key_suffix}"):
                if not reason.strip():
                    st.error("A reason is required.")
                else:
                    try:
                        service.apply_candidate(project_dir, line_item_id, candidate, reviewer, reason, approve=True)
                        st.success("Applied and approved.")
                        st.rerun()
                    except (service.RecommendationApplyBlockedError, review_service.ApprovalBlockedError) as exc:
                        st.error(str(exc))

            if c3.button("Reject", key=f"reject_{key_suffix}"):
                service.reject_candidate(project_dir, line_item_id, candidate, reviewer, reason=reason)
                st.info("Recorded as rejected.")
                st.rerun()

            if c4.button("Irrelevant", key=f"irrelevant_{key_suffix}"):
                service.mark_candidate_irrelevant(project_dir, line_item_id, candidate, reviewer, reason=reason)
                st.info("Recorded as irrelevant.")
                st.rerun()

            if c5.button("Open screenshot", key=f"screenshot_{key_suffix}"):
                path = service.resolve_candidate_screenshot(candidate, DEFAULT_EXTRACTED_ROOT)
                if path is None:
                    st.warning("Source-screenshot provenance unavailable for this candidate (the candidate is still valid).")
                else:
                    st.image(str(path), caption=candidate.source_image, use_container_width=True)

            with st.expander("Save as reusable verified rule"):
                st.caption("Uses the same Phase 3.5 verification workflow and confirmations as the Verified Catalog tab.")
                confirm_cs = st.checkbox("I personally verified this category and selector in Xactimate.", key=f"rule_confirm_cs_{key_suffix}")
                confirm_unit = st.checkbox("I verified that the unit matches the intended line item.", key=f"rule_confirm_unit_{key_suffix}")
                confirm_price = st.checkbox("I understand the price-list context of this record.", key=f"rule_confirm_price_{key_suffix}")
                rule_note = st.text_input("Notes (optional)", key=f"rule_note_{key_suffix}")
                if st.button("Save reusable rule", key=f"save_rule_{key_suffix}"):
                    confirmations = {
                        "confirmed_category_selector": confirm_cs,
                        "confirmed_unit": confirm_unit,
                        "confirmed_price_context": confirm_price,
                    }
                    normalized_by_id = service.load_normalized_items(project_dir)
                    item = service.build_recommendation_input(row, normalized_by_id.get(line_item_id))
                    try:
                        record = service.save_recommendation_as_verified_rule(
                            VERIFIED_CATALOG_PATH, BACKUPS_DIR, project_dir, item, candidate, reviewer,
                            confirmations=confirmations, reviewer_note=rule_note,
                        )
                        st.success(f"Saved reusable verified rule {record.category}/{record.selector} and applied it.")
                        st.rerun()
                    except vcs.VerificationConfirmationError as exc:
                        st.error(str(exc))
                    except vcs.RecordValidationError as exc:
                        st.error(f"Validation failed: {'; '.join(exc.errors)}")
                    except vcs.ApprovalOverrideBlockedError as exc:
                        st.error(str(exc))


def render_manual_selector_search(project_dir: Path) -> None:
    st.markdown("**Manual selector catalog search**")
    st.caption("Search the full Phase 3.6 catalog directly and apply a result to any line item -- same audited apply workflow as ranked candidates.")
    if not DEFAULT_SELECTOR_DB_PATH.exists():
        st.info("Selector catalog not built yet -- run `selectors import` first.")
        return

    from estimate_extractor.selector_catalog import database
    from estimate_extractor.selector_recommendation import query as recommendation_query
    from estimate_extractor.selector_recommendation.models import Candidate

    col1, col2, col3 = st.columns(3)
    text = col1.text_input("Description contains", key="manual_search_text")
    category = col2.text_input("Category (CAT)", key="manual_search_category")
    selector = col3.text_input("Selector (SEL)", key="manual_search_selector")
    include_uncertain = st.checkbox("Include uncertain references", value=False, key="manual_search_include_uncertain")

    if not (text or category or selector):
        st.caption("Enter a description, category, or selector to search.")
        return

    conn = database.create_database(DEFAULT_SELECTOR_DB_PATH)
    try:
        results = recommendation_query.manual_search(
            conn,
            text=text or None,
            category=category.upper() or None,
            selector=selector.upper() or None,
            include_uncertain=include_uncertain,
            limit=50,
        )
    finally:
        conn.close()
    st.caption(f"{len(results)} match(es).")
    if not results:
        return

    rows = review_service.build_effective_rows(project_dir)
    if not rows:
        for record in results:
            flag = " [NEEDS REVIEW]" if record.needs_review else ""
            st.write(f"**{record.category}/{record.selector}**{flag} -- {record.description_original}")
        return

    ids = [r["line_item_id"] for r in rows]
    target_id = st.selectbox("Apply a result to line item", ids, key="manual_search_target_item")
    reviewer = ui_state.get_reviewer_name()
    reason = st.text_input("Reason (required to apply)", key="manual_search_reason")

    for record in results:
        flag = " [NEEDS REVIEW]" if record.needs_review else ""
        cols = st.columns([5, 1])
        cols[0].write(f"**{record.category}/{record.selector}**{flag} -- {record.description_original}")
        if cols[1].button("Apply", key=f"manual_apply_{record.category}_{record.selector}"):
            if not reason.strip():
                st.error("A reason is required.")
            else:
                candidate = Candidate(
                    category=record.category,
                    selector=record.selector,
                    description=record.description_original,
                    source_needs_review=record.needs_review,
                    score=0.0,
                    rank=0,
                    match_reasons=["selected from manual selector-catalog search"],
                    source_image=record.primary_source_image,
                    ocr_confidence=record.ocr_confidence,
                )
                try:
                    service.apply_candidate(project_dir, target_id, candidate, reviewer, reason)
                    st.success(f"Applied {record.category}/{record.selector} to {target_id}.")
                    st.rerun()
                except service.RecommendationApplyBlockedError as exc:
                    st.error(str(exc))
