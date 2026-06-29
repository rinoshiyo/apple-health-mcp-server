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
    get_import_status,
    get_me_attributes,
    get_record_statistics,
    get_server_info,
    get_workout_details,
    get_workout_route,
    import_zip,
    list_correlations,
    list_data_sources,
    list_ecg_readings,
    list_record_types,
    list_state_of_mind,
    list_workouts,
    list_zips,
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
    # ``get_server_info`` (issue #137) is Python v0.3.0's runtime
    # self-diagnosis primitive; appended after the historical 17 so
    # the original Rust-mirrored ordering remains the prefix of this
    # list and side-by-side audits still match.
    get_server_info.register,
    # v0.4 (issue #148) ZIP-flow tools: ``list_zips`` is the discovery
    # entry-point the agent calls first, then ``import_zip(id=...)``
    # drives the importer inline on the server's writable handle.
    list_zips.register,
    import_zip.register,
    # v0.5 (issue #157): companion to the job-based async ``import_zip``.
    # The agent polls this after import_zip returns ``queued`` to track
    # progress and retrieve the final result without paying for an MCP
    # tool-call timeout on slow hardware.
    get_import_status.register,
]
