"""``get_record_statistics`` MCP tool."""

from __future__ import annotations

from threading import Lock
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import Field

from apple_health_mcp.server.query import run_query

if TYPE_CHECKING:
    import duckdb
    from mcp.server.fastmcp import FastMCP


DESCRIPTION = (
    "Get aggregated statistics for a record type over time periods. Returns: "
    "period, count, avg_value, min_value, max_value, sum_value. Uses "
    "pre-computed daily_record_stats table for fast aggregation. Prefer this "
    "over query_records for trends and summaries."
)

# Whitelisted period -> ``date_trunc`` expression. The expression is
# interpolated into the SQL, so the whitelist is the only sanitisation step.
_PERIOD_TRUNCS = {
    "day": "date",
    "week": "DATE_TRUNC('week', date)",
    "month": "DATE_TRUNC('month', date)",
    "year": "DATE_TRUNC('year', date)",
}


def register(mcp: FastMCP, conn: duckdb.DuckDBPyConnection, lock: Lock) -> None:
    @mcp.tool(description=DESCRIPTION)
    async def get_record_statistics(
        record_type: Annotated[
            str,
            Field(
                description="The health record type, e.g. HKQuantityTypeIdentifierHeartRate",
            ),
        ],
        start_date: Annotated[
            str | None,
            Field(description="Start date filter (ISO 8601 / YYYY-MM-DD)"),
        ] = None,
        end_date: Annotated[
            str | None,
            Field(description="End date filter (ISO 8601 / YYYY-MM-DD)"),
        ] = None,
        period: Annotated[
            str | None,
            Field(
                description="Aggregation period: day, week, month, or year (default: day)",
            ),
        ] = None,
    ) -> str:
        # Unknown periods fall back to ``day`` so a typo never leaks raw SQL.
        date_trunc = _PERIOD_TRUNCS.get(period or "day", "date")
        sql_parts = [
            f"SELECT {date_trunc} AS period, SUM(count) AS count, "
            "SUM(sum_value)/SUM(count) AS avg_value, "
            "MIN(min_value) AS min_value, MAX(max_value) AS max_value, "
            "SUM(sum_value) AS sum_value "
            "FROM daily_record_stats WHERE record_type = ?"
        ]
        params: list[Any] = [record_type]
        if start_date is not None:
            sql_parts.append("AND date >= ?")
            params.append(start_date)
        if end_date is not None:
            sql_parts.append("AND date <= ?")
            params.append(end_date)
        sql_parts.append(f"GROUP BY {date_trunc} ORDER BY period")
        return run_query(conn, " ".join(sql_parts), params, lock=lock)
