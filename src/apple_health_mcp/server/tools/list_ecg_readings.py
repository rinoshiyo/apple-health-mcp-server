"""``list_ecg_readings`` MCP tool."""

from __future__ import annotations

from threading import Lock
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import Field

from apple_health_mcp.server.query import normalise_end_date, run_query_envelope

if TYPE_CHECKING:
    import duckdb
    from mcp.server.fastmcp import FastMCP


DESCRIPTION = (
    "List ECG recordings. Returns `{items, total, next_offset}`; "
    "`next_offset` is `null` on the last page. Each item carries: "
    "ecg_hash, recorded_date, classification (e.g. SinusRhythm, "
    "AtrialFibrillation), device, sample_rate_hz. Use ecg_hash with "
    "get_ecg_data. Default limit 100, max 1000."
)

_DEFAULT_LIMIT = 100
_MAX_LIMIT = 1000


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
        limit: Annotated[
            int | None,
            Field(
                description="Maximum number of results (default 100, max 1000)",
            ),
        ] = None,
        offset: Annotated[
            int | None,
            Field(
                description="Skip the first N rows before returning the "
                "next `limit` items. Use with `limit` to paginate.",
            ),
        ] = None,
    ) -> str:
        # Issue #97 (T11): added a ``limit`` parameter so this tool matches
        # the rest of the list_* family. ECG readings are typically few
        # (dozens) but a long-tenured Apple Watch user may hit hundreds, so
        # the cap keeps responses LLM-friendly without forcing a contract
        # of "always returns everything".
        #
        # ``limit=0`` is rejected up front so the tool can never silently
        # return an empty list that a downstream LLM might mistake for a
        # "no recordings" result. Matches the behaviour of
        # ``get_workout_route``.
        if limit is not None and limit < 1:
            return "Error: limit must be >= 1"
        effective_limit = _DEFAULT_LIMIT if limit is None else min(limit, _MAX_LIMIT)
        effective_offset = 0 if offset is None else max(0, offset)
        sql_parts = [
            "SELECT ecg_hash, recorded_date, classification, device, "
            "sample_rate_hz, COUNT(*) OVER () AS _total "
            "FROM ecg_readings WHERE 1=1"
        ]
        params: list[Any] = []
        if start_date is not None:
            sql_parts.append("AND recorded_date >= ?")
            params.append(start_date)
        if end_date is not None:
            sql_parts.append("AND recorded_date <= ?")
            params.append(normalise_end_date(end_date))
        sql_parts.append(
            f"ORDER BY recorded_date DESC LIMIT {effective_limit} OFFSET {effective_offset}"
        )
        return run_query_envelope(
            conn,
            " ".join(sql_parts),
            params,
            offset=effective_offset,
            lock=lock,
        )
