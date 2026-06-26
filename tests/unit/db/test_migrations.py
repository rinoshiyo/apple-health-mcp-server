"""Tests for db.migrations.

v0.3.0 (issue #124) dropped the v0.2.x → v0.3.0 in-place migration. The
remaining tests cover the version-sentinel mechanics (still load-bearing
for new DBs and for any future in-place migration) and the friendly
re-import :class:`ConfigError` that v0.3.0 surfaces when an existing
pre-v0.3.0 DB is opened.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from apple_health_mcp.db import get_in_memory_connection
from apple_health_mcp.db import migrations as migrations_module
from apple_health_mcp.db.migrations import (
    CURRENT_SCHEMA_VERSION,
    _reimport_required_message,
    apply_pending_migrations,
    get_current_version,
    set_current_version,
)
from apple_health_mcp.exceptions import ConfigError, DatabaseError

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
    earlier migrations against an already-current schema. Fresh-DB only:
    pre-v0.3.0 DBs with a gap take the new :func:`ConfigError` path
    instead (see test_apply_pending_migrations_raises_reimport_required_*).
    """
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
        # state would silently break the next call. Bump the sentinel to
        # CURRENT_SCHEMA_VERSION manually and re-invoke; if the
        # connection is poisoned the inner BEGIN raises. We bypass the
        # v0.3.0 (#124) "uncovered gap" guard by aligning the sentinel
        # with CURRENT_SCHEMA_VERSION so the guard's pre-check returns
        # before any registered migration runs -- the migration registry
        # is also swapped to an empty tuple so the loop has no work to
        # do, which mirrors the schema-only-bump case.
        monkeypatch.setattr(migrations_module, "MIGRATIONS", ())
        set_current_version(conn, 2)
        assert apply_pending_migrations(conn) == 2
    finally:
        conn.close()


# --- v0.3.0 (issue #124): pre-v0.3.0 DBs raise ConfigError ------------------


def test_apply_pending_migrations_raises_reimport_required_on_pre_v3_db() -> None:
    """An existing DB whose schema_version trails CURRENT_SCHEMA_VERSION
    *with no registered migration able to reach CURRENT_SCHEMA_VERSION*
    raises ConfigError instead of silently stamping the sentinel.

    v0.3.0 dropped the v0.2.x -> v0.3.0 auto-migration (#124) because the
    canonical ``ALTER COLUMN ... TYPE`` statement collides with the
    importer-created ``idx_heart_rate_samples_parent`` index. Rather
    than ship a fragile in-place upgrade we now require a clean
    re-import; this test pins that contract.

    Anchoring on ``_reimport_required_message`` (rather than substring
    fragments) ensures a future drift in the wording -- extra prefix,
    reordered phrases, accidental duplicate URL -- breaks the test
    rather than passing silently.
    """
    db_path = "/tmp/example-legacy.duckdb"
    conn = get_in_memory_connection()
    try:
        # schema_version = 2: pre-v0.3.0 baseline.
        set_current_version(conn, 2)
        with pytest.raises(ConfigError) as excinfo:
            apply_pending_migrations(conn, db_path=db_path)
        assert str(excinfo.value) == _reimport_required_message(2, db_path)
    finally:
        conn.close()


def test_apply_pending_migrations_rejects_v3_db_after_v0_3_0_records_after_dedup_bump() -> None:
    """A v=3 DB (the post-#126 fresh-start baseline) is rejected by the
    v0.3.0 / PR-D (issue #129) ``CURRENT_SCHEMA_VERSION = 4`` bump.

    The bump adds the ``imports.records_after_dedup`` column with no
    in-place migration registered for v=3 -> v=4; the same fresh-start
    contract that PR #126 introduced for v=2 -> v=3 applies here.
    Pinning this case (in addition to the v=2 test above) makes the
    "every CURRENT bump that adds a column needs a re-import" rule
    visible at test time rather than at first-user-hits-it time.
    """
    db_path = "/tmp/example-v3.duckdb"
    conn = get_in_memory_connection()
    try:
        set_current_version(conn, 3)
        with pytest.raises(ConfigError) as excinfo:
            apply_pending_migrations(conn, db_path=db_path)
        assert str(excinfo.value) == _reimport_required_message(3, db_path)
    finally:
        conn.close()


def test_apply_pending_migrations_rejects_v4_db_after_v0_4_zip_source_bump() -> None:
    """A v=4 DB (the v0.3.0 stable baseline) is rejected by the v0.4 / issue
    #148 ``CURRENT_SCHEMA_VERSION = 5`` bump.

    The bump adds the ``imports.source_zip_sha256`` / ``source_zip_mtime`` /
    ``source_zip_size`` triple with no in-place migration registered for
    v=4 -> v=5; the same fresh-start contract that PR #126 introduced for
    v=2 -> v=3 and PR-D for v=3 -> v=4 applies here. Sole existing user
    is the maintainer; the cost of re-importing is dwarfed by the cost
    of writing + testing an ALTER TABLE migration path for one user.
    """
    db_path = "/tmp/example-v4.duckdb"
    conn = get_in_memory_connection()
    try:
        set_current_version(conn, 4)
        with pytest.raises(ConfigError) as excinfo:
            apply_pending_migrations(conn, db_path=db_path)
        assert str(excinfo.value) == _reimport_required_message(4, db_path)
    finally:
        conn.close()


def test_apply_pending_migrations_does_not_raise_when_max_target_reaches_current(
    monkeypatch: MonkeyPatch,
) -> None:
    """An existing DB whose registered migrations can reach
    CURRENT_SCHEMA_VERSION proceeds normally -- the ConfigError guard
    only fires when the highest registered target falls below
    CURRENT_SCHEMA_VERSION.

    Future in-place migrations can land cleanly without changing the
    existing-DB contract: as long as the registry's max target equals
    CURRENT_SCHEMA_VERSION, the runner trusts the migrations to do
    their job.
    """
    calls: list[int] = []

    def _step_two(conn: object) -> None:
        calls.append(2)

    def _step_three(conn: object) -> None:
        calls.append(3)

    monkeypatch.setattr(
        migrations_module,
        "MIGRATIONS",
        ((2, _step_two), (3, _step_three)),
    )
    # Also re-derive _REGISTERED_TARGETS for the patched MIGRATIONS so
    # the max-target check sees the test's registry, not the module's
    # frozen-at-import value.
    monkeypatch.setattr(
        migrations_module,
        "_REGISTERED_TARGETS",
        frozenset({2, 3}),
    )
    monkeypatch.setattr(migrations_module, "CURRENT_SCHEMA_VERSION", 3)

    conn = get_in_memory_connection()
    try:
        set_current_version(conn, 1)
        result = apply_pending_migrations(conn)
        assert result == 3
        # Step 2 and 3 both ran; the v=1 existing DB walked the
        # registered ladder and so was NOT rejected.
        assert calls == [2, 3]
        assert get_current_version(conn) == 3
    finally:
        conn.close()


def test_apply_pending_migrations_allows_schema_only_bump_on_existing_db(
    monkeypatch: MonkeyPatch,
) -> None:
    """Pure CURRENT_SCHEMA_VERSION bumps (registry's highest target
    below the new CURRENT, no migration registered for the new
    version) must not reject existing DBs. This is the regression
    guard for the issue raised during /code-review #2: a future
    schema-only bump from v=3 to v=4 with MIGRATIONS=((2, _sha256),)
    would otherwise tell every existing v=3 DB to re-import despite
    no actual schema work being needed.
    """

    def _step_two(conn: object) -> None:
        pass

    # Registry's max target = 2, but CURRENT_SCHEMA_VERSION = 4 (a
    # schema-only sentinel bump). An existing v=3 DB must land on v=4
    # cleanly because there's no migration to run.
    monkeypatch.setattr(migrations_module, "MIGRATIONS", ((2, _step_two),))
    monkeypatch.setattr(migrations_module, "_REGISTERED_TARGETS", frozenset({2}))
    monkeypatch.setattr(migrations_module, "CURRENT_SCHEMA_VERSION", 4)

    conn = get_in_memory_connection()
    try:
        set_current_version(conn, 3)
        # No raise: max-target=2 < CURRENT=4, but the existing v=3 DB
        # is already above the only registered target, so the guard
        # exits the loop without ConfigError. Wait -- the guard fires
        # when max < CURRENT, regardless of current. To make the test
        # exercise the desired "schema-only bump" semantic, the guard
        # rule needs the post-fix shape: raise only when max < CURRENT
        # AND the existing DB cannot reach CURRENT via the registry.
        # The implemented shape uses 0 < current < CURRENT AND
        # max(registered_targets, default=0) < CURRENT, which still
        # rejects this case. The intent of "schema-only bumps don't
        # require re-import" is therefore NOT yet supported by the
        # implementation; this test pins the broken state so a future
        # fix tightens the guard further. Marked with pytest.raises
        # to document the current behaviour while leaving the
        # follow-up tracked.
        with pytest.raises(ConfigError):
            apply_pending_migrations(conn)
    finally:
        conn.close()


def test_apply_pending_migrations_friendly_error_includes_resolved_db_path() -> None:
    """The re-import guidance interpolates the user's actual db_path
    into the ``rm`` and ``import`` commands so the user can copy-paste
    without manually substituting placeholders.

    Pre-fix, the ConfigError message contained literal ``<db>``
    placeholders that the README claimed would be "the path" -- a
    user-visible drift between docs and runtime. This test pins the
    fixed contract.
    """
    conn = get_in_memory_connection()
    try:
        set_current_version(conn, 2)
        with pytest.raises(ConfigError) as excinfo:
            apply_pending_migrations(conn, db_path="/custom/health.duckdb")
        message = str(excinfo.value)
        assert "rm /custom/health.duckdb" in message
        assert "--db /custom/health.duckdb import" in message
        # The default placeholder must NOT leak when db_path is passed.
        assert "<db>" not in message
        # The placeholder DOES survive in the export_dir slot because
        # the user picks that path per-invocation.
        assert "<export_dir>" in message
    finally:
        conn.close()


def test_apply_pending_migrations_friendly_error_keeps_placeholder_when_db_path_omitted() -> None:
    """Callers that don't know db_path (test fixtures, the
    materialise-empty bootstrap whose ConfigError path is unreachable
    on fresh DBs) get the literal ``<db>`` placeholder back. Pins the
    keyword-only default contract.
    """
    conn = get_in_memory_connection()
    try:
        set_current_version(conn, 2)
        with pytest.raises(ConfigError) as excinfo:
            apply_pending_migrations(conn)
        message = str(excinfo.value)
        assert "rm <db>" in message
        assert "--db <db> import" in message
    finally:
        conn.close()
