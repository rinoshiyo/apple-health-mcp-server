"""Tests for db.connection."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import duckdb
import pytest

from apple_health_mcp.db import connection as connection_module
from apple_health_mcp.db.connection import (
    default_db_path,
    get_connection,
    get_in_memory_connection,
)

if TYPE_CHECKING:
    from pytest import LogCaptureFixture, MonkeyPatch


def test_default_db_path_posix_with_xdg(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    result = default_db_path()
    assert result == tmp_path / "xdg" / "apple-health-mcp" / "health.duckdb"


def test_default_db_path_posix_without_xdg(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    result = default_db_path()
    assert result == tmp_path / ".local" / "share" / "apple-health-mcp" / "health.duckdb"


def test_default_db_path_windows_with_localappdata(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData" / "Local"))
    result = default_db_path()
    assert result == tmp_path / "AppData" / "Local" / "apple-health-mcp" / "health.duckdb"


def test_default_db_path_windows_without_localappdata(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    result = default_db_path()
    assert result == tmp_path / "AppData" / "Local" / "apple-health-mcp" / "health.duckdb"


def test_get_connection_uses_default_when_not_provided(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    conn = get_connection()
    try:
        row = conn.execute("SELECT 1").fetchone()
        assert row is not None
        assert row[0] == 1
    finally:
        conn.close()
    db_path = tmp_path / "data" / "apple-health-mcp" / "health.duckdb"
    assert db_path.exists()
    # We auto-create the default path's app subdir at 0700 because we own it;
    # a more permissive mode means the chmod tightening regressed. Skip the
    # POSIX-mode check on real Windows (Path.chmod is ACL-only there and the
    # mode bits do not reflect what we asked for).
    if os.name == "posix":
        assert (db_path.parent.stat().st_mode & 0o777) == 0o700


def test_get_connection_creates_parent_dir_without_chmod_on_user_path(tmp_path: Path) -> None:
    """User-supplied paths must NOT have their parent dir chmod-ed.

    Locking down ``$HOME`` / ``/tmp`` / a project dir to 0700 would silently
    break sshd StrictModes and any tool that expects 0755 home permissions.
    """
    db_path = tmp_path / "nested" / "dirs" / "h.duckdb"
    pre_existing_mode = (tmp_path / "nested").exists() or db_path.parent.exists()
    assert not pre_existing_mode
    conn = get_connection(db_path)
    try:
        assert db_path.parent.is_dir()
        assert db_path.exists()
        # Parent dir basename is "dirs", not "apple-health-mcp", so chmod must
        # not have fired. mkdir's default umask gives 0755 (or whatever the
        # ambient umask permits) — assert the chmod did NOT lock it down.
        assert (db_path.parent.stat().st_mode & 0o777) != 0o700
    finally:
        conn.close()


def test_get_connection_skips_chmod_on_windows(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    db_path = tmp_path / "win" / "h.duckdb"
    conn = get_connection(db_path)
    try:
        assert db_path.parent.is_dir()
    finally:
        conn.close()


def test_get_connection_read_only_opens_existing_db(tmp_path: Path) -> None:
    db_path = tmp_path / "ro.duckdb"
    seeder = get_connection(db_path)
    seeder.execute("CREATE TABLE t(x INTEGER);")
    seeder.execute("INSERT INTO t VALUES (1);")
    seeder.close()

    conn = get_connection(db_path, read_only=True)
    try:
        row = conn.execute("SELECT x FROM t").fetchone()
        assert row is not None
        assert row[0] == 1
        with pytest.raises(duckdb.Error):
            conn.execute("INSERT INTO t VALUES (2);")
    finally:
        conn.close()


def test_get_connection_read_only_materialises_empty_db_when_missing(
    tmp_path: Path,
) -> None:
    """Read-only open against a missing path bootstraps a schema-only DB.

    Before issue #38 this raised ``DatabaseError`` and ``serve`` exited, so
    the MCP client saw no tools at all and could not even surface the
    "run import first" guidance. Now we materialise an empty schema, open
    read-only against it, and let each tool return ``IMPORT_REQUIRED_MESSAGE``
    from a live MCP session.
    """
    db_path = tmp_path / "missing" / "ro.duckdb"
    conn = get_connection(db_path, read_only=True)
    try:
        # Parent dir auto-created during the bootstrap.
        assert db_path.parent.is_dir()
        assert db_path.exists()
        # ``imports`` table exists (schema was applied) but is empty.
        row = conn.execute("SELECT COUNT(*) FROM imports").fetchone()
        assert row is not None
        assert row[0] == 0
        # The handle is genuinely read-only — writes still fail. The INSERT
        # supplies a fully-valid row (matching the imports schema) so the
        # only possible cause of duckdb.Error is the read-only refusal: if
        # we passed NULL for the NOT NULL imported_at column, a constraint
        # failure could be mistaken for read-only enforcement and the test
        # would keep passing if RO was silently regressed.
        with pytest.raises(duckdb.Error):
            conn.execute(
                "INSERT INTO imports VALUES "
                "('x', '/tmp/x', TIMESTAMPTZ '2024-01-01 00:00:00+00', 0, 0, 0, NULL)"
            )
    finally:
        conn.close()


def test_materialise_empty_db_cleans_up_stale_bootstrap_tempfile(
    tmp_path: Path,
) -> None:
    """A leftover .bootstrap.<pid> from a previous crash is removed before retry.

    Without this guard, two successive bootstrap attempts from the same
    process (CLI invoked twice, second time after a crash mid-DDL on the
    first) would hit ``duckdb.Error`` opening the tmp path that already
    exists with a half-written DuckDB header.
    """
    db_path = tmp_path / "fresh" / "h.duckdb"
    tmp_marker = db_path.with_name(f"{db_path.name}.bootstrap.{os.getpid()}")
    tmp_marker.parent.mkdir(parents=True, exist_ok=True)
    tmp_marker.write_bytes(b"stale leftover from a previous crash")
    assert tmp_marker.exists()

    conn = get_connection(db_path, read_only=True)
    try:
        assert db_path.exists()
        # Stale tmp marker was removed before the bootstrap re-used the slot,
        # and the final atomic-rename consumed the new temp file too.
        assert not tmp_marker.exists()
    finally:
        conn.close()


def test_materialise_empty_db_removes_tempfile_when_bootstrap_raises(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    """A crash mid-DDL leaves no half-initialised file at the final path.

    Without the atomic-rename strategy, an aborted ensure_schema would
    leave a real DuckDB file at ``db_path`` that the next ``serve`` run
    would mistake for a complete DB and skip the bootstrap; every tool
    would then error with ``Error: Table imports does not exist``
    instead of returning ``IMPORT_REQUIRED_MESSAGE``.
    """
    from apple_health_mcp.db import schema as schema_mod

    boom = RuntimeError("simulated DDL crash")

    def _explode(_conn: object) -> None:
        raise boom

    monkeypatch.setattr(schema_mod, "ensure_schema", _explode)

    db_path = tmp_path / "fresh" / "h.duckdb"
    tmp_marker = db_path.with_name(f"{db_path.name}.bootstrap.{os.getpid()}")

    with pytest.raises(RuntimeError, match="simulated DDL crash"):
        get_connection(db_path, read_only=True)
    # Neither the final path nor the per-pid temp file remain on disk.
    assert not db_path.exists()
    assert not tmp_marker.exists()


def test_get_connection_read_only_preserves_existing_data_after_bootstrap(
    tmp_path: Path,
) -> None:
    """Bootstrap fires only when the file is missing — pre-existing rows survive."""
    from apple_health_mcp.db.schema import ensure_schema

    db_path = tmp_path / "ro.duckdb"
    seeder = get_connection(db_path)
    ensure_schema(seeder)
    seeder.execute(
        "INSERT INTO imports VALUES "
        "('imp1', '/tmp/x', TIMESTAMPTZ '2024-01-01 00:00:00+00', 1, 0, 1, NULL)"
    )
    seeder.close()

    conn = get_connection(db_path, read_only=True)
    try:
        row = conn.execute("SELECT import_id FROM imports").fetchone()
        assert row is not None
        assert row[0] == "imp1"
    finally:
        conn.close()


def _seed_legacy_v2_db(db_path: Path) -> None:
    """Build a v0.2.x-shaped DB file at ``db_path`` for the F1 migration tests.

    Builds the canonical schema, then drops ``heart_rate_samples`` and
    recreates it with the legacy VARCHAR ``sample_time`` column so the
    v=3 migration has something to convert. Stamps ``schema_version=2``
    so :func:`apply_pending_migrations` re-runs the v=3 step on the next
    open. The resulting file is what a user who imported under v0.2.x
    and then upgraded the package to v0.3.0+ would have on disk.

    Each phase runs on its own DuckDB connection + CHECKPOINT so the
    next read-only open does not inherit a stale catalog snapshot from
    the v=3 ensure_schema -> v=2 downgrade rewrite (DuckDB's MVCC
    otherwise treats the migration's ALTER as a conflict against the
    seeder's ALTER on the same logical table).
    """
    from apple_health_mcp.db.migrations import (
        set_current_version,
    )
    from apple_health_mcp.db.schema import ensure_schema

    # Phase 1: build the canonical schema (v=3 shape, including
    # imports.export_xml_sha256 and a DOUBLE sample_time) without
    # applying migrations -- the v=3 column is already DOUBLE here so
    # the migration registry would be a no-op anyway, but skipping the
    # stamp keeps schema_version at 0 ready for the downgrade.
    seeder = duckdb.connect(str(db_path), read_only=False)
    try:
        ensure_schema(seeder)
        seeder.execute("CHECKPOINT;")
    finally:
        seeder.close()

    # Phase 2: tear ``heart_rate_samples`` back down to the v0.2.x
    # shape on a fresh connection so the next probe's MVCC view sees a
    # clean post-CHECKPOINT baseline. Doing the DROP/CREATE on the
    # same connection as the migration probe is what triggered the
    # "another transaction has altered this table" failure.
    seeder = duckdb.connect(str(db_path), read_only=False)
    try:
        seeder.execute("DROP TABLE heart_rate_samples;")
        seeder.execute(
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
        seeder.execute(
            "INSERT INTO heart_rate_samples VALUES ('rh', 0, 70.0, '08:00:00.000', 'imp')"
        )
        set_current_version(seeder, 2)
        seeder.execute("CHECKPOINT;")
    finally:
        seeder.close()


def test_get_connection_read_only_migrates_legacy_v2_db_in_place(
    tmp_path: Path, caplog: LogCaptureFixture
) -> None:
    """Issue #109 (PR-F /code-review F1): a v0.2.x DB opened by ``serve``
    must have its schema migrated to the package's current version, even
    though the eventual handle is read-only.

    Without ``_migrate_if_needed`` the read-only open would land on the
    legacy VARCHAR ``sample_time`` column and ``get_heart_rate_samples``
    would wire strings instead of floats.
    """
    db_path = tmp_path / "legacy_v2.duckdb"
    _seed_legacy_v2_db(db_path)

    with caplog.at_level(logging.INFO, logger=connection_module.__name__):
        conn = get_connection(db_path, read_only=True)
    try:
        # The v=3 migration converted VARCHAR -> DOUBLE in place.
        type_row = conn.execute(
            "SELECT type FROM pragma_table_info('heart_rate_samples') WHERE name = 'sample_time'"
        ).fetchone()
        assert type_row is not None
        assert str(type_row[0]).upper() == "DOUBLE"
        # The single legacy row's ``08:00:00.000`` literal is now
        # 28800.0 seconds-of-day.
        row = conn.execute(
            "SELECT sample_time FROM heart_rate_samples ORDER BY sample_idx"
        ).fetchone()
        assert row is not None
        assert row[0] == 28800.0
    finally:
        conn.close()

    # The probe announced itself so an operator watching stderr knows the
    # migration ran on serve startup.
    infos = [
        r
        for r in caplog.records
        if r.levelno == logging.INFO and "migrating existing DB" in r.getMessage()
    ]
    assert len(infos) == 1


def test_get_connection_read_only_does_not_migrate_when_already_current(
    tmp_path: Path, caplog: LogCaptureFixture
) -> None:
    """Already-current DBs skip the migration log entirely.

    Confirms the gating ``current >= CURRENT_SCHEMA_VERSION`` branch in
    :func:`_migrate_if_needed`; without it every serve invocation would
    emit a misleading "migrating" log on a fresh DB.
    """
    from apple_health_mcp.db.migrations import apply_pending_migrations
    from apple_health_mcp.db.schema import ensure_schema

    db_path = tmp_path / "current.duckdb"
    seeder = duckdb.connect(str(db_path), read_only=False)
    try:
        ensure_schema(seeder)
        apply_pending_migrations(seeder)
    finally:
        seeder.close()

    with caplog.at_level(logging.INFO, logger=connection_module.__name__):
        conn = get_connection(db_path, read_only=True)
    try:
        # Sanity check: handle is usable and on the new schema.
        type_row = conn.execute(
            "SELECT type FROM pragma_table_info('heart_rate_samples') WHERE name = 'sample_time'"
        ).fetchone()
        assert type_row is not None
        assert str(type_row[0]).upper() == "DOUBLE"
    finally:
        conn.close()

    infos = [
        r
        for r in caplog.records
        if r.levelno == logging.INFO and "migrating existing DB" in r.getMessage()
    ]
    assert infos == []


def test_get_connection_read_only_skips_migration_when_imports_table_missing(
    tmp_path: Path,
) -> None:
    """Very-pre-v0.1.4 DBs lack the ``imports`` table; the probe must
    defer to tool-level error handling instead of crashing.

    The bare file is still openable read-only and the migration probe
    simply returns without trying to stamp a version.
    """
    db_path = tmp_path / "pre_imports.duckdb"
    seeder = duckdb.connect(str(db_path), read_only=False)
    try:
        seeder.execute("CREATE TABLE _placeholder (x INTEGER);")
    finally:
        seeder.close()

    conn = get_connection(db_path, read_only=True)
    try:
        row = conn.execute(
            "SELECT 1 FROM duckdb_tables() WHERE table_name = 'imports' LIMIT 1"
        ).fetchone()
        assert row is None
    finally:
        conn.close()


def test_get_in_memory_connection() -> None:
    conn = get_in_memory_connection()
    try:
        row = conn.execute("SELECT 1").fetchone()
        assert row is not None
        assert row[0] == 1
    finally:
        conn.close()


def test_get_in_memory_connection_applies_session_tz_from_env(
    monkeypatch: MonkeyPatch,
) -> None:
    """APPLE_HEALTH_TZ flows through to ``SET TimeZone`` on the new connection."""
    monkeypatch.setenv("APPLE_HEALTH_TZ", "Asia/Tokyo")
    conn = get_in_memory_connection()
    try:
        row = conn.execute("SELECT current_setting('TimeZone')").fetchone()
        assert row is not None
        assert row[0] == "Asia/Tokyo"
    finally:
        conn.close()


def test_get_in_memory_connection_rejects_invalid_session_tz(
    monkeypatch: MonkeyPatch,
) -> None:
    """Garbage in the env var is rejected before the SET TimeZone interpolation."""
    from apple_health_mcp.exceptions import ConfigError

    # A semicolon would be a SQL-injection vector if the connection layer
    # interpolated the env value directly; the validation regex rejects it.
    monkeypatch.setenv("APPLE_HEALTH_TZ", "Asia/Tokyo'; DROP TABLE x;--")
    with pytest.raises(ConfigError, match="invalid APPLE_HEALTH_TZ"):
        get_in_memory_connection()
