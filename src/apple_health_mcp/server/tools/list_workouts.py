"""``list_workouts`` MCP tool."""

from __future__ import annotations

from threading import Lock
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import Field

from apple_health_mcp.server.query import normalise_end_date, run_query

if TYPE_CHECKING:
    import duckdb
    from mcp.server.fastmcp import FastMCP


DESCRIPTION = (
    "List workouts with optional filtering. Returns: workout_hash, "
    "activity_type (e.g. HKWorkoutActivityTypeRunning), duration, duration_unit, "
    "total_distance, total_distance_unit, total_energy_burned, total_energy_unit, "
    "source_name, start_date, end_date. Use workout_hash with get_workout_details "
    "or get_workout_route."
)

_DEFAULT_LIMIT = 50
_MAX_LIMIT = 500


def register(mcp: FastMCP, conn: duckdb.DuckDBPyConnection, lock: Lock) -> None:
    @mcp.tool(description=DESCRIPTION)
    async def list_workouts(
        activity_type: Annotated[
            str | None,
            Field(
                description="Filter by workout activity type, e.g. HKWorkoutActivityTypeRunning",
            ),
        ] = None,
        start_date: Annotated[
            str | None,
            Field(description="Start date filter (ISO 8601 / YYYY-MM-DD)"),
        ] = None,
        end_date: Annotated[
            str | None,
            Field(description="End date filter (ISO 8601 / YYYY-MM-DD)"),
        ] = None,
        limit: Annotated[
            int | None,
            Field(description="Maximum number of results (default 50)"),
        ] = None,
    ) -> str:
        effective_limit = _DEFAULT_LIMIT if limit is None else max(0, min(limit, _MAX_LIMIT))
        sql_parts = [
            "SELECT workout_hash, activity_type, duration, duration_unit, "
            "total_distance, total_distance_unit, total_energy_burned, "
            "total_energy_unit, source_name, start_date, end_date "
            "FROM workouts WHERE 1=1"
        ]
        params: list[Any] = []
        if activity_type is not None:
            sql_parts.append("AND activity_type = ?")
            params.append(activity_type)
        if start_date is not None:
            sql_parts.append("AND start_date >= ?")
            params.append(start_date)
        if end_date is not None:
            sql_parts.append("AND end_date <= ?")
            params.append(normalise_end_date(end_date))
        sql_parts.append(f"ORDER BY start_date DESC LIMIT {effective_limit}")
        return run_query(conn, " ".join(sql_parts), params, lock=lock)
