"""Tests for db.migrations."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pytest

from apple_health_mcp.db import get_in_memory_connection
from apple_health_mcp.db import migrations as migrations_module
from apple_health_mcp.db.migrations import (
    CURRENT_SCHEMA_VERSION,
    _convert_heart_rate_sample_time_to_double,
    apply_pending_migrations,
    get_current_version,
    set_current_version,
)
from apple_health_mcp.exceptions import DatabaseError

if TYPE_CHECKING:
    from pytest import LogCaptureFixture, MonkeyPatch


# --- helpers for the PR-F heart_rate_samples sample_time migration -----------


def _create_legacy_heart_rate_samples_table(conn: object) -> None:
    """Create the pre-PR-F ``heart_rate_samples`` shape (VARCHAR sample_time).

    Mirrors the layout that ``ensure_schema`` shipped through v0.2.x so the
    migration has a realistic starting state to upgrade in place.
    """
    conn.execute(  # type: ignore[attr-defined]
        """
        CREATE TABLE heart_rate_samples (
            parent_record_hash  VARCHAR NOT NULL,
            sample_idx          INTEGER NOT NULL,
            bpm                 DOUBLE,
            sample_time         VARCHAR,
            import_id           VARCHAR NOT NULL
        );
        """
    )


def _sample_time_column_type(conn: object) -> str:
    row = conn.execute(  # type: ignore[attr-defined]
        "SELECT type FROM pragma_table_info('heart_rate_samples') WHERE name = 'sample_time'"
    ).fetchone()
    assert row is not None
    return str(row[0]).upper()


def test_fresh_database_reports_version_zero() -> None:
    conn = get_in_memory_connection()
    try:
        assert get_current_version(conn) == 0
    finally:
        conn.close()


def test_apply_pending_migrations_stamps_baseline_on_fresh_db() -> None:
    conn = get_in_memory_connection()
    try:
        result = apply_pending_migrations(conn)
        assert result == CURRENT_SCHEMA_VERSION
        assert get_current_version(conn) == CURRENT_SCHEMA_VERSION
    finally:
        conn.close()


def test_apply_pending_migrations_is_idempotent_when_already_current() -> None:
    conn = get_in_memory_connection()
    try:
        set_current_version(conn, CURRENT_SCHEMA_VERSION)
        result = apply_pending_migrations(conn)
        assert result == CURRENT_SCHEMA_VERSION
    finally:
        conn.close()


def test_apply_pending_migrations_runs_registered_steps(monkeypatch: MonkeyPatch) -> None:
    calls: list[int] = []

    def _step_two(conn: object) -> None:
        calls.append(2)

    monkeypatch.setattr(migrations_module, "MIGRATIONS", ((2, _step_two),))
    monkeypatch.setattr(migrations_module, "CURRENT_SCHEMA_VERSION", 2)

    conn = get_in_memory_connection()
    try:
        set_current_version(conn, 1)
        result = apply_pending_migrations(conn)
        assert result == 2
        assert calls == [2]
        assert get_current_version(conn) == 2
    finally:
        conn.close()


def test_apply_pending_migrations_skips_already_applied_on_restart(
    monkeypatch: MonkeyPatch,
) -> None:
    """Re-opening a fully-migrated DB must not re-run nor raise.

    Regression guard: an earlier draft raised DatabaseError whenever a
    registered migration's target was <= the persisted version, so every
    server restart after the first migration succeeded would crash.
    """
    calls: list[int] = []

    def _step_two(conn: object) -> None:
        calls.append(2)  # pragma: no cover - assertion below proves no call

    monkeypatch.setattr(migrations_module, "MIGRATIONS", ((2, _step_two),))
    monkeypatch.setattr(migrations_module, "CURRENT_SCHEMA_VERSION", 2)

    conn = get_in_memory_connection()
    try:
        set_current_version(conn, 2)
        result = apply_pending_migrations(conn)
        assert result == 2
        assert calls == []
        assert get_current_version(conn) == 2
    finally:
        conn.close()


def test_apply_pending_migrations_stamps_max_when_baseline_above_last_target(
    monkeypatch: MonkeyPatch,
) -> None:
    """When the highest registered migration is below CURRENT_SCHEMA_VERSION
    (schema-only bumps with no data migration), the version sentinel still
    advances to CURRENT_SCHEMA_VERSION so future restarts don't replay the
    earlier migrations against an already-current schema."""
    calls: list[int] = []

    def _step_one(conn: object) -> None:
        calls.append(1)

    monkeypatch.setattr(migrations_module, "MIGRATIONS", ((1, _step_one),))
    monkeypatch.setattr(migrations_module, "CURRENT_SCHEMA_VERSION", 2)

    conn = get_in_memory_connection()
    try:
        result = apply_pending_migrations(conn)
        assert result == 2
        assert calls == [1]
        assert get_current_version(conn) == 2
    finally:
        conn.close()


def test_database_newer_than_supported_raises() -> None:
    conn = get_in_memory_connection()
    try:
        set_current_version(conn, CURRENT_SCHEMA_VERSION + 1)
        with pytest.raises(DatabaseError, match="newer than"):
            apply_pending_migrations(conn)
    finally:
        conn.close()


def test_migration_target_exceeds_current_supported_raises(monkeypatch: MonkeyPatch) -> None:
    def _bogus(conn: object) -> None:
        pass  # pragma: no cover - never invoked

    monkeypatch.setattr(migrations_module, "MIGRATIONS", ((5, _bogus),))
    monkeypatch.setattr(migrations_module, "CURRENT_SCHEMA_VERSION", 1)

    conn = get_in_memory_connection()
    try:
        set_current_version(conn, 0)
        with pytest.raises(DatabaseError, match="exceeds CURRENT_SCHEMA_VERSION"):
            apply_pending_migrations(conn)
    finally:
        conn.close()


def test_apply_pending_migrations_rolls_back_on_migration_failure(
    monkeypatch: MonkeyPatch,
) -> None:
    """A migration that raises mid-loop leaves the schema_version unchanged.

    Regression guard for the transaction wrap (issue #62 gachima follow-up):
    without ``BEGIN TRANSACTION ... ROLLBACK`` around the loop, a kill
    after the ALTER but before ``set_current_version`` would leave the
    on-disk schema migrated but the sentinel pointing at the old
    version. The next run would replay the same step -- harmless under
    ``ADD COLUMN IF NOT EXISTS`` but data-corrupting under any non-
    idempotent step (e.g. a backfill that reads-then-writes a column).
    """

    def _step_one(conn: object) -> None:
        # Make a real schema change BEFORE raising so we can verify the
        # rollback also undoes the ALTER, not just the version stamp.
        conn.execute("CREATE TABLE _migration_smoke (x INTEGER)")  # type: ignore[attr-defined]
        raise RuntimeError("simulated migration crash")

    monkeypatch.setattr(migrations_module, "MIGRATIONS", ((2, _step_one),))
    monkeypatch.setattr(migrations_module, "CURRENT_SCHEMA_VERSION", 2)

    conn = get_in_memory_connection()
    try:
        set_current_version(conn, 1)
        with pytest.raises(RuntimeError, match="simulated migration crash"):
            apply_pending_migrations(conn)
        # Sentinel did NOT advance.
        assert get_current_version(conn) == 1
        # The mid-migration ALTER was rolled back too.
        row = conn.execute(
            "SELECT 1 FROM duckdb_tables() WHERE table_name = '_migration_smoke' LIMIT 1"
        ).fetchone()
        assert row is None

        # The connection must remain usable after rollback -- a future
        # refactor that leaves the connection in an aborted-transaction
        # state would silently break the next call. Swap MIGRATIONS to an
        # empty tuple and re-invoke; if the connection is poisoned the
        # inner BEGIN raises.
        monkeypatch.setattr(migrations_module, "MIGRATIONS", ())
        assert apply_pending_migrations(conn) == 2
    finally:
        conn.close()


# --- PR-F: heart_rate_samples.sample_time VARCHAR -> DOUBLE ------------------


def test_heart_rate_sample_time_migration_idempotent_on_missing_table() -> None:
    """Skip cleanly when ``heart_rate_samples`` does not exist yet.

    The migration registry can be invoked on a connection whose
    ``ensure_schema`` has not yet run; the step must be a no-op there.
    """
    conn = get_in_memory_connection()
    try:
        _convert_heart_rate_sample_time_to_double(conn)
        # No exception, and the table still does not exist.
        row = conn.execute(
            "SELECT 1 FROM duckdb_tables() WHERE table_name = 'heart_rate_samples' LIMIT 1"
        ).fetchone()
        assert row is None
    finally:
        conn.close()


def test_heart_rate_sample_time_migration_handles_empty_table() -> None:
    """Empty legacy table still gets the column swapped to DOUBLE."""
    conn = get_in_memory_connection()
    try:
        _create_legacy_heart_rate_samples_table(conn)
        assert _sample_time_column_type(conn) == "VARCHAR"
        _convert_heart_rate_sample_time_to_double(conn)
        assert _sample_time_column_type(conn) == "DOUBLE"
        # No surprise leftover columns from the rename dance.
        cols = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM pragma_table_info('heart_rate_samples')"
            ).fetchall()
        }
        assert "sample_time" in cols
        assert "sample_time_seconds" not in cols
    finally:
        conn.close()


def test_heart_rate_sample_time_migration_converts_valid_varchar_rows() -> None:
    """Legacy ``HH:MM:SS.SSS`` rows convert to seconds-of-day DOUBLE."""
    conn = get_in_memory_connection()
    try:
        _create_legacy_heart_rate_samples_table(conn)
        conn.execute(
            "INSERT INTO heart_rate_samples VALUES "
            "('rh', 0, 70.0, '00:00:00.000', 'imp'),"
            "('rh', 1, 72.0, '01:30:45.500', 'imp'),"
            "('rh', 2, 75.0, '23:59:59.999', 'imp')"
        )
        _convert_heart_rate_sample_time_to_double(conn)
        assert _sample_time_column_type(conn) == "DOUBLE"
        times = [
            row[0]
            for row in conn.execute(
                "SELECT sample_time FROM heart_rate_samples ORDER BY sample_idx"
            ).fetchall()
        ]
        assert times[0] == 0.0
        assert times[1] == 5445.5
        # Allow a tiny FP tolerance on the boundary value but the value is
        # representable exactly enough that == still holds in practice.
        assert times[2] == pytest.approx(86399.999, rel=0, abs=1e-9)
    finally:
        conn.close()


def test_heart_rate_sample_time_migration_handles_malformed_rows_with_warning(
    caplog: LogCaptureFixture,
) -> None:
    """Malformed legacy values become NULL and emit exactly one WARNING."""
    conn = get_in_memory_connection()
    try:
        _create_legacy_heart_rate_samples_table(conn)
        conn.execute(
            "INSERT INTO heart_rate_samples VALUES "
            "('rh', 0, 70.0, '08:00:00.000', 'imp'),"
            "('rh', 1, 72.0, 'not-a-time', 'imp')"
        )
        with caplog.at_level(logging.WARNING, logger=migrations_module.__name__):
            _convert_heart_rate_sample_time_to_double(conn)
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "1" in warnings[0].getMessage()
        assert "malformed" in warnings[0].getMessage()
        times = [
            row[0]
            for row in conn.execute(
                "SELECT sample_time FROM heart_rate_samples ORDER BY sample_idx"
            ).fetchall()
        ]
        assert times == [28800.0, None]
    finally:
        conn.close()


def test_heart_rate_sample_time_migration_parity_with_importer_on_fractional() -> None:
    """Issue #109 (PR-F /code-review F2): importer and migration produce
    identical seconds-of-day for a fractional ``HH:MM:SS`` input.

    Bridges the two code paths from the same VARCHAR literal:

    * Migration: ``TRY_CAST(split_part(...) AS DOUBLE)`` arithmetic in SQL.
    * Importer: ``float(parts[0])`` arithmetic in Python.

    A divergence here would break the CHANGELOG's "matching fallback"
    claim.
    """
    from apple_health_mcp.importers.xml import _parse_sample_time

    raw = "1.5:30:00.500"
    importer_value = _parse_sample_time(raw)
    conn = get_in_memory_connection()
    try:
        _create_legacy_heart_rate_samples_table(conn)
        conn.execute(
            "INSERT INTO heart_rate_samples VALUES ('rh', 0, 70.0, ?, 'imp')",
            [raw],
        )
        _convert_heart_rate_sample_time_to_double(conn)
        migration_row = conn.execute(
            "SELECT sample_time FROM heart_rate_samples ORDER BY sample_idx"
        ).fetchone()
        assert migration_row is not None
        migration_value = migration_row[0]
    finally:
        conn.close()
    assert importer_value is not None
    assert migration_value is not None
    assert importer_value == pytest.approx(migration_value)


def test_heart_rate_sample_time_migration_logs_progress_on_non_empty_table(
    caplog: LogCaptureFixture,
) -> None:
    """Issue #109 (PR-F /code-review F4): emit one INFO log on non-empty
    tables so a 10M-row migration does not look like a silent hang.
    """
    conn = get_in_memory_connection()
    try:
        _create_legacy_heart_rate_samples_table(conn)
        conn.execute("INSERT INTO heart_rate_samples VALUES ('rh', 0, 70.0, '08:00:00.000', 'imp')")
        with caplog.at_level(logging.INFO, logger=migrations_module.__name__):
            _convert_heart_rate_sample_time_to_double(conn)
        infos = [
            r
            for r in caplog.records
            if r.levelno == logging.INFO and "heart_rate_samples migration" in r.getMessage()
        ]
        assert len(infos) == 1
        assert "1" in infos[0].getMessage()
    finally:
        conn.close()


def test_heart_rate_sample_time_migration_silent_on_empty_table(
    caplog: LogCaptureFixture,
) -> None:
    """No progress log on empty tables -- the converting-N-rows INFO is
    gated on ``row_count > 0`` so a fresh-DB migration stays silent.
    """
    conn = get_in_memory_connection()
    try:
        _create_legacy_heart_rate_samples_table(conn)
        with caplog.at_level(logging.INFO, logger=migrations_module.__name__):
            _convert_heart_rate_sample_time_to_double(conn)
        infos = [
            r
            for r in caplog.records
            if r.levelno == logging.INFO and "heart_rate_samples migration" in r.getMessage()
        ]
        assert infos == []
    finally:
        conn.close()


def test_heart_rate_sample_time_migration_recovers_from_residue_column() -> None:
    """Issue #109 (PR-F /code-review F6): a half-completed prior run that
    left ``sample_time_seconds`` behind does not crash direct invocation.

    The production path is transaction-wrapped, but unit tests and any
    future direct caller exercise the function outside the transaction
    -- the defensive ``DROP COLUMN IF EXISTS`` keeps that surface
    idempotent.
    """
    conn = get_in_memory_connection()
    try:
        _create_legacy_heart_rate_samples_table(conn)
        conn.execute("INSERT INTO heart_rate_samples VALUES ('rh', 0, 70.0, '08:00:00.000', 'imp')")
        # Simulate a previous run that crashed after ADD COLUMN but
        # before DROP / RENAME.
        conn.execute("ALTER TABLE heart_rate_samples ADD COLUMN sample_time_seconds DOUBLE;")
        _convert_heart_rate_sample_time_to_double(conn)
        assert _sample_time_column_type(conn) == "DOUBLE"
        row = conn.execute("SELECT sample_time FROM heart_rate_samples").fetchone()
        assert row is not None
        assert row[0] == 28800.0
    finally:
        conn.close()


def test_heart_rate_sample_time_migration_is_rerunnable() -> None:
    """Running the migration twice is a no-op the second time around."""
    conn = get_in_memory_connection()
    try:
        _create_legacy_heart_rate_samples_table(conn)
        conn.execute("INSERT INTO heart_rate_samples VALUES ('rh', 0, 70.0, '08:00:00.000', 'imp')")
        _convert_heart_rate_sample_time_to_double(conn)
        assert _sample_time_column_type(conn) == "DOUBLE"
        before = conn.execute(
            "SELECT sample_time FROM heart_rate_samples ORDER BY sample_idx"
        ).fetchall()
        # Second call must not raise and must leave the column DOUBLE with
        # identical content.
        _convert_heart_rate_sample_time_to_double(conn)
        assert _sample_time_column_type(conn) == "DOUBLE"
        after = conn.execute(
            "SELECT sample_time FROM heart_rate_samples ORDER BY sample_idx"
        ).fetchall()
        assert before == after
    finally:
        conn.close()
