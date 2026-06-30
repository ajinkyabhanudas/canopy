"""
Schema drift integration test — skipped when PG_* credentials are absent.

Verifies that every table and a representative set of columns documented in
schema.py actually exist in the live database. This is the enforcement gate
for DECISIONS.md § D1 (Static schema representation — ❌ Gap).

Run automatically when PG_* env vars are set (developer machine, staging CI).
Silently skipped in unit-only runs (no credentials present).
"""

from __future__ import annotations

import os

import pytest

_db_configured = all(
    os.environ.get(v)
    for v in ("PG_HOST", "PG_PORT", "PG_DBNAME", "PG_USER", "PG_PASSWORD")
)

# Every table named in SCHEMA_CONTEXT must exist.
_EXPECTED_TABLES = frozenset({
    "species",
    "sites",
    "users",
    "ingestion_logs",
    "assignment_packages",
    "detections",
})

# (table, column) pairs that schema.py specifically calls out as semantically
# significant to the query layer or as sensitive. A missing entry here means
# the model will generate SQL referencing a column that does not exist.
_EXPECTED_COLUMNS = frozenset({
    ("species", "id"),
    ("species", "scientific_name"),
    ("sites", "id"),
    ("sites", "name"),
    ("detections", "id"),
    ("detections", "species_id"),
    ("detections", "site_id"),
    ("detections", "validation_status"),
    ("detections", "recorded_at"),
    ("detections", "confidence"),
    ("detections", "management_unit"),
    ("detections", "landscape"),
    ("detections", "model_id"),
    # Sensitive columns stripped by _SENSITIVE_COLUMNS in loop.py —
    # must exist in DB for the strip to have any effect.
    ("detections", "latitude"),
    ("detections", "longitude"),
})

# Validation status values documented in SCHEMA_CONTEXT and enforced by the
# S4 default-filter guardrail. New values require schema.py to be updated.
_DOCUMENTED_STATUSES = frozenset({"approved", "pending"})


@pytest.mark.skipif(not _db_configured, reason="PG_* variables not set")
def test_documented_tables_exist():
    """Every table named in schema.py must exist in the live DB."""
    from canopy.db.connection import get_connection

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
            )
            live_tables = frozenset(row[0] for row in cur.fetchall())
    finally:
        conn.close()

    missing = _EXPECTED_TABLES - live_tables
    assert not missing, (
        f"Tables in schema.py missing from live DB: {sorted(missing)}\n"
        "Update SCHEMA_CONTEXT in schema.py to match the current database."
    )


@pytest.mark.skipif(not _db_configured, reason="PG_* variables not set")
def test_documented_columns_exist():
    """Key columns named in schema.py must exist in the live DB."""
    from canopy.db.connection import get_connection

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema = 'public'"
            )
            live_columns = frozenset((row[0], row[1]) for row in cur.fetchall())
    finally:
        conn.close()

    missing = _EXPECTED_COLUMNS - live_columns
    assert not missing, (
        "Columns documented in schema.py or _SENSITIVE_COLUMNS missing from live DB:\n"
        + "\n".join(f"  {t}.{c}" for t, c in sorted(missing))
        + "\nUpdate schema.py or loop._SENSITIVE_COLUMNS to match the current database."
    )


@pytest.mark.skipif(not _db_configured, reason="PG_* variables not set")
def test_validation_status_values_match_schema():
    """Actual validation_status values must match what schema.py documents.

    New statuses (e.g. 'rejected') require updating SCHEMA_CONTEXT and the
    S4 default-filter guardrail — the model will ignore undocumented values.
    """
    from canopy.db.connection import get_connection

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT validation_status FROM detections ORDER BY 1"
            )
            live_statuses = frozenset(row[0] for row in cur.fetchall())
    finally:
        conn.close()

    undocumented = live_statuses - _DOCUMENTED_STATUSES
    assert not undocumented, (
        f"validation_status values in DB not documented in schema.py: {sorted(undocumented)}\n"
        "Update SCHEMA_CONTEXT and the approval guardrail in schema.py."
    )
