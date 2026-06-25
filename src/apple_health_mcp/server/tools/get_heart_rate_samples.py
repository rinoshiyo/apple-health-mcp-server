"""``get_heart_rate_samples`` MCP tool."""

from __future__ import annotations

from threading import Lock
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import Field

from apple_health_mcp.server.query import (
    query_to_json,
    require_imports_or_message,
    run_query_payload,
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


def _parse_sample_time(value: str | None) -> float | None:
    """Convert the stored ``HH:MM:SS.SSS`` offset to a seconds float.

    Issue #96 (T8): the column is stored verbatim as Apple emits it so a
    round-trip back into the export stays byte-identical, but the wire
    contract surfaces a numeric offset so downstream LLM math (window
    arithmetic, RMSSD calculations) does not have to re-parse the string.

    Defensive against unexpected shapes -- a malformed row falls back to
    ``None`` rather than raising, so one bad sample cannot poison the
    whole response.
    """
    if value is None:
        return None
    parts = value.split(":")
    if len(parts) != 3:
        return None
    try:
        hours = int(parts[0])
        minutes = int(parts[1])
        seconds = float(parts[2])
    except ValueError:
        return None
    return float(hours * 3600 + minutes * 60) + seconds


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
        offset: Annotated[
            int | None,
            Field(
                description="Skip the first N samples before returning the "
                "next `limit` items. Use with `limit` to paginate.",
            ),
        ] = None,
    ) -> str:
        effective_limit = _DEFAULT_LIMIT if limit is None else max(0, min(limit, _MAX_LIMIT))
        effective_offset = 0 if offset is None else max(0, offset)
        if msg := require_imports_or_message(conn, lock=lock):
            return msg
        # Issue #108 (PR-E): unified ``{items, total, next_offset}``
        # envelope. ``sample_time`` still needs post-processing (Issue
        # #96 / T8 normalisation), so this tool runs the SELECT itself
        # rather than going through ``run_query_envelope``; the envelope
        # is then assembled by hand around the normalised rows.
        sql = (
            "SELECT sample_idx, bpm, sample_time, COUNT(*) OVER () AS _total "
            "FROM heart_rate_samples WHERE parent_record_hash = ? "
            f"ORDER BY sample_idx LIMIT {effective_limit} OFFSET {effective_offset}"
        )
        try:
            rows = query_to_json(conn, sql, [record_hash], lock=lock)
        except Exception as exc:
            return f"Error: {exc}"
        total = int(rows[0]["_total"]) if rows else 0
        items: list[dict[str, Any]] = []
        for row in rows:
            item = {k: v for k, v in row.items() if k != "_total"}
            # Issue #96 (T8): normalise ``sample_time`` on the way out
            # only (the underlying VARCHAR column stays as Apple's raw
            # string so a future round-trip exporter has the literal
            # value to write back).
            item["sample_time"] = _parse_sample_time(item.get("sample_time"))
            items.append(item)
        next_offset: int | None = (
            effective_offset + len(items) if (effective_offset + len(items)) < total else None
        )
        payload: dict[str, Any] = {
            "items": items,
            "total": total,
            "next_offset": next_offset,
        }
        return run_query_payload(payload)
