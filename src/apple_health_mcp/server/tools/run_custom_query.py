"""``run_custom_query`` MCP tool."""

from __future__ import annotations

from threading import Lock
from typing import TYPE_CHECKING, Annotated

from pydantic import Field

from apple_health_mcp.server.query import run_query
from apple_health_mcp.server.safety import (
    MAX_CUSTOM_QUERY_ROWS,
    QueryValidationError,
    enforce_limit,
    validate_query,
)

if TYPE_CHECKING:
    import duckdb
    from mcp.server.fastmcp import FastMCP


DESCRIPTION = (
    "Run a read-only SQL query (DuckDB dialect). Must start with SELECT or "
    "WITH. Tables: records (record_hash, record_type, value, unit, "
    "source_name, device, start_date, end_date), workouts (workout_hash, "
    "activity_type, duration, total_distance, total_energy_burned, "
    "start_date, end_date), workout_events, workout_statistics, "
    "activity_summaries, ecg_readings, ecg_samples, route_points (latitude, "
    "longitude, elevation, timestamp, speed), daily_record_stats "
    "(record_type, date, unit, count, avg_value, min_value, max_value, "
    "sum_value), record_metadata (record_hash, key, value), state_of_mind "
    "(record_hash, valence, kind, labels, associations), imports."
)


def register(mcp: FastMCP, conn: duckdb.DuckDBPyConnection, lock: Lock) -> None:
    @mcp.tool(description=DESCRIPTION)
    async def run_custom_query(
        query: Annotated[
            str,
            Field(
                description="A read-only SQL query (must start with SELECT or WITH)",
            ),
        ],
    ) -> str:
        trimmed = query.strip()
        try:
            validate_query(trimmed)
        except QueryValidationError as exc:
            return f"Error: {exc}"
        sql = enforce_limit(trimmed, MAX_CUSTOM_QUERY_ROWS)
        return run_query(conn, sql, lock=lock)
