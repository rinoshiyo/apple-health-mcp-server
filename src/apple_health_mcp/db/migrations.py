"""Schema-version management for the canonical DuckDB schema.

v0.5 (issue #178) retired the migration-registry scaffolding. The
``apply_pending_migrations`` loop, the only-ever-registered
``_add_export_xml_sha256_column`` step, the re-import-required
``ConfigError`` path, and the matching v=N rejection tests were all
dead code by then: v0.3.0 (#124) made fresh-import the upgrade
contract, v0.4.1 (#156) added :func:`schema_version_is_stale` so
read tools surface ``NEEDS_REIMPORT`` and write tools auto-reset the
DB before the next import — both paths short-circuit before
``apply_pending_migrations`` could fire its rejection.

What remains is what the rest of the codebase actually uses:

* :data:`CURRENT_SCHEMA_VERSION` — the package's canonical schema id.
* :func:`schema_version_is_stale` — the v0.4.1 fresh-reset trigger.
* :func:`get_current_version` / :func:`set_current_version` — sentinel
  table accessors.
* :func:`stamp_current_version` — thin wrapper the import bootstrap
  uses to record :data:`CURRENT_SCHEMA_VERSION` once the canonical
  schema has been built by :func:`schema.ensure_schema`.

Module name kept as ``migrations`` rather than renamed to
``schema_version`` to keep the diff focused; the spirit is now
strictly "schema-version sentinel management".
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import duckdb

CURRENT_SCHEMA_VERSION = 6


def _table_exists_in_main(conn: duckdb.DuckDBPyConnection, name: str) -> bool:
    """Return True when ``name`` exists as a table in the ``main`` schema.

    The ``schema_name = 'main'`` filter prevents a connection with
    attached databases or user-created schemas from passing the probe
    on the basis of an unrelated same-named table.
    """
    row = conn.execute(
        "SELECT 1 FROM duckdb_tables() WHERE table_name = ? AND schema_name = 'main' LIMIT 1",
        [name],
    ).fetchone()
    return row is not None


def _ensure_version_table(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER NOT NULL
        );
        """
    )


def schema_version_is_stale(conn: duckdb.DuckDBPyConnection) -> bool:
    """Return True when the DB's ``schema_version`` trails CURRENT but is non-zero.

    A freshly created DB (no ``schema_version`` row, or 0) is *not*
    stale — it has simply not been stamped yet. An existing DB whose
    persisted version is between 1 and ``CURRENT_SCHEMA_VERSION - 1``
    (i.e. it was imported under an older package release) is stale;
    downstream callers in v0.4.1+ react by either surfacing the
    ``NEEDS_REIMPORT`` data-state envelope (read path) or auto-resetting
    the DB before the next import (write path).

    The probe runs purely as a SELECT and is safe on read-only handles.
    A missing ``schema_version`` table reads as "fresh" (returns False).
    """
    if not _table_exists_in_main(conn, "schema_version"):
        return False
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    current = int(row[0]) if row is not None and row[0] is not None else 0
    return 0 < current < CURRENT_SCHEMA_VERSION


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


def stamp_current_version(conn: duckdb.DuckDBPyConnection) -> None:
    """Stamp :data:`CURRENT_SCHEMA_VERSION` on a freshly built DB.

    Callers must have created the canonical schema via
    :func:`schema.ensure_schema` first; this helper only writes the
    version sentinel. Replaces the v0.4.x ``apply_pending_migrations``
    call site, which was a sentinel-stamping operation in disguise
    once the migration registry went empty (issue #178).
    """
    set_current_version(conn, CURRENT_SCHEMA_VERSION)
