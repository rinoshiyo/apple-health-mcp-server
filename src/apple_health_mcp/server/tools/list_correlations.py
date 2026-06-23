"""``list_correlations`` MCP tool."""

from __future__ import annotations

from threading import Lock
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import Field

from apple_health_mcp.server.query import normalise_end_date, run_query

if TYPE_CHECKING:
    import duckdb
    from mcp.server.fastmcp import FastMCP


DESCRIPTION = (
    "List Correlation groupings (e.g. HKCorrelationTypeIdentifierBloodPressure "
    "pairs a Systolic and a Diastolic record taken in the same reading, "
    "HKCorrelationTypeIdentifierFood groups a meal's nutrient breakdown). "
    "Returns: correlation_hash, correlation_type, source_name, start_date, "
    "end_date. Use correlation_hash with get_correlation_details to fetch "
    "the joined member records (e.g. matched Systolic + Diastolic values)."
)

_DEFAULT_LIMIT = 50
_MAX_LIMIT = 500


def register(mcp: FastMCP, conn: duckdb.DuckDBPyConnection, lock: Lock) -> None:
    @mcp.tool(description=DESCRIPTION)
    async def list_correlations(
        correlation_type: Annotated[
            str | None,
            Field(
                description="Filter by correlation type "
                "(e.g. HKCorrelationTypeIdentifierBloodPressure)",
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
            Field(description="Maximum number of results (default 50, max 500)"),
        ] = None,
    ) -> str:
        effective_limit = _DEFAULT_LIMIT if limit is None else max(0, min(limit, _MAX_LIMIT))
        sql_parts = [
            "SELECT correlation_hash, correlation_type, source_name, "
            "start_date, end_date FROM correlations WHERE 1=1"
        ]
        params: list[Any] = []
        if correlation_type is not None:
            sql_parts.append("AND correlation_type = ?")
            params.append(correlation_type)
        if start_date is not None:
            sql_parts.append("AND start_date >= ?")
            params.append(start_date)
        if end_date is not None:
            sql_parts.append("AND end_date <= ?")
            params.append(normalise_end_date(end_date))
        sql_parts.append(f"ORDER BY start_date DESC LIMIT {effective_limit}")
        return run_query(conn, " ".join(sql_parts), params, lock=lock)
