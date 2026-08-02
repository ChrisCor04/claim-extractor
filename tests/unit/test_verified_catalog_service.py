from __future__ import annotations

import json

import pytest

from estimate_extractor.ui import review_service, verified_catalog_service as vcs


# ---------------------------------------------------------------------------
# Helpers (mirrors tests/unit/test_review_service.py's fixture builders)
# ---------------------------------------------------------------------------


def _normalized_item(line_item_id, description, coverage_id=None, quantity=10.0, unit="SQ", action="remove_and_replace", trade="roofing", component="composition_shingles", section_name="Dwelling Roof"):
    return {
        "line_item_id": line_item_id,
        "original": {
            "description": description,
            "quantity": quantity,
            "unit_of_measure": unit,
            "coverage_id": coverage_id,
            "section_name": section_name,
            "area_name": "Dwelling",
            "source_pages": [7],
            "notes": [],
            "extraction_confidence": 0.95,
            "extraction_needs_review": False,
            "extraction_warnings": [],
        },
        "normalized": {
            "action": action,
            "trade": trade,
            "component": component,
            "material": "laminated composition shingles",
            "attributes": {},
            "quantity": quantity,
            "unit_of_measure": unit,
        },
        "confidence": {"overall": 0.9, "action": 0.9, "trade": 0.9, "component": 0.9, "material": 0.9},
        "needs_review": False,
        "review_reasons": [],
    }


def _mapped_item(line_item_id, coverage_id=None, status="unmapped", best_match=None):
    return {
        "line_item_id": line_item_id,
        "coverage_id": coverage_id,
        "normalization": {
            "action": "remove_and_replace",
            "trade": "roofing",
            "component": "composition_shingles",
            "material": "laminated composition shingles",
            "attributes": {},
            "quantity": 10.0,
            "unit_of_measure": "SQ",
        },
        "mapping": {
            "status": status,
            "best_match": best_match,
            "alternatives": [],
            "needs_review": status != "mapped",
            "review_reasons": [] if best_match and best_match.get("selector") else ["missing_selector"],
        },
    }


def _write_project(tmp_path, items):
    project_dir = tmp_path / "test-project"
    (project_dir / "mapping").mkdir(parents=True)
    (project_dir / "review").mkdir(parents=True)

    normalized = [n for n, _ in items]
    mapped = [m for _, m in items]
    (project_dir / "mapping" / "normalized_estimate.json").write_text(json.dumps(normalized), encoding="utf-8")
    (project_dir / "mapping" / "mapped_estimate.json").write_text(json.dumps(mapped), encoding="utf-8")
    return project_dir


def _catalog_paths(tmp_path):
    return tmp_path / "verified_xactimate_catalog.yaml", tmp_path / "backups"


VALID_CONFIRMATIONS = {"confirmed_category_selector": True, "confirmed_unit": True, "confirmed_price_context": True}


def _pipe_jack_fields(**overrides):
    fields = {
        "category": "RFG",
        "selector": "PJFLASH",
        "description": "Flashing - pipe jack",
        "unit": "EA",
        "activity_raw": "&",
        "aliases": ["R&R Flashing - pipe jack", "Pipe jack flashing", "Pipe flashing"],
        "trade": "roofing",
        "component": "pipe_flashing",
        "supported_actions": ["remove_and_replace", "install"],
    }
    fields.update(overrides)
    return fields


# ---------------------------------------------------------------------------
# Catalog-model tests
# ---------------------------------------------------------------------------


def test_category_selector_compound_uniqueness_rejects_duplicate(tmp_path):
    catalog_path, backups_dir = _catalog_paths(tmp_path)
    project_dir = tmp_path / "proj"
    project_dir.mkdir()

    vcs.add_record(catalog_path, backups_dir, project_dir, _pipe_jack_fields(), "tester", verification_status=vcs.VERIFICATION_STATUS_SCREENSHOT_TRANSCRIBED)

    with pytest.raises(vcs.RecordValidationError):
        vcs.add_record(catalog_path, backups_dir, project_dir, _pipe_jack_fields(description="different text"), "tester", verification_status=vcs.VERIFICATION_STATUS_SCREENSHOT_TRANSCRIBED)


def test_selector_punctuation_preserved_exactly(tmp_path):
    catalog_path, backups_dir = _catalog_paths(tmp_path)
    project_dir = tmp_path / "proj"
    project_dir.mkdir()

    for selector in ("ST", "ST-", "ST+", "ST++", "ST2", "SG2RS"):
        vcs.add_record(
            catalog_path, backups_dir, project_dir,
            {"category": "ACT", "selector": selector, "description": f"item {selector}", "unit": "SF"},
            "tester", verification_status=vcs.VERIFICATION_STATUS_SCREENSHOT_TRANSCRIBED,
        )

    reloaded = vcs.load_verified_catalog(catalog_path)
    selectors = {r.selector for r in reloaded}
    assert selectors == {"ST", "ST-", "ST+", "ST++", "ST2", "SG2RS"}


def test_same_selector_text_allowed_under_different_categories(tmp_path):
    catalog_path, backups_dir = _catalog_paths(tmp_path)
    project_dir = tmp_path / "proj"
    project_dir.mkdir()

    vcs.add_record(catalog_path, backups_dir, project_dir, {"category": "RFG", "selector": "MN", "description": "Roofing labor minimum", "unit": "EA"}, "tester", verification_status=vcs.VERIFICATION_STATUS_SCREENSHOT_TRANSCRIBED)
    vcs.add_record(catalog_path, backups_dir, project_dir, {"category": "ACT", "selector": "MN", "description": "Acoustical ceiling labor minimum", "unit": "EA"}, "tester", verification_status=vcs.VERIFICATION_STATUS_SCREENSHOT_TRANSCRIBED)

    reloaded = vcs.load_verified_catalog(catalog_path)
    assert len(reloaded) == 2
    assert vcs.find_record(reloaded, "RFG", "MN") is not None
    assert vcs.find_record(reloaded, "ACT", "MN") is not None


def test_stable_identity_separated_from_price_observations(tmp_path):
    catalog_path, backups_dir = _catalog_paths(tmp_path)
    project_dir = tmp_path / "proj"
    project_dir.mkdir()

    record = vcs.add_record(
        catalog_path, backups_dir, project_dir, _pipe_jack_fields(price_list="COFC8X_JUL26", unit_price=6.11),
        "tester", verification_status=vcs.VERIFICATION_STATUS_SCREENSHOT_TRANSCRIBED,
    )
    # Identity fields are on the record; price is on a nested observation --
    # never a top-level "price" attribute on the record itself.
    assert not hasattr(record, "price")
    assert not hasattr(record, "unit_price")
    assert record.price_observations[0].unit_price == 6.11
    assert record.price_observations[0].price_list == "COFC8X_JUL26"


def test_multiple_price_list_observations_per_selector(tmp_path):
    catalog_path, backups_dir = _catalog_paths(tmp_path)
    project_dir = tmp_path / "proj"
    project_dir.mkdir()

    vcs.add_record(catalog_path, backups_dir, project_dir, _pipe_jack_fields(price_list="COFC8X_JUL26", unit_price=6.11), "tester", verification_status=vcs.VERIFICATION_STATUS_SCREENSHOT_TRANSCRIBED)
    vcs.add_price_observation(
        catalog_path, backups_dir, project_dir, "RFG", "PJFLASH",
        {"price_list": "TXDF8X_JUL26", "location": "Dallas/Fort Worth, TX", "unit_price": 59.30}, "tester",
    )

    reloaded = vcs.load_verified_catalog(catalog_path)
    record = vcs.find_record(reloaded, "RFG", "PJFLASH")
    assert len(record.price_observations) == 2
    price_lists = {o.price_list for o in record.price_observations}
    assert price_lists == {"COFC8X_JUL26", "TXDF8X_JUL26"}


def test_verification_confirmations_required_for_human_verified(tmp_path):
    catalog_path, backups_dir = _catalog_paths(tmp_path)
    project_dir = tmp_path / "proj"
    project_dir.mkdir()

    with pytest.raises(vcs.VerificationConfirmationError):
        vcs.add_record(catalog_path, backups_dir, project_dir, _pipe_jack_fields(), "tester", verification_status=vcs.VERIFICATION_STATUS_HUMAN_VERIFIED, confirmations={"confirmed_category_selector": True})

    # Catalog file must not have been written to on a rejected attempt.
    assert vcs.load_verified_catalog(catalog_path) == []

    record = vcs.add_record(
        catalog_path, backups_dir, project_dir, _pipe_jack_fields(), "tester",
        verification_status=vcs.VERIFICATION_STATUS_HUMAN_VERIFIED, confirmations=VALID_CONFIRMATIONS,
    )
    assert record.verification_status == vcs.VERIFICATION_STATUS_HUMAN_VERIFIED
    assert record.verified_by == "tester"
    assert record.verified_at is not None


def test_screenshot_transcribed_vs_human_verified_status(tmp_path):
    catalog_path, backups_dir = _catalog_paths(tmp_path)
    project_dir = tmp_path / "proj"
    project_dir.mkdir()

    transcribed = vcs.add_record(catalog_path, backups_dir, project_dir, {"category": "ACT", "selector": "AV", "description": "Acoustic ceiling tile", "unit": "SF"}, "tester", verification_status=vcs.VERIFICATION_STATUS_SCREENSHOT_TRANSCRIBED)
    assert transcribed.verification_status == vcs.VERIFICATION_STATUS_SCREENSHOT_TRANSCRIBED
    assert transcribed.verified_by is None
    assert transcribed.verified_at is None
    assert transcribed.verification_status not in vcs.AUTOMATION_READY_STATUSES

    upgraded = vcs.upgrade_to_human_verified(catalog_path, backups_dir, project_dir, "ACT", "AV", "tester", VALID_CONFIRMATIONS)
    assert upgraded.verification_status == vcs.VERIFICATION_STATUS_HUMAN_VERIFIED
    assert upgraded.verified_by == "tester"


def test_invalid_unit_rejected(tmp_path):
    catalog_path, backups_dir = _catalog_paths(tmp_path)
    project_dir = tmp_path / "proj"
    project_dir.mkdir()

    with pytest.raises(vcs.RecordValidationError) as excinfo:
        vcs.add_record(catalog_path, backups_dir, project_dir, _pipe_jack_fields(unit="FURLONGS"), "tester", verification_status=vcs.VERIFICATION_STATUS_SCREENSHOT_TRANSCRIBED)
    assert any("unit" in e.lower() for e in excinfo.value.errors)


def test_missing_verification_provenance_rejected(tmp_path):
    catalog_path, backups_dir = _catalog_paths(tmp_path)
    project_dir = tmp_path / "proj"
    project_dir.mkdir()

    with pytest.raises(vcs.VerificationConfirmationError):
        vcs.add_record(catalog_path, backups_dir, project_dir, _pipe_jack_fields(), "", verification_status=vcs.VERIFICATION_STATUS_HUMAN_VERIFIED, confirmations=VALID_CONFIRMATIONS)


def test_unknown_activity_symbol_preserved(tmp_path):
    catalog_path, backups_dir = _catalog_paths(tmp_path)
    project_dir = tmp_path / "proj"
    project_dir.mkdir()

    record = vcs.add_record(catalog_path, backups_dir, project_dir, _pipe_jack_fields(activity_raw="???"), "tester", verification_status=vcs.VERIFICATION_STATUS_SCREENSHOT_TRANSCRIBED)
    assert record.activity_raw == "???"
    assert record.activity_interpretation is None  # never guessed


def test_green_indicator_tristate_preserved(tmp_path):
    catalog_path, backups_dir = _catalog_paths(tmp_path)
    project_dir = tmp_path / "proj"
    project_dir.mkdir()

    for selector, value in (("A1", None), ("A2", True), ("A3", False)):
        vcs.add_record(
            catalog_path, backups_dir, project_dir,
            {"category": "ACT", "selector": selector, "description": "x", "unit": "EA", "green_indicator": value},
            "tester", verification_status=vcs.VERIFICATION_STATUS_SCREENSHOT_TRANSCRIBED,
        )
    reloaded = {r.selector: r.green_indicator for r in vcs.load_verified_catalog(catalog_path)}
    assert reloaded == {"A1": None, "A2": True, "A3": False}


# ---------------------------------------------------------------------------
# Matching tests
# ---------------------------------------------------------------------------


def test_verified_match_coexists_with_placeholder_without_overwriting_it(tmp_path):
    catalog_path, backups_dir = _catalog_paths(tmp_path)
    project_dir = _write_project(
        tmp_path,
        [
            (
                _normalized_item("line_0001", "R&R Flashing - pipe jack", unit="EA", trade="roofing", component="pipe_flashing"),
                _mapped_item("line_0001", status="partially_mapped", best_match={"mapping_id": "placeholder_rule", "category": "RFG", "selector": None, "activity": "install", "description": "pipe jack flashing", "confidence": 0.85}),
            )
        ],
    )
    vcs.add_record(catalog_path, backups_dir, project_dir, _pipe_jack_fields(), "tester", verification_status=vcs.VERIFICATION_STATUS_HUMAN_VERIFIED, confirmations=VALID_CONFIRMATIONS)

    records = vcs.load_verified_catalog(catalog_path)
    row = review_service.build_effective_rows(project_dir)[0]
    matches = vcs.find_verified_matches(row, records)

    assert len(matches) == 1
    assert matches[0].record.verification_status == vcs.VERIFICATION_STATUS_HUMAN_VERIFIED
    # The machine's own placeholder suggestion is untouched by the presence
    # of a verified match -- verified matches are purely additive.
    assert row["mapping_status"] == "partially_mapped"
    assert row["selector"] is None


def test_unit_mismatch_blocks_verified_match(tmp_path):
    catalog_path, backups_dir = _catalog_paths(tmp_path)
    project_dir = _write_project(
        tmp_path,
        [(_normalized_item("line_0001", "R&R Flashing - pipe jack", unit="LF", trade="roofing", component="pipe_flashing"), _mapped_item("line_0001"))],
    )
    vcs.add_record(catalog_path, backups_dir, project_dir, _pipe_jack_fields(unit="EA"), "tester", verification_status=vcs.VERIFICATION_STATUS_HUMAN_VERIFIED, confirmations=VALID_CONFIRMATIONS)

    records = vcs.load_verified_catalog(catalog_path)
    row = review_service.build_effective_rows(project_dir)[0]
    assert vcs.find_verified_matches(row, records) == []


def test_action_conflict_blocks_verified_match(tmp_path):
    catalog_path, backups_dir = _catalog_paths(tmp_path)
    project_dir = _write_project(
        tmp_path,
        [(_normalized_item("line_0001", "R&R Flashing - pipe jack", action="paint", trade="roofing", component="pipe_flashing", unit="EA"), _mapped_item("line_0001"))],
    )
    vcs.add_record(catalog_path, backups_dir, project_dir, _pipe_jack_fields(supported_actions=["remove_and_replace"]), "tester", verification_status=vcs.VERIFICATION_STATUS_HUMAN_VERIFIED, confirmations=VALID_CONFIRMATIONS)

    records = vcs.load_verified_catalog(catalog_path)
    row = review_service.build_effective_rows(project_dir)[0]
    assert vcs.find_verified_matches(row, records) == []


def test_suffix_sensitive_selector_handling_via_negative_patterns(tmp_path):
    """'ST' (standard) and 'ST+' (high grade) must not be conflated -- the
    high-grade record's negative pattern excludes plain descriptions, and
    vice versa via distinct aliases."""
    catalog_path, backups_dir = _catalog_paths(tmp_path)
    project_dir = _write_project(
        tmp_path,
        [
            (_normalized_item("line_0001", "Suspended ceiling tile - 2x4", trade="other", component="unknown", unit="SF"), _mapped_item("line_0001")),
            (_normalized_item("line_0002", "Suspended ceiling tile - High grade - 2x4", trade="other", component="unknown", unit="SF"), _mapped_item("line_0002")),
        ],
    )
    vcs.add_record(
        catalog_path, backups_dir, project_dir,
        {"category": "ACT", "selector": "ST", "description": "Suspended ceiling tile", "unit": "SF", "trade": "other", "component": "unknown", "negative_patterns": ["high grade", "premium grade"]},
        "tester", verification_status=vcs.VERIFICATION_STATUS_HUMAN_VERIFIED, confirmations=VALID_CONFIRMATIONS,
    )
    vcs.add_record(
        catalog_path, backups_dir, project_dir,
        {"category": "ACT", "selector": "ST+", "description": "Suspended ceiling tile - High grade", "unit": "SF", "trade": "other", "component": "unknown"},
        "tester", verification_status=vcs.VERIFICATION_STATUS_HUMAN_VERIFIED, confirmations=VALID_CONFIRMATIONS,
    )

    records = vcs.load_verified_catalog(catalog_path)
    rows = {r["line_item_id"]: r for r in review_service.build_effective_rows(project_dir)}

    standard_matches = vcs.find_verified_matches(rows["line_0001"], records)
    assert [m.record.selector for m in standard_matches] == ["ST"]

    high_grade_matches = vcs.find_verified_matches(rows["line_0002"], records)
    assert "ST+" in [m.record.selector for m in high_grade_matches]
    assert "ST" not in [m.record.selector for m in high_grade_matches]


def test_negative_pattern_excludes_match(tmp_path):
    catalog_path, backups_dir = _catalog_paths(tmp_path)
    project_dir = _write_project(
        tmp_path,
        [(_normalized_item("line_0001", "Roofing felt - 15 lb w/out felt included note", trade="roofing", component="roofing_felt", unit="SQ"), _mapped_item("line_0001"))],
    )
    vcs.add_record(
        catalog_path, backups_dir, project_dir,
        {"category": "RFG", "selector": "FELT15", "description": "Roofing felt - 15 lb", "unit": "SQ", "trade": "roofing", "component": "roofing_felt", "negative_patterns": ["w/out felt"]},
        "tester", verification_status=vcs.VERIFICATION_STATUS_HUMAN_VERIFIED, confirmations=VALID_CONFIRMATIONS,
    )
    records = vcs.load_verified_catalog(catalog_path)
    row = review_service.build_effective_rows(project_dir)[0]
    assert vcs.find_verified_matches(row, records) == []


def test_item_only_verification_makes_item_automation_ready(tmp_path):
    project_dir = _write_project(
        tmp_path,
        [(_normalized_item("line_0001", "Unusual one-off item", trade="roofing", component="pipe_flashing", unit="EA"), _mapped_item("line_0001"))],
    )
    review_service.edit_mapping_field(project_dir, "line_0001", "category", "RFG", "tester", "matches item-only verification")
    review_service.edit_mapping_field(project_dir, "line_0001", "selector", "ONEOFF", "tester", "matches item-only verification")
    review_service.edit_mapping_field(project_dir, "line_0001", "activity", "install", "tester", "matches item-only verification")
    review_service.approve_item(project_dir, "line_0001", "tester")

    vcs.record_item_only_verification(
        project_dir, "line_0001",
        {"category": "RFG", "selector": "ONEOFF", "description": "one off item", "unit": "EA"},
        "tester", VALID_CONFIRMATIONS,
    )

    row = review_service.build_effective_rows(project_dir, line_item_ids=["line_0001"])[0]
    ready, reasons = vcs.is_automation_ready(row, project_dir, [], group_reviewed=True)
    assert ready is True, reasons


def test_reusable_verified_rule_makes_item_automation_ready(tmp_path):
    catalog_path, backups_dir = _catalog_paths(tmp_path)
    project_dir = _write_project(
        tmp_path,
        [(_normalized_item("line_0001", "R&R Flashing - pipe jack", trade="roofing", component="pipe_flashing", unit="EA"), _mapped_item("line_0001"))],
    )
    vcs.add_record(catalog_path, backups_dir, project_dir, _pipe_jack_fields(), "tester", verification_status=vcs.VERIFICATION_STATUS_HUMAN_VERIFIED, confirmations=VALID_CONFIRMATIONS)
    records = vcs.load_verified_catalog(catalog_path)

    vcs.apply_verified_match(project_dir, "line_0001", records[0], "tester", "verified via reusable rule")
    review_service.approve_item(project_dir, "line_0001", "tester")

    row = review_service.build_effective_rows(project_dir, line_item_ids=["line_0001"])[0]
    ready, reasons = vcs.is_automation_ready(row, project_dir, records, group_reviewed=True)
    assert ready is True, reasons


def test_approved_mapping_not_silently_overwritten_by_verified_match(tmp_path):
    catalog_path, backups_dir = _catalog_paths(tmp_path)
    project_dir = _write_project(
        tmp_path,
        [(_normalized_item("line_0001", "R&R Flashing - pipe jack", trade="roofing", component="pipe_flashing", unit="EA"), _mapped_item("line_0001"))],
    )
    # Approve the item with a DIFFERENT category/selector than the verified record.
    review_service.edit_mapping_field(project_dir, "line_0001", "category", "RFG", "tester", "manual entry")
    review_service.edit_mapping_field(project_dir, "line_0001", "selector", "MANUAL1", "tester", "manual entry")
    review_service.edit_mapping_field(project_dir, "line_0001", "activity", "install", "tester", "manual entry")
    review_service.approve_item(project_dir, "line_0001", "tester")

    vcs.add_record(catalog_path, backups_dir, project_dir, _pipe_jack_fields(selector="DIFFERENT"), "tester", verification_status=vcs.VERIFICATION_STATUS_HUMAN_VERIFIED, confirmations=VALID_CONFIRMATIONS)
    records = vcs.load_verified_catalog(catalog_path)

    with pytest.raises(vcs.ApprovalOverrideBlockedError):
        vcs.apply_verified_match(project_dir, "line_0001", records[0], "tester", "trying to apply a different verified rule")

    # Confirm nothing changed.
    row = review_service.build_effective_rows(project_dir, line_item_ids=["line_0001"])[0]
    assert row["selector"] == "MANUAL1"

    # Explicit override succeeds and is itself audited via edit_mapping_field's history.
    vcs.apply_verified_match(project_dir, "line_0001", records[0], "tester", "confirmed override after re-review", allow_override_approved=True)
    row = review_service.build_effective_rows(project_dir, line_item_ids=["line_0001"])[0]
    assert row["selector"] == "DIFFERENT"


def test_catalog_conflict_preview_flags_approved_item(tmp_path):
    project_dir = _write_project(
        tmp_path,
        [(_normalized_item("line_0001", "R&R Flashing - pipe jack", trade="roofing", component="pipe_flashing", unit="EA"), _mapped_item("line_0001"))],
    )
    review_service.edit_mapping_field(project_dir, "line_0001", "category", "RFG", "tester", "manual entry")
    review_service.edit_mapping_field(project_dir, "line_0001", "selector", "MANUAL1", "tester", "manual entry")
    review_service.edit_mapping_field(project_dir, "line_0001", "activity", "install", "tester", "manual entry")
    review_service.approve_item(project_dir, "line_0001", "tester")

    rows = review_service.build_effective_rows(project_dir)
    preview = vcs.preview_record_effect(_pipe_jack_fields(selector="DIFFERENT"), rows)
    assert "line_0001" in preview["already_approved_conflicts"]
    assert "line_0001" not in preview["new_matches"]


# ---------------------------------------------------------------------------
# Audit tests
# ---------------------------------------------------------------------------


def test_backup_created_before_every_write(tmp_path):
    catalog_path, backups_dir = _catalog_paths(tmp_path)
    project_dir = tmp_path / "proj"
    project_dir.mkdir()

    assert vcs.list_verified_catalog_backups(backups_dir) == []
    vcs.add_record(catalog_path, backups_dir, project_dir, _pipe_jack_fields(), "tester", verification_status=vcs.VERIFICATION_STATUS_SCREENSHOT_TRANSCRIBED)
    backups_after_first = vcs.list_verified_catalog_backups(backups_dir)
    assert len(backups_after_first) == 1

    vcs.add_price_observation(catalog_path, backups_dir, project_dir, "RFG", "PJFLASH", {"price_list": "TEST_PL", "unit_price": 1.0}, "tester")
    assert len(vcs.list_verified_catalog_backups(backups_dir)) == 2


def test_restore_last_backup_reverts_content(tmp_path):
    catalog_path, backups_dir = _catalog_paths(tmp_path)
    project_dir = tmp_path / "proj"
    project_dir.mkdir()

    vcs.add_record(catalog_path, backups_dir, project_dir, _pipe_jack_fields(), "tester", verification_status=vcs.VERIFICATION_STATUS_SCREENSHOT_TRANSCRIBED)
    before_second_write = catalog_path.read_text(encoding="utf-8")

    vcs.add_record(catalog_path, backups_dir, project_dir, {"category": "ACT", "selector": "AV", "description": "x", "unit": "SF"}, "tester", verification_status=vcs.VERIFICATION_STATUS_SCREENSHOT_TRANSCRIBED)
    assert catalog_path.read_text(encoding="utf-8") != before_second_write

    vcs.restore_last_verified_catalog_backup(catalog_path, backups_dir)
    assert catalog_path.read_text(encoding="utf-8") == before_second_write


def test_restore_raises_when_no_backups(tmp_path):
    catalog_path, backups_dir = _catalog_paths(tmp_path)
    with pytest.raises(vcs.VerifiedCatalogError):
        vcs.restore_last_verified_catalog_backup(catalog_path, backups_dir)


def test_catalog_change_event_records_provenance_and_hashes(tmp_path):
    catalog_path, backups_dir = _catalog_paths(tmp_path)
    project_dir = tmp_path / "proj"
    project_dir.mkdir()

    vcs.add_record(
        catalog_path, backups_dir, project_dir, _pipe_jack_fields(), "tester",
        verification_status=vcs.VERIFICATION_STATUS_HUMAN_VERIFIED, confirmations=VALID_CONFIRMATIONS, reviewer_note="verified in xactimate",
    )

    changes = json.loads((project_dir / "review" / "catalog_changes.json").read_text(encoding="utf-8"))
    assert len(changes) == 1
    change = changes[0]
    assert change["target"] == "verified_catalog"
    assert change["action"] == "add_verified_record"
    assert change["reviewer"] == "tester"
    assert change["reviewer_note"] == "verified in xactimate"
    assert "timestamp" in change
    assert change["previous_hash"] != change["new_hash"]
    assert change["affected_line_items"] == []


def test_selector_verification_event_has_full_provenance(tmp_path):
    project_dir = tmp_path / "proj"
    (project_dir / "review").mkdir(parents=True)
    (project_dir / "mapping").mkdir(parents=True)
    (project_dir / "mapping" / "normalized_estimate.json").write_text("[]", encoding="utf-8")
    (project_dir / "mapping" / "mapped_estimate.json").write_text("[]", encoding="utf-8")

    entry = vcs.record_item_only_verification(
        project_dir, "line_0001",
        {"category": "RFG", "selector": "ONEOFF", "description": "x", "unit": "EA", "price_list": "TXDF8X_JUL26"},
        "tester", VALID_CONFIRMATIONS, notes="one-off verification",
    )
    assert entry["verified_by"] == "tester"
    assert entry["verification_status"] == vcs.VERIFICATION_STATUS_HUMAN_VERIFIED
    assert entry["price_list"] == "TXDF8X_JUL26"
    assert "verified_at" in entry

    stored = vcs.get_item_only_verifications(project_dir)
    assert stored["line_0001"] == entry


def test_price_list_provenance_never_conflated_with_selector_identity(tmp_path):
    catalog_path, backups_dir = _catalog_paths(tmp_path)
    project_dir = tmp_path / "proj"
    project_dir.mkdir()

    vcs.add_record(catalog_path, backups_dir, project_dir, _pipe_jack_fields(price_list="COFC8X_JUL26", unit_price=6.11), "tester", verification_status=vcs.VERIFICATION_STATUS_SCREENSHOT_TRANSCRIBED)
    records_before = vcs.load_verified_catalog(catalog_path)
    id_before = records_before[0].catalog_record_id

    vcs.add_price_observation(catalog_path, backups_dir, project_dir, "RFG", "PJFLASH", {"price_list": "TXDF8X_JUL26", "unit_price": 59.30}, "tester")
    records_after = vcs.load_verified_catalog(catalog_path)

    assert len(records_after) == 1  # a new price observation never creates a new identity record
    assert records_after[0].catalog_record_id == id_before
