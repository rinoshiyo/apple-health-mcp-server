"""MCP tool implementations exposed by the server.

Every tool lives in its own ``server/tools/<name>.py`` module exposing a
``register(mcp, conn, lock)`` callable. The server bootstrap iterates
:data:`ALL_TOOLS` to attach them to the :class:`~mcp.server.fastmcp.FastMCP`
instance, so adding a new tool means writing a new module and appending it
to the list below — no decorator registry magic.
"""

from __future__ import annotations

from collections.abc import Callable
from threading import Lock
from typing import TYPE_CHECKING

from apple_health_mcp.server.tools import (
    get_activity_summaries,
    get_correlation_details,
    get_ecg_data,
    get_heart_rate_samples,
    get_import_history,
    get_me_attributes,
    get_record_statistics,
    get_workout_details,
    get_workout_route,
    list_correlations,
    list_data_sources,
    list_ecg_readings,
    list_record_types,
    list_state_of_mind,
    list_workouts,
    query_records,
    run_custom_query,
)

if TYPE_CHECKING:
    import duckdb
    from mcp.server.fastmcp import FastMCP


RegisterFn = Callable[["FastMCP", "duckdb.DuckDBPyConnection", Lock], None]


# Ordered the same way the Rust reference declared them so a side-by-side
# audit stays straightforward. ``list_state_of_mind`` (issue #13) and
# ``get_me_attributes`` (issue #30) are Python-only additions that surface
# structured Apple Health elements the Rust port left buried under the
# generic record / metadata path.
ALL_TOOLS: list[RegisterFn] = [
    list_record_types.register,
    query_records.register,
    get_record_statistics.register,
    list_workouts.register,
    get_workout_details.register,
    get_activity_summaries.register,
    get_workout_route.register,
    get_heart_rate_samples.register,
    list_correlations.register,
    get_correlation_details.register,
    list_ecg_readings.register,
    get_ecg_data.register,
    run_custom_query.register,
    list_data_sources.register,
    get_import_history.register,
    list_state_of_mind.register,
    get_me_attributes.register,
]
