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

# v0.5 (issue #162): the ±window the nearest-HR lateral join searches
# when ``with_heart_rate=True``. 30 s is wider than the typical Apple
# Watch sample cadence (~5 s) but tight enough that "nearest" stays
# meaningful when GPS and HR sensors briefly desync (tunnel, watch
# off-wrist, brief disconnect).
_HR_WINDOW_SECONDS = 30

# v0.5 (issue #161): upper bound for the equispaced-downsample stride.
# 1000 is well beyond any physically meaningful stride (a multi-hour
# workout sampled at 1 Hz tops out at ~36k points; N=100 already cuts
# to ~360 points, plenty for a polyline render); the cap is just a
# typo guard so a stray N=10_000_000 does not loop the WHERE clause.
_MAX_EVERY_NTH = 1000


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
    "``offset``. Call get_workout_details first to check has_route. "
    "``every_nth=N`` (issue #161): server-side equispaced downsampling "
    "— returns every N-th point ordered by timestamp (N=5 cuts row "
    "count to ~20%; capped at 1000). ``total`` reports the downsampled "
    "count, not the underlying route_points row count. Use this to "
    "stay under the size budget on long workouts where polyline "
    "fidelity matters more than per-second resolution. "
    "``with_heart_rate=True`` (issue #162): adds {heart_rate, "
    "heart_rate_offset_secs} to each item — the nearest "
    "HKQuantityTypeIdentifierHeartRate sample within ±30 s of the "
    "route point; heart_rate is null when no HR sample is in range. "
    "``heart_rate_offset_secs`` is the absolute time delta in seconds "
    "(rounded to 0.01 s) — use it to down-weight matches whose offset "
    "is large."
)


def register(mcp: FastMCP, conn: duckdb.DuckDBPyConnection, lock: Lock) -> None:
    @mcp.tool(description=DESCRIPTION)
    async def get_workout_route(
        workout_hash: Annotated[
            str,
            Field(description="The workout hash identifier", max_length=64),
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
            Field(description=OFFSET_DESCRIPTION, ge=0, le=2**63 - 1),
        ] = None,
        every_nth: Annotated[
            int | None,
            Field(
                description="Server-side equispaced downsampling (issue "
                "#161). When set to N >= 2, returns every N-th point "
                "ordered by timestamp; N=5 means 1 in 5. Capped at 1000. "
                "Default None / 1 = no downsampling.",
            ),
        ] = None,
        with_heart_rate: Annotated[
            bool,
            Field(
                description="When True (issue #162), each item carries "
                "the nearest HeartRate sample within ±30 s as "
                "{heart_rate, heart_rate_offset_secs}. heart_rate is null "
                "when no HR sample falls in the window. Default False.",
            ),
        ] = False,
    ) -> str:
        try:
            effective_limit, effective_offset = normalise_pagination(
                limit, offset, default_limit=_DEFAULT_LIMIT, max_limit=_MAX_LIMIT
            )
        except ValueError as exc:
            return f"Error: {exc}"

        if every_nth is not None and (every_nth < 1 or every_nth > _MAX_EVERY_NTH):
            return f"Error: every_nth must be between 1 and {_MAX_EVERY_NTH} (got {every_nth})."
        stride = every_nth if every_nth is not None and every_nth > 1 else None

        sql, params = _build_sql(
            workout_hash,
            stride=stride,
            with_heart_rate=with_heart_rate,
            limit=effective_limit,
            offset=effective_offset,
        )
        return run_query_envelope(
            conn,
            sql,
            params,
            offset=effective_offset,
            lock=lock,
            row_transform=_round_route_point,
        )


def _build_sql(
    workout_hash: str,
    *,
    stride: int | None,
    with_heart_rate: bool,
    limit: int,
    offset: int,
) -> tuple[str, list[Any]]:
    """Compose the SELECT for the configured downsample / HR-join combination.

    Built up in parts rather than as one f-string so each feature (stride
    via row_number, LATERAL HR-join) stays readable in isolation. Stride
    filter runs BEFORE the LATERAL HR-join so the join only fires for
    the points actually returned — the alternative (join first, then
    downsample) would scan every record and pay the join cost for
    points we are about to drop.
    """
    params: list[Any] = [workout_hash]
    if stride is not None:
        base = (
            "SELECT latitude, longitude, elevation, timestamp, speed, course "
            "FROM ("
            "  SELECT latitude, longitude, elevation, timestamp, speed, course, "
            "  row_number() OVER (ORDER BY timestamp) AS rn "
            "  FROM route_points WHERE workout_hash = ?"
            f") sub WHERE (rn - 1) % {stride} = 0"
        )
    else:
        base = (
            "SELECT latitude, longitude, elevation, timestamp, speed, course "
            "FROM route_points WHERE workout_hash = ?"
        )

    if with_heart_rate:
        # LATERAL pulls the nearest HR sample within ±_HR_WINDOW_SECONDS
        # of the route point. EPOCH() returns the difference in seconds;
        # DuckDB normalises INTERVAL arithmetic across TIMESTAMP /
        # TIMESTAMPTZ inputs, so the subtraction is timezone-safe.
        # ABS so the caller does not have to think about sign; the
        # window guarantees |delta| <= _HR_WINDOW_SECONDS.
        return (
            "SELECT rp.latitude, rp.longitude, rp.elevation, rp.timestamp, "
            "rp.speed, rp.course, hr.value AS heart_rate, "
            "hr.offset_secs AS heart_rate_offset_secs, "
            "COUNT(*) OVER () AS _total "
            f"FROM ({base}) rp "
            "LEFT JOIN LATERAL ("
            "  SELECT value, "
            "  ABS(EPOCH(start_date - rp.timestamp)) AS offset_secs "
            "  FROM records "
            "  WHERE record_type = 'HKQuantityTypeIdentifierHeartRate' "
            f"  AND start_date BETWEEN rp.timestamp - INTERVAL '{_HR_WINDOW_SECONDS} seconds' "
            f"  AND rp.timestamp + INTERVAL '{_HR_WINDOW_SECONDS} seconds' "
            "  ORDER BY ABS(EPOCH(start_date - rp.timestamp)) "
            "  LIMIT 1"
            ") hr ON true "
            f"ORDER BY rp.timestamp LIMIT {limit} OFFSET {offset}"
        ), params
    return (
        f"SELECT *, COUNT(*) OVER () AS _total FROM ({base}) sub "
        f"ORDER BY timestamp LIMIT {limit} OFFSET {offset}"
    ), params


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
    # heart_rate_offset_secs is added by the with_heart_rate LATERAL
    # join; round to 0.01 s (well below sensor precision) so the wire
    # bytes stay tight.
    if isinstance(item.get("heart_rate_offset_secs"), float):
        item["heart_rate_offset_secs"] = round(item["heart_rate_offset_secs"], 2)
    return item
