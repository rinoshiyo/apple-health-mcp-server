"""``get_workout_route`` MCP tool."""

from __future__ import annotations

from threading import Lock
from typing import TYPE_CHECKING, Annotated

from pydantic import Field

from apple_health_mcp.server.query import run_query_envelope

if TYPE_CHECKING:
    import duckdb
    from mcp.server.fastmcp import FastMCP


DESCRIPTION = (
    "Get GPS route data for a workout. Returns `{items, total, next_offset}`; "
    "`next_offset` is `null` on the last page. Each item carries "
    "{latitude, longitude, elevation (meters), timestamp, speed (m/s), "
    "course (degrees)}. ``items`` is capped at `limit` rows (default 5000, "
    "max 50000). Use get_workout_details first to check has_route."
)

_DEFAULT_LIMIT = 5000
_MAX_LIMIT = 50_000


def register(mcp: FastMCP, conn: duckdb.DuckDBPyConnection, lock: Lock) -> None:
    @mcp.tool(description=DESCRIPTION)
    async def get_workout_route(
        workout_hash: Annotated[
            str,
            Field(description="The workout hash identifier"),
        ],
        limit: Annotated[
            int | None,
            Field(
                description="Maximum number of route points to return "
                "(default 5000, max 50000). Long-form workouts can have tens "
                "of thousands of GPS samples; the default keeps responses in "
                "a reasonable LLM context budget.",
            ),
        ] = None,
        offset: Annotated[
            int | None,
            Field(
                description="Skip the first N route points before returning "
                "the next `limit` rows. Use with `limit` to paginate a long "
                "route in chunks.",
            ),
        ] = None,
    ) -> str:
        # Issue #108 (PR-E): unified pagination envelope across all
        # list/page tools. ``has_more`` is dropped — ``next_offset is
        # None`` is the canonical "last page" marker. ``total`` comes
        # from ``COUNT(*) OVER ()`` in the same SELECT as the page
        # rows so each page request takes one round trip.
        #
        # ``limit=0`` is rejected up front (rather than clamped to 0)
        # so a client cannot land in an infinite pagination loop where
        # each page returns ``items=[]`` but a non-null ``next_offset``.
        # The bare-cap ``min(limit, _MAX_LIMIT)`` still applies for
        # limit >= 1.
        if limit is not None and limit < 1:
            return "Error: limit must be >= 1"
        effective_limit = _DEFAULT_LIMIT if limit is None else min(limit, _MAX_LIMIT)
        effective_offset = 0 if offset is None else max(0, offset)
        sql = (
            "SELECT latitude, longitude, elevation, timestamp, speed, course, "
            "COUNT(*) OVER () AS _total FROM route_points WHERE workout_hash = ? "
            f"ORDER BY timestamp LIMIT {effective_limit} OFFSET {effective_offset}"
        )
        return run_query_envelope(
            conn,
            sql,
            [workout_hash],
            offset=effective_offset,
            lock=lock,
        )
