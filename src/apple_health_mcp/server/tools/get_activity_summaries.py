"""``get_activity_summaries`` MCP tool."""

from __future__ import annotations

from threading import Lock
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import Field

from apple_health_mcp.server.query import run_query

if TYPE_CHECKING:
    import duckdb
    from mcp.server.fastmcp import FastMCP


DESCRIPTION = (
    "Get Apple Watch activity ring data. Returns: date_components, "
    "active_energy_burned, active_energy_burned_goal, active_energy_burned_unit "
    "(e.g. kcal -- captured verbatim from the export so non-default-locale "
    "rings come back correctly), apple_move_time, apple_move_time_goal, "
    "apple_exercise_time, apple_exercise_time_goal, apple_stand_hours, "
    "apple_stand_hours_goal. Values are in <unit>, minutes, and hours "
    "respectively."
)

_DEFAULT_LIMIT = 30
_MAX_LIMIT = 365


def register(mcp: FastMCP, conn: duckdb.DuckDBPyConnection, lock: Lock) -> None:
    @mcp.tool(description=DESCRIPTION)
    async def get_activity_summaries(
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
            Field(description="Maximum number of results (default 30)"),
        ] = None,
    ) -> str:
        effective_limit = _DEFAULT_LIMIT if limit is None else max(0, min(limit, _MAX_LIMIT))
        # Issue #94 (T6): explicit column list keeps ``import_id`` (an
        # internal join key) off the wire and pins the public response
        # shape ahead of the v1.0.0 SemVer freeze.
        sql_parts = [
            "SELECT date_components, active_energy_burned, "
            "active_energy_burned_goal, active_energy_burned_unit, "
            "apple_move_time, apple_move_time_goal, apple_exercise_time, "
            "apple_exercise_time_goal, apple_stand_hours, apple_stand_hours_goal "
            "FROM activity_summaries WHERE 1=1"
        ]
        params: list[Any] = []
        if start_date is not None:
            sql_parts.append("AND date_components >= ?")
            params.append(start_date)
        if end_date is not None:
            sql_parts.append("AND date_components <= ?")
            params.append(end_date)
        sql_parts.append(f"ORDER BY date_components DESC LIMIT {effective_limit}")
        return run_query(conn, " ".join(sql_parts), params, lock=lock)
