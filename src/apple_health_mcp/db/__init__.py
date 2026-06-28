"""DuckDB schema, connection, and migration management."""

from __future__ import annotations

from apple_health_mcp.db.connection import (
    default_db_path,
    get_connection,
    get_in_memory_connection,
    resolve_db_path,
)
from apple_health_mcp.db.migrations import (
    CURRENT_SCHEMA_VERSION,
    apply_pending_migrations,
    get_current_version,
    schema_version_is_stale,
    set_current_version,
)
from apple_health_mcp.db.schema import (
    TABLE_COUNT,
    deduplicate_tables,
    ensure_schema,
    populate_workout_vestigial_columns,
    rebuild_daily_stats,
    reset_db_for_fresh_import,
)

__all__ = [
    "CURRENT_SCHEMA_VERSION",
    "TABLE_COUNT",
    "apply_pending_migrations",
    "deduplicate_tables",
    "default_db_path",
    "ensure_schema",
    "get_connection",
    "get_current_version",
    "get_in_memory_connection",
    "populate_workout_vestigial_columns",
    "rebuild_daily_stats",
    "reset_db_for_fresh_import",
    "resolve_db_path",
    "schema_version_is_stale",
    "set_current_version",
]
