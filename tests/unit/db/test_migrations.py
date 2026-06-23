"""Tests for db.migrations."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from apple_health_mcp.db import get_in_memory_connection
from apple_health_mcp.db import migrations as migrations_module
from apple_health_mcp.db.migrations import (
    CURRENT_SCHEMA_VERSION,
    apply_pending_migrations,
    get_current_version,
    set_current_version,
)
from apple_health_mcp.exceptions import DatabaseError

if TYPE_CHECKING:
    from pytest import MonkeyPatch


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
