"""``list_data_sources`` MCP tool."""

from __future__ import annotations

from threading import Lock
from typing import TYPE_CHECKING

from apple_health_mcp.server.query import run_query

if TYPE_CHECKING:
    import duckdb
    from mcp.server.fastmcp import FastMCP


DESCRIPTION = (
    "List all devices and apps that contributed health data. Returns: "
    "source_name, record_count, earliest_date, latest_date."
)

_SQL = (
    "SELECT source_name, COUNT(*) AS record_count, "
    "MIN(start_date) AS earliest_date, MAX(start_date) AS latest_date "
    "FROM records GROUP BY source_name ORDER BY record_count DESC"
)


def register(mcp: FastMCP, conn: duckdb.DuckDBPyConnection, lock: Lock) -> None:
    @mcp.tool(description=DESCRIPTION)
    async def list_data_sources() -> str:
        return run_query(conn, _SQL, lock=lock)
