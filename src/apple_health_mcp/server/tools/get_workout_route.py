"""``get_workout_route`` MCP tool."""

from __future__ import annotations

import json
import logging
from threading import Lock
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import Field

from apple_health_mcp.server.data_state import (
    DataState,
    build_state_error_payload,
    check_data_state,
)
from apple_health_mcp.server.query import (
    OFFSET_DESCRIPTION,
    normalise_pagination,
    query_to_json,
    run_query_payload,
)

if TYPE_CHECKING:
    import duckdb
    from mcp.server.fastmcp import FastMCP

_logger = logging.getLogger(__name__)


# v0.4.1 (issue #160): the previous _DEFAULT_LIMIT of 5000 paired with
# the wire-side ~175 bytes/point projection regularly tripped the host
# 1 MB transport ceiling (Claude truncates server responses larger than
# 1 MB to a generic "Tool result is too large" string, so the server
# never sees the failure and the caller cannot recover). 2000 points x
# the trimmed ~180 bytes/point projection lands well under the cap and
# the size-budget clamp below catches any oversize result the caller's
# own LIMIT forced.
_DEFAULT_LIMIT = 2000
_MAX_LIMIT = 50_000

# Host transport ceiling for tool results. Anthropic's MCP runtime
# refuses payloads larger than ~1 MB; the budget below keeps a 50 KB
# headroom so the envelope wrapper (``truncated_by_size``,
# ``size_budget_bytes``, JSON indentation overhead) stays under the
# wire limit even when the items list is at the maximum.
_SIZE_BUDGET_BYTES = 950_000


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
        state = check_data_state(conn, lock=lock)
        if state != DataState.READY:
            return build_state_error_payload(state)
        sql = (
            "SELECT latitude, longitude, elevation, timestamp, speed, course, "
            "COUNT(*) OVER () AS _total FROM route_points WHERE workout_hash = ? "
            f"ORDER BY timestamp LIMIT {effective_limit} OFFSET {effective_offset}"
        )
        try:
            rows = query_to_json(conn, sql, [workout_hash], lock=lock)
            if rows:
                total = int(rows[0]["_total"])
            elif effective_offset > 0:
                # ``COUNT(*) OVER ()`` rides on the page rows; paginating
                # past the dataset returns zero rows, so we recover the
                # true total with a targeted COUNT(*) — same fallback
                # ``run_query_envelope`` uses (issue #108 / PR-E F1).
                count_rows = query_to_json(
                    conn,
                    "SELECT COUNT(*) AS _total FROM route_points WHERE workout_hash = ?",
                    [workout_hash],
                    lock=lock,
                )
                total = int(count_rows[0]["_total"]) if count_rows else 0
            else:
                total = 0
        except Exception as exc:
            _logger.debug("query failed: %s", exc)
            return f"Error: {exc}"
        items = [_round_route_point(row) for row in rows]
        kept, truncated_by_size = _clip_to_size_budget(items, _SIZE_BUDGET_BYTES)
        next_offset: int | None
        if truncated_by_size or effective_offset + len(kept) < total:
            next_offset = effective_offset + len(kept)
        else:
            next_offset = None
        return run_query_payload(
            {
                "items": kept,
                "total": total,
                "next_offset": next_offset,
                "truncated_by_size": truncated_by_size,
                "size_budget_bytes": _SIZE_BUDGET_BYTES,
            }
        )


def _round_route_point(row: dict[str, Any]) -> dict[str, Any]:
    """Apply per-field numeric rounding so the wire payload stays compact.

    Rounding levels are below the underlying sensor precision so the
    information loss is zero in practice while the JSON byte cost drops
    by ~30-40 %. The ``_total`` column is stripped before the row
    leaves the function so the envelope's ``items`` view never carries
    the window-aggregate by-product. ``timestamp`` is preserved
    verbatim -- ``query_to_json`` already serialised it.
    """
    item = {k: v for k, v in row.items() if k != "_total"}
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


def _clip_to_size_budget(
    items: list[dict[str, Any]],
    budget_bytes: int,
) -> tuple[list[dict[str, Any]], bool]:
    """Greedily prefix ``items`` to stay under ``budget_bytes`` of JSON.

    Returns ``(kept, truncated)``. ``truncated`` is ``True`` when at
    least one item was dropped because adding it would overflow the
    budget. The per-item byte estimate uses ``json.dumps`` with the
    same options ``run_query_payload`` would have used (no indent,
    ``ensure_ascii=False``) so the rolling tally tracks the actual
    wire cost. The check is run BEFORE the envelope wrapper is built
    because the envelope adds a fixed ~200 bytes that we treat as
    headroom inside ``_SIZE_BUDGET_BYTES``.
    """
    kept: list[dict[str, Any]] = []
    used = 0
    truncated = False
    for item in items:
        approx = len(json.dumps(item, ensure_ascii=False)) + 2  # ", " between items
        if used + approx > budget_bytes:
            truncated = True
            break
        kept.append(item)
        used += approx
    return kept, truncated
