from __future__ import annotations

from estimate_extractor.xactimate_lookup import registry
from estimate_extractor.xactimate_lookup.models import MAPPING_STATUS_APPROVED, MAPPING_STATUS_DISABLED, InternalMappingRecord


def _record(signature, category="RFG", selector="ARMVN", status=MAPPING_STATUS_APPROVED, mapping_id=None):
    return InternalMappingRecord(
        mapping_id=mapping_id or f"map_{signature}_{selector}",
        item_signature=signature,
        source_description="Tear off composition shingles",
        search_phrase="composition shingles 3 tab",
        category=category,
        selector=selector,
        xactimate_description="Tear off composition shingles - 3 tab",
        unit="SQ",
        action="remove",
        reviewer="tester",
        approval_reason="matches",
        status=status,
    )


def test_create_database_creates_schema(tmp_path):
    conn = registry.create_database(tmp_path / "reg.db")
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='internal_mappings'")
    assert cur.fetchone() is not None
    conn.close()


def test_save_and_load_record_round_trips(tmp_path):
    conn = registry.create_database(tmp_path / "reg.db")
    registry.save_record(conn, _record("sig-1"))
    loaded = registry.load_all_records(conn)
    conn.close()
    assert len(loaded) == 1
    assert loaded[0].category == "RFG"
    assert loaded[0].selector == "ARMVN"


def test_find_reusable_mapping_returns_approved_only(tmp_path):
    conn = registry.create_database(tmp_path / "reg.db")
    registry.save_record(conn, _record("sig-1", status=MAPPING_STATUS_DISABLED, mapping_id="m1"))
    found = registry.find_reusable_mapping(conn, "sig-1")
    conn.close()
    assert found is None


def test_find_reusable_mapping_returns_approved_record(tmp_path):
    conn = registry.create_database(tmp_path / "reg.db")
    registry.save_record(conn, _record("sig-1", mapping_id="m1"))
    found = registry.find_reusable_mapping(conn, "sig-1")
    conn.close()
    assert found is not None
    assert found.mapping_id == "m1"


def test_find_reusable_mapping_never_returns_rejected(tmp_path):
    from estimate_extractor.xactimate_lookup.models import MAPPING_STATUS_REJECTED

    conn = registry.create_database(tmp_path / "reg.db")
    registry.save_record(conn, _record("sig-1", status=MAPPING_STATUS_REJECTED, mapping_id="m1"))
    found = registry.find_reusable_mapping(conn, "sig-1")
    conn.close()
    assert found is None


def test_record_usage_success_bumps_counters(tmp_path):
    conn = registry.create_database(tmp_path / "reg.db")
    registry.save_record(conn, _record("sig-1", mapping_id="m1"))
    registry.record_usage(conn, "m1", success=True)
    registry.record_usage(conn, "m1", success=True)
    record = registry.find_by_mapping_id(conn, "m1")
    conn.close()
    assert record.usage_count == 2
    assert record.success_count == 2
    assert record.rejection_count == 0


def test_record_usage_rejection_bumps_counters(tmp_path):
    conn = registry.create_database(tmp_path / "reg.db")
    registry.save_record(conn, _record("sig-1", mapping_id="m1"))
    registry.record_usage(conn, "m1", success=False)
    record = registry.find_by_mapping_id(conn, "m1")
    conn.close()
    assert record.usage_count == 1
    assert record.rejection_count == 1
    assert record.success_count == 0


def test_set_status_never_changes_category_selector(tmp_path):
    conn = registry.create_database(tmp_path / "reg.db")
    registry.save_record(conn, _record("sig-1", mapping_id="m1"))
    registry.set_status(conn, "m1", MAPPING_STATUS_DISABLED)
    record = registry.find_by_mapping_id(conn, "m1")
    conn.close()
    assert record.status == MAPPING_STATUS_DISABLED
    assert record.category == "RFG"
    assert record.selector == "ARMVN"


def test_backup_registry_creates_copy(tmp_path):
    db_path = tmp_path / "reg.db"
    conn = registry.create_database(db_path)
    registry.save_record(conn, _record("sig-1"))
    conn.close()
    backup_path = registry.backup_registry(db_path, tmp_path / "backups")
    assert backup_path.exists()


def test_backup_registry_handles_missing_source(tmp_path):
    backup_path = registry.backup_registry(tmp_path / "does_not_exist.db", tmp_path / "backups")
    assert backup_path.exists()
