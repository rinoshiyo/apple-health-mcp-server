"""Tests for the schema-version sentinel helpers in ``db.migrations``.

v0.5 (issue #178) retired the migration-registry scaffolding
(``apply_pending_migrations`` + ``_add_export_xml_sha256_column`` +
``_reimport_required_message`` + ``MIGRATIONS`` list + the v=N
rejection tests). The remaining surface is the schema-version sentinel
table plus :func:`stamp_current_version`; this module tests them.
"""

from __future__ import annotations

import pytest

from apple_health_mcp.db import get_in_memory_connection
from apple_health_mcp.db.migrations import (
    CURRENT_SCHEMA_VERSION,
    _ensure_version_table,
    _table_exists_in_main,
    get_current_version,
    schema_version_is_stale,
    set_current_version,
    stamp_current_version,
)


def test_fresh_database_reports_version_zero() -> None:
    conn = get_in_memory_connection()
    try:
        assert get_current_version(conn) == 0
    finally:
        conn.close()


def test_get_current_version_creates_sentinel_table_on_first_call() -> None:
    """get_current_version must be safe on a brand-new DB (idempotent
    table creation + 0 default), even when no other code has touched
    the schema_version table yet."""
    conn = get_in_memory_connection()
    try:
        assert get_current_version(conn) == 0
        assert _table_exists_in_main(conn, "schema_version")
    finally:
        conn.close()


def test_set_current_version_round_trips() -> None:
    conn = get_in_memory_connection()
    try:
        set_current_version(conn, 3)
        assert get_current_version(conn) == 3
        # Setting again overwrites — the table only ever holds one row.
        set_current_version(conn, CURRENT_SCHEMA_VERSION)
        assert get_current_version(conn) == CURRENT_SCHEMA_VERSION
    finally:
        conn.close()


def test_stamp_current_version_writes_current() -> None:
    """The bootstrap helper that orchestrator + connection.py now call."""
    conn = get_in_memory_connection()
    try:
        stamp_current_version(conn)
        assert get_current_version(conn) == CURRENT_SCHEMA_VERSION
    finally:
        conn.close()


def test_stamp_current_version_is_idempotent_when_already_current() -> None:
    """Calling stamp_current_version twice is a no-op on the second pass."""
    conn = get_in_memory_connection()
    try:
        stamp_current_version(conn)
        stamp_current_version(conn)
        assert get_current_version(conn) == CURRENT_SCHEMA_VERSION
    finally:
        conn.close()


def test_stamp_current_version_overwrites_stale_sentinel() -> None:
    """If a pre-#178 row carries an older sentinel, the stamp updates it.

    Production code never hits this path (v0.4.1 fresh-reset clears the
    table before the stamp runs), but the helper itself must be a pure
    UPSERT-of-one so test fixtures and future migration paths can call
    it without first wiping the row.
    """
    conn = get_in_memory_connection()
    try:
        set_current_version(conn, 3)
        stamp_current_version(conn)
        assert get_current_version(conn) == CURRENT_SCHEMA_VERSION
    finally:
        conn.close()


def test_schema_version_is_stale_false_on_fresh_db() -> None:
    """A DB with no schema_version table is "fresh", not stale."""
    conn = get_in_memory_connection()
    try:
        assert schema_version_is_stale(conn) is False
    finally:
        conn.close()


def test_schema_version_is_stale_false_when_table_empty() -> None:
    """An empty schema_version table reads as fresh (version=0)."""
    conn = get_in_memory_connection()
    try:
        _ensure_version_table(conn)
        assert schema_version_is_stale(conn) is False
    finally:
        conn.close()


@pytest.mark.parametrize("stale_version", [1, 2, 3, 4, 5])
def test_schema_version_is_stale_true_for_pre_current_versions(
    stale_version: int,
) -> None:
    """Any persisted version between 1 and CURRENT-1 is stale.

    The v0.4.1 fresh-reset path (orchestrator + read-tool data-state
    envelope) reacts to a True return here by rebuilding the schema or
    surfacing ``NEEDS_REIMPORT`` to the agent.
    """
    conn = get_in_memory_connection()
    try:
        set_current_version(conn, stale_version)
        assert schema_version_is_stale(conn) is True
    finally:
        conn.close()


def test_schema_version_is_stale_false_when_at_current() -> None:
    conn = get_in_memory_connection()
    try:
        set_current_version(conn, CURRENT_SCHEMA_VERSION)
        assert schema_version_is_stale(conn) is False
    finally:
        conn.close()
