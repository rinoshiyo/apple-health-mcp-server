"""Schema migration registry.

The v0.1.0 release ships a single canonical schema (see :mod:`schema`), so
migrations is a deliberately small surface: a version sentinel persisted in a
``schema_version`` table plus a stub :func:`apply_pending_migrations` ready
for future bumps. Future migrations register themselves in :data:`MIGRATIONS`
as ``(target_version, callable)`` pairs ordered by ascending target version.

Ordering contract: callers must invoke :func:`schema.ensure_schema` before
:func:`apply_pending_migrations` on a fresh database. The migration registry
only tracks the version sentinel; it does not create the canonical tables.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

from apple_health_mcp.exceptions import DatabaseError

if TYPE_CHECKING:
    import duckdb

_logger = logging.getLogger(__name__)

CURRENT_SCHEMA_VERSION = 2

Migration = Callable[["duckdb.DuckDBPyConnection"], None]


def _add_export_xml_sha256_column(conn: duckdb.DuckDBPyConnection) -> None:
    """Add ``imports.export_xml_sha256`` to a pre-#62 on-disk database.

    ``ADD COLUMN IF NOT EXISTS`` makes the step idempotent: a fresh DB built
    by :func:`schema.ensure_schema` already has the column (the canonical
    SQL declares it), so the migration runs after ensure_schema and is a
    no-op on first install. Existing rows backfill ``NULL``; the
    orchestrator's sha256 fast path filters for ``IS NOT NULL`` so pre-#62
    rows are skipped over and the next import stamps a real hash.

    The empty-DB guard exists because the migration registry can be invoked
    on a connection whose :func:`schema.ensure_schema` has not yet run --
    the version sentinel only needs the ``schema_version`` table, not the
    full canonical schema, so apply_pending_migrations is callable in that
    state. We skip the ALTER instead of failing in that case; the next
    ensure_schema call creates ``imports`` with the column already present.
    """
    # The schema_name filter keeps a connection with attached databases
    # or user-created schemas from passing this probe on the basis of an
    # unrelated ``imports`` table -- the unqualified ALTER below targets
    # ``main.imports`` and would otherwise raise on a fresh DB whose
    # ``ensure_schema`` has not yet run.
    row = conn.execute(
        "SELECT 1 FROM duckdb_tables() "
        "WHERE table_name = 'imports' AND schema_name = 'main' LIMIT 1"
    ).fetchone()
    if row is None:
        return
    conn.execute("ALTER TABLE imports ADD COLUMN IF NOT EXISTS export_xml_sha256 VARCHAR;")


MIGRATIONS: Sequence[tuple[int, Migration]] = ((2, _add_export_xml_sha256_column),)


def _ensure_version_table(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER NOT NULL
        );
        """
    )


def get_current_version(conn: duckdb.DuckDBPyConnection) -> int:
    """Return the persisted schema version, defaulting to 0 on a fresh DB."""
    _ensure_version_table(conn)
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    if row is None or row[0] is None:
        return 0
    return int(row[0])


def set_current_version(conn: duckdb.DuckDBPyConnection, version: int) -> None:
    """Record ``version`` as the latest applied schema migration."""
    _ensure_version_table(conn)
    conn.execute("DELETE FROM schema_version;")
    conn.execute("INSERT INTO schema_version (version) VALUES (?);", [version])


def apply_pending_migrations(conn: duckdb.DuckDBPyConnection) -> int:
    """Run every migration whose target version is above the current one.

    Returns the version the database is on after applying all pending steps.
    Already-applied migrations (``target <= applied``) are skipped so the
    function is idempotent across restarts. Raises :class:`DatabaseError` if
    the database reports a version newer than the package supports or if a
    registered migration targets a version above ``CURRENT_SCHEMA_VERSION``.

    The caller must have created the canonical schema via
    :func:`schema.ensure_schema` before invoking this function on a fresh
    database; the migration layer only tracks the version sentinel.

    Atomicity: every migration step and the final ``schema_version`` stamp
    run inside a single DuckDB transaction. Crash, SIGKILL, OOM, or any
    Python exception during the loop causes the transaction to roll back,
    so the database schema_version sentinel and the on-disk schema can
    never diverge. Today's only registered migration is an
    ``ADD COLUMN IF NOT EXISTS`` (so a partial-then-retry would converge
    anyway), but the transaction wrap is the load-bearing safety the next
    data migration -- e.g. backfilling a derived column or rewriting a
    row's contents -- depends on. Without it, a kill between the ALTER
    and the stamp would leave the DB in a "schema migrated but sentinel
    unchanged" state and the next run would replay the ALTER, which is
    fine for IF NOT EXISTS but corrupts a non-idempotent step.
    """
    current = get_current_version(conn)
    if current > CURRENT_SCHEMA_VERSION:
        raise DatabaseError(
            f"database schema_version={current} is newer than "
            f"the package supports ({CURRENT_SCHEMA_VERSION})"
        )

    conn.execute("BEGIN TRANSACTION;")
    try:
        applied = current
        for target, migration in MIGRATIONS:
            if target > CURRENT_SCHEMA_VERSION:
                raise DatabaseError(
                    f"migration target {target} exceeds "
                    f"CURRENT_SCHEMA_VERSION {CURRENT_SCHEMA_VERSION}"
                )
            if target <= applied:
                # Already applied on a previous run; idempotent skip.
                continue
            _logger.info("Applying migration to schema version %d", target)
            migration(conn)
            applied = target

        # Stamp the highest of (last migration we ran, CURRENT_SCHEMA_VERSION)
        # so that fresh databases on schema-only bumps (no data migration
        # registered) still record the package's current version.
        final = max(applied, CURRENT_SCHEMA_VERSION)
        if final != current:
            set_current_version(conn, final)
    except BaseException:
        # BaseException so KeyboardInterrupt and SystemExit also trigger
        # rollback -- the whole point of the transaction wrap is to keep
        # the on-disk schema and the sentinel in lockstep across every
        # abort, not just typed exceptions.
        conn.execute("ROLLBACK;")
        raise
    conn.execute("COMMIT;")
    return final
