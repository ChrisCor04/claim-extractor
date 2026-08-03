"""SQLite persistence for the internal lookup registry -- learned,
human-approved item-signature -> CAT/SEL resolutions. Same stdlib-only
``sqlite3`` approach as selector_catalog/database.py.

Only ``status == approved`` records are ever returned by
``find_reusable_mapping`` -- see build spec "Only genuinely approved
records may be reused automatically." Nothing here promotes a record to
approved on its own; that always requires an explicit reviewer action
(see service.py).
"""

from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from estimate_extractor.xactimate_lookup.models import REUSABLE_MAPPING_STATUSES, InternalMappingRecord

SCHEMA = """
CREATE TABLE IF NOT EXISTS internal_mappings (
    mapping_id TEXT PRIMARY KEY,
    item_signature TEXT NOT NULL,
    source_description TEXT NOT NULL,
    search_phrase TEXT NOT NULL,
    category TEXT NOT NULL,
    selector TEXT NOT NULL,
    xactimate_description TEXT NOT NULL,
    xactimate_item_number TEXT,
    unit TEXT,
    action TEXT,
    material TEXT,
    size_key TEXT,
    grade_key TEXT,
    normalized_description TEXT,
    xactimate_activity_raw TEXT,
    reviewer TEXT NOT NULL,
    approval_reason TEXT NOT NULL,
    evidence_reference TEXT,
    status TEXT NOT NULL DEFAULT 'approved',
    usage_count INTEGER NOT NULL DEFAULT 0,
    success_count INTEGER NOT NULL DEFAULT 0,
    rejection_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_internal_mappings_signature ON internal_mappings(item_signature);
CREATE INDEX IF NOT EXISTS idx_internal_mappings_status ON internal_mappings(status);
CREATE INDEX IF NOT EXISTS idx_internal_mappings_category_selector ON internal_mappings(category, selector);
"""

BACKUP_PREFIX = "internal_lookup_registry"


def create_database(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def backup_registry(db_path: Path, backups_dir: Path) -> Path:
    backups_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_path = backups_dir / f"{BACKUP_PREFIX}_{timestamp}.db"
    if db_path.exists():
        shutil.copy2(db_path, backup_path)
    else:
        create_database(backup_path).close()
    return backup_path


def _row_to_record(row: sqlite3.Row) -> InternalMappingRecord:
    return InternalMappingRecord(
        mapping_id=row["mapping_id"],
        item_signature=row["item_signature"],
        source_description=row["source_description"],
        search_phrase=row["search_phrase"],
        category=row["category"],
        selector=row["selector"],
        xactimate_description=row["xactimate_description"],
        xactimate_item_number=row["xactimate_item_number"],
        unit=row["unit"],
        action=row["action"],
        material=row["material"],
        size_key=row["size_key"],
        grade_key=row["grade_key"],
        normalized_description=row["normalized_description"],
        xactimate_activity_raw=row["xactimate_activity_raw"],
        reviewer=row["reviewer"],
        approval_reason=row["approval_reason"],
        evidence_reference=row["evidence_reference"],
        status=row["status"],
        usage_count=row["usage_count"],
        success_count=row["success_count"],
        rejection_count=row["rejection_count"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _record_to_row(r: InternalMappingRecord) -> tuple:
    return (
        r.mapping_id, r.item_signature, r.source_description, r.search_phrase, r.category, r.selector,
        r.xactimate_description, r.xactimate_item_number, r.unit, r.action, r.material, r.size_key,
        r.grade_key, r.normalized_description, r.xactimate_activity_raw, r.reviewer, r.approval_reason,
        r.evidence_reference, r.status, r.usage_count, r.success_count, r.rejection_count, r.created_at, r.updated_at,
    )


_INSERT_SQL = """
INSERT INTO internal_mappings (
    mapping_id, item_signature, source_description, search_phrase, category, selector,
    xactimate_description, xactimate_item_number, unit, action, material, size_key,
    grade_key, normalized_description, xactimate_activity_raw, reviewer, approval_reason, evidence_reference, status,
    usage_count, success_count, rejection_count, created_at, updated_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def save_record(conn: sqlite3.Connection, record: InternalMappingRecord) -> None:
    """Insert-or-replace by mapping_id -- the caller (service.py) is
    responsible for bumping ``updated_at`` and never silently changing an
    approved record's CAT/SEL (see build spec 'Do not silently change
    approved mappings.')."""
    conn.execute("DELETE FROM internal_mappings WHERE mapping_id = ?", (record.mapping_id,))
    conn.execute(_INSERT_SQL, _record_to_row(record))
    conn.commit()


def load_all_records(conn: sqlite3.Connection) -> list[InternalMappingRecord]:
    cur = conn.execute("SELECT * FROM internal_mappings ORDER BY item_signature")
    return [_row_to_record(row) for row in cur.fetchall()]


def find_by_signature(conn: sqlite3.Connection, item_signature: str) -> list[InternalMappingRecord]:
    cur = conn.execute("SELECT * FROM internal_mappings WHERE item_signature = ?", (item_signature,))
    return [_row_to_record(row) for row in cur.fetchall()]


def find_reusable_mapping(conn: sqlite3.Connection, item_signature: str) -> InternalMappingRecord | None:
    """The one sanctioned lookup for the fast CAT/SEL path: an exact
    signature match whose status is in REUSABLE_MAPPING_STATUSES. Never
    returns a disabled or rejected record."""
    for record in find_by_signature(conn, item_signature):
        if record.status in REUSABLE_MAPPING_STATUSES:
            return record
    return None


def find_by_mapping_id(conn: sqlite3.Connection, mapping_id: str) -> InternalMappingRecord | None:
    cur = conn.execute("SELECT * FROM internal_mappings WHERE mapping_id = ?", (mapping_id,))
    row = cur.fetchone()
    return _row_to_record(row) if row else None


def record_usage(conn: sqlite3.Connection, mapping_id: str, *, success: bool) -> None:
    """Bumps usage_count and success_count/rejection_count -- pure
    counters, never changes CAT/SEL/status. See build spec 'Do not
    silently change approved mappings.'"""
    now = datetime.now(timezone.utc).isoformat()
    if success:
        conn.execute(
            "UPDATE internal_mappings SET usage_count = usage_count + 1, success_count = success_count + 1, updated_at = ? WHERE mapping_id = ?",
            (now, mapping_id),
        )
    else:
        conn.execute(
            "UPDATE internal_mappings SET usage_count = usage_count + 1, rejection_count = rejection_count + 1, updated_at = ? WHERE mapping_id = ?",
            (now, mapping_id),
        )
    conn.commit()


def set_status(conn: sqlite3.Connection, mapping_id: str, status: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("UPDATE internal_mappings SET status = ?, updated_at = ? WHERE mapping_id = ?", (status, now, mapping_id))
    conn.commit()
