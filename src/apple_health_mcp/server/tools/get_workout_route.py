"""``get_workout_route`` MCP tool."""

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
    "Get GPS route data for a workout. Returns an envelope object: "
    "{points: [{latitude, longitude, elevation (meters), timestamp, "
    "speed (m/s), course (degrees)}, ...], total: <int total point count for "
    "this workout>, has_more: <bool, true when there are more points beyond "
    "the returned page>, next_offset: <int offset to pass on the next call, "
    "or null when has_more is false>}. ``points`` is capped at `limit` rows "
    "(default 5000, max 50000); use the returned ``next_offset`` to paginate "
    "longer routes. Use get_workout_details first to check has_route."
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
        # Issue #95 (T7): pre-v1.0.0 promotion from a bare array to a
        # ``{points, total, has_more, next_offset}`` envelope so clients
        # can detect the end of a paginated route without blind probing.
        effective_limit = _DEFAULT_LIMIT if limit is None else max(0, min(limit, _MAX_LIMIT))
        effective_offset = 0 if offset is None else max(0, offset)
        if msg := require_imports_or_message(conn, lock=lock):
            return msg
        try:
            # ``total`` is the full point count for the workout (independent
            # of ``offset`` / ``limit``) so clients can render a progress
            # bar or skip pagination entirely when total <= limit.
            total_rows = query_to_json(
                conn,
                "SELECT COUNT(*) AS n FROM route_points WHERE workout_hash = ?",
                [workout_hash],
                lock=lock,
            )
            total = int(total_rows[0]["n"]) if total_rows else 0
            points = query_to_json(
                conn,
                "SELECT latitude, longitude, elevation, timestamp, speed, course "
                "FROM route_points WHERE workout_hash = ? "
                f"ORDER BY timestamp LIMIT {effective_limit} OFFSET {effective_offset}",
                [workout_hash],
                lock=lock,
            )
        except Exception as exc:
            return f"Error: {exc}"
        # ``has_more`` compares the absolute index of the last returned
        # point against ``total``; ``next_offset`` is null when the page
        # already exhausted the route so a client can stop calling.
        has_more = (effective_offset + len(points)) < total
        next_offset: int | None = effective_offset + len(points) if has_more else None
        payload: dict[str, Any] = {
            "points": points,
            "total": total,
            "has_more": has_more,
            "next_offset": next_offset,
        }
        return run_query_payload(payload)
