"""``list_ecg_readings`` MCP tool."""

from __future__ import annotations

from threading import Lock
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import Field

from apple_health_mcp.server.query import normalise_end_date, run_query

if TYPE_CHECKING:
    import duckdb
    from mcp.server.fastmcp import FastMCP


DESCRIPTION = (
    "List ECG recordings. Returns: ecg_hash, recorded_date, classification "
    "(e.g. SinusRhythm, AtrialFibrillation), device, sample_rate_hz. Use "
    "ecg_hash with get_ecg_data."
)


def register(mcp: FastMCP, conn: duckdb.DuckDBPyConnection, lock: Lock) -> None:
    @mcp.tool(description=DESCRIPTION)
    async def list_ecg_readings(
        start_date: Annotated[
            str | None,
            Field(description="Start date filter (ISO 8601 / YYYY-MM-DD)"),
        ] = None,
        end_date: Annotated[
            str | None,
            Field(description="End date filter (ISO 8601 / YYYY-MM-DD)"),
        ] = None,
    ) -> str:
        sql_parts = [
            "SELECT ecg_hash, recorded_date, classification, device, "
            "sample_rate_hz FROM ecg_readings WHERE 1=1"
        ]
        params: list[Any] = []
        if start_date is not None:
            sql_parts.append("AND recorded_date >= ?")
            params.append(start_date)
        if end_date is not None:
            sql_parts.append("AND recorded_date <= ?")
            params.append(normalise_end_date(end_date))
        sql_parts.append("ORDER BY recorded_date DESC")
        return run_query(conn, " ".join(sql_parts), params, lock=lock)
