"""Schema migration registry.

v0.3.0 ships a single canonical schema (see :mod:`schema`) and intentionally
no longer carries an in-place upgrade path from pre-v0.3.0 databases. The
v0.2.x → v0.3.0 heart-rate-samples migration that PR #117 introduced was
removed because the ``ALTER TABLE ... ALTER COLUMN ... TYPE`` statement
fails with ``DependencyException`` whenever the table carries any
dependent index — which every real importer build creates
(:issue:`124`). Rather than ship a fragile migration into v1.0.0 we
require users on pre-v0.3.0 DBs to re-import; the importer is fast
(:issue:`50` / :pr:`57` / :pr:`60`) and the data is local, so the
operator-side cost is a few minutes.

The migration registry therefore tracks only the version sentinel plus
the historical ``imports.export_xml_sha256`` column-add (which is an
``ADD COLUMN IF NOT EXISTS`` so it remains safe and idempotent on every
DB). Fresh DBs built by :func:`schema.ensure_schema` already carry the
canonical shape; their first ``apply_pending_migrations`` call is a
schema_version stamping operation only.

Ordering contract: callers must invoke :func:`schema.ensure_schema`
before :func:`apply_pending_migrations` on a fresh database. The
migration registry only tracks the version sentinel; it does not create
the canonical tables.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

from apple_health_mcp.exceptions import ConfigError, DatabaseError

if TYPE_CHECKING:
    import duckdb

_logger = logging.getLogger(__name__)

CURRENT_SCHEMA_VERSION = 3

Migration = Callable[["duckdb.DuckDBPyConnection"], None]


def _table_exists_in_main(conn: duckdb.DuckDBPyConnection, name: str) -> bool:
    """Return True when ``name`` exists as a table in the ``main`` schema.

    Shared probe for migration steps that need to skip when invoked on a
    connection whose :func:`schema.ensure_schema` has not yet run -- the
    version sentinel only needs the ``schema_version`` table, not the
    full canonical schema, so :func:`apply_pending_migrations` is
    callable in that state and individual steps must defend themselves.
    The ``schema_name = 'main'`` filter prevents a connection with
    attached databases or user-created schemas from passing the probe
    on the basis of an unrelated same-named table.
    """
    row = conn.execute(
        "SELECT 1 FROM duckdb_tables() WHERE table_name = ? AND schema_name = 'main' LIMIT 1",
        [name],
    ).fetchone()
    return row is not None


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
    # The shared helper's schema_name filter keeps a connection with
    # attached databases or user-created schemas from passing this
    # probe on the basis of an unrelated ``imports`` table -- the
    # unqualified ALTER below targets ``main.imports`` and would
    # otherwise raise on a fresh DB whose ``ensure_schema`` has not
    # yet run.
    if not _table_exists_in_main(conn, "imports"):
        return
    conn.execute("ALTER TABLE imports ADD COLUMN IF NOT EXISTS export_xml_sha256 VARCHAR;")


MIGRATIONS: Sequence[tuple[int, Migration]] = ((2, _add_export_xml_sha256_column),)


# Sentinel message body for the friendly "re-import required" error.
# Exposed at module level so tests can assert equality against
# ``_reimport_required_message(...)`` rather than piecewise substrings.
# The leading ``rm <db>`` placement is load-bearing: when the user is
# already running ``import`` (the v0.2.x DB rejected case fires inside
# the importer path too), the destructive step needs to be the first
# thing they see -- otherwise they read the trailing ``import`` and
# assume the error is just a transient hiccup. See the README's
# "Upgrading from < v0.3.0" section for the full recovery flow.
_REIMPORT_REQUIRED_TEMPLATE = (
    "DB schema_version={current} is below the package's "
    "CURRENT_SCHEMA_VERSION={target}. v0.3.0 dropped the v0.2.x->v0.3.0 "
    "auto-migration (see issue #124). Recovery requires a clean "
    "re-import; the data is local, the importer is fast.\n"
    "    rm {db_path}\n"
    "    apple-health-mcp-server --db {db_path} import <export_dir>\n"
    "See README 'Upgrading from < v0.3.0' for context."
)


# Frozenset of registered migration targets, computed once at module
# import. Pre-#124 this was re-derived from MIGRATIONS on every
# apply_pending_migrations call; hoisting it makes the "static
# metadata" intent explicit and removes the per-call allocation.
_REGISTERED_TARGETS: frozenset[int] = frozenset(target for target, _ in MIGRATIONS)


def _reimport_required_message(current: int, db_path: object) -> str:
    """Build the canonical re-import guidance for a pre-v0.3.0 DB.

    The ``target`` value is read from :data:`CURRENT_SCHEMA_VERSION` at
    call time rather than captured as a default argument, so a test
    that monkeypatches ``CURRENT_SCHEMA_VERSION`` sees the patched
    value in the message.

    ``db_path`` is interpolated verbatim (str-coerced) so the user can
    copy-paste the ``rm`` / ``import`` commands without manually
    substituting ``<db>`` placeholders.
    """
    return _REIMPORT_REQUIRED_TEMPLATE.format(
        current=current,
        target=CURRENT_SCHEMA_VERSION,
        db_path=db_path,
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


def apply_pending_migrations(
    conn: duckdb.DuckDBPyConnection,
    *,
    db_path: object = "<db>",
) -> int:
    """Run every migration whose target version is above the current one.

    Returns the version the database is on after applying all pending steps.
    Already-applied migrations (``target <= applied``) are skipped so the
    function is idempotent across restarts. Raises :class:`DatabaseError` if
    the database reports a version newer than the package supports.

    **v0.3.0 behaviour change (issue #124):** existing databases whose
    persisted ``schema_version`` trails :data:`CURRENT_SCHEMA_VERSION`
    *and whose highest registered migration target cannot reach
    CURRENT_SCHEMA_VERSION* now raise :class:`ConfigError` carrying the
    re-import guidance. This replaces the pre-v0.3.0 implicit
    ``ALTER TABLE`` upgrade path (which was fragile in the presence of
    secondary indexes). Fresh databases (``current == 0``) are
    unaffected: they walk the registered migrations and land on
    :data:`CURRENT_SCHEMA_VERSION` cleanly. Pure schema-only
    ``CURRENT_SCHEMA_VERSION`` bumps against existing DBs (e.g. a future
    v=4 stamp with no v=4 migration) are also unaffected: the
    max-target check only fires when the registry cannot bring the DB
    forward at all.

    ``db_path`` is folded into the ConfigError message so the user
    gets a copy-pasteable recovery command. Callers that don't know
    the path (test fixtures, the materialise-empty bootstrap whose
    error path is unreachable on fresh DBs) may omit it and the
    message will contain ``<db>`` placeholders.

    The caller must have created the canonical schema via
    :func:`schema.ensure_schema` before invoking this function on a fresh
    database; the migration layer only tracks the version sentinel.

    Atomicity: registered migration steps, the final ``schema_version``
    stamp, and the COMMIT itself run inside a single DuckDB transaction.
    Crash, SIGKILL, OOM, or any Python exception during the loop --
    including a failed COMMIT -- triggers ROLLBACK so the database
    schema_version sentinel and the on-disk schema can never diverge.
    The :class:`ConfigError` raised by the v0.3.0 (#124) re-import
    guard fires BEFORE the BEGIN TRANSACTION (no rollback is needed
    because nothing has been written), so callers must close their
    own connection in a ``try/finally`` if they want the exception
    propagated rather than the connection leaked.

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

    # v0.3.0 (issue #124): refuse to silently bump the sentinel on an
    # existing DB whose persisted version trails the package AND whose
    # registered migrations cannot reach CURRENT_SCHEMA_VERSION. The
    # max-target check (rather than a per-version gap walk) keeps
    # future schema-only CURRENT_SCHEMA_VERSION bumps (no migration
    # registered for the new version) from rejecting existing DBs --
    # those land on the ``max(applied, CURRENT_SCHEMA_VERSION)``
    # stamping path below. The ``current > 0`` guard exempts fresh
    # bootstrap DBs (current == 0).
    registered_targets = _REGISTERED_TARGETS
    if (
        0 < current < CURRENT_SCHEMA_VERSION
        and max(registered_targets, default=0) < CURRENT_SCHEMA_VERSION
    ):
        raise ConfigError(_reimport_required_message(current, db_path))

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
        # registered) still record the package's current version. The
        # ConfigError guard above ensures we only reach this point when
        # either (a) the DB is fresh (current == 0) or (b) every gap is
        # already covered by a registered migration.
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
