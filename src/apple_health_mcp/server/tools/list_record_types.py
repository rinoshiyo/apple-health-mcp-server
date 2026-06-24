"""``list_record_types`` MCP tool."""

from __future__ import annotations

from threading import Lock
from typing import TYPE_CHECKING

from apple_health_mcp.server.query import run_query

if TYPE_CHECKING:
    import duckdb
    from mcp.server.fastmcp import FastMCP


DESCRIPTION = (
    "List all available health record types with counts and date ranges. "
    "Use this first to discover what data is available. Returns: record_type "
    "(e.g. HKQuantityTypeIdentifierHeartRate, HKQuantityTypeIdentifierStepCount), "
    "count, unit, earliest_date, latest_date."
)

# Issue #91 (T1): the column is selected as ``record_type`` -- not aliased to
# the generic ``type`` -- so the wire field name matches the other tools
# (``query_records`` etc.) and survives the v1.0.0 SemVer freeze.
_SQL = (
    "SELECT record_type, COUNT(*) AS count, unit, "
    "MIN(start_date) AS earliest_date, MAX(start_date) AS latest_date "
    "FROM records GROUP BY record_type, unit ORDER BY count DESC"
)


def register(mcp: FastMCP, conn: duckdb.DuckDBPyConnection, lock: Lock) -> None:
    @mcp.tool(description=DESCRIPTION)
    async def list_record_types() -> str:
        return run_query(conn, _SQL, lock=lock)
