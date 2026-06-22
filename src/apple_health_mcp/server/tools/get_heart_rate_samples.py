"""``get_heart_rate_samples`` MCP tool."""

from __future__ import annotations

from threading import Lock
from typing import TYPE_CHECKING, Annotated

from pydantic import Field

from apple_health_mcp.server.query import run_query

if TYPE_CHECKING:
    import duckdb
    from mcp.server.fastmcp import FastMCP


DESCRIPTION = (
    "Get beat-level heart-rate samples attached to a parent HR or HRV record. "
    "Returns an array of {sample_idx, bpm, sample_time} where sample_time is "
    "the relative HH:MM:SS.SSS offset Apple emits. Use this to reconstruct "
    "HRV metrics (RMSSD, pNN50, LF/HF) from a "
    "HKQuantityTypeIdentifierHeartRateVariabilitySDNN record, or to inspect "
    "the per-beat distribution behind an averaged "
    "HKQuantityTypeIdentifierHeartRate record (peak vs. average bpm within a "
    "window). The parent record_hash comes from query_records on those "
    "record types. Default limit 1000, max 10000."
)

_DEFAULT_LIMIT = 1000
_MAX_LIMIT = 10_000


def register(mcp: FastMCP, conn: duckdb.DuckDBPyConnection, lock: Lock) -> None:
    @mcp.tool(description=DESCRIPTION)
    async def get_heart_rate_samples(
        record_hash: Annotated[
            str,
            Field(
                description="The parent HR or HRV record hash to fetch beat-level samples for",
            ),
        ],
        limit: Annotated[
            int | None,
            Field(
                description="Maximum number of samples to return (default 1000, max 10000)",
            ),
        ] = None,
    ) -> str:
        effective_limit = _DEFAULT_LIMIT if limit is None else max(0, min(limit, _MAX_LIMIT))
        sql = (
            "SELECT sample_idx, bpm, sample_time FROM heart_rate_samples "
            "WHERE parent_record_hash = ? ORDER BY sample_idx "
            f"LIMIT {effective_limit}"
        )
        return run_query(conn, sql, [record_hash], lock=lock)
