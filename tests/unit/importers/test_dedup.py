"""Tests for importers.dedup.finalize_import."""

from __future__ import annotations

from apple_health_mcp.db import ensure_schema, get_in_memory_connection
from apple_health_mcp.importers.dedup import finalize_import


def test_finalize_import_runs_on_empty_schema() -> None:
    """All three sub-phases must be idempotent on an empty database."""
    conn = get_in_memory_connection()
    try:
        ensure_schema(conn)
        finalize_import(conn)
        # daily_record_stats is materialized by rebuild_daily_stats.
        row = conn.execute(
            "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = 'daily_record_stats'"
        ).fetchone()
        assert row is not None and int(row[0]) == 1
    finally:
        conn.close()


def test_finalize_import_collapses_duplicate_records() -> None:
    """Dedupe step must reduce two identical records to one row."""
    conn = get_in_memory_connection()
    try:
        ensure_schema(conn)
        for _ in range(2):
            conn.execute(
                "INSERT INTO records (record_hash, record_type, start_date, end_date, import_id)"
                " VALUES (?, ?, ?, ?, ?)",
                ["h1", "HKQuantityTypeIdentifierStepCount", "2024-01-01", "2024-01-01", "imp"],
            )
        finalize_import(conn)
        row = conn.execute("SELECT COUNT(*) FROM records").fetchone()
        assert row is not None and int(row[0]) == 1
    finally:
        conn.close()
