"""``get_import_history`` MCP tool."""

from __future__ import annotations

from threading import Lock
from typing import TYPE_CHECKING

from apple_health_mcp.server.query import run_query

if TYPE_CHECKING:
    import duckdb
    from mcp.server.fastmcp import FastMCP


DESCRIPTION = (
    "List all data imports. Returns: import_id, export_dir, imported_at, "
    "record_count, workout_count, duration_secs, export_xml_sha256 "
    "(hex sha256 of the source export.xml; NULL on rows finalized before "
    "the column was introduced)."
)

_SQL = "SELECT * FROM imports ORDER BY imported_at DESC"


def register(mcp: FastMCP, conn: duckdb.DuckDBPyConnection, lock: Lock) -> None:
    @mcp.tool(description=DESCRIPTION)
    async def get_import_history() -> str:
        # ``require_data=False`` because "list imports" is the canonical way
        # to confirm the empty-DB state — returning the guidance message
        # would make it impossible to ever observe the empty list.
        return run_query(conn, _SQL, lock=lock, require_data=False)
