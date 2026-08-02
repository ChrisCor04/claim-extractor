"""Renders the Claim Summary and Extraction Review screens: read-only
views of canonical_estimate.json / extraction_report.json, plus a narrow,
audited override path for the three attribution fields
(coverage/area/section) that the hardening-phase work already documents
as sometimes genuinely ambiguous. The extractor's own output files are
never modified -- see review_service.override_extraction_field().
"""

from __future__ import annotations

import json
from pathlib import Path

import streamlit as st

from estimate_extractor.ui import review_service, state as ui_state


def _load(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _field_value(claim: dict, key: str):
    f = claim.get(key) or {}
    return f.get("value")


def render_claim_summary(project_dir: Path) -> None:
    st.subheader("Claim Summary")
    canonical = _load(project_dir / "extraction" / "canonical_estimate.json", None)
    report = _load(project_dir / "extraction" / "extraction_report.json", None)
    mapping_report = _load(project_dir / "mapping" / "mapping_report.json", None)

    if canonical is None:
        st.info("This project hasn't been processed yet -- use the Upload / Process tab.")
        return

    document = canonical.get("document", {})
    claim = canonical.get("claim", {})
    address = claim.get("property_address") or {}

    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("Carrier", document.get("carrier_detected", "—"))
        st.caption(f"Confidence: {document.get('carrier_confidence', 0):.2f}")
        st.write(f"**Claim number:** {_field_value(claim, 'claim_number') or '—'}")
        st.write(f"**Estimate number:** {_field_value(claim, 'estimate_number') or '—'}")
        st.write(f"**Policy number:** {_field_value(claim, 'policy_number') or '—'}")
    with col2:
        st.write(f"**Insured:** {_field_value(claim, 'insured_name') or '—'}")
        st.write(f"**Property address:** {address.get('line1') or '—'}")
        st.write(f"**Date of loss:** {_field_value(claim, 'date_of_loss') or '—'}")
        st.write(f"**Price list:** {_field_value(claim, 'price_list') or '—'}")
    with col3:
        st.write(f"**Page count:** {document.get('page_count', '—')}")
        st.write(f"**Line items:** {len(canonical.get('line_items', []))}")
        st.write(f"**Extraction status:** {document.get('extraction_status', '—')}")
        if report:
            recon = report.get("reconciliation", {})
            tol = recon.get("within_tolerance")
            label = "PASS" if tol else ("FAIL" if tol is False else "N/A")
            st.write(f"**Reconciliation:** {label}")
        if mapping_report:
            s = mapping_report.get("summary", {})
            st.write(
                f"**Mapping:** {mapping_report.get('status', '—')} "
                f"(mapped={s.get('mapped', 0)} partial={s.get('partially_mapped', 0)} "
                f"review={s.get('needs_review', 0)} unmapped={s.get('unmapped', 0)} "
                f"unresolved_coverage={s.get('unresolved_coverage', 0)})"
            )
        else:
            st.write("**Mapping:** not run yet")

    if report and report.get("issues"):
        with st.expander(f"Extraction warnings ({len(report['issues'])})"):
            for issue in report["issues"]:
                st.write(f"[{issue['severity'].upper()}] {issue['code']}: {issue['message']}")

    if mapping_report and mapping_report.get("issues"):
        with st.expander(f"Mapping issues ({len(mapping_report['issues'])})"):
            for issue in mapping_report["issues"]:
                st.write(f"[{issue['severity'].upper()}] {issue['code']}: {issue['message']}")


def _build_extraction_rows(canonical: dict) -> list[dict]:
    sections_by_id = {s["section_id"]: s for s in canonical.get("sections", [])}
    areas_by_id = {a["area_id"]: a for a in canonical.get("areas", [])}
    coverages_by_id = {c["coverage_id"]: c for c in canonical.get("coverages", [])}

    rows = []
    for li in canonical.get("line_items", []):
        section = sections_by_id.get(li.get("section_id"))
        area = areas_by_id.get(li.get("area_id"))
        coverage = coverages_by_id.get(li.get("coverage_id"))
        confidence = li.get("confidence") or {}
        source = li.get("source") or {}
        rows.append(
            {
                "line_item_id": li["line_item_id"],
                "source_line_number": li.get("source_line_number"),
                "coverage": coverage["name"] if coverage else None,
                "area": area["name"] if area else None,
                "section": section["name"] if section else None,
                "description": li.get("description"),
                "quantity": li.get("quantity"),
                "unit": li.get("unit_of_measure"),
                "unit_price": li.get("unit_price"),
                "tax": li.get("tax"),
                "RCV": li.get("replacement_cost_value"),
                "depreciation": li.get("depreciation_amount"),
                "ACV": li.get("actual_cash_value"),
                "source_page": source.get("page_start"),
                "extraction_confidence": confidence.get("overall"),
                "extraction_review_required": li.get("needs_review", False),
            }
        )
    return rows


def render_extraction_review(project_dir: Path) -> None:
    st.subheader("Extraction Review")
    canonical = _load(project_dir / "extraction" / "canonical_estimate.json", None)
    if canonical is None:
        st.info("This project hasn't been processed yet -- use the Upload / Process tab.")
        return

    rows = _build_extraction_rows(canonical)
    if not rows:
        st.info("No line items were extracted from this document.")
        return

    import pandas as pd

    df = pd.DataFrame(rows)

    with st.expander("Filters"):
        coverages = sorted({r["coverage"] for r in rows if r["coverage"]})
        areas = sorted({r["area"] for r in rows if r["area"]})
        sections = sorted({r["section"] for r in rows if r["section"]})
        pages = sorted({r["source_page"] for r in rows if r["source_page"] is not None})

        coverage_filter = st.multiselect("Coverage", coverages, key="ext_filter_coverage")
        area_filter = st.multiselect("Area", areas, key="ext_filter_area")
        section_filter = st.multiselect("Section", sections, key="ext_filter_section")
        page_filter = st.multiselect("Page", pages, key="ext_filter_page")
        min_confidence = st.slider("Minimum extraction confidence", 0.0, 1.0, 0.0, 0.05, key="ext_filter_conf")
        warnings_only = st.checkbox("Only show items flagged for review", key="ext_filter_warn")

    filtered = df.copy()
    if coverage_filter:
        filtered = filtered[filtered["coverage"].isin(coverage_filter)]
    if area_filter:
        filtered = filtered[filtered["area"].isin(area_filter)]
    if section_filter:
        filtered = filtered[filtered["section"].isin(section_filter)]
    if page_filter:
        filtered = filtered[filtered["source_page"].isin(page_filter)]
    filtered = filtered[filtered["extraction_confidence"].fillna(0) >= min_confidence]
    if warnings_only:
        filtered = filtered[filtered["extraction_review_required"] == True]  # noqa: E712

    st.caption(f"{len(filtered)} of {len(df)} line items shown. Extracted facts below are read-only.")
    st.dataframe(filtered, use_container_width=True, hide_index=True)

    st.markdown("---")
    st.markdown(
        "**Override an attribution field** (coverage / area / section only -- the raw "
        "description, quantity, unit, and source page are permanently immutable everywhere in this UI)."
    )
    line_item_ids = [r["line_item_id"] for r in rows]
    selected_id = st.selectbox("Line item", line_item_ids, key="extraction_override_item")
    field = st.selectbox("Field", sorted(review_service.EDITABLE_EXTRACTION_FIELDS), key="extraction_override_field")
    new_value = st.text_input("New value", key="extraction_override_value")
    reason = st.text_input("Reason (required)", key="extraction_override_reason")
    if st.button("Save override", key="extraction_override_save"):
        if not reason.strip():
            st.error("A reason is required to override an extracted value.")
        else:
            review_service.override_extraction_field(
                project_dir, selected_id, field, new_value or None, ui_state.get_reviewer_name(), reason
            )
            st.success(f"Override saved for {selected_id}.{field}.")
            st.rerun()
