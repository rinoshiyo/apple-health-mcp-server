"""``get_heart_rate_samples`` MCP tool."""

from __future__ import annotations

from threading import Lock
from typing import TYPE_CHECKING, Annotated

from pydantic import Field

from apple_health_mcp.server.query import (
    OFFSET_DESCRIPTION,
    normalise_pagination,
    run_query_envelope,
)

if TYPE_CHECKING:
    import duckdb
    from mcp.server.fastmcp import FastMCP


DESCRIPTION = (
    "Get beat-level heart-rate samples attached to a parent HR or HRV record. "
    "Returns `{items, total, next_offset}`; `next_offset` is `null` on the "
    "last page. Each item carries {sample_idx, bpm, sample_time} where "
    "sample_time is the wall-clock seconds since 00:00 local on the parent "
    "record's day (float; e.g. ``28800.0`` = 08:00:00). Apple's "
    "``InstantaneousBeatsPerMinute.time`` attribute is a wall-clock value, "
    "not a delta from the parent record's ``start_date``; subtract the "
    "parent record's seconds-of-day if you need a relative offset. Use this "
    "to reconstruct HRV metrics (RMSSD, pNN50, LF/HF) from a "
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
                max_length=64,
            ),
        ],
        limit: Annotated[
            int | None,
            Field(
                description="Maximum number of samples to return (default 1000, max 10000)",
            ),
        ] = None,
        offset: Annotated[
            int | None,
            Field(description=OFFSET_DESCRIPTION, ge=0, le=2**63 - 1),
        ] = None,
    ) -> str:
        try:
            effective_limit, effective_offset = normalise_pagination(
                limit, offset, default_limit=_DEFAULT_LIMIT, max_limit=_MAX_LIMIT
            )
        except ValueError as exc:
            return f"Error: {exc}"
        # Issue #109 (PR-F): ``sample_time`` is now stored DOUBLE
        # (seconds-of-day since 00:00 local) at import time, so the
        # tool returns the column verbatim without a per-row transform.
        sql = (
            "SELECT sample_idx, bpm, sample_time, COUNT(*) OVER () AS _total "
            "FROM heart_rate_samples WHERE parent_record_hash = ? "
            f"ORDER BY sample_idx LIMIT {effective_limit} OFFSET {effective_offset}"
        )
        return run_query_envelope(
            conn,
            sql,
            [record_hash],
            offset=effective_offset,
            lock=lock,
        )
