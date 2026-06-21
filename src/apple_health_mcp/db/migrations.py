"""Schema migration registry.

The v0.1.0 release ships a single canonical schema (see :mod:`schema`), so
migrations is a deliberately small surface: a version sentinel persisted in a
``schema_version`` table plus a stub :func:`apply_pending_migrations` ready
for future bumps. Future migrations register themselves in :data:`MIGRATIONS`
as ``(target_version, callable)`` pairs.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

from apple_health_mcp.exceptions import DatabaseError

if TYPE_CHECKING:
    import duckdb

_logger = logging.getLogger(__name__)

CURRENT_SCHEMA_VERSION = 1

Migration = Callable[["duckdb.DuckDBPyConnection"], None]
MIGRATIONS: Sequence[tuple[int, Migration]] = ()


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
    Raises :class:`DatabaseError` if a migration targets a version at or
    below the persisted one (registration order bug).
    """
    current = get_current_version(conn)
    if current > CURRENT_SCHEMA_VERSION:
        raise DatabaseError(
            f"database schema_version={current} is newer than "
            f"the package supports ({CURRENT_SCHEMA_VERSION})"
        )

    applied = current
    for target, migration in MIGRATIONS:
        if target <= applied:
            raise DatabaseError(
                f"migration target {target} is not greater than applied version {applied}"
            )
        if target > CURRENT_SCHEMA_VERSION:
            raise DatabaseError(
                f"migration target {target} exceeds CURRENT_SCHEMA_VERSION {CURRENT_SCHEMA_VERSION}"
            )
        _logger.info("Applying migration to schema version %d", target)
        migration(conn)
        applied = target

    if applied != current:
        set_current_version(conn, applied)
    elif current == 0:
        # First-ever connection: stamp the baseline so future bumps have a
        # reference point even when no migration registered yet.
        set_current_version(conn, CURRENT_SCHEMA_VERSION)
        applied = CURRENT_SCHEMA_VERSION

    return applied
