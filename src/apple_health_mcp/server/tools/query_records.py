"""``query_records`` MCP tool."""

from __future__ import annotations

from threading import Lock
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import Field

from apple_health_mcp.server.query import run_query

if TYPE_CHECKING:
    import duckdb
    from mcp.server.fastmcp import FastMCP


DESCRIPTION = (
    "Query individual health records. Returns: record_hash, record_type, "
    "value (numeric measurement), text_value (categorical value, e.g. sleep "
    "stages like HKCategoryValueSleepAnalysisAsleepDeep), unit, source_name, "
    "start_date, end_date. Record types use Apple's HK identifiers "
    "(e.g. HKQuantityTypeIdentifierHeartRate, HKCategoryTypeIdentifierSleepAnalysis). "
    "Use list_record_types first to discover available types."
)

_DEFAULT_LIMIT = 100
_MAX_LIMIT = 1000


def register(mcp: FastMCP, conn: duckdb.DuckDBPyConnection, lock: Lock) -> None:
    @mcp.tool(description=DESCRIPTION)
    async def query_records(
        record_type: Annotated[
            str,
            Field(
                description="The health record type to query, "
                "e.g. HKQuantityTypeIdentifierHeartRate",
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
        source_name: Annotated[
            str | None,
            Field(description="Filter by source name"),
        ] = None,
        limit: Annotated[
            int | None,
            Field(description="Maximum number of results (default 100, max 1000)"),
        ] = None,
    ) -> str:
        # ``None`` -> default; explicit 0 stays 0; negatives clamp to 0 so DuckDB
        # never sees ``LIMIT -1`` (which would surface as a raw parser error).
        effective_limit = _DEFAULT_LIMIT if limit is None else max(0, min(limit, _MAX_LIMIT))
        sql_parts = [
            "SELECT record_hash, record_type, value, text_value, unit, source_name, "
            "start_date, end_date FROM records WHERE record_type = ?"
        ]
        params: list[Any] = [record_type]
        if start_date is not None:
            sql_parts.append("AND start_date >= ?")
            params.append(start_date)
        if end_date is not None:
            sql_parts.append("AND end_date <= ?")
            params.append(end_date)
        if source_name is not None:
            sql_parts.append("AND source_name = ?")
            params.append(source_name)
        sql_parts.append(f"ORDER BY start_date DESC LIMIT {effective_limit}")
        return run_query(conn, " ".join(sql_parts), params, lock=lock)
