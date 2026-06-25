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

import contextlib
import logging
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

from apple_health_mcp.exceptions import DatabaseError

if TYPE_CHECKING:
    import duckdb

_logger = logging.getLogger(__name__)

CURRENT_SCHEMA_VERSION = 3

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


def _convert_heart_rate_sample_time_to_double(conn: duckdb.DuckDBPyConnection) -> None:
    """Convert ``heart_rate_samples.sample_time`` from VARCHAR to DOUBLE.

    Issue #109 (PR-F): aligns the on-disk storage with the wire contract
    that ``get_heart_rate_samples`` exposes (seconds-of-day since 00:00
    local). Pre-PR-F databases stored Apple's raw ``HH:MM:SS.SSS`` literal
    and parsed it on the way out; from PR-F forward the importer writes
    DOUBLE directly and the tool reads it verbatim.

    Idempotent:
    * If the table is missing (a connection whose ``ensure_schema`` has
      not yet run), skip entirely; the next ensure_schema call creates
      it in the new shape.
    * If the column is already DOUBLE (already-migrated DB, or a fresh
      DB whose ensure_schema landed under the PR-F schema), skip.
    * If the table is empty, skip the populate step but still perform
      the column swap so a legacy empty DB lands in the new shape.

    Malformed legacy rows (any value where ``split_part`` + ``TRY_CAST``
    cannot recover three numeric segments) become ``NULL`` and a single
    WARNING is logged with the count. The warning never lists the
    offending values because they could carry user wall-clock data; the
    count alone is enough for the operator to investigate.
    """
    # Skip when the table has not been created yet (the migration registry
    # can be invoked on a connection whose ``ensure_schema`` has not yet
    # run -- the version sentinel only needs the ``schema_version`` table,
    # not the full canonical schema).
    table_row = conn.execute(
        "SELECT 1 FROM duckdb_tables() "
        "WHERE table_name = 'heart_rate_samples' AND schema_name = 'main' LIMIT 1"
    ).fetchone()
    if table_row is None:
        return

    # Probe the current type. ``pragma_table_info`` reports the canonical
    # DuckDB type name (``VARCHAR`` or ``DOUBLE``). Already-DOUBLE rows
    # are a no-op so the migration is rerunnable across restarts even if
    # ``apply_pending_migrations`` is called twice (defensive against
    # callers that forget to gate on ``get_current_version``).
    type_row = conn.execute(
        "SELECT type FROM pragma_table_info('heart_rate_samples') WHERE name = 'sample_time'"
    ).fetchone()
    if type_row is None or str(type_row[0]).upper() == "DOUBLE":
        return

    # Add the new DOUBLE column alongside the legacy VARCHAR one, then
    # populate it from the literal. ``TRY_CAST`` keeps malformed rows
    # from aborting the migration -- they land as NULL and we log the
    # count below.
    conn.execute("ALTER TABLE heart_rate_samples ADD COLUMN sample_time_seconds DOUBLE;")
    row_count_row = conn.execute("SELECT COUNT(*) FROM heart_rate_samples").fetchone()
    row_count = int(row_count_row[0]) if row_count_row is not None else 0
    if row_count > 0:
        conn.execute(
            """
            UPDATE heart_rate_samples
            SET sample_time_seconds =
                TRY_CAST(split_part(sample_time, ':', 1) AS DOUBLE) * 3600.0
              + TRY_CAST(split_part(sample_time, ':', 2) AS DOUBLE) * 60.0
              + TRY_CAST(split_part(sample_time, ':', 3) AS DOUBLE)
            """
        )
        malformed_row = conn.execute(
            "SELECT COUNT(*) FROM heart_rate_samples "
            "WHERE sample_time_seconds IS NULL AND sample_time IS NOT NULL"
        ).fetchone()
        malformed = int(malformed_row[0]) if malformed_row is not None else 0
        if malformed > 0:
            _logger.warning(
                "heart_rate_samples migration: %d row(s) had malformed sample_time "
                "literals and were converted to NULL",
                malformed,
            )

    # Drop the legacy column and rename the new one in. DuckDB rejects
    # ``ALTER TABLE ... RENAME COLUMN`` while the target name still
    # exists, so the DROP must happen first.
    conn.execute("ALTER TABLE heart_rate_samples DROP COLUMN sample_time;")
    conn.execute("ALTER TABLE heart_rate_samples RENAME COLUMN sample_time_seconds TO sample_time;")


MIGRATIONS: Sequence[tuple[int, Migration]] = (
    (2, _add_export_xml_sha256_column),
    (3, _convert_heart_rate_sample_time_to_double),
)


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

    Atomicity: every migration step, the final ``schema_version`` stamp,
    and the COMMIT itself run inside a single DuckDB transaction. Crash,
    SIGKILL, OOM, or any Python exception during the loop -- including
    a failed COMMIT -- triggers ROLLBACK so the database schema_version
    sentinel and the on-disk schema can never diverge. The transaction
    wrap is the load-bearing safety the next non-idempotent migration
    (e.g. backfilling a derived column or rewriting a row's contents)
    will rely on; with ``ADD COLUMN IF NOT EXISTS`` -only registries the
    wrap is also harmless and keeps tests honest.

    Precondition: ``conn`` must be in autocommit mode (no caller-opened
    transaction). The inner ``BEGIN TRANSACTION`` would otherwise raise
    ``TransactionException`` -- DuckDB does not allow nested transactions.
    Today's callers (``run_import`` and the read-only bootstrap) both
    invoke this in autocommit; future callers that want broader atomicity
    should issue migrations BEFORE opening their own transaction.
    """
    current = get_current_version(conn)
    if current > CURRENT_SCHEMA_VERSION:
        raise DatabaseError(
            f"database schema_version={current} is newer than "
            f"the package supports ({CURRENT_SCHEMA_VERSION})"
        )

    try:
        conn.execute("BEGIN TRANSACTION;")
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
        conn.execute("COMMIT;")
    except BaseException:
        # BaseException so KeyboardInterrupt and SystemExit also trigger
        # rollback -- the whole point of the transaction wrap is to keep
        # the on-disk schema and the sentinel in lockstep across every
        # abort, not just typed exceptions. ROLLBACK errors are swallowed
        # so the user sees the ORIGINAL migration failure, not a follow-up
        # 'connection closed' from a teardown race.
        with contextlib.suppress(Exception):
            conn.execute("ROLLBACK;")
        raise
    return final
