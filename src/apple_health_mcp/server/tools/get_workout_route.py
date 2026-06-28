"""``get_workout_route`` MCP tool."""

from __future__ import annotations

from threading import Lock
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import Field

from apple_health_mcp.server.query import (
    OFFSET_DESCRIPTION,
    normalise_pagination,
    run_query_envelope,
)

if TYPE_CHECKING:
    import duckdb
    from mcp.server.fastmcp import FastMCP


# v0.5 (issue #171): the size-budget clamp + ``truncated_by_size`` /
# ``size_budget_bytes`` envelope fields are now part of every
# ``run_query_envelope`` response, so this tool inherits the same
# protection automatically. The previously-inline ``_clip_to_size_budget``
# helper moved to :func:`server.query.clip_items_to_size_budget`.
_DEFAULT_LIMIT = 2000
_MAX_LIMIT = 50_000


DESCRIPTION = (
    "Get GPS route data for a workout. Returns "
    "``{items, total, next_offset, truncated_by_size, "
    "size_budget_bytes}``; ``next_offset`` is ``null`` on the last "
    "page, ``truncated_by_size`` is ``true`` when the server clipped "
    "the items list to stay under the host's 1 MB transport ceiling. "
    "Each item carries {latitude, longitude, elevation (m, rounded to "
    "0.1 m), timestamp, speed (m/s, rounded to 0.001), course (deg, "
    "rounded to 0.1)}. Lat/lon are rounded to 6 decimals (~0.1 m, "
    "below GPS error). ``items`` is capped at ``limit`` rows (default "
    "2000, max 50000); for long-form workouts page through with "
    "``offset``. Call get_workout_details first to check has_route."
)


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
                "(default 2000, max 50000). Long-form workouts can have tens "
                "of thousands of GPS samples; the default keeps responses "
                "comfortably under the host transport's 1 MB ceiling.",
            ),
        ] = None,
        offset: Annotated[
            int | None,
            Field(description=OFFSET_DESCRIPTION),
        ] = None,
    ) -> str:
        try:
            effective_limit, effective_offset = normalise_pagination(
                limit, offset, default_limit=_DEFAULT_LIMIT, max_limit=_MAX_LIMIT
            )
        except ValueError as exc:
            return f"Error: {exc}"
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
            row_transform=_round_route_point,
        )


def _round_route_point(row: dict[str, Any]) -> dict[str, Any]:
    """Apply per-field numeric rounding so the wire payload stays compact.

    Rounding levels are below the underlying sensor precision so the
    information loss is zero in practice while the JSON byte cost drops
    by ~30-40 %. ``run_query_envelope`` pops ``_total`` after this
    transform runs, so we leave it untouched here.
    """
    item = dict(row)
    # latitude / longitude are NOT NULL in the canonical schema, so the
    # round() call is unguarded. elevation / speed / course can be NULL
    # (Apple Health does not always populate them), so each guard
    # preserves the original None pass-through.
    item["latitude"] = round(item["latitude"], 6)
    item["longitude"] = round(item["longitude"], 6)
    if isinstance(item.get("elevation"), float):
        item["elevation"] = round(item["elevation"], 1)
    if isinstance(item.get("speed"), float):
        item["speed"] = round(item["speed"], 3)
    if isinstance(item.get("course"), float):
        item["course"] = round(item["course"], 1)
    return item
