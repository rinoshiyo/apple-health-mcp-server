"""``get_workout_details`` MCP tool."""

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
    "Get full workout details by workout_hash. Returns: workout object (all "
    "fields, with total_distance / total_energy_burned backfilled from "
    "workout_statistics for iOS 11+ exports), events (lap/pause markers), "
    "statistics (per-metric breakdowns like heart rate zones), metadata array "
    "of {key, value} (HKIndoorWorkout, HKAverageMETs, HKWeather*, app-specific "
    "keys), route object (file_path, source_name, source_version, "
    "creation_date, start_date, end_date, point_count) or null when the "
    "workout has no GPS track, and has_route boolean. Get the workout_hash "
    "from list_workouts."
)


def register(mcp: FastMCP, conn: duckdb.DuckDBPyConnection, lock: Lock) -> None:
    @mcp.tool(description=DESCRIPTION)
    async def get_workout_details(
        workout_hash: Annotated[
            str,
            Field(description="The workout hash identifier"),
        ],
    ) -> str:
        if msg := require_imports_or_message(conn, lock=lock):
            return msg
        try:
            # Issue #93 (T5): explicit column list keeps ``import_id`` (an
            # internal join key) off the wire and pins the public response
            # shape ahead of the v1.0.0 SemVer freeze.
            workout_rows = query_to_json(
                conn,
                "SELECT workout_hash, activity_type, duration, duration_unit, "
                "total_distance, total_distance_unit, total_energy_burned, "
                "total_energy_unit, source_name, source_version, device, "
                "creation_date, start_date, end_date "
                "FROM workouts WHERE workout_hash = ?",
                [workout_hash],
                lock=lock,
            )
            events = query_to_json(
                conn,
                "SELECT event_type, date, duration, duration_unit "
                "FROM workout_events WHERE workout_hash = ?",
                [workout_hash],
                lock=lock,
            )
            statistics = query_to_json(
                conn,
                "SELECT stat_type, start_date, end_date, average, minimum, "
                "maximum, sum, unit FROM workout_statistics WHERE workout_hash = ?",
                [workout_hash],
                lock=lock,
            )
            metadata = query_to_json(
                conn,
                "SELECT key, value FROM workout_metadata WHERE workout_hash = ? ORDER BY key",
                [workout_hash],
                lock=lock,
            )
            route_rows = query_to_json(
                conn,
                "SELECT wr.file_path, wr.source_name, wr.source_version, "
                "wr.creation_date, wr.start_date, wr.end_date, "
                "(SELECT COUNT(*) FROM route_points rp "
                " WHERE rp.workout_hash = wr.workout_hash) AS point_count "
                "FROM workout_routes wr WHERE wr.workout_hash = ?",
                [workout_hash],
                lock=lock,
            )
            # ``has_route`` stays true in two cases: ``workout_routes`` has a
            # row (XML claimed a GPX file -- ``route_rows`` already carries the
            # correlated point_count) OR route_points has rows without a
            # parent workout_routes row (GPX import succeeded against a
            # partial export). Only pay for the fallback count when the
            # cheap case is empty.
            if route_rows:
                route_obj: Any = route_rows[0]
                has_route = True
            else:
                route_obj = None
                fallback = query_to_json(
                    conn,
                    "SELECT COUNT(*) AS count FROM route_points WHERE workout_hash = ?",
                    [workout_hash],
                    lock=lock,
                )
                fallback_count = fallback[0]["count"] if fallback else 0
                has_route = isinstance(fallback_count, int) and fallback_count > 0
        except Exception as exc:
            return f"Error: {exc}"

        workout = workout_rows[0] if workout_rows else None
        route: Any = route_obj
        payload = {
            "workout": workout,
            "events": events,
            "statistics": statistics,
            "metadata": metadata,
            "route": route,
            "has_route": has_route,
        }
        return run_query_payload(payload)
